"""X posting via pasted cookies — the zero-API path.

Flow for the subscriber (10 seconds, one-time):
  1. Install Cookie-Editor extension in Chrome
     https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm
  2. Log in to x.com normally (human, no bot)
  3. Click the Cookie-Editor icon → Export → JSON → copies to clipboard
  4. Paste into Settings → "X cookies (no API)"
  5. Done. Every clip posts automatically from now on.

What this module does
---------------------
- `validate_cookies(json_text)`    — parses + sanity-checks the paste
- `whoami_via_cookies(cookies)`    — uses cookies to fetch @handle, confirms they're alive
- `post_via_cookies(cookies, text, image_path)` — posts a tweet via Playwright

Why cookies instead of user+password
-------------------------------------
- User never shares their password with us (we can't see it even if hacked)
- Works even with 2FA enabled on the account (2FA happens during the human login,
  cookies encapsulate the already-authenticated session)
- Less likely to trigger X's bot detection (no headless login dance)

Limits
------
- Cookies expire ~30 days after the last login. User has to re-paste.
  (We detect the expiry and surface a clear "Reconnect X" button.)
- Playwright is heavier than pure HTTP — uses ~200MB RAM per concurrent post.
- X may still flag automation on suspicious accounts. Behave humanly.
"""
from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any


REQUIRED_COOKIES = {"auth_token", "ct0"}


# ---------------------------------------------------------------------------
# Parse + validate the cookie JSON the user pastes
# ---------------------------------------------------------------------------

def _normalize_cookies(raw: list[dict]) -> list[dict]:
    """Turn Cookie-Editor / Chrome-extension JSON into a Playwright-ready list."""
    # Cookie-Editor exports sameSite as "no_restriction" / "unspecified" /
    # "lax" / "strict" / "none" — Playwright only accepts "Strict" | "Lax"
    # | "None". Map everything cleanly.
    _SAMESITE_MAP = {
        "strict":          "Strict",
        "lax":             "Lax",
        "none":            "None",
        "no_restriction":  "None",
        "unspecified":     "Lax",  # Chrome default
        "":                "Lax",
        None:              "Lax",
    }

    out: list[dict] = []
    for c in raw:
        name = (c.get("name") or "").strip()
        val  = (c.get("value") or "")
        if not name or not val:
            continue
        dom = c.get("domain") or ".x.com"
        if not dom.startswith("."):
            # Cookie-Editor exports domains like "x.com" — Playwright wants ".x.com"
            # when the cookie should apply to subdomains; keep hostOnly otherwise.
            if dom in ("x.com", "twitter.com"):
                dom = "." + dom
        raw_ss = c.get("sameSite")
        ss_key = raw_ss.lower() if isinstance(raw_ss, str) else raw_ss
        same_site = _SAMESITE_MAP.get(ss_key, "Lax")
        out.append({
            "name":     name,
            "value":    val,
            "domain":   dom,
            "path":     c.get("path", "/"),
            "secure":   bool(c.get("secure", True)),
            "httpOnly": bool(c.get("httpOnly", False)),
            "sameSite": same_site,
        })
    return out


def validate_cookies(raw_text: str) -> tuple[list[dict], str]:
    """Validate a pasted cookie blob. Returns (cookies, error_message).

    If error_message is empty, cookies is a normalized list ready for Playwright.
    """
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return [], "Empty paste"

    # Strip ```json fences in case user pastes markdown
    if raw_text.startswith("```"):
        raw_text = re.sub(r"^```[a-zA-Z]*\n", "", raw_text)
        raw_text = re.sub(r"\n```\s*$", "", raw_text)

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return [], f"Invalid JSON: {exc}"

    if not isinstance(data, list):
        return [], "Expected a JSON array of cookie objects (what Cookie-Editor exports)"

    cookies = _normalize_cookies(data)
    names = {c["name"] for c in cookies}
    missing = REQUIRED_COOKIES - names
    if missing:
        return [], (
            f"Missing required cookies: {sorted(missing)}. "
            f"Did you export from x.com while logged in? "
            f"The critical ones are 'auth_token' and 'ct0'."
        )

    # Only keep x.com / twitter.com cookies to avoid leaking others
    domain_filter = ("x.com", ".x.com", "twitter.com", ".twitter.com")
    cookies = [c for c in cookies if c["domain"] in domain_filter]
    return cookies, ""


# ---------------------------------------------------------------------------
# Quick alive-check via HTTP (no browser) — used by "Test connection" button
# ---------------------------------------------------------------------------

