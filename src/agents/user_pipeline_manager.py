"""user_pipeline_manager.py — Runs the full pipeline for every approved user.

Each user gets an isolated subprocess with only THEIR credentials in env.
Platforms are selected per-user: only platforms where the user has valid,
configured credentials are attempted. No sharing of accounts between users.

Queue layout:  queue/{user_id}/{date_slot}/
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

_HERE  = Path(__file__).resolve().parent
_REPO  = _HERE.parent.parent

MAX_PARALLEL_USERS = 8


# ---------------------------------------------------------------------------
# Determine which platforms a user actually has credentials for
# ---------------------------------------------------------------------------

def _get_user_platforms(settings: dict[str, Any]) -> set[str]:
    """Return only the platforms where this user has configured valid credentials."""
    result: set[str] = set()

    # Telegram: token must look like  123456789:ABC-...
    tg_token = settings.get("telegram_token", "")
    tg_chat  = settings.get("telegram_channel", "")
    if tg_token and re.match(r"^\d+:[\w-]{10,}", tg_token) and tg_chat:
        result.add("telegram")

    # X: cookies JSON  OR  OAuth 2.0  OR  API key pair  OR  Make webhook
    if (settings.get("x_cookies_json")
            or settings.get("x_oauth_access_token")
            or (settings.get("x_api_key") and settings.get("x_access_token"))
            or settings.get("make_x_webhook_url")):
        result.add("x")

    # Instagram
    if settings.get("ig_user_id") and settings.get("ig_access_token"):
        result.add("instagram")

    # TikTok
    if settings.get("tiktok_access_token") and settings.get("tiktok_client_key"):
        result.add("tiktok")

    # YouTube
    if (settings.get("yt_refresh_token")
            and settings.get("yt_client_id")
            and settings.get("yt_client_secret")):
        result.add("youtube")

    # Facebook
    if settings.get("fb_access_token") and settings.get("fb_page_id"):
        result.add("facebook")

    return result


# ---------------------------------------------------------------------------
# Build a clean env for the subprocess — only this user's credentials
# ---------------------------------------------------------------------------

def _build_user_env(settings: dict[str, Any], user_id: int, queue_root: Path) -> dict[str, str]:
    """Build subprocess env from user settings.

    Every credential key is explicitly set (even to "" when empty) so that
    load_dotenv() inside the subprocess cannot pull in admin credentials
    from the .env file for keys this user hasn't configured.
    """
    env = dict(os.environ)   # inherit PATH, PYTHONPATH, CLAUDE_MODEL, etc.

    # Mark as isolated subprocess — _pipeline_runner skips load_dotenv
    env["AUTOPOST_ISOLATED"] = "1"

    credential_map = {
        "ANTHROPIC_API_KEY":     settings.get("anthropic_key", ""),
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
        "YOUTUBE_CLIENT_ID":     settings.get("yt_client_id", "") or settings.get("youtube_client_id", ""),
        "YOUTUBE_CLIENT_SECRET": settings.get("yt_client_secret", "") or settings.get("youtube_client_secret", ""),
        "YOUTUBE_REFRESH_TOKEN": settings.get("yt_refresh_token", "") or settings.get("youtube_refresh_token", ""),
        "YOUTUBE_API_KEY":       settings.get("youtube_api_key", ""),
        "FB_ACCESS_TOKEN":       settings.get("fb_access_token", ""),
        "FB_PAGE_ID":            settings.get("fb_page_id", ""),
        "ELEVENLABS_API_KEY":    settings.get("elevenlabs_key", ""),
        "AUTOPOST_QUEUE":        str(queue_root / str(user_id)),
        "DATA_DIR":              str(queue_root / str(user_id)),
        "AUTOPOST_USER_ID":      str(user_id),
    }
    # Always write every key — empty string blocks .env inheritance
    for k, v in credential_map.items():
        env[k] = str(v) if v else ""

    return env


# ---------------------------------------------------------------------------
# Subprocess launcher
# ---------------------------------------------------------------------------

def _run_pipeline_subprocess(
    user_id: int,
    email: str,
    env: dict[str, str],
    slot: str,
    hours_back: int,
    publish_live: bool,
    platforms: set[str],
    published_after_iso: str | None,
) -> dict[str, Any]:
    script = str(_REPO / "src" / "agents" / "_pipeline_runner.py")
    args = {
        "slot":                slot,
        "hours_back":          hours_back,
        "publish_live":        publish_live,
        "platforms":           list(platforms),
        "published_after_iso": published_after_iso,
    }
    cmd = [sys.executable, script, json.dumps(args)]
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, env=env, cwd=str(_REPO),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
        )
        elapsed = time.time() - t0
        if proc.returncode == 0:
            print(f"[upm] user {user_id} ({email}) — OK in {elapsed:.0f}s  platforms={sorted(platforms)}")
            return {"status": "ok", "user_id": user_id, "elapsed": elapsed}
        else:
            err = (proc.stderr or proc.stdout or "")[-800:]
            print(f"[upm] user {user_id} ({email}) — FAILED: {err}")
            return {"status": "error", "user_id": user_id, "error": err, "elapsed": elapsed}
    except subprocess.TimeoutExpired:
        return {"status": "error", "user_id": user_id, "error": "timeout after 600s"}
    except Exception as exc:
        return {"status": "error", "user_id": user_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# Single-user runner (used by per-user scheduler)
# ---------------------------------------------------------------------------

def run_single_user(
    user_id: int,
    slot: str,
    hours_back: int,
    publish_live: bool,
    queue_root: Path,
    published_after_iso: str | None = None,
) -> dict[str, Any]:
    """Run pipeline for one specific user at their configured time."""
    try:
        if str(_REPO) not in sys.path:
            sys.path.insert(0, str(_REPO))
        from src.dashboard.database import get_user_by_id, get_user_settings
    except Exception as exc:
        return {"status": "error", "user_id": user_id, "error": f"DB import failed: {exc}"}

    user = get_user_by_id(user_id)
    if not user or not user.get("is_approved"):
        return {"status": "skipped", "user_id": user_id, "error": "user not found or not approved"}

    email    = user.get("email", f"user_{user_id}")
    settings = get_user_settings(user_id)

    # All platforms this user has configured
    all_platforms = {"telegram", "x", "instagram", "tiktok", "youtube", "facebook"}
    user_plats    = _get_user_platforms(settings) & all_platforms

    if not user_plats:
        print(f"[upm] user {user_id} ({email}) — skipped: no valid credentials")
        return {"status": "skipped", "user_id": user_id, "error": "no valid credentials"}

    print(f"[upm] user {user_id} ({email}) — slot={slot} platforms={sorted(user_plats)}")
    env = _build_user_env(settings, user_id, queue_root)
    return _run_pipeline_subprocess(
        user_id, email, env, slot, hours_back, publish_live,
        user_plats, published_after_iso,
    )


# ---------------------------------------------------------------------------
# All-users runner (used by manual triggers / admin)
# ---------------------------------------------------------------------------

def run_all_users(
    slot: str,
    hours_back: int,
    publish_live: bool,
    platforms: set[str],          # global enabled set from scheduler
    queue_root: Path,
    published_after_iso: str | None = None,
) -> list[dict[str, Any]]:
    """Run pipeline for every approved user, each with their own credentials and platforms."""
    try:
        if str(_REPO) not in sys.path:
            sys.path.insert(0, str(_REPO))
        from src.dashboard.database import get_all_users, get_user_settings
    except Exception as exc:
        print(f"[upm] DB import failed: {exc}")
        return []

    users = [u for u in get_all_users() if u.get("is_approved")]
    if not users:
        print("[upm] no approved users found")
        return []

    print(f"[upm] slot '{slot}' — {len(users)} approved user(s), parallel={MAX_PARALLEL_USERS}")
    results: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_USERS) as pool:
        futures: dict[Any, dict] = {}

        for user in users:
            uid      = user["id"]
            email    = user.get("email", f"user_{uid}")
            settings = get_user_settings(uid)

            # Platforms this user can actually post to
            user_plats = _get_user_platforms(settings) & platforms

            if not user_plats:
                print(f"[upm] user {uid} ({email}) — skipped: no valid credentials "
                      f"for any enabled platform {sorted(platforms)}")
                continue

            print(f"[upm] user {uid} ({email}) — platforms: {sorted(user_plats)}")
            env = _build_user_env(settings, uid, queue_root)

            future = pool.submit(
                _run_pipeline_subprocess,
                uid, email, env, slot, hours_back, publish_live,
                user_plats, published_after_iso,
            )
            futures[future] = {"user_id": uid, "email": email}

        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                info = futures[future]
                results.append({"status": "error", "user_id": info["user_id"], "error": str(exc)})

    ok    = sum(1 for r in results if r.get("status") == "ok")
    error = sum(1 for r in results if r.get("status") != "ok")
    print(f"[upm] done — {ok} ok, {error} errors out of {len(results)} users")
    return results


# ---------------------------------------------------------------------------
# Deferred platform publisher — posts already-generated content at peak time
# ---------------------------------------------------------------------------

def publish_deferred_platform(
    user_id: int,
    platform: str,
    queue_root: Path,
    publish_live: bool = True,
) -> dict[str, Any]:
    """Publish today's already-generated content to a single platform.

    Used for peak-time posting: generate once in the morning, then post
    to TikTok at 21:00 and YouTube at 15:00 from pre-built queue content.
    No full pipeline run — just publisher.py on the existing queue dir.

    Looks for today's latest slot dir for this user (morning → evening priority).
    """
    from datetime import date as _date
    try:
        if str(_REPO) not in sys.path:
            sys.path.insert(0, str(_REPO))
        from src.dashboard.database import get_user_by_id, get_user_settings
    except Exception as exc:
        return {"status": "error", "error": f"DB import failed: {exc}"}

    user = get_user_by_id(user_id)
    if not user or not user.get("is_approved"):
        return {"status": "skipped", "error": "user not found or not approved"}

    settings   = get_user_settings(user_id)
    email      = user.get("email", f"user_{user_id}")
    user_root  = queue_root / str(user_id)
    today      = _date.today().isoformat()

    # Find today's queue dir — prefer morning, then evening
    queue_dir = None
    for slot in ("morning", "evening"):
        candidate = user_root / f"{today}_{slot}"
        # Must have top2.json (content generated) AND at least one image
        if (candidate.exists()
                and (candidate / "top2.json").exists()
                and list(candidate.glob("*.png"))):
            queue_dir = candidate
            break

    if not queue_dir:
        return {"status": "skipped",
                "error": f"no generated content found for user {user_id} today ({today})"}

    # Check this platform hasn't already been posted successfully today
    log_path = queue_dir / "publish_log.json"
    if log_path.exists():
        try:
            prev = json.loads(log_path.read_text(encoding="utf-8"))
            if prev.get("results", {}).get(platform, {}).get("status") == "ok":
                return {"status": "skipped",
                        "error": f"{platform} already posted for this slot"}
        except Exception:
            pass

    # Make sure the user has credentials for this platform
    user_plats = _get_user_platforms(settings)
    if platform not in user_plats:
        return {"status": "skipped",
                "error": f"user {user_id} has no credentials for {platform}"}

    env = _build_user_env(settings, user_id, queue_root)

    # Run publisher-only via subprocess (honours isolation + env injection)
    script = str(_REPO / "src" / "agents" / "_pipeline_runner.py")
    args = {
        "slot":                queue_dir.name.split("_", 1)[1] if "_" in queue_dir.name else "morning",
        "hours_back":          0,        # no scouting
        "publish_live":        publish_live,
        "platforms":           [platform],
        "published_after_iso": None,
        "stop_after":          "publish",
        "skip_generation":     True,     # flag for _pipeline_runner to skip to publish
    }
    try:
        proc = subprocess.run(
            [sys.executable, script, json.dumps(args)],
            env=env, cwd=str(_REPO),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        if proc.returncode == 0:
            print(f"[upm] deferred {platform} -> user {user_id} ({email}) OK")
            return {"status": "ok", "user_id": user_id, "platform": platform,
                    "queue_dir": str(queue_dir)}
        err = (proc.stderr or proc.stdout or "")[-600:]
        print(f"[upm] deferred {platform} -> user {user_id} FAILED: {err}")
        return {"status": "error", "user_id": user_id, "platform": platform, "error": err}
    except Exception as exc:
        return {"status": "error", "user_id": user_id, "platform": platform, "error": str(exc)}
