"""Scheduler — runs the full AutoPost pipeline twice per day.

Morning slot: 08:00 GMT+2  — covers news published BEFORE 08:00 GMT+2 (overnight)
Evening slot: 18:00 GMT+2  — covers news published AFTER 08:00 GMT+2 up to 18:00 GMT+2

Usage:
    # Run scheduler (stays running, triggers at 08:00 and 18:00 every day)
    python src/agents/scheduler.py

    # Dry-run mode (no actual publishing, useful for testing)
    python src/agents/scheduler.py --dry-run

    # Run a single slot right now (skip scheduler, useful for manual triggers)
    python src/agents/scheduler.py --now morning
    python src/agents/scheduler.py --now evening

    # Run both slots now
    python src/agents/scheduler.py --now both

Requires:
    pip install schedule
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Force UTF-8 on stdout/stderr so log lines with unicode don't crash
# on Windows cp1252 terminals.
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Load .env into os.environ before anything else reads it
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass  # python-dotenv not installed — rely on shell environment

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("scheduler")


def _attach_file_handler() -> None:
    """Attach a file handler to the root logger, tolerant of Windows file locks.

    If `scheduler.log` is locked by a still-running scheduler instance, fall back
    to a PID-suffixed file so this process can keep logging instead of crashing.
    """
    primary = _REPO_ROOT / "scheduler.log"
    candidates = [
        primary,
        _REPO_ROOT / f"scheduler.{os.getpid()}.log",
        Path(os.environ.get("TEMP", ".")) / f"autopost_scheduler.{os.getpid()}.log",
    ]
    for path in candidates:
        try:
            fh = logging.FileHandler(path, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            logging.getLogger().addHandler(fh)
            if path != primary:
                log.warning("scheduler.log was locked; logging to %s instead", path)
            return
        except PermissionError as exc:
            # File is locked (another scheduler still holds it). Try next.
            sys.stderr.write(f"[scheduler] could not open {path}: {exc}\n")
        except Exception as exc:
            sys.stderr.write(f"[scheduler] log handler error for {path}: {exc}\n")
    sys.stderr.write("[scheduler] falling back to stderr-only logging\n")


_attach_file_handler()
LOG_FILE = _REPO_ROOT / "scheduler.log"
STATE_FILE = _REPO_ROOT / "scheduler_state.json"
HEARTBEAT_FILE = _REPO_ROOT / "scheduler_heartbeat.txt"
PAUSE_FILE = _REPO_ROOT / "autopost_paused.flag"


# ---------------------------------------------------------------------------
# State tracking — survives restarts so we can detect missed runs
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    """Read last-run timestamps from disk."""
    import json
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"could not parse scheduler_state.json: {e}")
        return {}


def _save_state(state: dict) -> None:
    import json
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"could not write scheduler_state.json: {e}")


def _mark_run(slot: str, success: bool) -> None:
    """Record that we just attempted a slot, with success flag."""
    state = _load_state()
    state[f"last_{slot}"] = datetime.now(timezone.utc).isoformat()
    state[f"last_{slot}_ok"] = bool(success)
    _save_state(state)


def _mark_started(slot: str) -> None:
    """Write 'started today' immediately at the top of run_slot() so any
    concurrent or watchdog-restarted scheduler instance skips catch-up."""
    state = _load_state()
    state[f"last_{slot}_started"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)


def _heartbeat() -> None:
    """Touch the heartbeat file so dashboard can detect a live scheduler."""
    try:
        HEARTBEAT_FILE.write_text(
            datetime.now(timezone.utc).isoformat(), encoding="utf-8"
        )
    except Exception:
        pass


def _acquire_slot_lock(slot: str) -> int | None:
    """Atomically acquire a per-slot lock using O_CREAT|O_EXCL.

    Returns the open file descriptor on success, or None if another process
    already holds the lock.  Stale locks (older than 2 h) are auto-removed so
    a crashed process never permanently blocks a slot.
    """
    lock_path = _REPO_ROOT / f"scheduler_{slot}.lock"
    if lock_path.exists():
        try:
            age = time.time() - lock_path.stat().st_mtime
            if age > 7200:
                lock_path.unlink(missing_ok=True)
                log.info(f"[slot-lock] removed stale lock for '{slot}' (age {age/60:.0f} min)")
        except Exception:
            pass
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode())
        except Exception:
            pass
        return fd
    except FileExistsError:
        return None


def _release_slot_lock(slot: str, fd: int) -> None:
    try:
        os.close(fd)
    except Exception:
        pass
    try:
        (_REPO_ROOT / f"scheduler_{slot}.lock").unlink(missing_ok=True)
    except Exception:
        pass


def _slot_already_ran_today(slot: str, user_id: int | None = None) -> bool:
    """True if this slot already ran today — checks global state or per-user state."""
    state = _load_state()
    tz    = timezone(timedelta(hours=TZ_OFFSET_HOURS))
    today = datetime.now(tz).date()

    # Per-user state lives under state["users"][str(user_id)]
    if user_id is not None:
        user_state = state.get("users", {}).get(str(user_id), {})
        keys_to_check = (f"last_{slot}_started", f"last_{slot}")
        for key in keys_to_check:
            raw = user_state.get(key)
            if not raw:
                continue
            try:
                last = datetime.fromisoformat(raw)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if last.astimezone(tz).date() == today:
                    return True
            except Exception:
                pass
        return False

    # Global (legacy) check
    for key in (f"last_{slot}_started", f"last_{slot}"):
        raw = state.get(key)
        if not raw:
            continue
        try:
            last = datetime.fromisoformat(raw)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if last.astimezone(tz).date() == today:
                return True
        except Exception:
            pass
    return False


def _mark_user_slot(user_id: int, slot: str, started: bool = False) -> None:
    """Record per-user slot run state."""
    state = _load_state()
    users = state.setdefault("users", {})
    entry = users.setdefault(str(user_id), {})
    key   = f"last_{slot}_started" if started else f"last_{slot}"
    entry[key] = datetime.now(timezone.utc).isoformat()
    _save_state(state)


def _check_per_user_slots(publish: bool = True) -> None:
    """Called every minute. Fires pipeline for each user whose slot time matches now.

    Handles 4 posting triggers per user per day:
      morning   (default 08:00) — full pipeline + Telegram + X + Instagram
      evening   (default 18:00) — full pipeline + Telegram + X + Instagram
      youtube   (default 15:00) — deferred: posts today's content to YouTube only
      tiktok    (default 21:00) — deferred: posts today's content to TikTok only
    """
    if PAUSE_FILE.exists():
        return
    try:
        if str(_REPO_ROOT) not in __import__("sys").path:
            __import__("sys").path.insert(0, str(_REPO_ROOT))
        from src.dashboard.database import get_all_users, get_user_settings
        from src.agents.user_pipeline_manager import run_single_user, publish_deferred_platform
    except Exception as exc:
        log.warning(f"[per-user] import failed: {exc}")
        return

    tz          = timezone(timedelta(hours=TZ_OFFSET_HOURS))
    now_local   = datetime.now(tz)
    current_hm  = f"{now_local.hour:02d}:{now_local.minute:02d}"
    queue_root  = Path(os.environ.get("AUTOPOST_QUEUE", str(_REPO_ROOT / "queue")))

    users = [u for u in get_all_users() if u.get("is_approved")]

    for user in users:
        uid      = user["id"]
        settings = get_user_settings(uid)
        morning  = (settings.get("post_time_morning") or "08:00").strip()
        evening  = (settings.get("post_time_evening") or "18:00").strip()
        yt_time  = (settings.get("post_time_youtube")  or "15:00").strip()
        tt_time  = (settings.get("post_time_tiktok")   or "21:00").strip()

        # ── Full pipeline slots (morning / evening) ───────────────────────
        for slot, slot_time in (("morning", morning), ("evening", evening)):
            if current_hm != slot_time:
                continue
            if _slot_already_ran_today(slot, user_id=uid):
                continue

            log.info(f"[per-user] user {uid} ({user.get('email')}) — slot={slot} at {slot_time}")
            _mark_user_slot(uid, slot, started=True)

            if slot == "morning":
                hours_back      = 12
                published_after = None
            else:
                hours_back      = 10
                published_after = _today_cutoff_utc(8)

            pub_after_iso = published_after.isoformat() if published_after else None

            try:
                _inject_admin_settings()
                result = run_single_user(
                    user_id=uid,
                    slot=slot,
                    hours_back=hours_back,
                    publish_live=publish,
                    queue_root=queue_root,
                    published_after_iso=pub_after_iso,
                )
                _mark_user_slot(uid, slot, started=False)
                log.info(f"[per-user] user {uid} slot={slot} -> {result.get('status')}")
            except Exception as exc:
                log.error(f"[per-user] user {uid} slot={slot} CRASHED: {exc}", exc_info=True)

        # ── Deferred YouTube slot (default 15:00) ─────────────────────────
        # Posts today's morning content to YouTube at peak afternoon time
        if current_hm == yt_time and yt_time not in (morning, evening):
            if not _slot_already_ran_today("youtube_deferred", user_id=uid):
                log.info(f"[per-user] user {uid} — YouTube deferred post at {yt_time}")
                _mark_user_slot(uid, "youtube_deferred", started=True)
                try:
                    result = publish_deferred_platform(
                        user_id=uid,
                        platform="youtube",
                        queue_root=queue_root,
                        publish_live=publish,
                    )
                    _mark_user_slot(uid, "youtube_deferred", started=False)
                    log.info(f"[per-user] user {uid} youtube_deferred -> {result.get('status')}")
                except Exception as exc:
                    log.error(f"[per-user] user {uid} youtube_deferred CRASHED: {exc}")

        # ── Deferred TikTok slot (default 21:00) ──────────────────────────
        # Posts today's content to TikTok at prime evening time
        if current_hm == tt_time and tt_time not in (morning, evening):
            if not _slot_already_ran_today("tiktok_deferred", user_id=uid):
                log.info(f"[per-user] user {uid} — TikTok deferred post at {tt_time}")
                _mark_user_slot(uid, "tiktok_deferred", started=True)
                try:
                    result = publish_deferred_platform(
                        user_id=uid,
                        platform="tiktok",
                        queue_root=queue_root,
                        publish_live=publish,
                    )
                    _mark_user_slot(uid, "tiktok_deferred", started=False)
                    log.info(f"[per-user] user {uid} tiktok_deferred -> {result.get('status')}")
                except Exception as exc:
                    log.error(f"[per-user] user {uid} tiktok_deferred CRASHED: {exc}")


def _check_scheduled_posts(publish: bool = True) -> None:
    """Every minute: run any pending scheduled posts whose time has arrived."""
    try:
        if str(_REPO_ROOT) not in __import__("sys").path:
            __import__("sys").path.insert(0, str(_REPO_ROOT))
        from src.dashboard.database import (get_due_scheduled_posts,
                                              update_scheduled_post_status,
                                              get_user_settings)
        from src.agents.publisher import publish as _pub
    except Exception as exc:
        log.warning(f"[sched_posts] import failed: {exc}")
        return

    due = get_due_scheduled_posts()
    for post in due:
        post_id  = post["id"]
        user_id  = post["user_id"]
        slot_nm  = post["slot_name"]
        plats    = set(post["platforms"])
        log.info(f"[sched_posts] running scheduled post id={post_id} user={user_id} slot={slot_nm} plats={plats}")
        update_scheduled_post_status(post_id, "running")
        try:
            queue_root = _REPO_ROOT / "queue" / str(user_id)
            slot_dir   = queue_root / slot_nm
            if not slot_dir.exists():
                log.warning(f"[sched_posts] slot not found: {slot_dir}")
                update_scheduled_post_status(post_id, "failed")
                continue
            settings = get_user_settings(user_id)
            # Build env and run publish
            try:
                from src.agents.user_pipeline_manager import _build_user_env
                import subprocess as _sp, json as _jj
                env = _build_user_env(settings, user_id, queue_root)
                args = _jj.dumps({
                    "slot": slot_nm, "hours_back": 0, "publish_live": publish,
                    "platforms": list(plats), "published_after_iso": None,
                    "skip_generation": True,
                })
                script = str(_REPO_ROOT / "src" / "agents" / "_pipeline_runner.py")
                # Override queue so publisher finds the right slot
                env["AUTOPOST_QUEUE"] = str(queue_root)
                proc = _sp.run(
                    [__import__("sys").executable, script, args],
                    env=env, cwd=str(_REPO_ROOT),
                    capture_output=True, text=True, timeout=120,
                )
                if proc.returncode == 0:
                    update_scheduled_post_status(post_id, "done")
                    log.info(f"[sched_posts] id={post_id} done")
                else:
                    update_scheduled_post_status(post_id, "failed")
                    log.warning(f"[sched_posts] id={post_id} failed: {proc.stderr[-200:]}")
            except Exception as exc:
                update_scheduled_post_status(post_id, "failed")
                log.warning(f"[sched_posts] id={post_id} exception: {exc}")
        except Exception as exc:
            update_scheduled_post_status(post_id, "failed")
            log.warning(f"[sched_posts] id={post_id} outer exception: {exc}")


def catch_up_missed_runs(publish: bool = True) -> list[str]:
    """At startup: run slots for users who missed their time (e.g. machine was off)."""
    try:
        if str(_REPO_ROOT) not in __import__("sys").path:
            __import__("sys").path.insert(0, str(_REPO_ROOT))
        from src.dashboard.database import get_all_users, get_user_settings
        from src.agents.user_pipeline_manager import run_single_user
    except Exception as exc:
        log.warning(f"[catch-up] import failed: {exc}")
        return []

    tz         = timezone(timedelta(hours=TZ_OFFSET_HOURS))
    now_local  = datetime.now(tz)
    queue_root = Path(os.environ.get("AUTOPOST_QUEUE", str(_REPO_ROOT / "queue")))
    caught_up: list[str] = []

    users = [u for u in get_all_users() if u.get("is_approved")]

    for user in users:
        uid      = user["id"]
        settings = get_user_settings(uid)
        morning  = (settings.get("post_time_morning") or "08:00").strip()
        evening  = (settings.get("post_time_evening") or "18:00").strip()

        for slot, slot_time in (("morning", morning), ("evening", evening)):
            s_h, s_m = int(slot_time.split(":")[0]), int(slot_time.split(":")[1])
            slot_mins = s_h * 60 + s_m
            now_mins  = now_local.hour * 60 + now_local.minute
            if now_mins < slot_mins:
                continue   # slot hasn't been due yet today
            if _slot_already_ran_today(slot):              # global run_slot ran today
                continue
            if _slot_already_ran_today(slot, user_id=uid): # per-user ran today
                continue

            log.warning(f"[catch-up] user {uid} missed {slot} at {slot_time} — running now")
            _mark_user_slot(uid, slot, started=True)
            _inject_admin_settings()

            if slot == "morning":
                hours_back, pub_after = 12, None
            else:
                hours_back = 10
                pub_after  = _today_cutoff_utc(8)

            try:
                result = run_single_user(
                    user_id=uid, slot=slot, hours_back=hours_back,
                    publish_live=publish, queue_root=queue_root,
                    published_after_iso=pub_after.isoformat() if pub_after else None,
                )
                _mark_user_slot(uid, slot, started=False)
                caught_up.append(f"user{uid}:{slot}")
                log.info(f"[catch-up] user {uid} {slot} -> {result.get('status')}")
            except Exception as exc:
                log.error(f"[catch-up] user {uid} {slot} failed: {exc}")


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

# Use system timezone offset (auto-handles DST: UTC+2 winter, UTC+3 summer in Romania)
# Override with AUTOPOST_TZ_OFFSET env var if needed (e.g. for Docker/UTC servers)
def _get_tz_offset() -> int:
    env_val = os.environ.get("AUTOPOST_TZ_OFFSET", "").strip()
    if env_val:
        return int(env_val)
    from datetime import datetime as _dt
    offset = _dt.now().astimezone().utcoffset()
    return int(offset.total_seconds() / 3600)

TZ_OFFSET_HOURS = _get_tz_offset()


def _today_cutoff_utc(hour_local: int) -> datetime:
    """Return today's `hour_local` in the configured timezone as a UTC datetime."""
    tz = timezone(timedelta(hours=TZ_OFFSET_HOURS))
    now_local = datetime.now(tz)
    cutoff_local = now_local.replace(hour=hour_local, minute=0, second=0, microsecond=0)
    return cutoff_local.astimezone(timezone.utc)


