"""Viral Scout — Agent that watches IG, TikTok and YouTube for trending videos.

This is the "video equivalent" of `news_scout.py`. Where news_scout pulls the
latest crypto headlines from RSS, viral_scout pulls the most-engaging short
videos from IG / TikTok / YouTube on the topics WE care about (crypto, BTC,
ETH, altcoins, crypto news).

Architecture:
  ┌─────────────┐  scrape  ┌─────────────┐  blueprint  ┌──────────────────┐
  │ viral_scout │─────────▶│viral_analyzer│────────────▶│viral_transformer │
  │ (this file) │          │   (purist)   │            │ (rewrites copy)  │
  └─────────────┘          └─────────────┘            └──────────────────┘

What this module exposes:
  scan_topics(topics, ...)       -> list[blueprint]   (one per topic)
  refresh_for_user(user_id, ...) -> list[blueprint]   (saves to DB)
  run_once(...)                  -> CLI orchestrator (used by scheduler)

Topics — locked to OUR niches (crypto, news, finance). The user explicitly
asked: "sa se rezume la cea ce facem noi, adica crypto, stiri etc". You can
override via env `VIRAL_TOPICS="crypto,bitcoin,ethereum,solana,xrp,crypto news"`
or per-user via `viral_topics` setting (comma-separated).

Cadence:
  Scheduler runs `run_once()` daily at 06:30 GMT+2 — well before the 08:00
  morning slot — so the morning copywriter / avatar_writer always have a
  fresh blueprint to imitate.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.agents.viral_analyzer import scan_viral  # noqa: E402
from src.agents.niches import niche_viral_topics, NICHE_VIRAL_TOPICS  # noqa: E402

log = logging.getLogger("viral_scout")
if not log.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [viral_scout] %(message)s",
                                     datefmt="%H:%M:%S"))
    log.addHandler(h)
log.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Default topics — strictly OUR niches. The user wants us focused on crypto/news.
# ---------------------------------------------------------------------------

DEFAULT_TOPICS: tuple[str, ...] = (
    "crypto",
    "bitcoin",
    "ethereum",
    "crypto news",
    "altcoin",
)

DEFAULT_PLATFORMS: tuple[str, ...] = ("youtube", "tiktok", "instagram", "x", "facebook")

# Cache TTL — avoid re-scraping if a fresh blueprint exists.
DEFAULT_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# Topic resolution
# ---------------------------------------------------------------------------

def _resolve_topics(user_settings: dict | None = None) -> list[str]:
    """Decide which topics to scout.

    Priority:
      1. per-user `viral_topics` setting (comma-separated, manual override)
      2. env VIRAL_TOPICS=...
      3. niche-specific topics derived from `settings.niche`
      4. DEFAULT_TOPICS (crypto fallback)
    """
    raw = ""
    if user_settings:
        raw = (user_settings.get("viral_topics") or "").strip()
    if not raw:
        raw = os.environ.get("VIRAL_TOPICS", "").strip()
    if raw:
        return [t.strip().lower() for t in raw.split(",") if t.strip()]

    # No explicit override → use the user's selected niche to pick topics
    niche_id = ""
    if user_settings:
        niche_id = (user_settings.get("niche") or "").strip().lower()
    if niche_id:
        try:
            topics = niche_viral_topics(niche_id)
            if topics:
                return topics
        except Exception:
            pass
    return list(DEFAULT_TOPICS)


def topics_for_niche(niche_id: str | None) -> list[str]:
    """Public helper used by the dashboard: return the topic list for a niche.

    Falls back to DEFAULT_TOPICS (crypto) for unknown / empty niches.
    """
    nid = (niche_id or "").strip().lower()
    if not nid:
        return list(DEFAULT_TOPICS)
    try:
        topics = niche_viral_topics(nid)
        return list(topics) if topics else list(DEFAULT_TOPICS)
    except Exception:
        return list(DEFAULT_TOPICS)


def _resolve_platforms() -> tuple[str, ...]:
    raw = os.environ.get("VIRAL_PLATFORMS", "").strip()
    if not raw:
        return DEFAULT_PLATFORMS
    return tuple(p.strip().lower() for p in raw.split(",")
                 if p.strip().lower() in {"youtube", "tiktok", "instagram", "x", "facebook"})


# ---------------------------------------------------------------------------
# Scout — top-level entry points
# ---------------------------------------------------------------------------

def scan_topics(topics: Iterable[str] | None = None,
                platforms: Iterable[str] | None = None,
                limit_per_platform: int = 100,
                youtube_api_key: str | None = None,
                niche: str | None = None) -> list[dict]:
    """Run a viral scan for each topic. Returns a list of blueprints.

    Pure function: no DB writes. Use refresh_for_user() to also persist.
    """
    topics_list = list(topics) if topics else list(DEFAULT_TOPICS)
    plats = tuple(platforms) if platforms else _resolve_platforms()
    api_key = (youtube_api_key
               or os.environ.get("YOUTUBE_API_KEY", "").strip()
               or None)

    blueprints: list[dict] = []
    for topic in topics_list:
        t0 = time.time()
        try:
            bp = scan_viral(
                topic=topic,
                platforms=plats,
                limit_per_platform=limit_per_platform,
                youtube_api_key=api_key,
                niche=niche,
            )
        except Exception as exc:
            log.warning(f"scan failed for topic={topic!r}: {exc}")
            continue
        bp["topic"] = topic
        bp["scouted_at"] = datetime.now(timezone.utc).isoformat()
        log.info(f"topic={topic!r:>16} sample={bp.get('sample_size', 0):>3} "
                 f"hooks={len(bp.get('hook_patterns') or [])} "
                 f"hashtags={len(bp.get('top_hashtags') or [])} "
                 f"({time.time()-t0:.1f}s)")
        blueprints.append(bp)
    return blueprints


def refresh_for_user(user_id: int,
                     topics: Iterable[str] | None = None,
                     ttl_hours: int = DEFAULT_TTL_HOURS,
                     force: bool = False) -> list[dict]:
    """Refresh blueprints for ONE user, persisting each to the DB.

    Skips topics whose cached blueprint is younger than `ttl_hours` unless
    `force=True`. This is what the scheduler calls each morning.
    """
    from src.dashboard.database import (
        get_user_settings, save_viral_blueprint, get_viral_blueprint,
    )

    settings = get_user_settings(user_id)
    topics_list = list(topics) if topics else _resolve_topics(settings)
    platforms = _resolve_platforms()
    yt_key = (settings.get("youtube_api_key") or "").strip() or None
    niche = (settings.get("niche") or "").strip().lower() or None

    out: list[dict] = []
    for topic in topics_list:
        if not force:
            cached = get_viral_blueprint(user_id, topic, max_age_hours=ttl_hours)
            if cached and cached.get("sample_size"):
                log.info(f"user={user_id} topic={topic!r} -> cache hit "
                         f"(skip rescrape)")
                out.append(cached)
                continue
        bp_list = scan_topics(
            topics=[topic],
            platforms=platforms,
            limit_per_platform=100,
            youtube_api_key=yt_key,
            niche=niche,
        )
        if not bp_list:
            continue
        bp = bp_list[0]
        try:
            save_viral_blueprint(user_id, topic, bp)
            log.info(f"user={user_id} topic={topic!r} -> saved")
        except Exception as exc:
            log.warning(f"could not save blueprint user={user_id} topic={topic}: {exc}")
        out.append(bp)
    return out


def refresh_all_admins(force: bool = False) -> dict[int, list[dict]]:
    """Run the scout for every approved admin user.

    Returns {user_id: [blueprint, ...]}. Used by the scheduler hook.
    """
    try:
        from src.dashboard.database import get_all_users
    except Exception as exc:
        log.warning(f"DB not reachable: {exc}")
        return {}

    users = [u for u in get_all_users()
             if u.get("is_approved") and (u.get("is_admin") or u.get("plan"))]
    if not users:
        log.info("no eligible users — nothing to scout")
        return {}

    results: dict[int, list[dict]] = {}
    for u in users:
        try:
            log.info(f"-- scouting for user_id={u['id']} ({u.get('email','')}) --")
            results[u["id"]] = refresh_for_user(u["id"], force=force)
        except Exception as exc:
            log.error(f"refresh failed for user {u.get('email','')}: {exc}")
            results[u["id"]] = []
    return results


# ---------------------------------------------------------------------------
# CLI / scheduler hook
# ---------------------------------------------------------------------------

def run_once(force: bool = False) -> int:
    """Entry point used by scheduler.py. Returns # of blueprints refreshed."""
    log.info("=" * 60)
    log.info(f"Viral Scout starting (force={force})")
    log.info("=" * 60)
    t0 = time.time()
    results = refresh_all_admins(force=force)
    total = sum(len(v) for v in results.values())
    log.info(f"Viral Scout finished: {total} blueprint(s) across "
             f"{len(results)} user(s) in {time.time()-t0:.1f}s")
    return total


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Viral Scout — refresh viral blueprints")
    ap.add_argument("--force", action="store_true",
                    help="ignore TTL and re-scrape every topic")
    ap.add_argument("--topic", action="append",
                    help="override topic list (repeat for multiple)")
    ap.add_argument("--user-id", type=int,
                    help="refresh for a single user only")
    ap.add_argument("--print", action="store_true",
                    help="print blueprints instead of saving")
    args = ap.parse_args(argv[1:])

    # Best-effort .env load so YOUTUBE_API_KEY etc. are picked up
    try:
        from dotenv import load_dotenv
        load_dotenv(_REPO_ROOT / ".env")
    except ImportError:
        pass

    topics = args.topic if args.topic else None

    if args.print:
        bps = scan_topics(topics=topics)
        import json as _json
        print(_json.dumps(bps, indent=2)[:4000])
        return 0

    if args.user_id:
        refresh_for_user(args.user_id, topics=topics, force=args.force)
        return 0

    run_once(force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
