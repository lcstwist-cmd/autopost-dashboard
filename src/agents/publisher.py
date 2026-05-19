"""Publisher — multi-platform social media publisher.

Platforms:
  * Telegram: Telegram Bot API (sendPhoto with caption)
  * X / Twitter: pasted cookies (pure HTTP) → OAuth 2.0 → Make.com → tweepy
  * Instagram: Meta Graph API (native)
  * TikTok: Content Posting API (native)
  * YouTube: Data API v3 (native)

Env vars:
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

    X / Twitter (tried in order):
      X_COOKIES_JSON               → pasted session cookies (preferred, free)
      X_OAUTH_ACCESS_TOKEN         → OAuth 2.0 user-context
      MAKE_X_WEBHOOK_URL           → Make.com webhook (legacy)
      X_API_KEY + X_API_SECRET + X_ACCESS_TOKEN + X_ACCESS_TOKEN_SECRET → tweepy

    Instagram: IG_USER_ID, IG_ACCESS_TOKEN, PUBLIC_BASE_URL
    TikTok:    TIKTOK_ACCESS_TOKEN, TIKTOK_REFRESH_TOKEN, TIKTOK_CLIENT_KEY/SECRET
    YouTube:   YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN

Usage:
    python src/agents/publisher.py queue/2026-04-19_morning          # dry-run
    python src/agents/publisher.py queue/2026-04-19_morning --publish

    # only Telegram
    python src/agents/publisher.py queue/2026-04-19_morning --publish --platforms telegram
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent

# Ensure repo root and src/ are importable (needed when run as subprocess)
for _p in (str(_REPO_ROOT), str(_HERE.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

if not os.environ.get("AUTOPOST_ISOLATED"):
    try:
        from dotenv import load_dotenv as _load_dotenv
        _load_dotenv(_REPO_ROOT / ".env")
    except ImportError:
        pass

from typing import Any


# --- Audience-tracker shortlinks --------------------------------------------
# Replaces every http(s) URL in the post text with a /r/{code} that goes
# through the dashboard's redirect endpoint (logs IP → ipstack → country).
# Silent no-op when AUTOPOST_USER_ID or PUBLIC_BASE_URL are missing — the
# original URLs are preserved verbatim.

import re as _re_short

_URL_RE = _re_short.compile(r"https?://[^\s\)\]\}\>]+", _re_short.IGNORECASE)


def _wrap_urls_with_shortlinks(text: str, *, platform: str = "",
                                 slot_name: str = "") -> str:
    """Replace each http(s) URL in `text` with a tracker shortlink.

    Falls back to the original text whenever:
      * AUTOPOST_USER_ID is missing (publisher running standalone)
      * PUBLIC_BASE_URL is missing (no public redirect target)
      * the database / shortlink service is unavailable
    """
    if not text:
        return text
    uid = (os.environ.get("AUTOPOST_USER_ID") or "").strip()
    base = (os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if not uid or not base:
        return text
    try:
        uid_int = int(uid)
    except ValueError:
        return text
    try:
        from src.dashboard.database import create_shortlink as _db_create
    except Exception:
        try:
            from dashboard.database import create_shortlink as _db_create  # type: ignore
        except Exception:
            return text

    cache: dict[str, str] = {}

    def _swap(m):
        url = m.group(0).rstrip(".,;:!?")
        suffix = m.group(0)[len(url):]
        if url in cache:
            return cache[url] + suffix
        # Skip self-referential links (don't double-wrap)
        if url.startswith(base + "/r/"):
            cache[url] = url
            return url + suffix
        try:
            link = _db_create(
                user_id=uid_int, target_url=url,
                label=f"{platform or 'auto'}",
                platform=platform, slot_name=slot_name,
            )
            short = f"{base}/r/{link['code']}"
        except Exception as exc:
            print(f"[publisher] shortlink wrap failed for {url[:60]}: {exc}")
            short = url
        cache[url] = short
        return short + suffix

    return _URL_RE.sub(_swap, text)


# --- Trending hashtag injection for X ---------------------------------------

_BODY_MAX_CHARS = 195   # leave ~80 chars for hashtags/cashtags


def _inject_trending_tags(text: str, tone: str = "crypto") -> str:
    """Append trending hashtags & cashtags to an X post.

    Body is capped at _BODY_MAX_CHARS so there is always room for 6-8 tags.
    Fetches from the x_trending cache (auto-refreshed every 6h).
    """
    try:
        from src.agents.x_trending import get_trending_tags
    except ImportError:
        try:
            from .x_trending import get_trending_tags  # type: ignore
        except ImportError:
            try:
                import importlib.util as _ilu
                _spec = _ilu.spec_from_file_location(
                    "x_trending", Path(__file__).parent / "x_trending.py"
                )
                _mod = _ilu.module_from_spec(_spec)  # type: ignore
                _spec.loader.exec_module(_mod)        # type: ignore
                get_trending_tags = _mod.get_trending_tags
            except Exception:
                return text

    tags = get_trending_tags(max_cashtags=4, max_hashtags=6, tone=tone)
    if not tags:
        return text

    # Deduplicate: skip tags whose symbol already appears in the body
    existing_low = text.lower()
    new_tags = [t for t in tags if t.lstrip("#$").lower() not in existing_low]
    if not new_tags:
        new_tags = tags  # everything already mentioned — still append for reach

    # Trim body to _BODY_MAX_CHARS at a word boundary, preserving full words
    base = text.rstrip()
    if x_weighted_len(base) > _BODY_MAX_CHARS:
        while x_weighted_len(base) > _BODY_MAX_CHARS and " " in base:
            base = base.rsplit(" ", 1)[0].rstrip(" ,;:")
        base = base + "…"

    # Greedily append tags until we hit the 275-char ceiling
    added: list[str] = []
    for tag in new_tags:
        candidate = base + "\n" + " ".join(added + [tag])
        if x_weighted_len(candidate) <= 275:
            added.append(tag)
        else:
            break

    if not added:
        return base
    # Cashtags ($) on their own line — higher visibility in X discovery
    cashtags = [t for t in added if t.startswith("$")]
    hashtags  = [t for t in added if t.startswith("#")]
    tag_block = ""
    if cashtags:
        tag_block += "\n" + " ".join(cashtags)
    if hashtags:
        tag_block += "\n" + " ".join(hashtags)
    return base + tag_block


# --- Fact-checker integration -----------------------------------------------

def _fact_check_post(text: str, tickers: list[str] | None = None) -> None:
    """Run fact checker on post text; logs warnings but never blocks."""
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "fact_checker", Path(__file__).parent / "fact_checker.py"
        )
        _mod = _ilu.module_from_spec(_spec)   # type: ignore
        _spec.loader.exec_module(_mod)         # type: ignore
        result = _mod.check_post(text, tickers=tickers or [], strict=False)
        if result.get("issues"):
            for issue in result["issues"]:
                sev = issue.get("severity", "warn")
                ticker = issue.get("ticker", "?")
                diff = issue.get("diff_pct") or issue.get("diff_pp", 0)
                print(f"[fact_check] {sev.upper()} — {ticker}: "
                      f"claim={issue.get('claim')}, live={issue.get('live')}, "
                      f"diff={diff}%")
    except Exception:
        pass   # fact checker is always non-blocking


# --- Weighted char count (X) ------------------------------------------------

def x_weighted_len(text: str) -> int:
    """Twitter/X weighted length: emoji + CJK chars count as 2."""
    count = 0
    for ch in text:
        cp = ord(ch)
        if (cp >= 0x1F000 or
            0x2E80 <= cp <= 0x9FFF or
            0xA000 <= cp <= 0xD7FF or
            0xF900 <= cp <= 0xFAFF):
            count += 2
        else:
            count += 1
    return count


# --- Queue loader -----------------------------------------------------------

REQUIRED_FILES = {
    "post_x":       "post_x.txt",         # variant A (default)
    "post_tg":      "post_telegram.md",   # fallback: post_tg.txt accepted too
    "image_x":      "image_x_1200x675.png",
    "image_tg":     "image_tg_1080x1080.png",
}


def _pick_variant(queue_dir: Path, override: str | None) -> tuple[str, str]:
    """Decide which X variant to send.

    Returns (variant_label, file_used).
      - override="A"|"B" forces a variant
      - otherwise: use AB tester learned winner if available, else 50/50
    """
    if override:
        ov = override.strip().upper()
        if ov not in ("A", "B"):
            raise ValueError(f"[publisher] --variant must be A or B, got {override!r}")
        return ov, "post_x.txt" if ov == "A" else "post_x_b.txt"

    has_b = (queue_dir / "post_x_b.txt").exists()
    if not has_b:
        return "A", "post_x.txt"

    # Use AB tester learned winner if available (enough data to trust)
    try:
        _REPO_ROOT = Path(__file__).resolve().parent.parent.parent
        brain_path = _REPO_ROOT / "analytics" / "agent_brains" / "ab_tester" / "learned_params.json"
        if brain_path.exists():
            params = json.loads(brain_path.read_text(encoding="utf-8"))
            preferred = params.get("preferred_variant", "").upper()
            if preferred in ("A", "B"):
                print(f"[publisher] variant: using AB-learned winner = {preferred}")
                return preferred, "post_x.txt" if preferred == "A" else "post_x_b.txt"
    except Exception:
        pass

    # Fallback: deterministic 50/50 based on queue dir name
    h = hashlib.sha256(queue_dir.name.encode()).digest()[0]
    variant = "A" if h % 2 == 0 else "B"
    return variant, "post_x.txt" if variant == "A" else "post_x_b.txt"


def load_queue(queue_dir: Path, variant_override: str | None = None) -> dict[str, Any]:
    if not queue_dir.is_dir():
        raise FileNotFoundError(f"[publisher] queue dir not found: {queue_dir}")

    variant, x_file = _pick_variant(queue_dir, variant_override)

    # post_tg accepts both post_telegram.md (legacy) and post_tg.txt (current)
    _tg_file = (REQUIRED_FILES["post_tg"]
                if (queue_dir / REQUIRED_FILES["post_tg"]).exists()
                else "post_tg.txt")
    required = [_tg_file, REQUIRED_FILES["image_x"],
                REQUIRED_FILES["image_tg"], x_file]
    missing = [fname for fname in required if not (queue_dir / fname).exists()]
    if missing:
        raise FileNotFoundError(f"[publisher] missing files in {queue_dir}: {missing}")

    post_x = (queue_dir / x_file).read_text(encoding="utf-8").strip()
    # Append CTA link before trimming so ensure_x_budget never drops it.
    # Twitter wraps any URL to 23 chars (t.co), but x_weighted_len counts each
    # character, so we reserve the full raw length as a conservative budget.
    _X_CTA = "\n\nhttps://elitemargindesk.io/app?trial=1&utm_source=x"
    _cta_w = x_weighted_len(_X_CTA)          # = 54 (conservative)
    _body_limit = 280 - _cta_w               # = 226
    from src.agents.copywriter import ensure_x_budget as _cap
    post_x = _cap(post_x, hard_limit=_body_limit)
    post_x = post_x.rstrip() + _X_CTA
    print(f"[publisher] post_x with CTA: {x_weighted_len(post_x)} weighted chars (twitter≈{x_weighted_len(post_x) - _cta_w + 25})")
    post_tg = (queue_dir / _tg_file).read_text(encoding="utf-8").strip()
    image_x = queue_dir / REQUIRED_FILES["image_x"]
    image_tg = queue_dir / REQUIRED_FILES["image_tg"]

    # Platform-specific captions (generated by copywriter per viral blueprint)
    def _read_opt(fname: str) -> str:
        p = queue_dir / fname
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""

    post_tiktok  = _read_opt("post_tiktok.txt") or post_tg
    post_ig      = _read_opt("post_ig.txt")     or post_tg
    post_youtube = _read_opt("post_youtube.txt") or post_tg

    # Load URL to append (if the ranker stored top2.json, use its top story URL)
    source_url = ""
    top2 = queue_dir / "top2.json"
    if top2.exists():
        try:
            blob = json.loads(top2.read_text(encoding="utf-8"))
            stories = blob.get("stories") or []
            if stories:
                source_url = stories[0].get("url", "") or ""
        except json.JSONDecodeError:
            pass

    return {
        "post_x":       post_x,
        "post_tg":      post_tg,
        "post_tiktok":  post_tiktok,
        "post_ig":      post_ig,
        "post_youtube": post_youtube,
        "image_x":      image_x,
        "image_tg":     image_tg,
        "source_url":   source_url,
        "variant":      variant,
        "x_file":       x_file,
    }


# --- Safety rails -----------------------------------------------------------

def validate(queue: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    wx = x_weighted_len(queue["post_x"])
    if wx > 280:
        errors.append(f"post_x is {wx} weighted chars (X limit: 280)")

    if not queue["image_x"].exists():
        errors.append(f"image_x missing: {queue['image_x']}")
    if not queue["image_tg"].exists():
        errors.append(f"image_tg missing: {queue['image_tg']}")

    if len(queue["post_tg"]) < 80:
        errors.append(f"post_tg suspiciously short ({len(queue['post_tg'])} chars)")
    if len(queue["post_tg"]) > 4000:
        errors.append(f"post_tg too long for Telegram caption ({len(queue['post_tg'])} chars)")

    return errors


# --- Telegram publisher -----------------------------------------------------

def publish_telegram(queue: dict[str, Any], dry_run: bool = True) -> dict[str, Any]:
    """Send photo with caption — text and image in one message."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    caption = queue["post_tg"]
    # Wrap any URL in the caption through /r/{code} so we can geolocate
    # subscribers when they tap. Wrapping happens BEFORE the 1024-char
    # truncation because shortlinks are typically shorter than originals.
    caption = _wrap_urls_with_shortlinks(
        caption, platform="telegram",
        slot_name=str(queue.get("_slot", "")),
    )
    # Hard cap: Telegram allows max 1024 chars for photo caption
    if len(caption) > 1024:
        caption = caption[:1021].rstrip() + "..."

    payload_preview = {
        "method":     "sendPhoto",
        "chat_id":    chat_id or "<TELEGRAM_CHAT_ID>",
        "photo":      str(queue["image_tg"]),
        "caption":    caption,
        "parse_mode": "Markdown",
    }

    if dry_run:
        return {"status": "dry_run", "payload": payload_preview}

    if not token or not chat_id:
        return {"status": "error",
                "error": "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set"}

    try:
        import requests
    except ImportError:
        return {"status": "error",
                "error": "requests not installed (pip install requests)"}

    base = f"https://api.telegram.org/bot{token}"

    try:
        with open(queue["image_tg"], "rb") as fh:
            r = requests.post(
                f"{base}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"},
                files={"photo": fh},
                timeout=30,
            )
        r.raise_for_status()
        return {"status": "ok",
                "message_id": r.json().get("result", {}).get("message_id")}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)}