def whoami_via_cookies(cookies: list[dict]) -> tuple[str, str]:
    """Return (handle, error). Handle empty string if cookies are dead.

    X's v1.1 public API (api.x.com/1.1/...) no longer serves unauth'd
    cookie-based calls — those return 404. The web app itself hits
    x.com/i/api/... with the same bearer token plus ct0 CSRF. We try a
    couple of those web-client endpoints; if they all fail but cookies
    look structurally complete (auth_token + ct0 present), we return
    success with an empty handle rather than blocking Save.
    """
    try:
        import requests
    except ImportError:
        return "", "requests not installed"

    # Build a cookiejar-like dict for requests
    jar: dict[str, str] = {}
    for c in cookies:
        jar[c["name"]] = c["value"]

    if "auth_token" not in jar or "ct0" not in jar:
        return "", "Missing auth_token/ct0"

    # Web-client bearer token (same one x.com embeds in its JS bundle).
    bearer = ("AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%"
              "3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")
    headers = {
        "Authorization":   f"Bearer {bearer}",
        "x-csrf-token":    jar["ct0"],
        "User-Agent":      ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"),
        "Accept":          "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://x.com/",
        "Origin":          "https://x.com",
        "x-twitter-auth-type":     "OAuth2Session",
        "x-twitter-client-language": "en",
        "x-twitter-active-user":   "yes",
    }

    # Try each endpoint in order; return on the first 200.
    endpoints = [
        # Web client settings endpoint — most stable, no GraphQL hashes.
        "https://x.com/i/api/1.1/account/settings.json",
        # Verify credentials — includes screen_name
        "https://x.com/i/api/1.1/account/verify_credentials.json",
        # Backup: viewer config (lighter, sometimes still available)
        "https://x.com/i/api/1.1/help/settings.json",
    ]
    last_err = ""
    for url in endpoints:
        try:
            r = requests.get(url, headers=headers, cookies=jar, timeout=20)
        except Exception as exc:
            last_err = f"Network error: {exc}"
            continue
        if r.status_code == 200:
            try:
                data = r.json() if r.content else {}
            except Exception:
                data = {}
            handle = (data.get("screen_name")
                      or data.get("username")
                      or data.get("user", {}).get("screen_name", ""))
            if handle:
                return handle, ""
            # 200 but no handle — try next endpoint before giving up.
            last_err = f"X returned 200 but no screen_name at {url}"
            continue
        if r.status_code in (401, 403):
            # Cookies are structurally present but X rejects them — dead.
            return "", (f"X rejected cookies with HTTP {r.status_code} — "
                        f"likely expired. Re-export from x.com and paste again.")
        last_err = f"X returned HTTP {r.status_code}: {r.text[:120]}"

    # Fallback: parse `twid` cookie for the numeric user ID and look them up
    # via users/show.json (lightweight, always returns screen_name).
    twid = jar.get("twid", "")
    if twid:
        import re
        m = re.search(r"u%3D(\d+)|u=(\d+)", twid)
        if m:
            uid_num = m.group(1) or m.group(2)
            try:
                r = requests.get(
                    f"https://x.com/i/api/1.1/users/show.json?user_id={uid_num}",
                    headers=headers, cookies=jar, timeout=20,
                )
                if r.status_code == 200:
                    data = r.json() if r.content else {}
                    sn = data.get("screen_name", "")
                    if sn:
                        return sn, ""
            except Exception as exc:
                last_err = f"users/show lookup failed: {exc}"

    # All endpoints failed with non-auth errors (404 / 5xx / network).
    # Return empty handle + warning so the UI can show "cookies saved but
    # handle unverified" instead of a hard fail. Real test is publishing.
    msg = (f"Could not verify handle via X web API "
           f"(last: {last_err}). Cookies look structurally OK; "
           f"the real test is publishing a post.")
    return "", msg


# ---------------------------------------------------------------------------
# Post a tweet via pure HTTP (no playwright) using stored cookies
# ---------------------------------------------------------------------------

_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%"
    "3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# Known stable CreateTweet GraphQL query IDs (try in order).
_CREATE_TWEET_QUERY_IDS = [
    "SoVnbfCycZ7fERGCwpZkYA",
    "a1p9RWpkYKBjWv_fsagpyA",
    "tTsjMKyhajZvK4q76mpIBg",
]

_TWEET_FEATURES = {
    "tweetypie_unmention_optimization_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": False,
    "tweet_awards_web_tipping_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "interactive_text_enabled": True,
}


def _x_session_headers(ct0: str) -> dict:
    return {
        "Authorization":               f"Bearer {_BEARER}",
        "x-csrf-token":                ct0,
        "User-Agent":                  ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                                        "Chrome/124.0.0.0 Safari/537.36"),
        "Accept":                      "*/*",
        "Accept-Language":             "en-US,en;q=0.9",
        "Referer":                     "https://x.com/compose/tweet",
        "Origin":                      "https://x.com",
        "x-twitter-auth-type":         "OAuth2Session",
        "x-twitter-client-language":   "en",
        "x-twitter-active-user":       "yes",
    }