def _inject_admin_settings():
    """Load first admin user's API keys into os.environ if not already set."""
    try:
        import sys
        if str(_REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(_REPO_ROOT))
        from src.dashboard.database import get_all_users, get_user_settings
        admins = [u for u in get_all_users() if u.get("is_admin") and u.get("is_approved")]
        if not admins:
            return
        s = get_user_settings(admins[0]["id"])
        mapping = {
            "ANTHROPIC_API_KEY":     s.get("anthropic_key", ""),
            "TELEGRAM_BOT_TOKEN":    s.get("telegram_token", ""),
            "TELEGRAM_CHAT_ID":      s.get("telegram_channel", ""),
            "TELEGRAM_ALERT_CHAT":   s.get("telegram_alert_chat", "") or s.get("telegram_channel", ""),
            "X_API_KEY":             s.get("x_api_key", ""),
            "X_API_SECRET":          s.get("x_api_secret", ""),
            "X_ACCESS_TOKEN":        s.get("x_access_token", ""),
            "X_ACCESS_TOKEN_SECRET": s.get("x_access_secret", ""),
            "MAKE_X_WEBHOOK_URL":    s.get("make_x_webhook_url", ""),
            "X_USERNAME":            s.get("x_username", ""),
            "X_EMAIL":                s.get("x_email", ""),
            "X_PASSWORD":            s.get("x_password", ""),
            # X cookies path (priority #1 in publish_x — preferred, free)
            "X_COOKIES_JSON":        s.get("x_cookies_json", ""),
            # X OAuth 2.0 (priority #2)
            "X_OAUTH_ACCESS_TOKEN":  s.get("x_oauth_access_token", ""),
            "X_OAUTH_REFRESH_TOKEN": s.get("x_oauth_refresh_token", ""),
            "X_OAUTH_EXPIRES_AT":    str(s.get("x_oauth_expires_at", "") or ""),
            # Per-user cookie cache dir (used by browser cookie publisher)
            "DATA_DIR":              str((_REPO_ROOT / "queue" / str(admins[0]["id"])).resolve()),
            "ELEVENLABS_API_KEY":    s.get("elevenlabs_key", ""),
            # Instagram (native Graph API)
            "IG_USER_ID":            s.get("ig_user_id", ""),
            "IG_ACCESS_TOKEN":       s.get("ig_access_token", ""),
            "IMGBB_API_KEY":         s.get("imgbb_api_key", ""),
            "PUBLIC_BASE_URL":       s.get("public_base_url", ""),
            # TikTok (Content Posting API)
            "TIKTOK_ACCESS_TOKEN":   s.get("tiktok_access_token", ""),
            "TIKTOK_REFRESH_TOKEN":  s.get("tiktok_refresh_token", ""),
            "TIKTOK_CLIENT_KEY":     s.get("tiktok_client_key", ""),
            "TIKTOK_CLIENT_SECRET":  s.get("tiktok_client_secret", ""),
            # YouTube Data API v3
            "YOUTUBE_CLIENT_ID":     s.get("youtube_client_id", ""),
            "YOUTUBE_CLIENT_SECRET": s.get("youtube_client_secret", ""),
            "YOUTUBE_REFRESH_TOKEN": s.get("youtube_refresh_token", ""),
            "YOUTUBE_API_KEY":       s.get("youtube_api_key", ""),
        }
        for k, v in mapping.items():
            if v:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)   # clear key so it doesn't leak to subprocesses
        log.info(f"Loaded API keys from admin user: {admins[0]['email']}")
    except Exception as exc:
        log.warning(f"Could not load admin settings from DB: {exc}")