# --- X via OAuth 2.0 (SaaS-grade, multi-user safe) --------------------------

def publish_x_oauth2(queue: dict[str, Any], dry_run: bool = True) -> dict[str, Any]:
    """Post a tweet using OAuth 2.0 user-context tokens.

    Requires these env vars (set per user by the dashboard via _uenv):
      X_OAUTH_ACCESS_TOKEN   — fresh bearer token for this user
      X_OAUTH_REFRESH_TOKEN  — used if access token is expired
      X_OAUTH_EXPIRES_AT     — unix seconds

    The caller (dashboard) is responsible for refreshing expired tokens
    BEFORE calling publish(); that keeps token persistence out of the
    publisher process and lets it stay stateless.
    """
    text:       str  = queue["post_x"]
    image_path: Path = queue["image_x"]
    text = _wrap_urls_with_shortlinks(
        text, platform="x",
        slot_name=str(queue.get("_slot", "")),
    )
    text = _inject_trending_tags(text, tone=queue.get("_tone", "crypto"))

    if dry_run:
        return {"status": "dry_run", "text": text, "via": "oauth2"}

    access = os.environ.get("X_OAUTH_ACCESS_TOKEN", "").strip()
    if not access:
        return {"status": "error", "error": "no X OAuth 2.0 access_token in env"}

    try:
        from agents.x_oauth import post_tweet, upload_image_v2
    except ImportError:
        try:
            from .x_oauth import post_tweet, upload_image_v2  # type: ignore
        except ImportError:
            try:
                from src.agents.x_oauth import post_tweet, upload_image_v2  # type: ignore
            except ImportError as exc:
                return {"status": "error", "error": f"x_oauth import failed: {exc}"}

    media_ids: list[str] = []
    if image_path and Path(image_path).exists():
        try:
            mid = upload_image_v2(access, Path(image_path))
            if mid:
                media_ids.append(mid)
        except Exception as exc:
            print(f"[publisher] oauth2 media upload failed: {exc}")

    try:
        return post_tweet(access, text, media_ids=media_ids or None)
    except Exception as exc:
        return {"status": "error", "error": f"oauth2: {exc}"}


