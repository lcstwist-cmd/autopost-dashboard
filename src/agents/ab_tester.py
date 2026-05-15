"""ab_tester.py — A/B test post variants and declare winners automatically.

Tracks A vs B variant performance across all queue slots. After 24h it
compares available engagement metrics and declares a winner per metric.
Winner signals feed back into the copywriter + reel_writer via brain
learned_params so the system auto-favors the better format.

What it tests:
  - Post length (word count)
  - Hook style (question vs statement vs number)
  - Tone (hype vs professional vs casual)
  - With/without cashtags at the end
  - CTA presence and type

Data sources:
  - queue/*/publish_log.json  — variant label, timestamps
  - queue/*/metrics.json      — impressions, likes, retweets, views
  - queue/*/top2.json         — story tickers, titles
  - queue/*/post_x.txt        — actual post text for feature extraction

Saves results to:
  data/ab_test_results.json   — full history of all A/B comparisons
  analytics/agent_brains/ab_tester/learned_params.json  — winner signals

Called by scheduler every 6 hours.

Usage:
    python src/agents/ab_tester.py
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
_RESULTS_FILE  = _REPO / "data" / "ab_test_results.json"
_BRAIN_PARAMS  = _REPO / "analytics" / "agent_brains" / "ab_tester" / "learned_params.json"

# Minimum hours before we consider a post's metrics "settled"
MIN_AGE_HOURS = 6
# Minimum number of A/B pairs needed for a recommendation to be trusted
MIN_PAIRS = 3


def _load_json(p: Path) -> dict | list | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_json(p: Path, data: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str),
                 encoding="utf-8")


# ── Feature extraction from post text ──────────────────────────────────────

def _extract_features(text: str) -> dict[str, Any]:
    """Extract testable features from post text."""
    words = text.split()
    sentences = re.split(r"[.!?]", text)
    first_sentence = sentences[0].strip() if sentences else ""

    has_question    = "?" in first_sentence
    has_number      = bool(re.search(r"\b\d+[\.,]?\d*\b", first_sentence))
    has_cashtag     = bool(re.search(r"\$[A-Z]{2,}", text))
    has_hashtag     = bool(re.search(r"#\w+", text))
    has_emoji       = bool(re.search(r"[\U0001F300-\U0001FAFF]", text))
    has_cta         = bool(re.search(
        r"\b(follow|like|share|subscribe|click|check|watch|don.t miss)\b",
        text.lower()))

    # Tone detection
    if any(w in text.lower() for w in ["!", "massive", "explod", "moon", "pump", "insane"]):
        tone = "hype"
    elif any(w in text.lower() for w in ["according to", "analysis", "report", "official"]):
        tone = "professional"
    else:
        tone = "neutral"

    # Hook type
    if has_question:
        hook_type = "question"
    elif has_number:
        hook_type = "number"
    elif first_sentence.strip().startswith(("⚠️", "🚨", "BREAKING", "ALERT")):
        hook_type = "urgency"
    else:
        hook_type = "statement"

    return {
        "word_count":    len(words),
        "has_question":  has_question,
        "has_number":    has_number,
        "has_cashtag":   has_cashtag,
        "has_hashtag":   has_hashtag,
        "has_emoji":     has_emoji,
        "has_cta":       has_cta,
        "tone":          tone,
        "hook_type":     hook_type,
    }


# ── Engagement score ────────────────────────────────────────────────────────

def _eng_score(metrics: dict[str, Any]) -> float | None:
    """Compute composite engagement score from metrics dict."""
    if not metrics:
        return None

    score = 0.0
    has_data = False

    # X metrics
    x = metrics.get("x", {})
    if x:
        imp  = float(x.get("impressions", 0) or 0)
        likes = float(x.get("likes", 0) or 0)
        rt    = float(x.get("retweets", 0) or 0)
        if imp > 0:
            score += (likes * 3 + rt * 5) / imp * 1000
            has_data = True

    # Telegram
    tg = metrics.get("telegram", {})
    if tg:
        views = float(tg.get("views", 0) or 0)
        fwd   = float(tg.get("forwards", 0) or 0)
        if views > 0:
            score += views / 100 + fwd * 5
            has_data = True

    # Instagram
    ig = metrics.get("instagram", {})
    if ig:
        reach = float(ig.get("reach", 0) or 0)
        likes = float(ig.get("likes", 0) or 0)
        if reach > 0:
            score += likes / reach * 500
            has_data = True

    return round(score, 3) if has_data else None


# ── Slot record builder ─────────────────────────────────────────────────────

def _collect_ab_records(queue_root: Path) -> list[dict[str, Any]]:
    """Collect all slot records that have variant + metrics data."""
    records: list[dict[str, Any]] = []

    for log_path in sorted(queue_root.rglob("publish_log.json")):
        log = _load_json(log_path)
        if not log:
            continue

        variant = log.get("variant")
        if variant not in ("A", "B"):
            continue

        slot_dir  = log_path.parent
        slot_name = slot_dir.name
        timestamp = log.get("timestamp", "")

        # Only include settled results
        if timestamp:
            try:
                ts = datetime.fromisoformat(timestamp)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
                if age_h < MIN_AGE_HOURS:
                    continue
            except Exception:
                pass

        metrics = _load_json(slot_dir / "metrics.json") or {}
        metrics = {k: v for k, v in metrics.items() if not k.startswith("_")}
        score   = _eng_score(metrics)

        # Post text features
        post_file = slot_dir / "post_x.txt"
        features: dict[str, Any] = {}
        if post_file.exists():
            text = post_file.read_text(encoding="utf-8", errors="replace")
            features = _extract_features(text)

        # Success indicator (published ok)
        any_ok = any(
            r.get("status") == "ok"
            for r in log.get("results", {}).values()
            if isinstance(r, dict)
        )

        # Date from slot name
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", slot_name)
        date_str = m.group(1) if m else ""

        records.append({
            "slot_name": slot_name,
            "date":      date_str,
            "variant":   variant,
            "timestamp": timestamp,
            "score":     score,
            "published": any_ok,
            "features":  features,
            "metrics":   metrics,
        })

    return records


# ── Comparison engine ───────────────────────────────────────────────────────

def _compare_variants(records: list[dict]) -> dict[str, Any]:
    """Compare A vs B across all available dimensions."""
    a_records = [r for r in records if r["variant"] == "A"]
    b_records = [r for r in records if r["variant"] == "B"]

    # Overall engagement score comparison
    a_scores = [r["score"] for r in a_records if r["score"] is not None]
    b_scores = [r["score"] for r in b_records if r["score"] is not None]

    a_mean = round(sum(a_scores) / len(a_scores), 3) if a_scores else None
    b_mean = round(sum(b_scores) / len(b_scores), 3) if b_scores else None

    score_winner = None
    if a_mean is not None and b_mean is not None:
        if abs(a_mean - b_mean) < 0.5:
            score_winner = "tie"
        else:
            score_winner = "A" if a_mean > b_mean else "B"

    # Publish success rate
    a_pub_rate = round(sum(r["published"] for r in a_records) / max(1, len(a_records)) * 100, 1)
    b_pub_rate = round(sum(r["published"] for r in b_records) / max(1, len(b_records)) * 100, 1)

    # Feature analysis: which features correlate with higher score
    feature_winners: dict[str, str] = {}
    feature_keys = ["has_question", "has_number", "has_cashtag", "has_emoji", "has_cta"]

    for feat in feature_keys:
        with_feat    = [r["score"] for r in records if r["features"].get(feat) and r["score"] is not None]
        without_feat = [r["score"] for r in records if not r["features"].get(feat) and r["score"] is not None]
        if len(with_feat) >= 2 and len(without_feat) >= 2:
            wf_mean = sum(with_feat) / len(with_feat)
            wo_mean = sum(without_feat) / len(without_feat)
            if abs(wf_mean - wo_mean) > 0.3:
                feature_winners[feat] = "yes" if wf_mean > wo_mean else "no"

    # Hook type analysis
    hook_scores: dict[str, list[float]] = defaultdict(list)
    for r in records:
        ht = r["features"].get("hook_type")
        if ht and r["score"] is not None:
            hook_scores[ht].append(r["score"])

    best_hook = None
    if hook_scores:
        best_hook = max(hook_scores, key=lambda h: sum(hook_scores[h]) / max(1, len(hook_scores[h])))

    # Tone analysis
    tone_scores: dict[str, list[float]] = defaultdict(list)
    for r in records:
        tone = r["features"].get("tone")
        if tone and r["score"] is not None:
            tone_scores[tone].append(r["score"])

    best_tone = None
    if tone_scores:
        best_tone = max(tone_scores, key=lambda t: sum(tone_scores[t]) / max(1, len(tone_scores[t])))

    return {
        "total_records":   len(records),
        "a_count":         len(a_records),
        "b_count":         len(b_records),
        "a_mean_score":    a_mean,
        "b_mean_score":    b_mean,
        "score_winner":    score_winner,
        "a_publish_rate":  a_pub_rate,
        "b_publish_rate":  b_pub_rate,
        "feature_winners": feature_winners,
        "best_hook_type":  best_hook,
        "best_tone":       best_tone,
        "hook_scores":     {h: round(sum(s)/len(s), 3) for h, s in hook_scores.items()},
        "tone_scores":     {t: round(sum(s)/len(s), 3) for t, s in tone_scores.items()},
    }


# ── Recommendation builder ──────────────────────────────────────────────────

def _build_recommendations(comparison: dict) -> list[str]:
    """Turn comparison data into actionable recommendations."""
    recs: list[str] = []

    winner = comparison.get("score_winner")
    a_n    = comparison.get("a_count", 0)
    b_n    = comparison.get("b_count", 0)

    if winner and winner != "tie" and min(a_n, b_n) >= MIN_PAIRS:
        a_s = comparison.get("a_mean_score")
        b_s = comparison.get("b_mean_score")
        recs.append(
            f"Variant {winner} wins engagement: A={a_s} vs B={b_s} "
            f"(based on {a_n + b_n} posts)"
        )
    elif winner == "tie":
        recs.append(f"A and B are tied — keep testing ({a_n + b_n} posts so far)")

    best_hook = comparison.get("best_hook_type")
    if best_hook:
        hook_scores = comparison.get("hook_scores", {})
        score = hook_scores.get(best_hook, 0)
        recs.append(f"Best hook type: '{best_hook}' (avg score: {score})")

    best_tone = comparison.get("best_tone")
    if best_tone:
        tone_scores = comparison.get("tone_scores", {})
        score = tone_scores.get(best_tone, 0)
        recs.append(f"Best tone: '{best_tone}' (avg score: {score})")

    feat_w = comparison.get("feature_winners", {})
    if feat_w.get("has_cashtag") == "yes":
        recs.append("Cashtags ($BTC etc.) increase engagement — always include them")
    elif feat_w.get("has_cashtag") == "no":
        recs.append("Posts without cashtags perform better — use fewer")

    if feat_w.get("has_question") == "yes":
        recs.append("Opening with a question boosts engagement")

    if feat_w.get("has_emoji") == "yes":
        recs.append("Emojis improve performance — keep using them")

    if feat_w.get("has_cta") == "yes":
        recs.append("Including a CTA improves results")

    if not recs:
        recs.append(f"Need more data — {comparison.get('total_records', 0)} records so far (need {MIN_PAIRS * 2})")

    return recs


# ── Brain params writer ─────────────────────────────────────────────────────

def _save_brain_params(comparison: dict, recommendations: list[str]) -> None:
    """Write learned signals to brain params for other agents to read."""
    params: dict[str, Any] = {"_updated": datetime.now(timezone.utc).isoformat()}

    winner = comparison.get("score_winner")
    if winner and winner != "tie":
        params["preferred_variant"] = winner
        params["variant_scores"] = {
            "A": comparison.get("a_mean_score"),
            "B": comparison.get("b_mean_score"),
        }

    if comparison.get("best_hook_type"):
        params["best_hook_type"] = comparison["best_hook_type"]
        params["hook_scores"]    = comparison.get("hook_scores", {})

    if comparison.get("best_tone"):
        params["best_tone"]  = comparison["best_tone"]
        params["tone_scores"] = comparison.get("tone_scores", {})

    feat_w = comparison.get("feature_winners", {})
    params["feature_signals"] = feat_w
    params["use_cashtags"]    = feat_w.get("has_cashtag") == "yes"
    params["use_questions"]   = feat_w.get("has_question") == "yes"
    params["use_cta"]         = feat_w.get("has_cta") == "yes"
    params["recommendations"] = recommendations

    _BRAIN_PARAMS.parent.mkdir(parents=True, exist_ok=True)
    _BRAIN_PARAMS.write_text(json.dumps(params, indent=2, ensure_ascii=False),
                             encoding="utf-8")


# ── Main entry point ────────────────────────────────────────────────────────

def run_ab_test(queue_root: Path | None = None) -> dict[str, Any]:
    """Run full A/B analysis. Returns results dict."""
    import os
    if queue_root is None:
        queue_root = Path(os.environ.get("AUTOPOST_QUEUE", str(_REPO / "queue")))

    print("[ab_tester] scanning queue directories...")
    records    = _collect_ab_records(queue_root)

    # Also scan root queue if user sub-dir has no data
    if len(records) < 3:
        root_records = _collect_ab_records(_REPO / "queue")
        seen = {r["slot_name"] for r in records}
        records += [r for r in root_records if r["slot_name"] not in seen]

    print(f"[ab_tester] {len(records)} settled records (A={sum(1 for r in records if r['variant']=='A')}, "
          f"B={sum(1 for r in records if r['variant']=='B')})")

    comparison    = _compare_variants(records)
    recommendations = _build_recommendations(comparison)
    _save_brain_params(comparison, recommendations)

    results = {
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "comparison":     comparison,
        "recommendations": recommendations,
        "records_count":  len(records),
    }

    _save_json(_RESULTS_FILE, results)
    print(f"[ab_tester] saved -> {_RESULTS_FILE}")
    for r in recommendations:
        r_safe = r.encode("ascii", "replace").decode("ascii")
        print(f"  -> {r_safe}")

    return results


def get_results(max_age_seconds: int = 21600) -> dict[str, Any]:
    """Return cached results (refresh if older than max_age_seconds)."""
    if _RESULTS_FILE.exists():
        try:
            data = json.loads(_RESULTS_FILE.read_text(encoding="utf-8"))
            ts   = datetime.fromisoformat(data.get("generated_at", "2000-01-01T00:00:00+00:00"))
            age  = (datetime.now(timezone.utc) - ts).total_seconds()
            if age < max_age_seconds:
                return data
        except Exception:
            pass
    return run_ab_test()


def get_winner() -> str | None:
    """Quick helper: return preferred variant from cache (None if no clear winner)."""
    try:
        results = get_results()
        winner = results.get("comparison", {}).get("score_winner")
        return winner if winner not in (None, "tie") else None
    except Exception:
        return None


if __name__ == "__main__":
    sys.path.insert(0, str(_REPO))
    results = run_ab_test()
    comp = results["comparison"]
    print(f"\nA: {comp['a_count']} posts, mean score={comp['a_mean_score']}")
    print(f"B: {comp['b_count']} posts, mean score={comp['b_mean_score']}")
    print(f"Winner: {comp['score_winner'] or 'not enough data'}")
    if comp.get("best_hook_type"):
        print(f"Best hook: {comp['best_hook_type']}")
    if comp.get("best_tone"):
        print(f"Best tone: {comp['best_tone']}")
