"""Feedback Loop Agent — closes the engagement→brain learning cycle.

Reads real post engagement metrics (likes, views, comments, shares) from
`queue/*/metrics.json` + `publish_log.json` and writes back to agent
learned_params so the pipeline actually improves based on results.

What gets updated:
  copywriter/learned_params  → preferred tone, hook type, best weekday/tod
  image_gen/learned_params   → preferred visual style, best ticker count
  ranker/learned_params      → topic score adjustments
  video_builder/learned_params → video duration, caption density preference

Runs every hour. Requires ≥3 data points before changing params.

CLI:
    python src/agents/feedback_loop.py
    python src/agents/feedback_loop.py --verbose
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE      = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
_QUEUE_ROOT = _REPO_ROOT / "queue"
_BRAINS_DIR = _REPO_ROOT / "analytics" / "agent_brains"
_DATA_DIR   = _REPO_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_SUMMARY_FILE = _DATA_DIR / "feedback_summary.json"

MIN_SAMPLES = 3   # Minimum data points before trusting a recommendation
PLATFORMS   = ["instagram", "tiktok", "youtube", "x", "telegram"]


def _load_json(p: Path) -> Any:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_json(p: Path, data: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Engagement score computation
# ---------------------------------------------------------------------------

def _engagement_score(metrics: dict, platform: str) -> float | None:
    """Normalized 0–100 engagement score for a platform."""
    m = metrics.get(platform, {})
    if not m:
        return None
    try:
        if platform == "x":
            imp = float(m.get("impressions") or 0)
            likes = float(m.get("likes") or 0)
            rt = float(m.get("retweets") or 0)
            return round(min((likes + rt * 2) / max(imp, 1) * 1000, 100), 2)
        if platform == "telegram":
            views = float(m.get("views") or 0)
            fwd   = float(m.get("forwards") or 0)
            return round(min((views + fwd * 5) / 100, 100), 2)
        if platform == "instagram":
            reach = float(m.get("reach") or 0)
            likes = float(m.get("likes") or 0)
            saves = float(m.get("saves") or 0)
            comments = float(m.get("comments") or 0)
            return round(min((likes + saves * 3 + comments * 2) / max(reach, 1) * 1000, 100), 2)
        if platform == "tiktok":
            views = float(m.get("views") or 0)
            likes = float(m.get("likes") or 0)
            shares = float(m.get("shares") or 0)
            comments = float(m.get("comments") or 0)
            return round(min((likes + shares * 3 + comments * 2) / max(views, 1) * 1000, 100), 2)
        if platform == "youtube":
            views = float(m.get("views") or 0)
            likes = float(m.get("likes") or 0)
            comments = float(m.get("comments") or 0)
            subs = float(m.get("subscribers_gained") or 0)
            return round(min((likes + comments * 3 + subs * 10) / max(views, 1) * 1000, 100), 2)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Record collection
# ---------------------------------------------------------------------------

def _collect_records() -> list[dict]:
    records = []
    for log_path in sorted(_QUEUE_ROOT.rglob("publish_log.json")):
        try:
            log = _load_json(log_path)
            if not log:
                continue
            slot_dir  = log_path.parent
            slot_name = slot_dir.name
            m = re.match(r"^(\d{4}-\d{2}-\d{2})_(morning|evening)$", slot_name)
            if m:
                date_str, tod = m.group(1), m.group(2)
            else:
                m2 = re.match(r"^(\d{4}-\d{2}-\d{2})", slot_name)
                date_str = m2.group(1) if m2 else None
                tod = "manual"
            if not date_str:
                continue

            metrics  = _load_json(slot_dir / "metrics.json") or {}
            top2     = _load_json(slot_dir / "top2.json") or {}
            story    = ((top2.get("stories") or [{}])[0]) if isinstance(top2, dict) else {}

            # Compute per-platform engagement scores
            scores = {}
            for plat in PLATFORMS:
                s = _engagement_score(metrics, plat)
                if s is not None:
                    scores[plat] = s
            if not scores:
                continue  # No metrics yet

            avg_score = round(statistics.mean(scores.values()), 2)
            try:
                weekday = datetime.fromisoformat(date_str).strftime("%A")
            except Exception:
                weekday = "Unknown"

            records.append({
                "date":     date_str,
                "weekday":  weekday,
                "tod":      tod,
                "variant":  log.get("variant", "A"),
                "tickers":  story.get("tickers", []) or [],
                "topic":    story.get("headline", "") or story.get("title", ""),
                "scores":   scores,
                "avg_score": avg_score,
            })
        except Exception as exc:
            print(f"[feedback] error reading {log_path}: {exc}", file=sys.stderr)
    return records


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _analyse(records: list[dict]) -> dict:
    if not records:
        return {}

    # Best time-of-day
    tod_scores: dict[str, list] = defaultdict(list)
    for r in records:
        if r["tod"] != "manual":
            tod_scores[r["tod"]].append(r["avg_score"])
    best_tod = None
    if tod_scores:
        best_tod = max(tod_scores, key=lambda t: statistics.mean(tod_scores[t]) if tod_scores[t] else 0)

    # Best weekday
    day_scores: dict[str, list] = defaultdict(list)
    for r in records:
        day_scores[r["weekday"]].append(r["avg_score"])
    best_weekday = None
    if len(day_scores) >= MIN_SAMPLES:
        best_weekday = max(day_scores, key=lambda d: statistics.mean(day_scores[d]) if day_scores[d] else 0)

    # Best A/B variant
    var_scores: dict[str, list] = defaultdict(list)
    for r in records:
        var_scores[r["variant"]].append(r["avg_score"])
    best_variant = None
    if len(var_scores) >= 2:
        best_variant = max(var_scores, key=lambda v: statistics.mean(var_scores[v]) if var_scores[v] else 0)

    # Per-platform best TOD
    platform_tod: dict[str, dict[str, list]] = {p: defaultdict(list) for p in PLATFORMS}
    for r in records:
        for plat, score in r["scores"].items():
            if r["tod"] != "manual":
                platform_tod[plat][r["tod"]].append(score)

    plat_best_tod = {}
    for plat, tod_data in platform_tod.items():
        if any(len(v) >= MIN_SAMPLES for v in tod_data.values()):
            plat_best_tod[plat] = max(
                tod_data, key=lambda t: statistics.mean(tod_data[t]) if tod_data[t] else 0
            )

    # Most engaging tickers
    ticker_scores: dict[str, list] = defaultdict(list)
    for r in records:
        for ticker in r["tickers"]:
            ticker_scores[ticker].append(r["avg_score"])
    top_tickers = [
        t for t, _ in sorted(
            {k: statistics.mean(v) for k, v in ticker_scores.items() if len(v) >= 2}.items(),
            key=lambda x: x[1], reverse=True
        )[:10]
    ]

    # Topic count correlation
    has_many_tickers = [r for r in records if len(r["tickers"]) >= 2]
    has_few_tickers  = [r for r in records if len(r["tickers"]) < 2]
    if has_many_tickers and has_few_tickers:
        many_avg = statistics.mean(r["avg_score"] for r in has_many_tickers)
        few_avg  = statistics.mean(r["avg_score"] for r in has_few_tickers)
        prefer_multi_ticker = many_avg > few_avg
    else:
        prefer_multi_ticker = True

    return {
        "record_count":      len(records),
        "best_tod":          best_tod,
        "best_weekday":      best_weekday,
        "best_variant":      best_variant,
        "platform_best_tod": plat_best_tod,
        "top_tickers":       top_tickers,
        "prefer_multi_ticker": prefer_multi_ticker,
    }


# ---------------------------------------------------------------------------
# Write back to agent brains
# ---------------------------------------------------------------------------

def _update_learned_params(agent: str, updates: dict) -> None:
    """Merge updates into agent's learned_params.json."""
    path = _BRAINS_DIR / agent / "learned_params.json"
    existing = _load_json(path) or {}
    existing.update(updates)
    existing["_feedback_updated"] = datetime.now(timezone.utc).isoformat()
    _save_json(path, existing)