def _resolve_enabled_platforms() -> set[str]:
    """Build the set of platforms to attempt, based on credentials present in
    the environment (after _inject_admin_settings has run) plus any explicit
    `enabled_platforms` user setting (comma-separated).

    Each individual publisher will still skip gracefully if its creds are
    missing — this function is just an early prune so we don't even try
    platforms the user has clearly not connected.
    """
    # 1) Honor user opt-out list if present.
    try:
        import sys
        if str(_REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(_REPO_ROOT))
        from src.dashboard.database import get_all_users, get_user_settings
        admins = [u for u in get_all_users() if u.get("is_admin") and u.get("is_approved")]
        opt_in: set[str] | None = None
        if admins:
            s = get_user_settings(admins[0]["id"])
            raw = (s.get("enabled_platforms") or "").strip()
            if raw:
                opt_in = {p.strip().lower() for p in raw.split(",") if p.strip()}
    except Exception:
        opt_in = None

    # 2) Detect which platforms have creds.
    has_creds = {
        "telegram":  bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")),
        "x":         bool(
            os.environ.get("X_COOKIES_JSON")           # pasted cookies (priority 1 — preferred)
            or os.environ.get("X_OAUTH_ACCESS_TOKEN")  # OAuth 2.0 (priority 2)
            or (os.environ.get("X_API_KEY") and os.environ.get("X_ACCESS_TOKEN"))
            or os.environ.get("MAKE_X_WEBHOOK_URL")
            or os.environ.get("X_USERNAME")
        ),
        "instagram": bool(os.environ.get("IG_USER_ID") and os.environ.get("IG_ACCESS_TOKEN")),
        "tiktok":    bool(os.environ.get("TIKTOK_ACCESS_TOKEN")),
        "youtube":   bool(
            os.environ.get("YOUTUBE_REFRESH_TOKEN")
            and os.environ.get("YOUTUBE_CLIENT_ID")
            and os.environ.get("YOUTUBE_CLIENT_SECRET")
        ),
        "facebook":  bool(os.environ.get("FB_ACCESS_TOKEN") and os.environ.get("FB_PAGE_ID")),
    }

    enabled = {p for p, ok in has_creds.items() if ok}

    if opt_in is not None:
        enabled = enabled & opt_in

    if not enabled:
        # safety: never end up with an empty set; force at least Telegram
        # so the slot still has somewhere to go
        enabled = {"telegram"}

    log.info(f"Platforms enabled this run: {sorted(enabled)} "
             f"(creds present: {[p for p, ok in has_creds.items() if ok]})")
    return enabled


