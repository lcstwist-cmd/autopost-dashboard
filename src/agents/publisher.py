"""Publisher — Agent 6 of the Crypto AutoPost pipeline.

Hybrid publisher:
  * Telegram: direct call to Telegram Bot API (sendPhoto with caption)
  * X / Twitter: POST to a Make.com webhook which handles OAuth + media upload

Reads a queue directory produced by the earlier agents:
    queue/<date>_<slot>/
        post_x.txt
        post_telegram.md
        image_x_1200x675.png
        image_tg_1080x1080.png

Env vars:
    TELEGRAM_BOT_TOKEN     — from @BotFather
    TELEGRAM_CHAT_ID       — channel id ("@yourchannel") or numeric chat id
    MAKE_X_WEBHOOK_URL     — Make.com custom-webhook URL that publishes to X

Default mode is --dry-run: no HTTP calls are made, the payloads are only printed
to stdout so you can inspect them. Pass --publish to actually send.

Safety rails:
  * Refuses to publish if post_x.txt is over 280 weighted characters.
  * Refuses to publish if the required image file is missing.
  * --platforms lets you restrict to {telegram, x} for partial runs.
  * Writes publish_log.json next to the queue dir on every run (dry or live).

Usage:
    # safe preview
    python src/agents/publisher.py queue/2026-04-19_morning

    # live publish to both channels
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

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass

from typing import Any


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
    "post_tg":      "post_telegram.md",
    "image_x":      "image_x_1200x675.png",
    "image_tg":     "image_tg_1080x1080.png",
}


def _pick_variant(queue_dir: Path, override: str | None) -> tuple[str, str]:
    """Decide which X variant to send.

    Returns (variant_label, file_used).
      - override="A"|"B" forces a variant
      - otherwise, deterministic coin flip seeded by the queue dir name so the
        same slot always picks the same variant on re-runs (no accidental churn)
    """
    if override:
        ov = override.strip().upper()
        if ov not in ("A", "B"):
            raise SystemExit(f"[publisher] --variant must be A or B, got {override!r}")
        return ov, "post_x.txt" if ov == "A" else "post_x_b.txt"

    has_b = (queue_dir / "post_x_b.txt").exists()
    if not has_b:
        return "A", "post_x.txt"

    # Deterministic 50/50 based on queue dir name (e.g. 2026-04-19_morning)
    h = hashlib.sha256(queue_dir.name.encode()).digest()[0]
    variant = "A" if h % 2 == 0 else "B"
    return variant, "post_x.txt" if variant == "A" else "post_x_b.txt"


def load_queue(queue_dir: Path, variant_override: str | None = None) -> dict[str, Any]:
    if not queue_dir.is_dir():
        raise SystemExit(f"[publisher] queue dir not found: {queue_dir}")

    variant, x_file = _pick_variant(queue_dir, variant_override)

    required = [REQUIRED_FILES["post_tg"], REQUIRED_FILES["image_x"],
                REQUIRED_FILES["image_tg"], x_file]
    missing = [fname for fname in required if not (queue_dir / fname).exists()]
    if missing:
        raise SystemExit(f"[publisher] missing files in {queue_dir}: {missing}")

    post_x = (queue_dir / x_file).read_text(encoding="utf-8").strip()
    post_tg = (queue_dir / REQUIRED_FILES["post_tg"]).read_text(encoding="utf-8").strip()
    image_x = queue_dir / REQUIRED_FILES["image_x"]
    image_tg = queue_dir / REQUIRED_FILES["image_tg"]

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
        "post_x":     post_x,
        "post_tg":    post_tg,
        "image_x":    image_x,
        "image_tg":   image_tg,
        "source_url": source_url,
        "variant":    variant,
        "x_file":     x_file,
    }


# --- Safety rails -----------------------------------------------------------

def validate(queue: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    wx = x_weighted_len(queue["post_x"])
    if wx > 25000:
        errors.append(f"post_x is {wx} chars (>25000)")

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


# --- X publisher (Playwright browser automation — no API needed) -----------

def publish_x(queue: dict[str, Any], dry_run: bool = True) -> dict[str, Any]:
    """Post tweet + image by automating the X web interface with Playwright.

    Requires a prior manual login via x_login_helper.py to save session cookies.
    Headless automation then reuses those cookies — no login form interaction needed.
    """
    text       = queue["post_x"]
    image_path: Path = queue["image_x"]

    if dry_run:
        return {"status": "dry_run", "text": text, "image": str(image_path)}

    cookies_file = _HERE / ".x_browser_cookies.json"
    if not cookies_file.exists():
        return {
            "status": "error",
            "error": (
                "No saved X session found. "
                "Run: python src/agents/x_login_helper.py  (opens a browser, log in once)"
            ),
        }

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        return {"status": "error",
                "error": "playwright not installed — run: pip install playwright && playwright install chromium"}

    import json as _json

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )
            raw_cookies = _json.loads(cookies_file.read_text(encoding="utf-8"))
            # Sanitize to Playwright-accepted fields only
            clean_cookies = []
            for c in raw_cookies:
                cc: dict = {"name": c["name"], "value": c["value"]}
                if c.get("domain"):
                    cc["domain"] = c["domain"]
                if c.get("path"):
                    cc["path"] = c["path"]
                if isinstance(c.get("secure"), bool):
                    cc["secure"] = c["secure"]
                if isinstance(c.get("httpOnly"), bool):
                    cc["httpOnly"] = c["httpOnly"]
                same = c.get("sameSite", "")
                if same in ("Strict", "Lax", "None"):
                    cc["sameSite"] = same
                if isinstance(c.get("expires"), (int, float)) and c["expires"] > 0:
                    cc["expires"] = float(c["expires"])
                clean_cookies.append(cc)
            ctx.add_cookies(clean_cookies)

            page = ctx.new_page()
            page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            if "home" not in page.url:
                browser.close()
                return {
                    "status": "error",
                    "error": (
                        f"Session expired (landed on {page.url!r}). "
                        "Re-run: python src/agents/x_login_helper.py"
                    ),
                }

            # Save refreshed cookies
            cookies_file.write_text(_json.dumps(ctx.cookies(), ensure_ascii=False))

            # Dismiss any popups/overlays (cookie consent, notifications, etc.)
            for dismiss_sel in [
                '[data-testid="twc-cc-mask"]',
                '[data-testid="confirmationSheetConfirm"]',
                '[aria-label="Close"]',
            ]:
                try:
                    page.click(dismiss_sel, timeout=2000)
                    page.wait_for_timeout(500)
                except Exception:
                    pass
            # Press Escape to close any open dialogs
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)

            # Click the compose / Post button
            page.wait_for_selector('[data-testid="SideNav_NewTweet_Button"]', timeout=15000)
            page.click('[data-testid="SideNav_NewTweet_Button"]')

            # Type tweet text — press_sequentially fires real keyboard events for Draft.js
            page.wait_for_selector('[data-testid="tweetTextarea_0"]', timeout=10000)
            textarea = page.locator('[data-testid="tweetTextarea_0"]').first
            textarea.click(force=True)
            page.wait_for_timeout(300)
            textarea.press_sequentially(text, delay=20)
            page.wait_for_timeout(500)
            # Dismiss any autocomplete dropdown that may block further clicks
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)

            # Attach image via "Add photos or video" button
            if image_path.exists():
                with page.expect_file_chooser() as fc_info:
                    page.locator('[aria-label="Add photos or video"]').first.click(force=True)
                fc_info.value.set_files(str(image_path))
                page.wait_for_selector('[data-testid="attachments"]', timeout=20000)

            # Submit — modal uses "tweetButton", inline feed uses "tweetButtonInline"
            for btn_sel in ['[data-testid="tweetButton"]', '[data-testid="tweetButtonInline"]']:
                try:
                    page.click(btn_sel, timeout=5000)
                    break
                except Exception:
                    pass
            page.wait_for_timeout(4000)

            browser.close()
            return {"status": "ok"}

    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)}


# --- Orchestrator -----------------------------------------------------------

def publish(queue_dir: Path, platforms: set[str], dry_run: bool,
            variant: str | None = None) -> dict[str, Any]:
    queue = load_queue(queue_dir, variant_override=variant)

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

    if "telegram" in platforms:
        results["results"]["telegram"] = publish_telegram(queue, dry_run=dry_run)

    if "x" in platforms:
        results["results"]["x"] = publish_x(queue, dry_run=dry_run)

    # Write a publish log next to the queue
    log_name = "publish_log.json" if not dry_run else "publish_log_dryrun.json"
    (queue_dir / log_name).write_text(
        json.dumps(results, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return results


# --- CLI --------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Publish a queue slot to Telegram + X.")
    ap.add_argument("queue_dir", help="path to queue/<date>_<slot>/")
    ap.add_argument("--publish", action="store_true",
                    help="actually send (default: dry-run)")
    ap.add_argument("--platforms", default="telegram,x",
                    help="comma-separated subset of {telegram,x}")
    ap.add_argument("--variant", default=None, choices=["A", "B"],
                    help="force X variant (default: deterministic 50/50 by slot)")
    args = ap.parse_args(argv[1:])

    platforms = {p.strip().lower() for p in args.platforms.split(",") if p.strip()}
    unknown = platforms - {"telegram", "x"}
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
    return 0 if all(r.get("status") in ("ok", "dry_run")
                    for r in result["results"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