# --- X via pasted cookies (no API, no password, 10-sec setup) -------------

def publish_x_cookies(queue: dict[str, Any], dry_run: bool = True) -> dict[str, Any]:
    """Post using cookies the user pasted in dashboard Settings.

    Reads `X_COOKIES_JSON` and tries:
      1. Pure-HTTP via X's internal GraphQL API (no playwright needed).
      2. Playwright headless browser (fallback, requires playwright installed).
    Zero developer account, zero credentials stored.
    """
    text:       str  = queue["post_x"]
    image_path: Path = queue["image_x"]
    text = _wrap_urls_with_shortlinks(
        text, platform="x", slot_name=str(queue.get("_slot", "")),
    )
    text = _inject_trending_tags(text, tone=queue.get("_tone", "crypto"))

    if dry_run:
        return {"status": "dry_run", "text": text, "via": "cookies"}

    raw = os.environ.get("X_COOKIES_JSON", "").strip()
    if not raw:
        return {"status": "error", "error": "no X cookies in env"}

    try:
        from agents.x_cookies import validate_cookies, post_via_cookies_http, post_via_cookies
    except ImportError:
        try:
            from .x_cookies import validate_cookies, post_via_cookies_http, post_via_cookies  # type: ignore
        except ImportError:
            try:
                from src.agents.x_cookies import validate_cookies, post_via_cookies_http, post_via_cookies  # type: ignore
            except ImportError as exc:
                return {"status": "error", "error": f"x_cookies import failed: {exc}"}

    cookies, err = validate_cookies(raw)
    if err:
        return {"status": "error", "error": f"cookies invalid: {err}"}

    # Try pure-HTTP first (no playwright needed — same session auth x.com uses).
    result = post_via_cookies_http(cookies, text, image_path=image_path)
    if result.get("status") == "ok":
        return result
    http_err = result.get("error", "unknown")
    print(f"[publisher] cookies-http: {http_err} — trying playwright fallback")

    # Playwright fallback (requires: pip install playwright && playwright install chromium).
    return post_via_cookies(cookies, text, image_path=image_path, headless=True)


# --- X via browser automation (Playwright — no API plan needed) -------------

