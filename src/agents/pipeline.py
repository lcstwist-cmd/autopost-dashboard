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

# Force UTF-8 on stdout/stderr so log lines with unicode (e.g. "≈", "→", "✓")
# don't crash on Windows cp1252 terminals.
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

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass

import re as _re

from src.agents.news_scout import collect_news  # noqa: E402
from src.agents.ranker import rank              # noqa: E402

STAGES = ["scout", "rank", "copy", "image", "reel", "video", "avatar", "publish"]


# ---------------------------------------------------------------------------
# Cross-slot deduplication helpers
# ---------------------------------------------------------------------------

def _title_tokens(title: str) -> frozenset:
    return frozenset(_re.findall(r"[a-z0-9]+", title.lower()))


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _posted_titles_today(out_root: Path, today: str, current_slot: str) -> list[str]:
    """Return titles from slots already run today (to avoid re-posting same story)."""
    titles: list[str] = []
    for slot in ("morning", "evening"):
        if slot == current_slot:
            continue
        p = out_root / f"{today}_{slot}" / "top2.json"
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            stories = data.get("stories", []) if isinstance(data, dict) else data
            titles.extend(s.get("title", "") for s in stories if s.get("title"))
        except Exception:
            pass
    return titles


def _dedup_against_posted(stories: list[dict], posted_titles: list[str],
                           threshold: float = 0.45) -> list[dict]:
    """Filter out stories whose title is too similar to already-posted ones."""
    if not posted_titles:
        return stories
    posted_tok = [_title_tokens(t) for t in posted_titles if t]
    kept, dropped = [], 0
    for story in stories:
        toks = _title_tokens(story.get("title", ""))
        if any(_jaccard(toks, pt) >= threshold for pt in posted_tok):
            dropped += 1
            print(f"[pipeline] dedup-skip (similar to today's post): {story.get('title','')[:70]}")
        else:
            kept.append(story)
    if dropped:
        print(f"[pipeline] cross-slot dedup: removed {dropped} duplicate(s), {len(kept)} remain")
    return kept