def run_viral_scout() -> bool:
    """Refresh viral blueprints (IG / TikTok / YouTube) for every admin user.

    Called daily at VIRAL_SCOUT_TIME (default 06:30 GMT+2 — well before the
    08:00 morning slot) so the copywriter and avatar_writer have a fresh
    blueprint to imitate when they run.
    """
    log.info("=" * 60)
    log.info("Starting viral scout (IG / TikTok / YouTube trending)")
    log.info("=" * 60)
    _inject_admin_settings()
    try:
        from src.agents.viral_scout import run_once as _viral_run_once
        n = _viral_run_once(force=False)
        log.info(f"Viral scout produced {n} blueprint(s)")
        return True
    except Exception as exc:
        log.error(f"Viral scout failed: {exc}", exc_info=True)
        return False


def run_slot(slot: str, publish: bool = True) -> bool:
    """Run the full pipeline for one slot. Returns True on success."""
    # Emergency stop — if the pause flag exists, refuse to run.
    if PAUSE_FILE.exists():
        log.warning(f"[PAUSED] autopost_paused.flag is set — slot '{slot}' blocked. Remove the flag to resume.")
        return False

    # Acquire atomic lock — prevents concurrent processes from double-running
    # the same slot when multiple scheduler instances are alive simultaneously.
    lock_fd = _acquire_slot_lock(slot)
    if lock_fd is None:
        log.warning(f"[slot-lock] slot '{slot}' is already running in another process — skipping")
        return False

    try:
        return _run_slot_locked(slot, publish=publish)
    finally:
        _release_slot_lock(slot, lock_fd)