def _upload_media_http(jar: dict, ct0: str, image_path: Path) -> str | None:
    """Upload image to X media upload endpoint. Returns media_id_string or None."""
    try:
        import requests
        suffix = image_path.suffix.lower()
        mime = "image/png" if suffix == ".png" else "image/jpeg"
        hdrs = _x_session_headers(ct0)
        hdrs.pop("Content-Type", None)
        resp = requests.post(
            "https://upload.twitter.com/1.1/media/upload.json",
            files={"media": (image_path.name, image_path.read_bytes(), mime)},
            headers=hdrs,
            cookies=jar,
            timeout=60,
        )
        if resp.status_code == 200:
            return str(resp.json().get("media_id_string", ""))
        print(f"[x_cookies] media upload HTTP {resp.status_code}: {resp.text[:120]}")
    except Exception as exc:
        print(f"[x_cookies] media upload error: {exc}")
    return None


def post_via_cookies_http(cookies: list[dict], text: str,
                          image_path: Path | None = None) -> dict[str, Any]:
    """Post a tweet via X's internal GraphQL API using session cookies.

    No playwright needed — uses pure HTTP with the same bearer + CSRF auth
    that the x.com web app uses. Falls back to text-only if image upload fails.
    """
    try:
        import requests
    except ImportError:
        return {"status": "error", "error": "requests not installed"}

    jar: dict[str, str] = {c["name"]: c["value"] for c in cookies}
    if "auth_token" not in jar or "ct0" not in jar:
        return {"status": "error", "error": "Missing auth_token/ct0 cookies"}

    ct0 = jar["ct0"]
    headers = {**_x_session_headers(ct0), "Content-Type": "application/json"}

    media_id: str | None = None
    if image_path and Path(image_path).exists():
        media_id = _upload_media_http(jar, ct0, Path(image_path))
        if not media_id:
            print("[x_cookies] image upload failed — posting text-only")

    media_entities = (
        [{"media_id": media_id, "tagged_users": []}] if media_id else []
    )
    variables = {
        "tweet_text": text,
        "dark_request": False,
        "media": {
            "media_entities": media_entities,
            "possibly_sensitive": False,
        },
        "semantic_annotation_ids": [],
    }

    last_err = ""
    for qid in _CREATE_TWEET_QUERY_IDS:
        try:
            resp = requests.post(
                f"https://x.com/i/api/graphql/{qid}/CreateTweet",
                json={"variables": variables, "features": _TWEET_FEATURES,
                      "queryId": qid},
                headers=headers,
                cookies=jar,
                timeout=30,
            )
        except Exception as exc:
            last_err = f"network: {exc}"
            continue

        if resp.status_code in (401, 403):
            return {
                "status": "error",
                "error": (f"X rejected cookies (HTTP {resp.status_code}) — "
                          "they may be expired. Re-export from x.com and paste again."),
            }

        if resp.status_code != 200:
            last_err = f"HTTP {resp.status_code}: {resp.text[:160]}"
            continue

        try:
            data = resp.json()
        except Exception:
            last_err = f"JSON decode error: {resp.text[:120]}"
            continue

        # X sometimes returns 200 with an errors payload — that means the tweet
        # was NOT created (e.g. duplicate, rate-limit, auth).
        if data.get("errors"):
            errs  = data["errors"]
            codes = [e.get("code") for e in errs]
            msgs  = "; ".join(e.get("message", "")[:80] for e in errs)
            last_err = f"X API error {codes}: {msgs}"
            print(f"[x_cookies] CreateTweet error ({qid}): {last_err}")
            if 187 in codes:
                # duplicate content — treat as transient, try next query-ID
                continue
            # auth / permission errors — no point retrying other query-IDs
            if any(c in codes for c in (32, 64, 89, 135, 215, 326)):
                return {"status": "error",
                        "error": (f"X auth/permission error {codes}: {msgs}. "
                                  "Cookies may be expired — re-export from x.com.")}
            continue

        top_data = data.get("data") or {}
        result   = (top_data
                    .get("create_tweet", {})
                    .get("tweet_results", {})
                    .get("result", {}))

        # X sometimes wraps the tweet in TweetWithVisibilityResults
        if result.get("__typename") == "TweetWithVisibilityResults":
            result = result.get("tweet", {})

        tweet_id = (result.get("rest_id")
                    or result.get("legacy", {}).get("id_str", ""))

        if not tweet_id:
            if not result:
                # tweet_results.result was null — tweet was NOT created by this qid
                last_err = f"tweet_results.result was null (qid={qid})"
                print(f"[x_cookies] result null for qid={qid}, trying next")
                continue
            # result has keys but no rest_id — tweet probably posted, just
            # can't extract the URL (X changed schema). Log and return ok.
            print(f"[x_cookies] WARN: posted but no tweet_id. "
                  f"result keys={list(result.keys()) if isinstance(result, dict) else result}")

        url = (f"https://x.com/i/web/status/{tweet_id}"
               if tweet_id else "https://x.com/home")
        return {"status": "ok", "url": url, "via": "cookies-http",
                "has_image": media_id is not None}

    return {"status": "error",
            "error": f"X GraphQL CreateTweet failed: {last_err}"}