def _stage_scout(out_dir: Path, hours_back: int, published_after=None,
                 niche: str = "crypto") -> list[dict]:
    stories = collect_news(hours_back=hours_back, published_after=published_after,
                           niche=niche)
    (out_dir / "raw_news.json").write_text(
        json.dumps(stories, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[pipeline] scout: {len(stories)} candidates -> raw_news.json")
    return stories


def _stage_rank(out_dir: Path, stories: list[dict], slot: str, niche: str = "crypto") -> list[dict]:
    # Boost stories matching trending coins from forecast
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "trend_predictor", Path(__file__).parent / "trend_predictor.py"
        )
        _mod = _ilu.module_from_spec(_spec)   # type: ignore
        _spec.loader.exec_module(_mod)         # type: ignore
        top_tickers = set(_mod.get_top_tickers(n=5))
        if top_tickers:
            for s in stories:
                story_tickers = {t.upper() for t in (s.get("tickers") or [])}
                if story_tickers & top_tickers:
                    s["score"] = s.get("score", 50) + 15
            print(f"[pipeline] trend boost applied for: {top_tickers}")
    except Exception:
        pass

    picked = rank(stories, top_n=3, niche=niche)
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


def _stage_copy(out_dir: Path, model: str, niche: str = "crypto") -> None:
    from src.agents.copywriter import write_outputs
    top2 = json.loads((out_dir / "top2.json").read_text(encoding="utf-8"))
    stories = top2.get("stories") or []
    if not stories:
        print("[pipeline] copy: no stories in top2.json — skipping", file=sys.stderr)
        return
    story = stories[0]
    stats = write_outputs(story, out_dir, model=model, niche=niche)
    print(f"[pipeline] copy: post_x={stats['post_x_chars']}wc  "
          f"tg={stats['post_telegram_chars']}c  img_prompt={stats['image_prompt_chars']}c")


def _stage_image(out_dir: Path, template: Path, niche: str = "crypto") -> None:
    from src.agents.image_gen import render_all, render_roundup_image
    top2 = json.loads((out_dir / "top2.json").read_text(encoding="utf-8"))
    stories = top2.get("stories") or []
    if not stories:
        print("[pipeline] image: no stories in top2.json — skipping", file=sys.stderr)
        return
    daily_seed = datetime.now().toordinal()
    # X (landscape 16:9), Telegram (square 1:1) and YouTube thumbnail (16:9 1280x720)
    render_all(stories[0], out_dir, seed=daily_seed, sizes=["x", "tg", "yt_thumb"], niche=niche)
    # Reel (9:16) and IG feed (4:5) — multi-story roundup with top 3
    top3 = stories[:3]
    if len(top3) > 1:
        render_roundup_image(top3, out_dir / "image_reel_1080x1920.png",
                             width=1080, height=1920, seed=daily_seed)
        render_roundup_image(top3, out_dir / "image_ig_1080x1350.png",
                             width=1080, height=1350, seed=daily_seed)
        print(f"[pipeline] image: x+tg+yt_thumb single-story, reel+ig roundup top-{len(top3)} (niche={niche})")
    else:
        render_all(stories[0], out_dir, seed=daily_seed, sizes=["reel", "ig_feed"], niche=niche)
        print(f"[pipeline] image: rendered x+tg+yt_thumb+reel+ig_feed (niche={niche})")


def _stage_reel(out_dir: Path) -> None:
    from src.agents import reel_writer
    from src.agents import avatar_writer
    top2 = json.loads((out_dir / "top2.json").read_text(encoding="utf-8"))
    stories = top2.get("stories") or []
    if not stories:
        print("[pipeline] reel: no stories in top2.json — skipping", file=sys.stderr)
        return

    # CapCut package — still uses top story only (single-story format)
    reel_dir = out_dir / "reel"
    reel_dir.mkdir(parents=True, exist_ok=True)
    reel_writer.write_package(stories[0], reel_dir)
    print(f"[pipeline] reel: CapCut package -> {reel_dir}")

    # Avatar script package — roundup mode when 2+ stories available (45s digest)
    avatar_dir = out_dir / "avatar"
    use_llm = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    if len(stories) >= 2:
        # Multi-story roundup: 45-second script covering top 3 stories
        target_s = 45
        avatar_writer.write_package(stories[:3], avatar_dir, use_llm=False,
                                    target_seconds=target_s)
        print(f"[pipeline] avatar: roundup script ({len(stories[:3])} stories, {target_s}s) -> {avatar_dir}")
    else:
        avatar_writer.write_package(stories[0], avatar_dir, use_llm=use_llm)
        print(f"[pipeline] avatar: single-story script -> {avatar_dir}")


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
        status = str(res.get('status', 'unknown'))
        error = str(res.get('error', ''))
        # Sanitize for Windows console encoding
        error = ''.join(c if ord(c) < 128 else '?' for c in error)
        extra = f" -- {error}" if status == "error" else ""
        msg = f"    [{plat}] {status}{extra}"
        # More aggressive cleaning
        msg = msg.encode('ascii', errors='replace').decode('ascii')
        print(msg)


def run_pipeline(slot, hours_back, out_root, stop_after, model, template_path,
                 publish_live, platforms, variant=None, published_after=None,
                 niche: str = "crypto"):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = out_root / f"{today}_{slot}"
    out_dir.mkdir(parents=True, exist_ok=True)

    stop_idx = STAGES.index(stop_after)
    print(f"[pipeline] slot={slot}  hours={hours_back}  out={out_dir}  "
          f"stop_after={stop_after}  publish={'live' if publish_live else 'dry-run'}")

    stories = []
    if stop_idx >= STAGES.index("scout"):
        stories = _stage_scout(out_dir, hours_back, published_after=published_after,
                               niche=niche)

    if stop_idx >= STAGES.index("rank"):
        if not stories:
            raw = json.loads((out_dir / "raw_news.json").read_text(encoding="utf-8"))
            stories = raw if isinstance(raw, list) else raw.get("stories", [])
        if not stories:
            print("[pipeline] no stories to rank -- stopping.")
            return out_dir

        # Cross-slot dedup: skip stories already covered in today's other slots
        posted = _posted_titles_today(out_root, today, slot)
        stories = _dedup_against_posted(stories, posted)
        if not stories:
            print("[pipeline] all stories already covered today — stopping.")
            return out_dir

        _stage_rank(out_dir, stories, slot, niche=niche)

    # OPTIMIZATION: Parallelize copy + image stages (they don't depend on each other)
    # This reduces time from ~135s to ~120s (they run 90% in parallel due to I/O)
    if stop_idx >= STAGES.index("copy") or stop_idx >= STAGES.index("image"):
        from concurrent.futures import ThreadPoolExecutor
        
        def _run_copy():
            if stop_idx >= STAGES.index("copy"):
                _stage_copy(out_dir, model, niche=niche)
        
        def _run_image():
            if stop_idx >= STAGES.index("image"):
                _stage_image(out_dir, template_path, niche=niche)
        
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_copy = executor.submit(_run_copy)
            future_image = executor.submit(_run_image)
            future_copy.result()
            future_image.result()
    elif stop_idx >= STAGES.index("copy"):
        _stage_copy(out_dir, model, niche=niche)
    elif stop_idx >= STAGES.index("image"):
        _stage_image(out_dir, template_path, niche=niche)

    if stop_idx >= STAGES.index("reel"):
        try:
            _stage_reel(out_dir)
        except Exception as exc:
            print(f"[pipeline] reel stage error (non-fatal): {exc}")

    if stop_idx >= STAGES.index("video"):
        _video_generated = False
        try:
            from src.agents.video_builder import build_video
            build_video(out_dir)
            _video_generated = True
        except Exception as exc:
            import traceback
            print("\n" + "!" * 70)
            print(f"[pipeline] !!! VIDEO STAGE FAILED -- trying ffmpeg fallback !!!")
            print(f"[pipeline] Error: {exc}")
            traceback.print_exc()
            print("!" * 70 + "\n")

        if not _video_generated:
            # Fallback: create a 20s video from the still image via ffmpeg.
            # Requires only ffmpeg in PATH — no additional Python dependencies.
            try:
                from src.agents.simple_video_gen import build_simple_video
                fb = build_simple_video(out_dir)
                if fb:
                    print(f"[pipeline] ffmpeg fallback video -> {fb.name}")
                    _video_generated = True
                else:
                    print("[pipeline] ffmpeg fallback also failed -- skipping video")
            except Exception as exc2:
                print(f"[pipeline] ffmpeg fallback error (non-fatal): {exc2}")

    # Avatar stage is only needed for TikTok/YouTube video content.
    # Skip it when neither platform is in the enabled set to save ~5 minutes.
    _video_platforms = {"tiktok", "youtube"}
    if stop_idx >= STAGES.index("avatar") and (platforms or set()) & _video_platforms:
        try:
            from src.agents.avatar_video import build_avatar_reel
            presenter = os.environ.get("DID_PRESENTER", "default")
            out = build_avatar_reel(out_dir, presenter=presenter)
            print(f"[pipeline] avatar video -> {out}")
        except Exception as exc:
            import traceback
            print(f"[pipeline] avatar stage failed (non-fatal): {exc}")
            traceback.print_exc()
    elif stop_idx >= STAGES.index("avatar"):
        print(f"[pipeline] avatar stage skipped — no TikTok/YouTube in platforms {sorted(platforms or [])}")

    if stop_idx >= STAGES.index("publish"):
        try:
            from src.agents.pre_post_optimizer import optimize as _optimize
            plat_list = list(platforms) if platforms else ["tiktok", "instagram", "youtube", "x"]
            _opt_report = _optimize(out_dir, platforms=plat_list)
            winner_summary = {
                p: f"{v.get('winner_style')}({v.get('winner_score',0):.0f})"
                for p, v in (_opt_report.get("platforms") or {}).items()
            }
            print(f"[pipeline] optimizer: {winner_summary}")
        except Exception as exc:
            import traceback
            print(f"[pipeline] optimizer non-fatal error: {exc}")
            traceback.print_exc()

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