def _tg_alert(message: str) -> None:
    """Send an alert to the admin alert channel (TELEGRAM_ALERT_CHAT).
    Falls back to TELEGRAM_CHAT_ID only if no separate alert channel is set.
    Silent no-op if credentials are missing.
    """
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = (os.environ.get("TELEGRAM_ALERT_CHAT", "").strip()
               or os.environ.get("TELEGRAM_CHAT_ID", "").strip())
    if not token or not chat_id:
        return
    try:
        text = f"[AutoPost Alert]\n{message}"
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=8)
    except Exception:
        pass  # Never crash the scheduler because of a notification failure


def _run_retry_cycle() -> None:
    """Retry failed platform posts (called every 15 min)."""
    try:
        from src.agents.retry_agent import run_retry_cycle
        outcomes = run_retry_cycle()
        if outcomes:
            log.info(f"[retry] {len(outcomes)} post(s) retried")
    except Exception as exc:
        log.warning(f"[retry] cycle failed (non-fatal): {exc}")


def _run_health_check() -> None:
    """Run platform health checks and send alerts if needed."""
    try:
        from src.agents.health_monitor import run_checks, is_stale
        if is_stale():
            log.info("[health] running platform checks...")
            report = run_checks()
            log.info(f"[health] overall={report['overall']} — "
                     + ", ".join(f"{p}:{r['status']}" for p, r in report["platforms"].items()))
    except Exception as exc:
        log.warning(f"[health] check failed (non-fatal): {exc}")