# ---------------------------------------------------------------------------
# Post a tweet via Playwright using stored cookies
# ---------------------------------------------------------------------------

def post_via_cookies(cookies: list[dict], text: str,
                     image_path: Path | None = None,
                     headless: bool = True) -> dict[str, Any]:
    """Open x.com with the given cookies and post a tweet.

    Returns {"status":"ok|error", ...}. Falls back to text-only if image fails.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        return {"status": "error",
                "error": "playwright not installed — pip install playwright && playwright install chromium"}

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"),
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            context.add_cookies(cookies)
            page = context.new_page()
            page.set_default_timeout(20000)

            # Intercept CreateTweet GraphQL response to capture the real tweet_id
            tweet_id_ref: list[str] = []

            def _on_resp(resp):  # noqa: E306
                try:
                    if "CreateTweet" in resp.url and resp.status == 200:
                        body = resp.json()
                        result = (body.get("data", {})
                                     .get("create_tweet", {})
                                     .get("tweet_results", {})
                                     .get("result", {}))
                        rid = (result.get("rest_id")
                               or result.get("legacy", {}).get("id_str", ""))
                        if rid:
                            tweet_id_ref.append(rid)
                except Exception:
                    pass

            page.on("response", _on_resp)

            page.goto("https://x.com/compose/tweet", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)

            if "login" in page.url or "i/flow/login" in page.url:
                browser.close()
                return {"status": "error",
                        "error": "Cookies expired. Re-export from x.com and paste again in Settings."}

            # Find the textarea
            textarea = None
            for sel in ['[data-testid="tweetTextarea_0"]',
                        'div[role="textbox"][data-testid*="tweetText"]',
                        '[contenteditable="true"][role="textbox"]']:
                try:
                    el = page.locator(sel).first
                    el.wait_for(state="visible", timeout=6000)
                    textarea = el
                    break
                except Exception:
                    continue

            if not textarea:
                browser.close()
                return {"status": "error",
                        "error": "Can't find compose box — X UI may have changed"}

            textarea.click()
            page.wait_for_timeout(250)
            page.keyboard.insert_text(text)
            page.wait_for_timeout(800)

            if image_path and Path(image_path).exists():
                try:
                    inputs = page.locator('input[type="file"]').all()
                    if inputs:
                        inputs[0].set_input_files(str(image_path))
                        try:
                            page.locator('[data-testid="attachments"]').wait_for(
                                state="visible", timeout=12000)
                            page.wait_for_timeout(1500)
                        except PWTimeout:
                            pass
                except Exception as exc:
                    print(f"[x_cookies] image upload skipped: {exc}")

            # Wait for post button to become enabled
            btn_sel = '[data-testid="tweetButton"]'
            try:
                page.locator(btn_sel).first.wait_for(state="visible", timeout=8000)
                for _ in range(20):
                    if page.locator(btn_sel).first.is_enabled():
                        break
                    page.wait_for_timeout(500)
            except PWTimeout:
                pass

            # Submit: keyboard shortcut first, button click fallback
            posted = False
            try:
                import platform as _p
                mod = "Meta" if _p.system() == "Darwin" else "Control"
                page.keyboard.press(f"{mod}+Enter")
                page.wait_for_timeout(3500)
                posted = True
            except Exception:
                pass

            if not posted:
                try:
                    btn = page.locator(btn_sel).first
                    if btn.is_visible() and btn.is_enabled():
                        btn.click()
                        page.wait_for_timeout(3500)
                        posted = True
                except Exception as exc:
                    browser.close()
                    return {"status": "error", "error": f"submit click failed: {exc}"}

            success = posted and (
                "home" in page.url or "compose" not in page.url
            )
            browser.close()
            if not success:
                return {"status": "error",
                        "error": "Submit silently failed — compose stayed open. Try re-pasting cookies."}
            tweet_id = tweet_id_ref[0] if tweet_id_ref else None
            tweet_url = (f"https://x.com/i/web/status/{tweet_id}"
                         if tweet_id else "https://x.com/home")
            return {"status": "ok",
                    "url":    tweet_url,
                    "via":    "cookies"}

    except Exception as exc:
        return {"status": "error",
                "error": f"browser automation crashed: {str(exc)[:200]}"}
