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
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

LOG_FILE = _REPO_ROOT / "scheduler.log"
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger().addHandler(_fh)


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

# GMT+2 offset — change this if you're in a different timezone
TZ_OFFSET_HOURS = int(os.environ.get("AUTOPOST_TZ_OFFSET", "2"))


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
            "ANTHROPIC_API_KEY":   s.get("anthropic_key", ""),
            "TELEGRAM_TOKEN":      s.get("telegram_token", ""),
            "TELEGRAM_CHANNEL_ID": s.get("telegram_channel", ""),
            "X_API_KEY":           s.get("x_api_key", ""),
            "X_API_SECRET":        s.get("x_api_secret", ""),
            "X_ACCESS_TOKEN":      s.get("x_access_token", ""),
            "X_ACCESS_SECRET":     s.get("x_access_secret", ""),
            "ELEVENLABS_API_KEY":  s.get("elevenlabs_key", ""),
        }
        for k, v in mapping.items():
            if v and not os.environ.get(k):
                os.environ[k] = v
        log.info(f"Loaded API keys from admin user: {admins[0]['email']}")
    except Exception as exc:
        log.warning(f"Could not load admin settings from DB: {exc}")


def run_slot(slot: str, publish: bool = True) -> bool:
    """Run the full pipeline for one slot. Returns True on success."""
    import os
    from src.agents.pipeline import run_pipeline, STAGES

    log.info("=" * 60)
    log.info(f"Starting slot: {slot.upper()}  (publish={publish})")
    log.info("=" * 60)

    _inject_admin_settings()

    queue_root = Path(os.environ.get("AUTOPOST_QUEUE", str(_REPO_ROOT / "queue")))

    if slot == "morning":
        # Morning: last 12h of news, no lower bound (catches overnight)
        hours_back = 12
        published_after = None
        log.info("Morning window: last 12h (overnight news before 08:00 GMT+2)")
    else:
        # Evening: only news published after 08:00 GMT+2 today
        hours_back = 10
        published_after = _today_cutoff_utc(8)
        log.info(f"Evening window: news after {published_after.isoformat()} UTC (08:00 GMT+2)")

    try:
        out_dir = run_pipeline(
            slot=slot,
            hours_back=hours_back,
            out_root=queue_root,
            stop_after="publish",
            model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
            template_path=_REPO_ROOT / "src" / "templates" / "news_card.html",
            publish_live=publish,
            platforms={"telegram", "x"},
            published_after=published_after,
        )
        log.info(f"Slot {slot} completed -> {out_dir}")
        return True
    except Exception as exc:
        log.error(f"Slot {slot} FAILED: {exc}", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Scheduler loop
# ---------------------------------------------------------------------------

def start_scheduler(publish: bool = True) -> None:
    try:
        import schedule
    except ImportError:
        raise SystemExit(
            "schedule not installed — run: pip install schedule"
        )

    morning_time = os.environ.get("AUTOPOST_MORNING", "08:00")
    evening_time = os.environ.get("AUTOPOST_EVENING", "18:00")

    schedule.every().day.at(morning_time).do(run_slot, slot="morning", publish=publish)
    schedule.every().day.at(evening_time).do(run_slot, slot="evening", publish=publish)

    log.info(f"Scheduler started. Morning: {morning_time}, Evening: {evening_time}")
    log.info(f"Publishing: {'YES' if publish else 'DRY-RUN'}")
    log.info("Press Ctrl+C to stop.")

    next_morning = schedule.next_run()
    log.info(f"Next scheduled run: {next_morning}")

    while True:
        schedule.run_pending()
        time.sleep(30)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="AutoPost twice-daily scheduler")
    ap.add_argument("--dry-run", action="store_true",
                    help="run pipeline without publishing")
    ap.add_argument("--now", choices=["morning", "evening", "both"],
                    help="run immediately (skip scheduler)")
    args = ap.parse_args(argv[1:])

    publish = not args.dry_run

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