def _run_web_learning() -> None:
    """Daily internet research for all agent brains — runs at 07:00."""
    log.info("[web_learner] Starting daily web learning cycle...")
    try:
        from src.agents.web_learner import run_web_learning
        results = run_web_learning(force=False)
        total   = sum(v for v in results.values())
        agents_learned = [n for n, c in results.items() if c > 0]
        log.info(
            f"[web_learner] {total} total lessons — "
            f"agents updated: {', '.join(agents_learned) or 'none (all up to date)'}"
        )
    except Exception as exc:
        log.warning(f"[web_learner] cycle failed (non-fatal): {exc}")


def _run_competitor_tracker() -> None:
    """Daily competitor scan — runs at 09:00."""
    log.info("[competitor_tracker] Starting daily competitor scan...")
    try:
        from src.agents.competitor_tracker import run_all_users
        result = run_all_users(force=False)
        log.info(
            f"[competitor_tracker] scanned={result.get('total_scanned',0)} "
            f"errors={result.get('errors',0)}"
        )
    except Exception as exc:
        log.warning(f"[competitor_tracker] scan failed (non-fatal): {exc}")


def _run_supervisor() -> None:
    """Run agent supervisor — checks all agents and alerts on failures."""
    try:
        from src.agents.agent_supervisor import run_supervision, is_stale
        if is_stale(max_age_min=14):
            report = run_supervision()
            s = report.get("summary", {})
            log.info(
                f"[supervisor] ok={s.get('ok',0)} warn={s.get('warn',0)} "
                f"fail={s.get('fail',0)} overall={report.get('overall','?')}"
            )
            if report.get("critical_issues"):
                for issue in report["critical_issues"]:
                    log.warning(f"[supervisor] CRITICAL: {issue}")
    except Exception as exc:
        log.warning(f"[supervisor] check failed (non-fatal): {exc}")


def _refresh_x_trending() -> None:
    """Silently refresh the X trending cache if it's older than 6h."""
    try:
        from src.agents.x_trending import _load_cache, refresh_cache
        if _load_cache() is None:
            log.info("[x_trending] cache stale — refreshing...")
            refresh_cache()
            log.info("[x_trending] cache updated")
    except Exception as exc:
        log.warning(f"[x_trending] refresh failed (non-fatal): {exc}")


def _run_ab_test() -> None:
    """Run A/B test analysis and update winner signals (every 6h)."""
    try:
        from src.agents.ab_tester import run_ab_test
        queue_root = Path(os.environ.get("AUTOPOST_QUEUE", str(_REPO_ROOT / "queue")))
        results = run_ab_test(queue_root)
        winner = results.get("comparison", {}).get("score_winner")
        recs   = results.get("recommendations", [])
        log.info(f"[ab_test] winner={winner or 'pending'} | {recs[0] if recs else 'no recs'}")
    except Exception as exc:
        log.warning(f"[ab_test] cycle failed (non-fatal): {exc}")


def _run_engagement_optimizer() -> None:
    """Analyze post history and update engagement insights (runs every 6h)."""
    try:
        from src.agents.engagement_optimizer import run_optimization
        queue_root = Path(os.environ.get("AUTOPOST_QUEUE", str(_REPO_ROOT / "queue")))
        insights = run_optimization(queue_root)
        recs = insights.get("recommendations", [])
        log.info(f"[engagement] updated insights — {len(recs)} recommendation(s)")
        for r in recs[:3]:
            log.info(f"  -> {r}")
    except Exception as exc:
        log.warning(f"[engagement] optimizer failed (non-fatal): {exc}")