def _browser_cookies_path() -> Path:
    """Per-user Playwright cookies path."""
    data_dir = os.environ.get("DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir) / "x_browser_cookies.json"
    return _HERE / ".x_browser_cookies.json"




def publish_x_browser(queue: dict[str, Any], dry_run: bool = True) -> dict[str, Any]:
    """Post to X using browser automation with saved cookies.

    Strategy (rewritten 2026-04-23):
      1. Go DIRECTLY to https://x.com/compose/tweet (no need to click compose button)
      2. Wait for textarea; fill; attach image via <input type="file">
      3. Wait for the post button to become *enabled* (X disables it until valid)
      4. Use Ctrl/Cmd+Enter keyboard shortcut as primary submit path
      5. Fall back to clicking the tweetButton by data-testid

    Workflow for the user:
      1. First time: `python src/agents/x_login_helper.py` → saves cookies
      2. Then the scheduler can post automatically
    """
    text:       str  = queue["post_x"]
    image_path: Path = queue["image_x"]
    text = _wrap_urls_with_shortlinks(
        text, platform="x", slot_name=str(queue.get("_slot", "")),
    )
    text = _inject_trending_tags(text, tone=queue.get("_tone", "crypto"))

    if dry_run:
        return {"status": "dry_run", "text": text, "via": "browser"}

    cookies_file = _browser_cookies_path()
    if not cookies_file.exists():
        return {"status": "error",
                "error": "No browser cookies found. Run: python src/agents/x_login_helper.py "
                         "(or connect X via OAuth 2.0 in the dashboard for automatic posting)"}

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        return {"status": "error",
                "error": "playwright not installed — pip install playwright && playwright install chromium"}

    try:
        with open(cookies_file, "r", encoding="utf-8") as f:
            cookies = json.load(f)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=['--disable-blink-features=AutomationControlled',
                      '--no-sandbox', '--disable-dev-shm-usage'],
            )
            context = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"),
                viewport={"width": 1280, "height": 900},
            )
            context.add_cookies(cookies)

            page = context.new_page()
            page.set_default_timeout(20000)

            # Intercept CreateTweet GraphQL response to capture real tweet_id
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

            # Go directly to the compose dialog — no need to click the side-nav
            print("[publisher] browser: opening compose tweet URL directly...")
            page.goto("https://x.com/compose/tweet", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)

            # Check that we're logged in — redirect to /login means cookies expired
            if "login" in page.url or "i/flow/login" in page.url:
                browser.close()
                return {"status": "error",
                        "error": "Cookies expired. Run: python src/agents/x_login_helper.py"}

            # Find the textarea
            textarea_selectors = [
                '[data-testid="tweetTextarea_0"]',
                'div[role="textbox"][data-testid*="tweetText"]',
                '[contenteditable="true"][role="textbox"]',
            ]
            compose_box = None
            for sel in textarea_selectors:
                try:
                    elem = page.locator(sel).first
                    elem.wait_for(state="visible", timeout=6000)
                    compose_box = elem
                    print(f"[publisher] browser: found textarea via '{sel}'")
                    break
                except Exception:
                    continue

            if not compose_box:
                browser.close()
                return {"status": "error",
                        "error": "Could not find compose textarea — X UI may have changed or cookies expired"}

            # Click + fill text (using fill is more reliable than type for contenteditable)
            compose_box.click()
            page.wait_for_timeout(300)
            # Use keyboard because fill() doesn't always work on draft-js contenteditable
            page.keyboard.insert_text(text)
            page.wait_for_timeout(800)
            print(f"[publisher] browser: inserted {len(text)} chars of text")

            # Attach image
            if image_path and image_path.exists():
                try:
                    # The <input type="file"> may be hidden; set directly
                    file_inputs = page.locator('input[type="file"]').all()
                    if file_inputs:
                        file_inputs[0].set_input_files(str(image_path))
                        print("[publisher] browser: image attached, waiting for upload...")
                        # Wait for the image preview to appear (X uploads async)
                        try:
                            page.locator('[data-testid="attachments"]').wait_for(
                                state="visible", timeout=12000)
                            page.wait_for_timeout(1500)
                        except PWTimeout:
                            print("[publisher] browser: image preview didn't show — proceeding anyway")
                except Exception as img_exc:
                    print(f"[publisher] browser: image upload skipped ({img_exc})")

            # Wait for post button to become ENABLED (X disables it until text is valid)
            post_btn_sel = '[data-testid="tweetButton"]'
            try:
                page.locator(post_btn_sel).first.wait_for(state="visible", timeout=8000)
                # Poll until the button is enabled
                for _ in range(20):  # up to 10s
                    btn = page.locator(post_btn_sel).first
                    if btn.is_enabled():
                        break
                    page.wait_for_timeout(500)
            except PWTimeout:
                pass

            # Submit via keyboard shortcut (Ctrl+Enter or Cmd+Enter)
            # This is the most reliable path on X; no dependency on button layout
            posted = False
            try:
                import platform as _plat
                mod = "Meta" if _plat.system() == "Darwin" else "Control"
                page.keyboard.press(f"{mod}+Enter")
                print(f"[publisher] browser: submitted via {mod}+Enter")
                page.wait_for_timeout(3500)
                posted = True
            except Exception as kb_exc:
                print(f"[publisher] browser: keyboard shortcut failed ({kb_exc}), trying button click")

            # Fallback: click the tweet button
            if not posted:
                try:
                    btn = page.locator(post_btn_sel).first
                    if btn.is_visible() and btn.is_enabled():
                        btn.click()
                        page.wait_for_timeout(3500)
                        posted = True
                        print("[publisher] browser: submitted via button click")
                except Exception as click_exc:
                    print(f"[publisher] browser: click fallback failed ({click_exc})")

            # Confirm post: check if we got redirected back to /home (compose closes on success)
            current_url = page.url
            success = posted and ("home" in current_url or "compose" not in current_url)

            if not success:
                browser.close()
                return {"status": "error",
                        "error": "Submit may have failed — compose dialog still open. Check cookies."}

            browser.close()
            print("[publisher] browser: tweet posted successfully")
            tweet_id = tweet_id_ref[0] if tweet_id_ref else None
            tweet_url = (f"https://x.com/i/web/status/{tweet_id}"
                         if tweet_id else "https://x.com/home")
            return {"status": "ok", "url": tweet_url, "via": "browser"}

    except Exception as exc:
        return {"status": "error",
                "error": f"browser automation failed: {str(exc)[:200]}"}


# --- X via Make.com webhook (free, no API plan needed) ----------------------

