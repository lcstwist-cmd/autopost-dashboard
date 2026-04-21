"""Analytics — Agent 7 of the Crypto AutoPost pipeline.

Reads all queue/*/publish_log.json + optional metrics.json and produces:
    analytics/rollup.json   — machine-readable snapshot
    analytics/rollup.md     — human-readable summary

Inputs per slot folder (queue/<date>_<slot>/):
    publish_log.json         (required)   — written by publisher.py
    metrics.json             (optional)   — { "x": {"impressions": 1234,
                                              "likes": 12, "retweets": 3},
                                              "telegram": {"views": 800},
                                              "tiktok": {...}, etc. }
                                           — populated manually, via a Make.com
                                             inbound scenario, or a future X API
                                             poller.

What this agent computes:
    * total posts per day / slot
    * success rate (ok vs error vs blocked) per platform
    * A/B variant distribution and — if metrics.json exists — per-variant perf:
        - avg impressions, likes, retweets for X
        - winner variant per metric
    * top performing kickers and moods (from news_card params)

Usage:
    python src/agents/analytics.py
    python src/agents/analytics.py --queue-root queue --out-dir analytics
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# --- Loading ---------------------------------------------------------------

def _load_json(p: Path) -> dict | list | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def collect_slots(queue_root: Path) -> list[dict[str, Any]]:
    """Walk queue/* directories and collect slot records."""
    if not queue_root.is_dir():
        return []

    slots: list[dict[str, Any]] = []
    for entry in sorted(queue_root.iterdir()):
        if not entry.is_dir():
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2})_(morning|evening)$", entry.name)
        if not m:
            continue
        date, slot = m.group(1), m.group(2)

        log = _load_json(entry / "publish_log.json")
        if log is None:
            # allow dry-run logs too for visibility
            log = _load_json(entry / "publish_log_dryrun.json")

        top2 = _load_json(entry / "top2.json") or {}
        story = (top2.get("stories") or [{}])[0]
        raw_metrics = _load_json(entry / "metrics.json") or {}
        # Strip metadata keys; keep only platform sub-dicts
        metrics = {k: v for k, v in raw_metrics.items() if not k.startswith("_")}

        slots.append({
            "date":     date,
            "slot":     slot,
            "dir":      str(entry),
            "title":    story.get("title", ""),
            "url":      story.get("url", ""),
            "tickers":  story.get("tickers", []) or [],
            "log":      log,
            "metrics":  metrics,
        })
    return slots


# --- Aggregation -----------------------------------------------------------

def _platform_status(log: dict | None, platform: str) -> str:
    """Return 'ok', 'dry_run', 'error', 'blocked', or 'missing'."""
    if not log:
        return "missing"
    if log.get("status") == "blocked":
        return "blocked"
    res = (log.get("results") or {}).get(platform)
    if not res:
        return "missing"
    return res.get("status", "missing")


def aggregate(slots: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(slots)
    by_day: Counter[str] = Counter()
    by_variant: Counter[str] = Counter()
    by_platform_status: dict[str, Counter[str]] = {
        "telegram": Counter(), "x": Counter()
    }
    ticker_counter: Counter[str] = Counter()

    # A/B performance buckets
    ab_x_metrics: dict[str, list[dict[str, int]]] = {"A": [], "B": []}

    for s in slots:
        by_day[s["date"]] += 1
        log = s["log"] or {}
        variant = log.get("variant") or "?"
        by_variant[variant] += 1
        for plat in ("telegram", "x"):
            by_platform_status[plat][_platform_status(log, plat)] += 1
        for t in s["tickers"]:
            ticker_counter[t] += 1

        # Attach any X metrics to the corresponding variant
        mx = (s["metrics"] or {}).get("x") or {}
        if variant in ("A", "B") and mx:
            ab_x_metrics[variant].append({
                "impressions": int(float(mx.get("impressions") or 0)),
                "likes":       int(float(mx.get("likes") or 0)),
                "retweets":    int(float(mx.get("retweets") or 0)),
            })

    # A/B comparison
    ab_summary: dict[str, Any] = {}
    for key in ("impressions", "likes", "retweets"):
        a_vals = [m[key] for m in ab_x_metrics["A"] if m[key] >= 0]
        b_vals = [m[key] for m in ab_x_metrics["B"] if m[key] >= 0]
        a_mean = statistics.mean(a_vals) if a_vals else None
        b_mean = statistics.mean(b_vals) if b_vals else None
        winner = None
        if a_mean is not None and b_mean is not None:
            winner = "A" if a_mean > b_mean else ("B" if b_mean > a_mean else "tie")
        ab_summary[key] = {
            "A_mean":   a_mean,
            "B_mean":   b_mean,
            "A_count":  len(a_vals),
            "B_count":  len(b_vals),
            "winner":   winner,
        }

    return {
        "generated_at":        datetime.now(timezone.utc).isoformat(),
        "total_slots":         total,
        "by_day":              dict(by_day),
        "by_variant":          dict(by_variant),
        "by_platform_status":  {p: dict(c) for p, c in by_platform_status.items()},
        "top_tickers":         ticker_counter.most_common(10),
        "ab_x_performance":    ab_summary,
    }


# --- Rendering -------------------------------------------------------------

def render_markdown(agg: dict[str, Any], slots: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    lines.append("# Crypto AutoPost — Analytics Rollup")
    lines.append("")
    lines.append(f"Generated: {agg['generated_at']}")
    lines.append(f"Total slots tracked: **{agg['total_slots']}**")
    lines.append("")

    lines.append("## Volume")
    lines.append("")
    lines.append("| Day | Posts |")
    lines.append("|---|---|")
    for day in sorted(agg["by_day"]):
        lines.append(f"| {day} | {agg['by_day'][day]} |")
    lines.append("")

    lines.append("## Platform success rate")
    lines.append("")
    lines.append("| Platform | ok | dry_run | error | blocked | missing |")
    lines.append("|---|---|---|---|---|---|")
    for plat, counts in agg["by_platform_status"].items():
        lines.append(
            f"| {plat} | {counts.get('ok', 0)} | {counts.get('dry_run', 0)} | "
            f"{counts.get('error', 0)} | {counts.get('blocked', 0)} | "
            f"{counts.get('missing', 0)} |"
        )
    lines.append("")

    lines.append("## A/B variant distribution")
    lines.append("")
    for v, n in sorted(agg["by_variant"].items()):
        lines.append(f"- **{v}**: {n} posts")
    lines.append("")

    ab = agg["ab_x_performance"]
    if any((v["A_count"] or v["B_count"]) for v in ab.values()):
        lines.append("## A/B performance (X)")
        lines.append("")
        lines.append("| Metric | A (n) | A mean | B (n) | B mean | Winner |")
        lines.append("|---|---|---|---|---|---|")
        for metric in ("impressions", "likes", "retweets"):
            v = ab[metric]
            a_mean = f"{v['A_mean']:.1f}" if v['A_mean'] is not None else "-"
            b_mean = f"{v['B_mean']:.1f}" if v['B_mean'] is not None else "-"
            lines.append(
                f"| {metric} | {v['A_count']} | {a_mean} | {v['B_count']} | "
                f"{b_mean} | {v['winner'] or '-'} |"
            )
        lines.append("")
    else:
        lines.append("## A/B performance (X)")
        lines.append("")
        lines.append("_No metrics.json files found yet — populate one per slot_")
        lines.append("_to see A vs B performance._")
        lines.append("")

    lines.append("## Top tickers")
    lines.append("")
    for t, n in agg["top_tickers"]:
        lines.append(f"- {t}: {n}")
    lines.append("")

    lines.append("## Recent slots")
    lines.append("")
    lines.append("| Date | Slot | Variant | X | TG | Title |")
    lines.append("|---|---|---|---|---|---|")
    for s in sorted(slots, key=lambda x: (x["date"], x["slot"]), reverse=True)[:20]:
        log = s["log"] or {}
        variant = log.get("variant", "-")
        x_st = _platform_status(log, "x")
        tg_st = _platform_status(log, "telegram")
        title = (s["title"] or "")[:60].replace("|", "\\|")
        lines.append(f"| {s['date']} | {s['slot']} | {variant} | {x_st} | {tg_st} | {title} |")
    lines.append("")

    return "\n".join(lines)


# --- Cross-process safe refresh --------------------------------------------

@contextlib.contextmanager
def _rollup_lock(an_dir: Path):
    """Exclusive lock via a .lock sentinel file (cross-process, Windows-safe)."""
    lock_path = an_dir / "rollup.lock"
    deadline = time.monotonic() + 10
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                lock_path.unlink(missing_ok=True)  # stale lock — remove and proceed
                break
            time.sleep(0.05)
    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def refresh_and_write(queue_root: Path, an_dir: Path) -> int:
    """Collect slots, aggregate, write rollup.json + rollup.md.  Returns slot count."""
    an_dir.mkdir(parents=True, exist_ok=True)
    slots = collect_slots(queue_root)
    agg = aggregate(slots)
    agg["slots"] = [
        {
            "date":      s["date"],
            "slot":      s["slot"],
            "title":     s["title"],
            "url":       s["url"],
            "tickers":   s["tickers"],
            "variant":   (s["log"] or {}).get("variant"),
            "x_status":  _platform_status(s["log"], "x"),
            "tg_status": _platform_status(s["log"], "telegram"),
            "metrics":   s["metrics"],
        }
        for s in slots
    ]
    with _rollup_lock(an_dir):
        (an_dir / "rollup.json").write_text(
            json.dumps(agg, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        (an_dir / "rollup.md").write_text(render_markdown(agg, slots), encoding="utf-8")
    return len(slots)


# --- CLI -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue-root", default="queue",
                    help="root folder containing <date>_<slot> directories")
    ap.add_argument("--out-dir", default="analytics",
                    help="where to write rollup.json + rollup.md")
    args = ap.parse_args()

    queue_root = Path(args.queue_root).resolve()
    out_dir = Path(args.out_dir).resolve()

    if not collect_slots(queue_root):
        print(f"[analytics] no slots found in {queue_root}", file=sys.stderr)

    n = refresh_and_write(queue_root, out_dir)
    print(f"[analytics] wrote rollup.json + rollup.md  (slots={n}) -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
