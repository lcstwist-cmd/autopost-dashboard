"""_pipeline_runner.py — Subprocess entry point for per-user pipeline runs.

Called by user_pipeline_manager with a JSON args string.
Reads all credentials from environment variables (set by user_pipeline_manager).
Writes output to the user's own queue directory.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
sys.path.insert(0, str(_REPO))

if not os.environ.get("AUTOPOST_ISOLATED"):
    # Only load .env when running standalone (not as a per-user subprocess).
    # When isolated, all credentials are already injected by user_pipeline_manager.
    try:
        from dotenv import load_dotenv
        load_dotenv(_REPO / ".env")
    except ImportError:
        pass


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: _pipeline_runner.py '<json_args>'", file=sys.stderr)
        return 1

    args = json.loads(argv[1])
    slot               = args["slot"]
    hours_back         = args["hours_back"]
    publish_live       = args["publish_live"]
    platforms          = set(args["platforms"])
    published_after_iso = args.get("published_after_iso")
    skip_generation    = args.get("skip_generation", False)

    published_after = None
    if published_after_iso:
        published_after = datetime.fromisoformat(published_after_iso)

    queue_root = Path(os.environ.get("AUTOPOST_QUEUE", str(_REPO / "queue")))
    queue_root.mkdir(parents=True, exist_ok=True)

    from src.agents.pipeline import run_pipeline
    from src.agents.rate_limiter import get_limiter

    user_id = int(os.environ.get("AUTOPOST_USER_ID", "0"))
    rl      = get_limiter(user_id)

    # Prune platforms where rate limit is still active
    ready_platforms = {p for p in platforms if rl.is_ready(p)}
    if ready_platforms != platforms:
        skipped = platforms - ready_platforms
        print(f"[runner] rate-limited platforms skipped: {skipped}")
    if not ready_platforms:
        print("[runner] all platforms rate-limited — skipping this slot")
        return 0

    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

    # Read niche from user settings
    niche = "crypto"
    try:
        from src.dashboard.database import get_user_settings
        niche = get_user_settings(user_id).get("niche", "crypto") or "crypto"
    except Exception:
        pass

    # skip_generation: publish-only run using already-generated queue content
    if skip_generation:
        today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_dir  = None
        for s in ("morning", "evening"):
            candidate = queue_root / f"{today}_{s}"
            if candidate.exists() and (candidate / "top2.json").exists():
                out_dir = candidate
                break
        if not out_dir:
            print(f"[runner] skip_generation: no queue dir found for today ({today})")
            return 0
        from src.agents.publisher import publish
        result = publish(out_dir, platforms=ready_platforms, dry_run=not publish_live)
        print(f"[runner] deferred publish -> {out_dir}  platforms={sorted(ready_platforms)}")
        try:
            for plat, res in result.get("results", {}).items():
                if res.get("status") == "ok":
                    rl.mark_posted(plat)
        except Exception:
            pass
        return 0

    out_dir = run_pipeline(
        slot=slot,
        hours_back=hours_back,
        out_root=queue_root,
        stop_after="publish",
        model=model,
        template_path=_REPO / "src" / "templates" / "news_card.html",
        publish_live=publish_live,
        platforms=ready_platforms,
        published_after=published_after,
        niche=niche,
    )

    # Mark successfully posted platforms in rate limiter
    try:
        pub_log = Path(out_dir) / "publish_log.json"
        if pub_log.exists():
            results = json.loads(pub_log.read_text(encoding="utf-8")).get("results", {})
            for plat, res in results.items():
                if res.get("status") == "ok":
                    rl.mark_posted(plat)
    except Exception:
        pass

    # Award XP + increment runs counter for all agent brains
    try:
        from src.agents.agent_brain import BrainManager
        BrainManager().record_pipeline_run(Path(out_dir))
    except Exception:
        pass

    print(f"[runner] completed -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