def _publish_x_makecom(text: str, image_path: Path, webhook_url: str) -> dict[str, Any]:
    """POST to a Make.com Custom Webhook that posts to X.

    Make.com setup (once, free at make.com):
      1. New Scenario → Webhooks > Custom Webhook  (copy the URL to Settings)
      2. Add module: Twitter (X) > Create a Tweet
      3. Map: Text = {{1.text}}
      4. (Optional for image) add Tools > Base64 Decode → Twitter > Upload Media
         then pass media_id to Create a Tweet
      5. Save & Activate
    """
    try:
        import requests as _req
    except ImportError:
        return {"status": "error", "error": "requests not installed"}

    payload: dict[str, Any] = {"text": text}

    # Attach image as base64 so Make.com can optionally upload it
    if image_path and image_path.exists():
        try:
            with open(image_path, "rb") as fh:
                payload["image_base64"]  = base64.b64encode(fh.read()).decode()
                payload["image_filename"] = image_path.name
        except Exception:
            pass  # image is optional

    try:
        resp = _req.post(webhook_url, json=payload, timeout=30)
        if resp.status_code in (200, 204):
            return {"status": "ok", "via": "make.com", "response": resp.text[:120]}
        return {
            "status": "error",
            "error": f"Make.com webhook returned HTTP {resp.status_code}: {resp.text[:200]}",
        }
    except Exception as exc:
        return {"status": "error", "error": f"Make.com request failed: {exc}"}


# --- Generic Make.com webhook poster ----------------------------------------