def _run_trend_prediction() -> None:
    """Refresh market trend forecast (runs before each slot and every 2h)."""
    try:
        from src.agents.trend_predictor import run_prediction
        fc = run_prediction()
        top3 = [c["symbol"] for c in fc.get("top_coins", [])[:3]]
        fg   = fc.get("fear_greed", {})
        log.info(f"[trends] forecast updated — top: {top3}, F&G: {fg.get('label')} ({fg.get('value')})")
    except Exception as exc:
        log.warning(f"[trends] prediction failed (non-fatal): {exc}")


def _run_market_intelligence() -> None:
    """Continuous market analysis — scrapes trends, hooks, visual style signals (every 30min)."""
    try:
        from src.agents.market_intelligence import run_analysis
        pulse = run_analysis(force=False)
        kw_count = len(pulse.get("trending_keywords", []))
        sentiment = pulse.get("market_sentiment", "?")
        coins = [c["symbol"] for c in pulse.get("trending_coins", [])[:3]]
        log.info(f"[market] pulse updated — sentiment={sentiment} trending={coins} keywords={kw_count}")
    except Exception as exc:
        log.warning(f"[market] intelligence failed (non-fatal): {exc}")


def _run_feedback_loop() -> None:
    """Close the engagement→brain feedback loop — update agent params from real post metrics (every 1h)."""
    try:
        from src.agents.feedback_loop import run_feedback_loop
        summary = run_feedback_loop(verbose=False)
        analysis = summary.get("analysis", {})
        log.info(f"[feedback] loop complete — best_tod={analysis.get('best_tod')}, "
                 f"best_day={analysis.get('best_weekday')}, records={analysis.get('record_count', 0)}")
    except Exception as exc:
        log.warning(f"[feedback] loop failed (non-fatal): {exc}")


def _run_platform_timing() -> None:
    """Compute optimal posting hours per platform (runs every 4h)."""
    try:
        from src.agents.platform_timing import run_platform_timing, format_timing_summary
        run_platform_timing()
        log.info("[timing] platform timing updated")
        log.info(format_timing_summary())
    except Exception as exc:
        log.warning(f"[timing] platform timing failed (non-fatal): {exc}")


def _run_slot_locked(slot: str, publish: bool = True) -> bool:
    """Inner implementation of run_slot — called only after the slot lock is held."""
    # Mark started immediately — any watchdog-restarted or catch-up instance
    # that reads scheduler_state.json will see this and skip the slot.
    _mark_started(slot)

    import os, json as _json
    from src.agents.user_pipeline_manager import run_all_users

    log.info("=" * 60)
    log.info(f"Starting slot: {slot.upper()}  (publish={publish})")
    log.info("=" * 60)

    _inject_admin_settings()
    _run_health_check()
    _refresh_x_trending()
    _run_trend_prediction()
    _run_market_intelligence()
    _run_feedback_loop()

    queue_root = Path(os.environ.get("AUTOPOST_QUEUE", str(_REPO_ROOT / "queue")))

    if slot == "morning":
        hours_back      = 12
        published_after = None
        log.info("Morning window: last 12h (overnight news before 08:00 GMT+2)")
    else:
        hours_back      = 10
        published_after = _today_cutoff_utc(8)
        log.info(f"Evening window: news after {published_after.isoformat()} UTC (08:00 GMT+2)")

    enabled = _resolve_enabled_platforms()
    pub_after_iso = published_after.isoformat() if published_after else None

    try:
        results = run_all_users(
            slot=slot,
            hours_back=hours_back,
            publish_live=publish,
            platforms=enabled,
            queue_root=queue_root,
            published_after_iso=pub_after_iso,
        )

        ok_users    = [r for r in results if r.get("status") == "ok"]
        fail_users  = [r for r in results if r.get("status") != "ok"]
        log.info(f"Slot {slot} completed — {len(ok_users)} OK, {len(fail_users)} failed")
        _mark_run(slot, success=bool(ok_users))

        # Mark per-user state so catch_up_missed_runs won't re-run these users.
        for r in results:
            uid = r.get("user_id")
            if uid is not None:
                _mark_user_slot(int(uid), slot, started=False)

        if fail_users:
            fail_info = "; ".join(f"user {r['user_id']}: {r.get('error','?')[:80]}" for r in fail_users)
            _tg_alert(f"Slot {slot} PARTIAL FAIL\n{len(fail_users)} user(s) failed:\n{fail_info}")

        return bool(ok_users or not fail_users)

    except Exception as exc:
        log.error(f"Slot {slot} CRASHED: {exc}", exc_info=True)
        _mark_run(slot, success=False)
        _tg_alert(f"Slot {slot} CRASHED\nError: {exc}\nCheck: scheduler.log")
        return False


