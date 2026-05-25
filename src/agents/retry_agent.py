"""retry_agent.py — Retries failed platform posts with exponential backoff.

Scans all queue directories for publish_log.json entries where status != "ok".
Retries eligible posts at 15 min → 30 min → 60 min intervals.
After 3 failed retries marks as permanently_failed and sends Telegram alert.

IMPORTANT: Each retry uses the credentials of the user who OWNS the queue slot
(extracted from queue/{user_id}/{slot_name}/ path). Never uses admin credentials
for another user's posts.

Called by scheduler every 15 minutes.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE  = Path(__file__).resolve().parent
_REPO  = _HERE.parent.parent

RETRY_DELAYS   = [15 * 60, 30 * 60, 60 * 60]   # seconds between retries
MAX_RETRIES    = 3
QUEUE_ROOT     = _REPO / "queue"
MAX_AGE_HOURS  = 12   # never retry slots older than this — crypto news stale after ~12h

# Credential env var keys that must be isolated per-user
_CRED_KEYS = [
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ALERT_CHAT",
    "X_COOKIES_JSON", "X_OAUTH_ACCESS_TOKEN", "X_OAUTH_REFRESH_TOKEN",
    "X_OAUTH_EXPIRES_AT", "X_USERNAME", "X_EMAIL", "X_PASSWORD",
    "X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET",
    "MAKE_X_WEBHOOK_URL",
    "IG_USER_ID", "IG_ACCESS_TOKEN", "IMGBB_API_KEY", "PUBLIC_BASE_URL",
    "TIKTOK_ACCESS_TOKEN", "TIKTOK_REFRESH_TOKEN",
    "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET",
    "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET",
    "YOUTUBE_REFRESH_TOKEN", "YOUTUBE_API_KEY",
    "ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY",
    "DATA_DIR", "AUTOPOST_USER_ID",
]


def _extract_user_id(log_path: Path) -> int | None:
    """Extract user_id from queue/{user_id}/{slot_name}/publish_log.json.

    Returns None for old flat-format slots (queue/{slot_name}/publish_log.json)
    which belong to the admin and should inherit admin's env credentials.
    """
    try:
        uid_str = log_path.parent.parent.name
        return int(uid_str)
    except (ValueError, IndexError):
        return None


@contextmanager
def _user_env(user_id: int | None):
    """Temporarily inject this user's credentials into os.environ.

    Saves and restores all credential keys around the yield so no leakage
    occurs between retries of different users.
    If user_id is None (admin flat slot) env is left as-is.
    """
    if user_id is None:
        yield
        return

    try:
        if str(_REPO) not in __import__("sys").path:
            __import__("sys").path.insert(0, str(_REPO))
        from src.dashboard.database import get_user_settings
        settings = get_user_settings(user_id)
    except Exception as exc:
        print(f"[retry] cannot load settings for user {user_id}: {exc}")
        yield
        return

    cred_map = {
        "TELEGRAM_BOT_TOKEN":    settings.get("telegram_token", ""),
        "TELEGRAM_CHAT_ID":      settings.get("telegram_channel", ""),
        "TELEGRAM_ALERT_CHAT":   settings.get("telegram_alert_chat", ""),
        "X_COOKIES_JSON":        settings.get("x_cookies_json", ""),
        "X_OAUTH_ACCESS_TOKEN":  settings.get("x_oauth_access_token", ""),
        "X_OAUTH_REFRESH_TOKEN": settings.get("x_oauth_refresh_token", ""),
        "X_OAUTH_EXPIRES_AT":    str(settings.get("x_oauth_expires_at", "") or ""),
        "X_USERNAME":            settings.get("x_username", ""),
        "X_EMAIL":               settings.get("x_email", ""),
        "X_PASSWORD":            settings.get("x_password", ""),
        "X_API_KEY":             settings.get("x_api_key", ""),
        "X_API_SECRET":          settings.get("x_api_secret", ""),
        "X_ACCESS_TOKEN":        settings.get("x_access_token", ""),
        "X_ACCESS_TOKEN_SECRET": settings.get("x_access_secret", ""),
        "MAKE_X_WEBHOOK_URL":    settings.get("make_x_webhook_url", ""),
        "IG_USER_ID":            settings.get("ig_user_id", ""),
        "IG_ACCESS_TOKEN":       settings.get("ig_access_token", ""),
        "IMGBB_API_KEY":         settings.get("imgbb_api_key", ""),
        "PUBLIC_BASE_URL":       settings.get("public_base_url", ""),
        "TIKTOK_ACCESS_TOKEN":   settings.get("tiktok_access_token", ""),
        "TIKTOK_REFRESH_TOKEN":  settings.get("tiktok_refresh_token", ""),
        "TIKTOK_CLIENT_KEY":     settings.get("tiktok_client_key", ""),
        "TIKTOK_CLIENT_SECRET":  settings.get("tiktok_client_secret", ""),
        "YOUTUBE_CLIENT_ID":     settings.get("yt_client_id", ""),
        "YOUTUBE_CLIENT_SECRET": settings.get("yt_client_secret", ""),
        "YOUTUBE_REFRESH_TOKEN": settings.get("yt_refresh_token", ""),
        "YOUTUBE_API_KEY":       settings.get("youtube_api_key", ""),
        "ANTHROPIC_API_KEY":     settings.get("anthropic_key", ""),
        "ELEVENLABS_API_KEY":    settings.get("elevenlabs_key", ""),
        "DATA_DIR":              str(QUEUE_ROOT / str(user_id)),
        "AUTOPOST_USER_ID":      str(user_id),
        "AUTOPOST_ISOLATED":     "1",
    }

    # Save current values for all credential keys
    saved = {k: os.environ.get(k) for k in _CRED_KEYS + ["AUTOPOST_ISOLATED"]}

    # Inject this user's credentials (empty string = clear the key)
    for k, v in cred_map.items():
        if v:
            os.environ[k] = str(v)
        else:
            os.environ.pop(k, None)

    try:
        yield
    finally:
        # Restore original env exactly
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _scan_failed(queue_root: Path = QUEUE_ROOT) -> list[dict[str, Any]]:
    """Return list of failed posts that need retry, with per-user context."""
    candidates = []
    now = time.time()
    max_age_secs = MAX_AGE_HOURS * 3600

    for log_path in sorted(queue_root.rglob("publish_log.json")):
        try:
            log = json.loads(log_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        slot_ts_str = log.get("timestamp", "")
        slot_age = max_age_secs + 1
        if slot_ts_str:
            try:
                slot_ts = datetime.fromisoformat(slot_ts_str)
                if slot_ts.tzinfo is None:
                    slot_ts = slot_ts.replace(tzinfo=timezone.utc)
                slot_age = now - slot_ts.timestamp()
            except Exception:
                pass

        results = log.get("results", {})
        slot_modified = False
        user_id = _extract_user_id(log_path)

        for platform, res in results.items():
            if not isinstance(res, dict):
                continue
            status = res.get("status", "")
            if status in ("ok", "dry_run") or res.get("skipped_duplicate") or res.get("permanently_failed"):
                continue

            if slot_age > max_age_secs:
                res["permanently_failed"] = True
                res["permanently_failed_reason"] = f"slot older than {MAX_AGE_HOURS}h — stale news"
                slot_modified = True
                continue

            candidates.append({
                "queue_dir": log_path.parent,
                "log_path":  log_path,
                "platform":  platform,
                "result":    res,
                "log":       log,
                "user_id":   user_id,   # None = admin flat slot
            })

        if slot_modified:
            try:
                log_path.write_text(
                    json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            except Exception:
                pass

    return candidates


def _is_eligible(result: dict[str, Any]) -> bool:
    retry_count = result.get("retry_count", 0)
    if retry_count >= MAX_RETRIES:
        return False
    last_attempt = result.get("last_retry_at") or result.get("timestamp")
    if not last_attempt:
        return True
    try:
        last_ts = datetime.fromisoformat(last_attempt).timestamp()
    except Exception:
        return True
    delay = RETRY_DELAYS[min(retry_count, len(RETRY_DELAYS) - 1)]
    return (time.time() - last_ts) >= delay


def _retry_platform(queue_dir: Path, platform: str) -> dict[str, Any]:
    """Re-run publisher for a single platform. Credentials must already be
    injected in os.environ by the caller (_user_env context manager)."""
    try:
        from src.agents.publisher import publish
    except ImportError:
        from agents.publisher import publish  # type: ignore
    result = publish(queue_dir, platforms={platform}, dry_run=False)
    return result.get("results", {}).get(platform, {"status": "error", "error": "no result"})


def run_retry_cycle(queue_root: Path = QUEUE_ROOT) -> list[dict[str, Any]]:
    """Scan for failed posts and retry eligible ones with per-user credentials."""
    pause_flag = _REPO / "autopost_paused.flag"
    if pause_flag.exists():
        print("[retry] autopost_paused.flag is set — retry blocked.")
        return []
    candidates = _scan_failed(queue_root)
    outcomes: list[dict[str, Any]] = []

    for item in candidates:
        result   = item["result"]
        platform = item["platform"]
        log_path = item["log_path"]
        user_id  = item["user_id"]

        if not _is_eligible(result):
            continue

        retry_count = result.get("retry_count", 0)
        uid_label   = f"user {user_id}" if user_id else "admin"
        print(f"[retry] attempt {retry_count + 1}/{MAX_RETRIES} — {uid_label} / {platform} @ {item['queue_dir'].name}")

        with _user_env(user_id):
            new_result = _retry_platform(item["queue_dir"], platform)

        new_result["retry_count"]   = retry_count + 1
        new_result["last_retry_at"] = datetime.now(timezone.utc).isoformat()

        if new_result.get("status") != "ok" and (retry_count + 1) >= MAX_RETRIES:
            new_result["permanently_failed"] = True
            _send_permanent_failure_alert(platform, item["queue_dir"].name, new_result.get("error", ""), user_id)

        try:
            log = json.loads(log_path.read_text(encoding="utf-8"))
            log["results"][platform] = new_result
            log_path.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            print(f"[retry] could not update log: {exc}")

        outcomes.append({"platform": platform, "slot": item["queue_dir"].name, "user_id": user_id, "result": new_result})
        print(f"[retry] {uid_label} / {platform} -> {new_result.get('status', '?')}")

    return outcomes


def _send_permanent_failure_alert(platform: str, slot: str, error: str, user_id: int | None = None) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = (os.environ.get("TELEGRAM_ALERT_CHAT", "").strip()
               or os.environ.get("TELEGRAM_CHAT_ID", "").strip())
    if not token or not chat_id:
        # Fallback: try admin credentials from DB
        try:
            if str(_REPO) not in __import__("sys").path:
                __import__("sys").path.insert(0, str(_REPO))
            from src.dashboard.database import get_all_users, get_user_settings
            admins = [u for u in get_all_users() if u.get("is_admin") and u.get("is_approved")]
            if admins:
                s = get_user_settings(admins[0]["id"])
                token   = s.get("telegram_token", "").strip()
                chat_id = (s.get("telegram_alert_chat", "") or s.get("telegram_channel", "")).strip()
        except Exception:
            pass
    if not token or not chat_id:
        return

    uid_label = f"User {user_id}" if user_id else "Admin"
    text = (f"[AutoPost] Post DEFINITIV ESUAT\n"
            f"{uid_label} / Platform: {platform.upper()}\nSlot: {slot}\n"
            f"Eroare: {error[:200]}\nAu fost 3 reincercari fara succes.")
    try:
        import urllib.request, urllib.parse
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=8)
    except Exception:
        pass


if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.path.insert(0, str(_REPO))
    outcomes = run_retry_cycle()
    if outcomes:
        for o in outcomes:
            uid = f"user {o['user_id']}" if o.get('user_id') else "admin"
            print(f"  {o['slot']} / {uid} / {o['platform']} -> {o['result'].get('status')}")
    else:
        print("[retry] Nimic de reincercat.")
