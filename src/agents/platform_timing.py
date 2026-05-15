"""Platform Timing Agent — tracks and predicts optimal posting hours per platform.

Combines three data sources:
  1. Real engagement data from our own post history (feedback_loop records)
  2. Research-based optimal times (from industry studies, updated quarterly)
  3. Market pulse signals (fear&greed, trending coins affect optimal timing)

Outputs `data/platform_timing.json` with:
  - best_hours: list of recommended UTC hours per platform
  - best_days: list of recommended weekdays
  - heatmap: 7x24 grid of engagement scores for visualization
  - confidence: 0–1 based on how many real data points we have
  - next_best_slot: absolute datetime for next optimal post per platform

Used by:
  - Dashboard backoffice (timing visualization)
  - Scheduler (optional adaptive timing)
  - Copywriter brain (schedule-aware CTAs like "dropping tomorrow")

CLI:
    python src/agents/platform_timing.py
    python src/agents/platform_timing.py --json   # print full JSON
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_HERE       = Path(__file__).resolve().parent
_REPO_ROOT  = _HERE.parent.parent
_QUEUE_ROOT = _REPO_ROOT / "queue"
_DATA_DIR   = _REPO_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_TIMING_FILE  = _DATA_DIR / "platform_timing.json"
_PULSE_FILE   = _DATA_DIR / "market_pulse.json"

PLATFORMS = ["youtube", "tiktok", "instagram", "x", "telegram"]

# ── Research-based baseline (weighted: study consensus) ───────────────────────
# Values: engagement index 0–10 per hour (0=UTC for universal comparison)
# Sources: Later, Sprout Social, HubSpot, Buffer 2024-2025 studies combined

_RESEARCH_HEATMAP: dict[str, list[int]] = {
    # hour:  0  1  2  3  4  5  6  7  8  9 10 11 12 13 14 15 16 17 18 19 20 21 22 23
    "youtube":   [1, 0, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 7, 6, 8, 9,10, 9, 8, 7, 6, 5, 4, 2],
    "tiktok":    [3, 2, 1, 1, 2, 3, 7, 8, 6, 5, 7, 6, 7, 5, 5, 5, 5, 6, 7, 9,10, 9, 8, 6],
    "instagram": [1, 1, 0, 0, 0, 1, 3, 5, 6, 8, 9, 9, 7, 6, 7, 6, 5, 6, 7, 8, 7, 6, 5, 3],
    "x":         [2, 1, 1, 1, 2, 3, 5, 7, 9,10, 9, 8, 8, 8, 8, 7, 7, 8, 7, 6, 5, 5, 4, 3],
    "telegram":  [1, 1, 0, 0, 0, 2, 5, 8,10, 9, 7, 6, 6, 5, 5, 5, 5, 7, 9,10, 9, 8, 7, 5],
}

_RESEARCH_WEEKDAYS: dict[str, list[str]] = {
    "youtube":   ["Friday", "Saturday", "Sunday", "Thursday"],
    "tiktok":    ["Tuesday", "Wednesday", "Thursday", "Friday"],
    "instagram": ["Monday", "Tuesday", "Wednesday", "Thursday"],
    "x":         ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
    "telegram":  ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
}

_WEEKDAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _load_json(p: Path) -> Any:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Collect real engagement data per platform + hour
# ---------------------------------------------------------------------------

def _collect_platform_hour_data() -> dict[str, dict[str, list[float]]]:
    """Walk queue and collect {platform: {hour: [scores]}}."""
    data: dict[str, dict[str, list[float]]] = {p: defaultdict(list) for p in PLATFORMS}

    for log_path in sorted(_QUEUE_ROOT.rglob("publish_log.json")):
        log = _load_json(log_path)
        if not log:
            continue
        slot_dir = log_path.parent
        metrics  = _load_json(slot_dir / "metrics.json") or {}
        ts       = log.get("timestamp") or ""
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            hour = dt.hour
            weekday = dt.strftime("%A")
        except Exception:
            continue

        for plat in PLATFORMS:
            m = metrics.get(plat, {})
            if not m:
                continue
            score = _compute_score(m, plat)
            if score is not None:
                data[plat][str(hour)].append(score)

    return data


def _compute_score(m: dict, platform: str) -> float | None:
    try:
        if platform == "x":
            imp = float(m.get("impressions") or 0)
            return round((float(m.get("likes") or 0) + float(m.get("retweets") or 0) * 2) / max(imp, 1) * 1000, 2)
        if platform == "telegram":
            return round(min((float(m.get("views") or 0) + float(m.get("forwards") or 0) * 5) / 100, 100), 2)
        if platform == "instagram":
            reach = float(m.get("reach") or 0)
            return round((float(m.get("likes") or 0) + float(m.get("saves") or 0) * 3) / max(reach, 1) * 1000, 2)
        if platform in ("tiktok", "youtube"):
            views = float(m.get("views") or 0)
            return round((float(m.get("likes") or 0) + float(m.get("shares") or 0) * 3) / max(views, 1) * 1000, 2)
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Build timing output
# ---------------------------------------------------------------------------

def _build_platform_timing(real_data: dict[str, dict[str, list[float]]]) -> dict:
    result = {}
    now_utc = datetime.now(timezone.utc)

    for plat in PLATFORMS:
        research_hours = _RESEARCH_HEATMAP[plat]
        real_hours = real_data.get(plat, {})
        n_real = sum(len(v) for v in real_hours.values())

        # Blend research baseline with real data (real data weight grows with sample count)
        blend_weight = min(n_real / 20.0, 0.7)  # max 70% real data weight
        blended = []
        for h in range(24):
            research_score = research_hours[h]
            real_scores    = real_hours.get(str(h), [])
            real_avg       = statistics.mean(real_scores) * 10 if real_scores else research_score
            blended_score  = round(research_score * (1 - blend_weight) + real_avg * blend_weight, 2)
            blended.append(blended_score)

        # Best hours = top 5 from blended heatmap
        indexed = sorted(enumerate(blended), key=lambda x: x[1], reverse=True)
        best_hours = sorted([h for h, _ in indexed[:5]])

        # Build 7x24 heatmap (days × hours) — just research-based for now
        days_weights = {d: 1.0 for d in _WEEKDAY_ORDER}
        for i, day in enumerate(_RESEARCH_WEEKDAYS.get(plat, [])):
            if day in days_weights:
                days_weights[day] = 1.0 + (4 - i) * 0.15
        heatmap = []
        for day in _WEEKDAY_ORDER:
            w = days_weights[day]
            heatmap.append([round(h * w, 1) for h in research_hours])

        # Next best slot
        next_slot = None
        for delta_h in range(0, 48):
            candidate = now_utc + timedelta(hours=delta_h)
            if candidate.hour in best_hours:
                day_name = candidate.strftime("%A")
                if day_name in _RESEARCH_WEEKDAYS.get(plat, _WEEKDAY_ORDER):
                    next_slot = candidate.isoformat()
                    break
        if not next_slot:
            # Fallback: just next occurrence of first best hour
            for delta_h in range(0, 48):
                candidate = now_utc + timedelta(hours=delta_h)
                if candidate.hour == best_hours[0]:
                    next_slot = candidate.isoformat()
                    break

        result[plat] = {
            "best_hours":     best_hours,
            "best_days":      _RESEARCH_WEEKDAYS.get(plat, []),
            "heatmap_24h":    blended,
            "heatmap_7x24":   heatmap,
            "next_best_slot": next_slot,
            "confidence":     round(min(n_real / 20.0, 1.0), 2),
            "n_real_samples": n_real,
        }

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_platform_timing() -> dict:
    real_data = _collect_platform_hour_data()
    timing    = _build_platform_timing(real_data)

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "platforms":  timing,
        "note": "Hours are UTC. Add your timezone offset to get local times.",
    }
    _TIMING_FILE.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print("[timing] platform_timing.json updated", file=sys.stderr)
    return output


def get_platform_timing() -> dict:
    """Read cached timing. Recomputes if missing or >4h old."""
    import time
    if _TIMING_FILE.exists():
        try:
            data = json.loads(_TIMING_FILE.read_text(encoding="utf-8"))
            age = time.time() - datetime.fromisoformat(data.get("updated_at", "2000-01-01")).timestamp()
            if age < 4 * 3600:
                return data
        except Exception:
            pass
    return run_platform_timing()


def format_timing_summary() -> str:
    """Human-readable summary for dashboard/log display."""
    data = get_platform_timing()
    lines = ["Platform optimal posting hours (UTC):"]
    for plat, info in data.get("platforms", {}).items():
        hours_str = ", ".join(f"{h:02d}:00" for h in info["best_hours"])
        days_str  = ", ".join(info["best_days"][:3])
        conf      = f"{info['confidence']*100:.0f}%"
        lines.append(f"  {plat.upper():10} {hours_str}  |  {days_str}  (conf: {conf})")
    return "\n".join(lines)


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

    ap = __import__("argparse").ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()

    result = run_platform_timing()
    if args.json:
        print(json.dumps(result, indent=2))
    elif args.summary or True:
        print(format_timing_summary())