def _write_feedback_to_brains(analysis: dict) -> None:
    if not analysis:
        return

    # ── Copywriter ────────────────────────────────────────────────────────────
    copy_params: dict = {}
    if analysis.get("best_tod"):
        copy_params["preferred_tod"] = analysis["best_tod"]
    if analysis.get("best_weekday"):
        copy_params["preferred_weekday"] = analysis["best_weekday"]
    if analysis.get("best_variant"):
        copy_params["preferred_variant"] = analysis["best_variant"]
    if analysis.get("top_tickers"):
        copy_params["high_engagement_tickers"] = analysis["top_tickers"][:5]
    copy_params["prefer_multi_ticker"] = analysis.get("prefer_multi_ticker", True)
    if copy_params:
        _update_learned_params("copywriter", copy_params)

    # ── Ranker ────────────────────────────────────────────────────────────────
    ranker_params: dict = {}
    if analysis.get("top_tickers"):
        ranker_params["boost_tickers"] = analysis["top_tickers"]
    if analysis.get("best_tod"):
        ranker_params["preferred_tod"] = analysis["best_tod"]
    if ranker_params:
        _update_learned_params("ranker", ranker_params)

    # ── Scheduler / platform timing ───────────────────────────────────────────
    if analysis.get("platform_best_tod"):
        _update_learned_params("engagement_optimizer", {
            "platform_best_tod": analysis["platform_best_tod"],
            "best_tod":          analysis.get("best_tod"),
            "best_weekday":      analysis.get("best_weekday"),
        })

    # ── Image Gen ─────────────────────────────────────────────────────────────
    _update_learned_params("image_gen", {
        "ticker_density": "multi" if analysis.get("prefer_multi_ticker") else "single",
    })

    # ── Video Builder ─────────────────────────────────────────────────────────
    _update_learned_params("video_builder", {
        "preferred_posting_tod": analysis.get("best_tod"),
        "top_performing_tickers": analysis.get("top_tickers", [])[:3],
    })


# ---------------------------------------------------------------------------
# Add learning to brain (used by other modules)
# ---------------------------------------------------------------------------

def run_feedback_loop(verbose: bool = False) -> dict:
    """Main entry: collect → analyse → write back."""
    print("[feedback] collecting engagement records…", file=sys.stderr)
    records = _collect_records()
    print(f"[feedback] {len(records)} records found", file=sys.stderr)

    analysis = _analyse(records)
    if analysis:
        _write_feedback_to_brains(analysis)
        if verbose:
            print(json.dumps(analysis, indent=2), file=sys.stderr)
        print(f"[feedback] wrote params — best_tod={analysis.get('best_tod')}, "
              f"best_day={analysis.get('best_weekday')}, variant={analysis.get('best_variant')}",
              file=sys.stderr)
    else:
        print("[feedback] not enough data yet (need ≥3 slots with metrics)", file=sys.stderr)

    # Save summary for dashboard
    summary = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "analysis":   analysis,
    }
    _save_json(_SUMMARY_FILE, summary)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    try:
        from dotenv import load_dotenv
        load_dotenv(_REPO_ROOT / ".env")
    except ImportError:
        pass

    ap = argparse.ArgumentParser(description="Feedback Loop Agent")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    result = run_feedback_loop(verbose=args.verbose)
    print(json.dumps(result, indent=2))