def _post_makecom_media(webhook_url: str, text: str, image_path: Path,
                         video_path: Path | None = None,
                         extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """POST caption + image (+ optional video) to a Make.com Custom Webhook."""
    try:
        import requests as _req
    except ImportError:
        return {"status": "error", "error": "requests not installed"}

    payload: dict[str, Any] = {"text": text, "caption": text}
    if extra:
        payload.update(extra)

    if image_path and image_path.exists():
        try:
            with open(image_path, "rb") as fh:
                payload["image_base64"]  = base64.b64encode(fh.read()).decode()
                payload["image_filename"] = image_path.name
        except Exception:
            pass

    if video_path and video_path.exists():
        try:
            with open(video_path, "rb") as fh:
                payload["video_base64"]  = base64.b64encode(fh.read()).decode()
                payload["video_filename"] = video_path.name
        except Exception:
            pass

    try:
        resp = _req.post(webhook_url, json=payload, timeout=60)
        if resp.status_code in (200, 204):
            return {"status": "ok", "via": "make.com",
                    "response": resp.text[:120]}
        return {
            "status": "error",
            "error": f"Make.com webhook returned HTTP {resp.status_code}: {resp.text[:200]}",
        }
    except Exception as exc:
        return {"status": "error", "error": f"Make.com request failed: {exc}"}


# --- Helper: find a vertical video in the queue dir -------------------------

def _find_video(queue_dir: Path) -> Path | None:
    """Return the best available vertical video in the slot (for TT/YT/IG reel).

    Priority: avatar (best quality) > reel_final (moviepy) > reel_avatar
              > reel_simple (ffmpeg fallback) > any other mp4.
    If no video exists at all, auto-generates one via simple_video_gen.
    """
    for pat in ("avatar_*.mp4", "reel_final.mp4", "reel_avatar.mp4",
                "reel_simple.mp4", "reel_*.mp4", "*.mp4"):
        matches = [p for p in sorted(queue_dir.glob(pat)) if p.stat().st_size > 10_000]
        if matches:
            return matches[0]

    # No video found — generate one automatically with ffmpeg
    try:
        from src.agents.simple_video_gen import build_simple_video
    except ImportError:
        try:
            from agents.simple_video_gen import build_simple_video  # type: ignore
        except ImportError:
            return None
    return build_simple_video(queue_dir)


# --- Instagram publisher (NATIVE Graph API) ---------------------------------

def publish_instagram(queue_dir: Path, queue: dict[str, Any],
                      dry_run: bool = True) -> dict[str, Any]:
    """Publish to Instagram via the official Meta Graph API.

    Priority:
      1. Reel  — when an MP4 exists (avatar_*.mp4, reel_*.mp4, reel_simple.mp4).
                 Reels get 3-5x more reach than feed photos on IG algorithm.
      2. Feed photo — fallback when no video available (1080x1080 square).

    Needs: IG_USER_ID, IG_ACCESS_TOKEN
    Video/image served via litterbox/catbox/PUBLIC_BASE_URL (no IMGBB key needed).
    """
    text       = queue.get("post_ig") or queue["post_tg"] or queue["post_x"]
    image_path = queue["image_tg"]
    caption    = text[:2200]

    if dry_run:
        video = _find_video(queue_dir)
        return {"status": "dry_run",
                "text":   text[:80] + "...",
                "format": "reel" if (video and video.exists()) else "feed_photo",
                "image":  str(image_path)}

    try:
        from agents.social_publishers import (publish_instagram_api,
                                               publish_instagram_reel_api)
        from agents.media_host import get_public_url
    except ImportError:
        try:
            from .social_publishers import (publish_instagram_api,      # type: ignore
                                             publish_instagram_reel_api)
            from .media_host        import get_public_url               # type: ignore
        except ImportError as exc:
            return {"status": "error", "error": f"import failed: {exc}"}

    # ── Try Reel first (video) — significantly better reach ─────────────────
    video = _find_video(queue_dir)
    if video and video.exists():
        print(f"[publisher] instagram: posting as Reel ({video.name})")
        video_url, vid_provider = get_public_url(video)
        if video_url:
            # Cover image = roundup card (9:16) or fallback to TG square
            cover_candidates = [
                queue_dir / "image_reel_1080x1920.png",
                queue_dir / "image_ig_1080x1350.png",
                image_path,
            ]
            cover_path = next((p for p in cover_candidates if p.exists()), image_path)
            cover_url, _ = get_public_url(cover_path)
            result = publish_instagram_reel_api(caption, video_url,
                                                cover_url=cover_url)
            result["video_host"] = vid_provider
            result["format"] = "reel"
            return result
        print("[publisher] instagram: video upload failed, falling back to feed photo")

    # ── Fallback: feed photo ─────────────────────────────────────────────────
    # Prefer roundup IG image (4:5) over square
    ig_img = queue_dir / "image_ig_1080x1350.png"
    feed_image = ig_img if ig_img.exists() else image_path
    public_url, provider = get_public_url(feed_image)
    if not public_url:
        return {"status": "error",
                "error": "Cannot produce a public image URL. Set IMGBB_API_KEY "
                         "or PUBLIC_BASE_URL, or all free hosts failed."}

    result = publish_instagram_api(caption, public_url)
    result["image_host"] = provider
    result["format"] = "feed_photo"
    return result


# --- TikTok publisher (NATIVE Content Posting API) -------------------------

def publish_tiktok(queue_dir: Path, queue: dict[str, Any],
                   dry_run: bool = True) -> dict[str, Any]:
    """Publish a vertical video to TikTok via the Content Posting API.

    Uploads the MP4 directly (chunked). Does not require a public URL.
    """
    text  = queue.get("post_tiktok") or queue["post_tg"] or queue["post_x"]
    video = _find_video(queue_dir)

    if dry_run:
        return {"status": "dry_run",
                "video": str(video) if video else None,
                "text":  text[:80] + "..."}

    if not video or not video.exists():
        return {"status": "skipped",
                "error": "no video file in queue (TikTok requires an MP4)"}

    try:
        from agents.social_publishers import publish_tiktok_api
    except ImportError:
        try:
            from .social_publishers import publish_tiktok_api  # type: ignore
        except ImportError as exc:
            return {"status": "error", "error": f"import failed: {exc}"}

    caption = text[:2200]

    # Append SRT transcript to description so TikTok can auto-caption
    # (TikTok API v2 does not accept external SRT files; burning into video
    # is handled by video_builder. We add the transcript text as well.)
    try:
        srt_candidates = [
            queue_dir / "reel" / "03_captions.srt",
            queue_dir / "reel" / "captions_20s.srt",
        ]
        for srt_path in srt_candidates:
            if srt_path.exists():
                import re as _re
                srt_raw = srt_path.read_text(encoding="utf-8", errors="replace")
                # Extract plain text lines (skip index/timestamp lines)
                lines = [l.strip() for l in srt_raw.splitlines()
                         if l.strip() and not l.strip().isdigit()
                         and "-->" not in l]
                transcript = " ".join(lines)[:300]
                if transcript and transcript not in caption:
                    caption = (caption + "\n\n" + transcript)[:2200]
                break
    except Exception:
        pass

    return publish_tiktok_api(caption, video)


# --- Facebook Page publisher (Graph API v18) --------------------------------

def _fb_post_reel(page_id: str, token: str, description: str, video_path: "Path") -> dict:
    """Upload and publish a Facebook Reel via the 3-step Video Reels API."""
    import urllib.request as _ur, urllib.parse as _up
    file_size = video_path.stat().st_size

    # 1) Initialise upload
    init_qs = _up.urlencode({"upload_phase": "start", "file_size": file_size,
                              "access_token": token})
    req = _ur.Request(
        f"https://graph.facebook.com/v18.0/{page_id}/video_reels?{init_qs}",
        data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
    )
    with _ur.urlopen(req, timeout=30) as r:
        init = json.loads(r.read())
    video_id  = init.get("video_id")
    upload_url = init.get("upload_url")
    if not video_id or not upload_url:
        raise RuntimeError(f"Reels init failed: {init}")

    # 2) Upload bytes
    with open(str(video_path), "rb") as fh:
        video_bytes = fh.read()
    up_req = _ur.Request(
        upload_url, data=video_bytes,
        headers={"Authorization": f"OAuth {token}", "Content-Type": "video/mp4",
                 "offset": "0", "file_size": str(file_size)},
        method="POST",
    )
    with _ur.urlopen(up_req, timeout=300) as r:
        up_res = json.loads(r.read())
    if not up_res.get("success"):
        raise RuntimeError(f"Reels upload failed: {up_res}")

    # 3) Publish
    finish_qs = _up.urlencode({"upload_phase": "finish", "video_id": video_id,
                                "title": description[:255], "description": description[:2000],
                                "video_state": "PUBLISHED", "access_token": token})
    fin_req = _ur.Request(
        f"https://graph.facebook.com/v18.0/{page_id}/video_reels?{finish_qs}",
        data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
    )
    with _ur.urlopen(fin_req, timeout=30) as r:
        pub = json.loads(r.read())
    if not pub.get("success"):
        raise RuntimeError(f"Reels publish failed: {pub}")
    return {"status": "ok", "video_id": video_id, "reel": True}


def publish_facebook(queue_dir: "Path", queue: dict, dry_run: bool = True) -> dict:
    """Post to Facebook Page: Reel (if video exists) or photo+text post.

    Requires env vars:
      FB_ACCESS_TOKEN — long-lived Page Access Token
      FB_PAGE_ID      — numeric Facebook Page ID
    """
    token   = os.environ.get("FB_ACCESS_TOKEN", "").strip()
    page_id = os.environ.get("FB_PAGE_ID", "").strip()
    if not token or not page_id:
        return {"status": "error",
                "error": "FB_ACCESS_TOKEN and FB_PAGE_ID required in .env / Settings"}

    text  = (queue.get("post_facebook")
             or queue.get("post_tg")
             or queue.get("post_x", ""))[:63206]  # Facebook's text limit
    video = _find_video(queue_dir)

    if dry_run:
        return {"status": "dry_run", "text": text[:80] + "...",
                "video": str(video) if video else None}

    import urllib.request as _ur, urllib.parse as _up

    # Prefer Reel when a video file exists
    if video and video.exists():
        try:
            return _fb_post_reel(page_id, token, text, video)
        except Exception as exc:
            print(f"[facebook] Reel upload failed ({exc}), falling back to photo post")

    # Photo post fallback
    image = queue_dir / "image_tg_1080x1080.png"
    if not image.exists():
        image = queue_dir / "image_x_1200x675.png"

    try:
        if image.exists():
            boundary = "----FBFormBoundary7kQtw"
            with open(str(image), "rb") as fh:
                img_bytes = fh.read()
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"source\"; "
                f"filename=\"{image.name}\"\r\nContent-Type: image/png\r\n\r\n"
            ).encode() + img_bytes + (
                f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n"
                + text +
                f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"access_token\"\r\n\r\n"
                + token + f"\r\n--{boundary}--\r\n"
            ).encode()
            req = _ur.Request(
                f"https://graph.facebook.com/v18.0/{page_id}/photos",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            with _ur.urlopen(req, timeout=60) as r:
                res = json.loads(r.read())
            return {"status": "ok", "id": res.get("id", ""), "post_id": res.get("post_id", "")}
        else:
            # Text-only post
            data = _up.urlencode({"message": text, "access_token": token}).encode()
            req = _ur.Request(f"https://graph.facebook.com/v18.0/{page_id}/feed", data=data)
            with _ur.urlopen(req, timeout=30) as r:
                res = json.loads(r.read())
            return {"status": "ok", "id": res.get("id", "")}
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:500]}


# --- YouTube Shorts publisher (NATIVE Data API v3) --------------------------