# ---------------------------------------------------------------------------
# Scheduler loop
# ---------------------------------------------------------------------------

def _load_schedule_times() -> tuple[str, str]:
    """Read morning/evening times from admin DB settings, fall back to env vars."""
    try:
        from src.dashboard.database import get_all_users, get_user_settings
        admins = [u for u in get_all_users() if u.get("is_admin") and u.get("is_approved")]
        if admins:
            s = get_user_settings(admins[0]["id"])
            morning = (s.get("post_time_morning") or "").strip()
            evening = (s.get("post_time_evening") or "").strip()
            if morning and evening:
                return morning, evening
    except Exception:
        pass
    return (
        os.environ.get("AUTOPOST_MORNING", "08:00"),
        os.environ.get("AUTOPOST_EVENING", "18:00"),
    )


def start_scheduler(publish: bool = True) -> None:
    try:
        import schedule
    except ImportError:
        raise SystemExit("schedule not installed — run: pip install schedule")

    viral_time          = os.environ.get("VIRAL_SCOUT_TIME",      "06:30")
    competitor_time     = os.environ.get("COMPETITOR_SCAN_TIME",  "09:00")

    # Per-user scheduling: check every minute which users have a slot due.
    # Each user has their own post_time_morning / post_time_evening in DB.
    schedule.every().minute.do(_check_per_user_slots, publish=publish)
    schedule.every().minute.do(_check_scheduled_posts, publish=publish)

    schedule.every().day.at(viral_time).do(run_viral_scout)
    schedule.every(2).hours.do(_run_web_learning)   # continuous — every 2h
    schedule.every().day.at(competitor_time).do(_run_competitor_tracker)
    schedule.every(30).minutes.do(_run_health_check)
    schedule.every(15).minutes.do(_run_retry_cycle)
    schedule.every(15).minutes.do(_run_supervisor)
    schedule.every(15).minutes.do(catch_up_missed_runs, publish=publish)
    schedule.every(2).hours.do(_run_trend_prediction)
    schedule.every(6).hours.do(_run_engagement_optimizer)
    schedule.every(6).hours.do(_run_ab_test)
    # New intelligence agents
    schedule.every(30).minutes.do(_run_market_intelligence)
    schedule.every(1).hours.do(_run_feedback_loop)
    schedule.every(4).hours.do(_run_platform_timing)

    log.info(f"Scheduler started (per-user mode). Viral scout: {viral_time} | Web learning: every 2h")
    log.info(f"Publishing: {'YES' if publish else 'DRY-RUN'}")
    log.info("Each user posts at their own configured morning/evening time.")

    # First-launch viral warm-up
    try:
        _inject_admin_settings()
        from src.dashboard.database import get_all_users, get_viral_blueprint
        admins = [u for u in get_all_users() if u.get("is_admin") and u.get("is_approved")]
        if admins:
            cached = get_viral_blueprint(admins[0]["id"], "crypto")
            if not cached or not cached.get("sample_size"):
                log.info("[viral] no cached blueprint — running warm-up scout")
                from src.agents.viral_scout import run_once as _viral_run_once
                _viral_run_once(force=False)
    except Exception as exc:
        log.warning(f"[viral] startup warm-up skipped: {exc}")

    # Immediate web learning run at startup — force so it always runs once now
    import threading as _th_wl
    _th_wl.Thread(target=_run_web_learning, daemon=True, name="web-learn-startup").start()
    log.info("[web_learner] Startup cycle launched in background")

    # Catch-up missed runs (machine was off at scheduled time)
    try:
        caught = catch_up_missed_runs(publish=publish)
        if caught:
            log.info(f"Catch-up complete for: {', '.join(caught)}")
    except Exception as exc:
        log.error(f"catch-up failed: {exc}", exc_info=True)

    _heartbeat()

    while True:
        try:
            schedule.run_pending()
        except Exception as _loop_exc:
            log.error(f"[scheduler] loop error (recovered): {_loop_exc}", exc_info=True)
        _heartbeat()
        time.sleep(30)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="AutoPost twice-daily scheduler")
    ap.add_argument("--dry-run", action="store_true",
                    help="run pipeline without publishing")
    ap.add_argument("--now", choices=["morning", "evening", "both", "viral"],
                    help="run immediately (skip scheduler). 'viral' refreshes "
                         "the IG/TikTok/YouTube viral blueprint only.")
    args = ap.parse_args(argv[1:])

    publish = not args.dry_run

    if args.now == "viral":
        ok = run_viral_scout()
        return 0 if ok else 1

    if args.now:
        slots = ["morning", "evening"] if args.now == "both" else [args.now]
        ok = True
        for slot in slots:
            ok = run_slot(slot, publish=publish) and ok
        return 0 if ok else 1

    start_scheduler(publish=publish)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
