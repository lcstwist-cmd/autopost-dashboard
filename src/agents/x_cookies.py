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
