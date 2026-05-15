"""engagement_optimizer.py — Analyzes post history and recommends optimal timing/format.

Reads all queue/*/publish_log.json + metrics.json files to identify:
- Best posting time slots (morning vs evening, by day of week)
- Best performing content styles (A/B variant, tickers, tone)
- Platform-specific engagement patterns
- Recommended next slot adjustments

Saves insights to data/engagement_insights.json.
Called by scheduler every 6 hours.

Usage:
    python src/agents/engagement_optimizer.py
"""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
_INSIGHTS_FILE = _REPO / "data" / "engagement_insights.json"
_BRAIN_PARAMS   = _REPO / "analytics" / "agent_brains" / "engagement_optimizer" / "learned_params.json"


def _load_brain_params() -> dict[str, Any]:
    if _BRAIN_PARAMS.exists():
        try:
            return json.loads(_BRAIN_PARAMS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

PLATFORMS = ["telegram", "x", "instagram", "tiktok", "youtube"]

# Minimum engagement records needed before we trust a recommendation
MIN_SAMPLES = 3


def _load_json(p: Path) -> dict | list | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _collect_records(queue_root: Path) -> list[dict[str, Any]]:
    """Walk all queue dirs (including user sub-dirs) and collect post records."""
    records: list[dict[str, Any]] = []

    for log_path in sorted(queue_root.rglob("publish_log.json")):
        log = _load_json(log_path)
        if not log:
            continue

        slot_dir = log_path.parent
        slot_name = slot_dir.name

        # Parse date and time-of-day from slot name
        m = re.match(r"^(\d{4}-\d{2}-\d{2})_(morning|evening)$", slot_name)
        if m:
            date_str, tod = m.group(1), m.group(2)
            slot_type = "scheduled"
        else:
            # manual / user pipeline slots — extract date
            m2 = re.match(r"^(\d{4}-\d{2}-\d{2})_", slot_name)
            date_str = m2.group(1) if m2 else None
            tod = "manual"
            slot_type = "manual"

        if not date_str:
            continue

        try:
            dt = datetime.fromisoformat(date_str)
            weekday = dt.strftime("%A")  # Monday, Tuesday, ...
        except Exception:
            weekday = "Unknown"

        # Engagement metrics
        metrics = _load_json(slot_dir / "metrics.json") or {}
        metrics = {k: v for k, v in metrics.items() if not k.startswith("_")}

        # Top story info
        top2 = _load_json(slot_dir / "top2.json") or {}
        story = ((top2.get("stories") or [{}])[0]) if isinstance(top2, dict) else {}
        tickers = story.get("tickers", []) or []

        results = log.get("results", {})
        variant = log.get("variant", "?")
        timestamp = log.get("timestamp", "")

        records.append({
            "slot_name":  slot_name,
            "date":       date_str,
            "weekday":    weekday,
            "tod":        tod,           # morning / evening / manual
            "slot_type":  slot_type,
            "variant":    variant,
            "tickers":    tickers,
            "results":    results,
            "metrics":    metrics,
            "timestamp":  timestamp,
        })

    return records


def _engagement_score(metrics: dict[str, Any], platform: str) -> float | None:
    """Compute a normalized engagement score (0–100) for a platform."""
    m = metrics.get(platform, {})
    if not m:
        return None
    if platform == "x":
        impressions = float(m.get("impressions", 0) or 0)
        likes = float(m.get("likes", 0) or 0)
        retweets = float(m.get("retweets", 0) or 0)
        if impressions == 0:
            return None
        eng_rate = (likes + retweets * 2) / impressions * 100
        return round(min(eng_rate * 10, 100), 2)   # scale to 0–100
    if platform == "telegram":
        views = float(m.get("views", 0) or 0)
        fwd   = float(m.get("forwards", 0) or 0)
        if views == 0:
            return None
        return round(min((views + fwd * 5) / 100, 100), 2)
    if platform == "instagram":
        reach = float(m.get("reach", 0) or 0)
        likes = float(m.get("likes", 0) or 0)
        if reach == 0:
            return None
        return round(min((likes / reach) * 1000, 100), 2)
    if platform in ("tiktok", "youtube"):
        views = float(m.get("views", 0) or 0)
        likes = float(m.get("likes", 0) or 0)
        if views == 0:
            return None
        return round(min((likes / views) * 1000, 100), 2)
    return None


def _best_slot_time(records: list[dict]) -> dict[str, Any]:
    """Determine if morning or evening performs better overall."""
    tod_scores: dict[str, list[float]] = defaultdict(list)

    for r in records:
        tod = r["tod"]
        if tod == "manual":
            continue
        for plat in PLATFORMS:
            score = _engagement_score(r["metrics"], plat)
            if score is not None:
                tod_scores[tod].append(score)

    result: dict[str, Any] = {}
    for tod, scores in tod_scores.items():
        if scores:
            result[tod] = {"count": len(scores), "avg_score": round(statistics.mean(scores), 2)}

    best = None
    if "morning" in result and "evening" in result:
        best = "morning" if result["morning"]["avg_score"] >= result["evening"]["avg_score"] else "evening"
    elif result:
        best = next(iter(result))

    return {"by_tod": result, "recommended": best}


def _best_weekday(records: list[dict]) -> dict[str, Any]:
    """Find which days of the week get highest engagement."""
    day_scores: dict[str, list[float]] = defaultdict(list)

    for r in records:
        for plat in PLATFORMS:
            score = _engagement_score(r["metrics"], plat)
            if score is not None:
                day_scores[r["weekday"]].append(score)

    ranked = sorted(
        [(day, round(statistics.mean(s), 2), len(s)) for day, s in day_scores.items() if s],
        key=lambda x: x[1], reverse=True
    )
    return {
        "ranked": [{"day": d, "avg_score": s, "samples": n} for d, s, n in ranked],
        "best":   ranked[0][0] if ranked else None,
    }


def _ab_analysis(records: list[dict]) -> dict[str, Any]:
    """Compare A vs B variant engagement."""
    ab: dict[str, list[float]] = {"A": [], "B": []}

    for r in records:
        v = r["variant"]
        if v not in ("A", "B"):
            continue
        for plat in PLATFORMS:
            score = _engagement_score(r["metrics"], plat)
            if score is not None:
                ab[v].append(score)

    summary: dict[str, Any] = {}
    for v, scores in ab.items():
        summary[v] = {
            "count":     len(scores),
            "avg_score": round(statistics.mean(scores), 2) if scores else None,
        }

    winner = None
    if summary.get("A", {}).get("avg_score") and summary.get("B", {}).get("avg_score"):
        a_s = summary["A"]["avg_score"]
        b_s = summary["B"]["avg_score"]
        if abs(a_s - b_s) < 2:
            winner = "tie"
        else:
            winner = "A" if a_s > b_s else "B"

    return {"variants": summary, "winner": winner}


def _top_tickers(records: list[dict]) -> list[dict]:
    """Find which tickers appear most in successful posts."""
    ticker_ok: Counter[str] = Counter()
    ticker_total: Counter[str] = Counter()

    for r in records:
        has_success = any(
            res.get("status") == "ok" for res in r["results"].values()
        )
        for t in r["tickers"]:
            ticker_total[t] += 1
            if has_success:
                ticker_ok[t] += 1

    result = []
    for ticker, total in ticker_total.most_common(15):
        ok_rate = round(ticker_ok[ticker] / total * 100, 1) if total else 0
        result.append({"ticker": ticker, "total": total, "ok_rate": ok_rate})
    return result


def _platform_success_rates(records: list[dict]) -> dict[str, Any]:
    """Success rate per platform."""
    counts: dict[str, Counter[str]] = {p: Counter() for p in PLATFORMS}

    for r in records:
        for plat in PLATFORMS:
            res = r["results"].get(plat, {})
            if not res:
                counts[plat]["missing"] += 1
            else:
                counts[plat][res.get("status", "unknown")] += 1

    out: dict[str, Any] = {}
    for plat, c in counts.items():
        total = sum(c.values())
        if total == 0:
            continue
        ok = c.get("ok", 0)
        out[plat] = {
            "ok":       ok,
            "error":    c.get("error", 0),
            "missing":  c.get("missing", 0),
            "success_rate": round(ok / total * 100, 1),
        }
    return out


def _generate_recommendations(
    slot_time: dict,
    weekday: dict,
    ab: dict,
    success_rates: dict,
) -> list[str]:
    """Produce human-readable recommendations, enriched by brain learned_params."""
    recs: list[str] = []
    brain = _load_brain_params()

    # Brain-informed best time (overrides computed if brain has more data)
    brain_tod = brain.get("best_tod")
    if brain_tod:
        tod_rates = brain.get("tod_success_rates", {})
        rate = round(tod_rates.get(brain_tod, 0) * 100)
        recs.append(f"Post in the {brain_tod} — brain-confirmed {rate}% success rate")
    elif slot_time.get("recommended"):
        best = slot_time["recommended"]
        tod_data = slot_time.get("by_tod", {}).get(best, {})
        samples = tod_data.get("count", 0)
        if samples >= MIN_SAMPLES:
            recs.append(f"Post in the {best} — higher engagement on average ({samples} samples)")

    if weekday.get("best"):
        ranked = weekday.get("ranked", [])
        if ranked and ranked[0].get("samples", 0) >= MIN_SAMPLES:
            recs.append(f"Best day: {weekday['best']} (avg score: {ranked[0]['avg_score']})")

    # Brain-informed variant preference
    brain_variant = brain.get("preferred_variant")
    ab_winner = ab.get("winner")
    if brain_variant and brain_variant in ("A", "B"):
        var_stats = brain.get("variant_success", {})
        va, vb = var_stats.get("A", 0), var_stats.get("B", 0)
        recs.append(f"Use variant {brain_variant} — brain learned: A={va}% B={vb}% success")
    elif ab_winner and ab_winner != "tie":
        v_data = ab.get("variants", {}).get(ab_winner, {})
        n = v_data.get("count", 0)
        if n >= MIN_SAMPLES:
            recs.append(f"Use variant {ab_winner} — outperforms the other (n={n})")

    # Brain-informed top platform
    top_plat = brain.get("top_performing_platform")
    if top_plat:
        plat_rates = brain.get("platform_success_rates", {})
        rate = plat_rates.get(top_plat, 0)
        recs.append(f"Focus on {top_plat.upper()} — brain best platform ({rate}% success)")

    for plat, data in success_rates.items():
        rate = data.get("success_rate", 100)
        if rate < 50 and data.get("ok", 0) + data.get("error", 0) >= 3:
            recs.append(f"{plat.upper()} success rate low ({rate}%) — check credentials or rate limits")

    if not recs:
        recs.append("Not enough data yet — keep posting to build engagement history")

    return recs


def run_optimization(queue_root: Path | None = None) -> dict[str, Any]:
    """Main entry: analyze all post history and return insights dict."""
    if queue_root is None:
        queue_root = Path(os.environ.get("AUTOPOST_QUEUE", str(_REPO / "queue")))

    records = _collect_records(queue_root)

    # Also scan parent queue root if we're in a user sub-dir
    if not records:
        records = _collect_records(_REPO / "queue")

    slot_time   = _best_slot_time(records)
    weekday     = _best_weekday(records)
    ab          = _ab_analysis(records)
    tickers     = _top_tickers(records)
    success_rates = _platform_success_rates(records)
    recs        = _generate_recommendations(slot_time, weekday, ab, success_rates)

    insights = {
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "total_records":    len(records),
        "best_slot_time":   slot_time,
        "best_weekday":     weekday,
        "ab_analysis":      ab,
        "top_tickers":      tickers,
        "platform_success": success_rates,
        "recommendations":  recs,
    }

    (_REPO / "data").mkdir(parents=True, exist_ok=True)
    _INSIGHTS_FILE.write_text(
        json.dumps(insights, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    print(f"[engagement] analyzed {len(records)} records -> {_INSIGHTS_FILE}")
    for r in recs:
        r_safe = r.encode("ascii", "replace").decode("ascii")
        print(f"  -> {r_safe}")

    return insights


def get_insights() -> dict[str, Any]:
    """Return cached insights (run analysis if no cache)."""
    if _INSIGHTS_FILE.exists():
        try:
            data = json.loads(_INSIGHTS_FILE.read_text(encoding="utf-8"))
            # Refresh if older than 6 hours
            ts = datetime.fromisoformat(data.get("generated_at", "2000-01-01T00:00:00+00:00"))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age < 6 * 3600:
                return data
        except Exception:
            pass
    return run_optimization()


if __name__ == "__main__":
    sys.path.insert(0, str(_REPO))
    insights = run_optimization()
    print(f"\nTop tickers: {[t['ticker'] for t in insights['top_tickers'][:5]]}")
    print(f"Recommendations:")
    for r in insights["recommendations"]:
        print(f"  - {r}")