def publish_youtube(queue_dir: Path, queue: dict[str, Any],
                    dry_run: bool = True) -> dict[str, Any]:
    """Publish a vertical video to YouTube (Shorts-compatible) via Data API v3.

    Uploads the MP4 directly via resumable upload.
    """
    text  = queue.get("post_youtube") or queue["post_tg"] or queue["post_x"]
    video = _find_video(queue_dir)

    if dry_run:
        return {"status": "dry_run",
                "video": str(video) if video else None,
                "text":  text[:80] + "..."}

    if not video or not video.exists():
        return {"status": "skipped",
                "error": "no video file in queue (YouTube requires an MP4)"}

    try:
        from agents.social_publishers import publish_youtube_api
    except ImportError:
        try:
            from .social_publishers import publish_youtube_api  # type: ignore
        except ImportError as exc:
            return {"status": "error", "error": f"import failed: {exc}"}

    # Title = first line of post_youtube (stripped of markdown), max 100 chars.
    first_line = (text.strip().splitlines() or ["Crypto Update"])[0]
    title = first_line.lstrip("# *_").strip()[:95] + " #Shorts"
    description = text[:4900]
    tags = ["crypto", "bitcoin", "ethereum", "shorts", "cryptonews"]

    return publish_youtube_api(title, description, video,
                               tags=tags, privacy="public",
                               made_for_kids=False)


# --- X publisher (tweepy API v2) -------------------------------------------

def publish_x(queue: dict[str, Any], dry_run: bool = True) -> dict[str, Any]:
    """Post tweet + image via Twitter/X.

    Priority order:
    1. X_COOKIES_JSON        — pasted session cookies (HTTP, no playwright, free)
    2. X_OAUTH_ACCESS_TOKEN  — OAuth 2.0 user-context
    3. MAKE_X_WEBHOOK_URL    — Make.com webhook (legacy)
    4. .x_browser_cookies    — on-disk Playwright cookies (needs playwright)
    5. X_API_KEY + secrets   — tweepy, requires X Basic plan ($100/mo)
    """
    text:       str  = queue["post_x"]
    image_path: Path = queue["image_x"]

    if dry_run:
        preview = _inject_trending_tags(text, tone=queue.get("_tone", "crypto"))
        return {"status": "dry_run", "text": preview, "image": str(image_path)}

    # Track what we tried, so the final error can explain.
    tried: list[str] = []
    failures: list[str] = []

    # ── Priority 1: Pasted cookies (FREE, no API plan — preferred) ────────
    if os.environ.get("X_COOKIES_JSON", "").strip():
        tried.append("cookies")
        result = publish_x_cookies(queue, dry_run=False)
        if result.get("status") == "ok":
            return result
        err = result.get("error", "unknown error")
        failures.append(f"Cookies: {err}")
        print(f"[publisher] cookies: {err}")

    # ── Priority 2: OAuth 2.0 user-context ────────────────────────────────
    if os.environ.get("X_OAUTH_ACCESS_TOKEN", "").strip():
        tried.append("OAuth 2.0")
        result = publish_x_oauth2(queue, dry_run=False)
        if result.get("status") == "ok":
            return result
        err = result.get("error", "unknown error")
        failures.append(f"OAuth: {err}")
        print(f"[publisher] oauth2: {err}")

    # ── Priority 3: Make.com webhook ────────────────────────────────────────
    makecom_url = os.environ.get("MAKE_X_WEBHOOK_URL", "").strip()
    if makecom_url:
        tried.append("make.com")
        result = _publish_x_makecom(text, image_path, makecom_url)
        if result["status"] == "ok":
            return result
        err = result.get("error", "unknown error")
        failures.append(f"Make.com: {err}")
        print(f"[publisher] make.com: {err}")

    # ── Priority 4: Browser automation with on-disk Playwright cookies ───────
    cookies_file = _HERE / ".x_browser_cookies.json"
    if cookies_file.exists() and os.environ.get("X_USERNAME"):
        tried.append("browser")
        result = publish_x_browser(queue, dry_run=False)
        if result["status"] == "ok":
            return result
        err = result.get("error", "unknown error")
        failures.append(f"Browser: {err}")
        print(f"[publisher] browser: {err}")

    # ── Priority 5: Tweepy (requires X Basic plan $100/mo) ──────────────────
    api_key    = os.environ.get("X_API_KEY", "").strip()
    api_secret = os.environ.get("X_API_SECRET", "").strip()
    acc_token  = os.environ.get("X_ACCESS_TOKEN", "").strip()
    acc_secret = os.environ.get("X_ACCESS_TOKEN_SECRET", "").strip()

    missing = [k for k, v in {
        "X_API_KEY":               api_key,
        "X_API_SECRET":            api_secret,
        "X_ACCESS_TOKEN":          acc_token,
        "X_ACCESS_TOKEN_SECRET":   acc_secret,
    }.items() if not v]
    if missing:
        tried_str  = (" (attempted: " + ", ".join(tried) + ")") if tried else ""
        fail_str   = (" — " + " | ".join(failures[:2])) if failures else ""
        return {
            "status": "error",
            "error": (
                "X nu este conectat. Recomandat (gratis): deschide "
                "Settings -> X / Twitter -> Paste cookies si urmeaza pasii. "
                "Dureaza ~30 de secunde si nu are nevoie de plan platit."
                + tried_str + fail_str
            ),
        }

    try:
        import tweepy
    except ImportError:
        return {"status": "error", "error": "tweepy not installed — run: pip install tweepy"}

    # ── Step 1: upload image via v1.1 ───────────────────────────────────────
    # Requires Basic plan ($100/mo) or Elevated legacy access.
    # Falls back to text-only if v1.1 is unavailable.
    media_id = None
    media_upload_note = ""
    if image_path and image_path.exists():
        try:
            auth   = tweepy.OAuth1UserHandler(api_key, api_secret, acc_token, acc_secret)
            api_v1 = tweepy.API(auth)
            media  = api_v1.media_upload(filename=str(image_path))
            media_id = media.media_id
        except Exception as exc:
            media_upload_note = f"image skipped ({exc})"
            print(f"[publisher] X image upload failed, posting text-only: {exc}")

    # ── Step 2: create tweet via v2 ─────────────────────────────────────────
    try:
        client = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=acc_token,
            access_token_secret=acc_secret,
        )
        kwargs: dict = {"text": text}   # copywriter already keeps it ≤ 280
        if media_id:
            kwargs["media_ids"] = [str(media_id)]

        resp = client.create_tweet(**kwargs)
        tweet_id = resp.data["id"]
        result: dict[str, Any] = {
            "status":   "ok",
            "tweet_id": tweet_id,
            "url":      f"https://x.com/i/web/status/{tweet_id}",
            "has_image": media_id is not None,
        }
        if media_upload_note:
            result["note"] = media_upload_note
        return result

    except tweepy.errors.Unauthorized as exc:
        return {
            "status": "error",
            "error": (
                "X 401 Unauthorized — keys are wrong or expired. "
                "Check API Key & Secret + Access Token & Secret in Settings. "
                "Make sure your X app has Read+Write permission, then regenerate "
                "the Access Token & Secret after changing permissions. "
                f"Detail: {exc}"
            ),
        }
    except tweepy.errors.Forbidden as exc:
        return {
            "status": "error",
            "error": (
                "X 403 Forbidden — your X developer account needs the Basic plan "
                "($100/month) to post tweets via API. Free tier is read-only. "
                "Or your Access Token was created before enabling Write permission — "
                "regenerate it at developer.x.com → Your App → Keys and Tokens. "
                f"Detail: {exc}"
            ),
        }
    except tweepy.errors.BadRequest as exc:
        return {
            "status": "error",
            "error": (
                f"X 400 Bad Request — the tweet was rejected. "
                f"Check that the post text is under 280 characters and contains no "
                f"duplicate content. Detail: {exc}"
            ),
        }
    except tweepy.errors.TweepyException as exc:
        return {"status": "error", "error": f"X API error: {exc}"}
    except Exception as exc:
        return {"status": "error", "error": f"X unexpected error: {exc}"}


