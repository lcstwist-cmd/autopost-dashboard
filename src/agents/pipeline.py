"""Pipeline orchestrator — full Crypto AutoPost flow.

Runs: Scout -> Ranker -> Copywriter -> Image Generator -> Reel Writer [-> Publisher]

Each stage is optional via --stop-after.

Usage:
    python src/agents/pipeline.py --slot morning
    python src/agents/pipeline.py --slot morning --publish
    python src/agents/pipeline.py --slot morning --stop-after rank
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass

from src.agents.news_scout import collect_news  # noqa: E402
from src.agents.ranker import rank              # noqa: E402

STAGES = ["scout", "rank", "copy", "image", "reel", "video", "publish"]


def _stage_scout(out_dir: Path, hours_back: int, published_after=None) -> list[dict]:
    stories = collect_news(hours_back=hours_back, published_after=published_after)
    (out_dir / "raw_news.json").write_text(
        json.dumps(stories, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[pipeline] scout: {len(stories)} candidates -> raw_news.json")
    return stories


def _stage_rank(out_dir: Path, stories: list[dict], slot: str) -> list[dict]:
    picked = rank(stories, top_n=2)
    report = {
        "picked_at": datetime.now(timezone.utc).isoformat(),
        "slot": slot,
        "total_candidates": len(stories),
        "stories": picked,
    }
    (out_dir / "top2.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    today = out_dir.name.split("_", 1)[0]
    lines = [
        f"Crypto AutoPost -- {slot.upper()} slot -- {today}",
        f"Candidates scanned: {len(stories)}",
        "",
        "TOP 2 STORIES:",
        "",
    ]
    for i, s in enumerate(picked, 1):
        lines.append(f"#{i}  [score {s['_score_total']}]  {s['title']}")
        lines.append(f"     source: {s['source']}  | published: {s.get('published_at','?')}")
        lines.append(f"     tickers: {', '.join(s.get('tickers', []) or ['-'])}")
        lines.append(f"     why: {s['_rationale']}")
        lines.append(f"     url: {s['url']}")
        lines.append("")
    (out_dir / "summary.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"[pipeline] rank: picked {len(picked)} -> top2.json, summary.txt")
    return picked


def _stage_copy(out_dir: Path, model: str) -> None:
    from src.agents.copywriter import write_outputs
    top2 = json.loads((out_dir / "top2.json").read_text(encoding="utf-8"))
    stories = top2.get("stories") or []
    if not stories:
        print("[pipeline] copy: no stories in top2.json — skipping", file=sys.stderr)
        return
    story = stories[0]
    stats = write_outputs(story, out_dir, model=model)
    print(f"[pipeline] copy: post_x={stats['post_x_chars']}wc  "
          f"tg={stats['post_telegram_chars']}c  img_prompt={stats['image_prompt_chars']}c")


def _stage_image(out_dir: Path, template: Path) -> None:
    from src.agents.image_gen import render_all
    top2 = json.loads((out_dir / "top2.json").read_text(encoding="utf-8"))
    stories = top2.get("stories") or []
    if not stories:
        print("[pipeline] image: no stories in top2.json — skipping", file=sys.stderr)
        return
    story = stories[0]
    # Render X card + Telegram square + Reels/Shorts 9:16 vertical
    render_all(story, out_dir, seed=42, sizes=["x", "tg", "reel"])
    print(f"[pipeline] image: rendered x + tg + reel (9:16)")


def _stage_reel(out_dir: Path) -> None:
    from src.agents import reel_writer
    from src.agents import avatar_writer
    top2 = json.loads((out_dir / "top2.json").read_text(encoding="utf-8"))
    stories = top2.get("stories") or []
    if not stories:
        print("[pipeline] reel: no stories in top2.json — skipping", file=sys.stderr)
        return
    story = stories[0]

    # CapCut package
    reel_dir = out_dir / "reel"
    reel_dir.mkdir(parents=True, exist_ok=True)
    reel_writer.write_package(story, reel_dir)
    print(f"[pipeline] reel: CapCut package -> {reel_dir}")

    # Avatar script package
    avatar_dir = out_dir / "avatar"
    use_llm = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    avatar_writer.write_package(story, avatar_dir, use_llm=use_llm)
    print(f"[pipeline] avatar: script package -> {avatar_dir}")


def _stage_publish(out_dir: Path, platforms: set[str], dry_run: bool,
                   variant: str | None = None) -> None:
    from src.agents.publisher import publish
    result = publish(out_dir, platforms=platforms, dry_run=dry_run, variant=variant)
    mode = "DRY-RUN" if dry_run else "LIVE"
    if result.get("status") == "blocked":
        print(f"[pipeline] publish BLOCKED ({mode}):")
        for e in result["errors"]:
            print(f"    - {e}")
        return
    print(f"[pipeline] publish {mode} -- platforms: {result['platforms']}  "
          f"variant: {result.get('variant')}")
    for plat, res in result["results"].items():
        extra = f" -- {res.get('error')}" if res.get("status") == "error" else ""
        print(f"    [{plat}] {res.get('status')}{extra}")


def run_pipeline(slot, hours_back, out_root, stop_after, model, template_path,
                 publish_live, platforms, variant=None, published_after=None):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = out_root / f"{today}_{slot}"
    out_dir.mkdir(parents=True, exist_ok=True)

    stop_idx = STAGES.index(stop_after)
    print(f"[pipeline] slot={slot}  hours={hours_back}  out={out_dir}  "
          f"stop_after={stop_after}  publish={'live' if publish_live else 'dry-run'}")

    stories = []
    if stop_idx >= STAGES.index("scout"):
        stories = _stage_scout(out_dir, hours_back, published_after=published_after)

    if stop_idx >= STAGES.index("rank"):
        if not stories:
            raw = json.loads((out_dir / "raw_news.json").read_text(encoding="utf-8"))
            stories = raw if isinstance(raw, list) else raw.get("stories", [])
        if not stories:
            print("[pipeline] no stories to rank -- stopping.")
            return out_dir
        _stage_rank(out_dir, stories, slot)

    if stop_idx >= STAGES.index("copy"):
        _stage_copy(out_dir, model)

    if stop_idx >= STAGES.index("image"):
        _stage_image(out_dir, template_path)

    if stop_idx >= STAGES.index("reel"):
        try:
            _stage_reel(out_dir)
        except Exception as exc:
            print(f"[pipeline] reel stage error (non-fatal): {exc}")

    if stop_idx >= STAGES.index("video"):
        try:
            from src.agents.video_builder import build_video
            build_video(out_dir)
        except Exception as exc:
            print(f"[pipeline] video stage error (non-fatal): {exc}")

    if stop_idx >= STAGES.index("publish"):
        _stage_publish(out_dir, platforms=platforms, dry_run=not publish_live,
                       variant=variant)
        # Post-publish: refresh analytics + dashboard so the user can see
        # the latest state on disk. Non-fatal if they fail.
        try:
            from src.agents.analytics import refresh_and_write as _refresh
            from src.dashboard import build as _db
            an_dir = _REPO_ROOT / "analytics"
            n = _refresh(out_root, an_dir)
            _db.build_dashboard(an_dir / "rollup.json",
                                _REPO_ROOT / "src" / "dashboard" / "index.html")
            print(f"[pipeline] analytics + dashboard refreshed ({n} slots)")
        except Exception as exc:
            print(f"[pipeline] analytics stage error (non-fatal): {exc}")

    print(f"[pipeline] done -> {out_dir}")
    return out_dir


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", choices=["morning", "evening"], required=True)
    ap.add_argument("--hours", type=int, default=12)
    ap.add_argument("--out-root",
                    default=os.environ.get("AUTOPOST_QUEUE", "queue"))
    ap.add_argument("--stop-after", choices=STAGES, default="publish")
    ap.add_argument("--model",
                    default=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"))
    ap.add_argument("--template",
                    default=str(_REPO_ROOT / "src" / "templates" / "news_card.html"))
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--platforms", default="telegram,x")
    ap.add_argument("--variant", default=None, choices=["A", "B"],
                    help="force X variant (default: deterministic 50/50 by slot)")
    args = ap.parse_args()

    platforms = {p.strip().lower() for p in args.platforms.split(",") if p.strip()}
    unknown = platforms - {"telegram", "x"}
    if unknown:
        print(f"[pipeline] unknown platforms: {unknown}", file=sys.stderr)
        return 2

    run_pipeline(
        slot=args.slot,
        hours_back=args.hours,
        out_root=Path(args.out_root),
        stop_after=args.stop_after,
        model=args.model,
        template_path=Path(args.template).resolve(),
        publish_live=args.publish,
        platforms=platforms,
        variant=args.variant,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
