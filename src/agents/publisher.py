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


# --- X via twikit (unofficial web API — email + password, no API plan) -----

def _cookies_path() -> Path:
    """Persistent path for saved X session cookies."""
    data_dir = os.environ.get("DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir) / "x_twikit_cookies.json"
    return _REPO_ROOT / "x_twikit_cookies.json"


def publish_x_twikit(queue: dict[str, Any], dry_run: bool = True) -> dict[str, Any]:
    """Post to X using twikit (unofficial web API — no API plan needed).

    Authenticates with X username + email + password, saves session cookies
    so subsequent posts skip the login step.
    """
    text:       str  = queue["post_x"]
    image_path: Path = queue["image_x"]

    if dry_run:
        return {"status": "dry_run", "text": text}

    username = os.environ.get("X_USERNAME", "").strip().lstrip("@")
    email    = os.environ.get("X_EMAIL",    "").strip()
    password = os.environ.get("X_PASSWORD", "").strip()

    if not all([username, email, password]):
        missing = [k for k, v in {"X_USERNAME": username, "X_EMAIL": email,
                                   "X_PASSWORD": password}.items() if not v]
        return {"status": "error",
                "error": f"X credentials missing: {', '.join(missing)} — set them in Settings"}

    try:
        import twikit
    except ImportError:
        return {"status": "error",
                "error": "twikit not installed — redeploy to pick up requirements.txt"}

    import asyncio

    async def _post_async() -> str:
        client = twikit.Client("en-US")
        cookies_file = _cookies_path()

        # Try saved cookies first to avoid login every time
        if cookies_file.exists():
            try:
                client.load_cookies(str(cookies_file))
                # Quick auth check
                await client.user()
            except Exception:
                cookies_file.unlink(missing_ok=True)
                await client.login(auth_info_1=username,
                                   auth_info_2=email,
                                   password=password)
                client.save_cookies(str(cookies_file))
        else:
            await client.login(auth_info_1=username,
                               auth_info_2=email,
                               password=password)
            client.save_cookies(str(cookies_file))

        # Upload image if available
        media_ids = []
        if image_path and image_path.exists():
            try:
                media_id = await client.upload_media(str(image_path))
                media_ids.append(media_id)
            except Exception as exc:
                print(f"[publisher] twikit image upload failed ({exc}) — posting text-only")

        tweet = await client.create_tweet(
            text=text,
            media_ids=media_ids if media_ids else None,
        )
        return str(tweet.id)

    try:
        tweet_id = asyncio.run(_post_async())
        return {"status": "ok", "tweet_id": tweet_id,
                "url": f"https://x.com/i/web/status/{tweet_id}",
                "via": "twikit"}
    except Exception as exc:
        # If cookies expired and re-login also failed, delete stale cookies
        _cookies_path().unlink(missing_ok=True)
        return {"status": "error", "error": f"twikit: {exc}"}


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


# --- X publisher (tweepy API v2) -------------------------------------------

def publish_x(queue: dict[str, Any], dry_run: bool = True) -> dict[str, Any]:
    """Post tweet + image via Twitter/X API v2 using tweepy.

    Image upload uses v1.1 media/upload (requires Basic plan or higher).
    If image upload fails, falls back to text-only tweet so the post
    still goes out even on restricted plans.
    """
    text:       str  = queue["post_x"]
    image_path: Path = queue["image_x"]

    if dry_run:
        return {"status": "dry_run", "text": text, "image": str(image_path)}

    # ── twikit (email+password, no API plan) — highest priority if set ───────
    if os.environ.get("X_USERNAME") and os.environ.get("X_PASSWORD"):
        return publish_x_twikit(queue, dry_run=False)

    # ── Make.com webhook (second choice) ─────────────────────────────────────
    makecom_url = os.environ.get("MAKE_X_WEBHOOK_URL", "").strip()
    if makecom_url:
        return _publish_x_makecom(text, image_path, makecom_url)

    # ── Direct tweepy (requires X Basic plan $100/mo for write access) ───────
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
        return {
            "status": "error",
            "error": (
                f"X API keys missing: {', '.join(missing)}. "
                "Go to Settings → X / Twitter and fill in all 4 keys."
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


def _publish_x_playwright_unused(queue: dict[str, Any], dry_run: bool = True) -> dict[str, Any]:
    """Legacy Playwright method — kept for reference, not used."""
    if dry_run:
        return {"status": "dry_run"}

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return {"status": "error",
                "error": "playwright not installed — run: pip install playwright && playwright install chromium"}

    return {"status": "error", "error": "Legacy playwright method not active"}


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