# --- Orchestrator -----------------------------------------------------------

def publish(queue_dir: Path, platforms: set[str], dry_run: bool,
            variant: str | None = None) -> dict[str, Any]:
    queue = load_queue(queue_dir, variant_override=variant)

    # Non-blocking fact check on X post text
    if queue.get("post_x"):
        _fact_check_post(
            queue["post_x"],
            tickers=queue.get("_tickers") or [],
        )

    errors = validate(queue)
    if errors:
        return {
            "status":    "blocked",
            "errors":    errors,
            "queue_dir": str(queue_dir),
        }

    results: dict[str, Any] = {
        "queue_dir": str(queue_dir),
        "dry_run":   dry_run,
        "platforms": sorted(platforms),
        "variant":   queue["variant"],
        "x_file":    queue["x_file"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "results":   {},
    }

    # ── Dedup guard (ALL platforms) ─────────────────────────────────────────
    # If a previous invocation for this same queue dir already posted successfully,
    # skip that platform. Last line of defence against multiple scheduler instances
    # posting the same slot (race condition on _mark_started in scheduler.py).
    if not dry_run:
        prev_log = queue_dir / "publish_log.json"
        if prev_log.exists():
            try:
                prev = json.loads(prev_log.read_text(encoding="utf-8"))
                prev_results = prev.get("results", {})
                for plat in list(platforms):
                    if prev_results.get(plat, {}).get("status") == "ok":
                        results["results"][plat] = {
                            **prev_results[plat],
                            "skipped_duplicate": True,
                            "note": f"already posted to {plat} in a prior run of this slot",
                        }
                        platforms = platforms - {plat}
                        print(f"[publisher] dedup: skipping {plat} — already posted this slot")
            except Exception:
                pass

    if "telegram" in platforms:
        results["results"]["telegram"] = publish_telegram(queue, dry_run=dry_run)

    if "x" in platforms:
        results["results"]["x"] = publish_x(queue, dry_run=dry_run)

    if "instagram" in platforms:
        results["results"]["instagram"] = publish_instagram(queue_dir, queue, dry_run=dry_run)

    if "tiktok" in platforms:
        results["results"]["tiktok"] = publish_tiktok(queue_dir, queue, dry_run=dry_run)

    if "youtube" in platforms:
        results["results"]["youtube"] = publish_youtube(queue_dir, queue, dry_run=dry_run)

    if "facebook" in platforms:
        results["results"]["facebook"] = publish_facebook(queue_dir, queue, dry_run=dry_run)

    # Write a publish log next to the queue
    log_name = "publish_log.json" if not dry_run else "publish_log_dryrun.json"
    (queue_dir / log_name).write_text(
        json.dumps(results, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Telegram alert when any platform fails
    if not dry_run:
        _publish_failure_alert(queue_dir, results)

    return results


def _publish_failure_alert(queue_dir: Path, results: dict) -> None:
    """Send Telegram alert when a platform fails to post."""
    failed = {p: r for p, r in results.get("results", {}).items()
              if r.get("status") == "error"}
    if not failed:
        return
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = (os.environ.get("TELEGRAM_ALERT_CHAT", "").strip()
               or os.environ.get("TELEGRAM_CHAT_ID", "").strip())
    if not token or not chat_id:
        return
    slot_name = Path(queue_dir).name
    lines = [f"⚠️ AutoPost — postare eșuată\n\U0001f4cc Slot: {slot_name}"]
    for plat, res in failed.items():
        err = (res.get("error") or "eroare necunoscută")[:200]
        lines.append(f"❌ {plat.upper()}: {err}")
    ok_plats = [p for p, r in results.get("results", {}).items() if r.get("status") == "ok"]
    if ok_plats:
        lines.append(f"✅ Postat pe: {', '.join(ok_plats)}")
    try:
        import urllib.request as _ur, urllib.parse as _up
        text = "\n".join(lines)
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        data = _up.urlencode({"chat_id": chat_id, "text": text}).encode()
        _ur.urlopen(_ur.Request(url, data=data), timeout=8)
    except Exception:
        pass


# --- CLI --------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Publish a queue slot to Telegram + X.")
    ap.add_argument("queue_dir", help="path to queue/<date>_<slot>/")
    ap.add_argument("--publish", action="store_true",
                    help="actually send (default: dry-run)")
    ap.add_argument("--platforms", default="telegram,x",
                    help="comma-separated subset of {telegram,x,instagram,tiktok,youtube,facebook}")
    ap.add_argument("--variant", default=None, choices=["A", "B"],
                    help="force X variant (default: deterministic 50/50 by slot)")
    args = ap.parse_args(argv[1:])

    platforms = {p.strip().lower() for p in args.platforms.split(",") if p.strip()}
    unknown = platforms - {"telegram", "x", "instagram", "tiktok", "youtube", "facebook"}
    if unknown:
        print(f"[publisher] unknown platforms: {unknown}", file=sys.stderr)
        return 2

    dry_run = not args.publish
    result = publish(Path(args.queue_dir).resolve(),
                     platforms=platforms, dry_run=dry_run,
                     variant=args.variant)

    if result.get("status") == "blocked":
        print("[publisher] BLOCKED — safety rails tripped:")
        for e in result["errors"]:
            print(f"  - {e}")
        return 3

    mode = "DRY-RUN" if dry_run else "LIVE"
    print(f"[publisher] {mode} — platforms: {result['platforms']}  "
          f"variant: {result.get('variant')} ({result.get('x_file')})")
    for plat, res in result["results"].items():
        print(f"  [{plat}] {res.get('status')}"
              + (f" — {res.get('error')}" if res.get("status") == "error" else ""))
    return 0 if all(r.get("status") in ("ok", "dry_run", "skipped")
                    for r in result["results"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
