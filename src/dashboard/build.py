"""Dashboard builder — inlines rollup.json into a standalone index.html.

Reads:
    analytics/rollup.json   (produced by src/agents/analytics.py)

Writes:
    src/dashboard/index.html   — a single-file static dashboard.

Usage:
    python src/agents/analytics.py
    python src/dashboard/build.py
    open src/dashboard/index.html
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Crypto AutoPost -- Dashboard</title>
<style>
  :root {
    --bg: #0a0f1a; --panel: #121a2e; --border: #1e2a44;
    --text: #f4f6fb; --muted: #8a93a8; --accent: #2a8cff;
    --ok: #2ecc71; --warn: #f39c12; --err: #e74c3c; --gray: #7f8c8d;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, system-ui, sans-serif;
    background: var(--bg); color: var(--text); padding: 32px;
    max-width: 1400px; margin: 0 auto; }
  h1 { font-size: 28px; margin-bottom: 4px; letter-spacing: -0.02em; }
  h2 { font-size: 18px; margin: 32px 0 12px; color: var(--accent);
    letter-spacing: 0.02em; text-transform: uppercase; font-weight: 600; }
  .meta { color: var(--muted); margin-bottom: 24px; font-size: 13px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 16px; margin-bottom: 24px; }
  .card { background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; padding: 18px 20px; }
  .card .label { font-size: 12px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.08em; }
  .card .value { font-size: 28px; font-weight: 700; margin-top: 8px; }
  table { width: 100%; border-collapse: collapse; background: var(--panel);
    border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
  th, td { padding: 12px 14px; text-align: left; font-size: 14px;
    border-bottom: 1px solid var(--border); }
  th { background: rgba(255,255,255,0.03); color: var(--muted);
    font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.05em; font-size: 12px; }
  tr:last-child td { border-bottom: none; }
  .pill { display: inline-block; padding: 3px 10px; border-radius: 999px;
    font-size: 11px; font-weight: 600; letter-spacing: 0.03em; }
  .pill.ok { background: rgba(46,204,113,0.18); color: var(--ok); }
  .pill.error { background: rgba(231,76,60,0.18); color: var(--err); }
  .pill.blocked { background: rgba(243,156,18,0.18); color: var(--warn); }
  .pill.dry_run { background: rgba(127,140,141,0.18); color: var(--gray); }
  .pill.missing { background: rgba(127,140,141,0.10); color: var(--gray); }
  .variant { display: inline-block; width: 24px; height: 24px;
    border-radius: 50%; text-align: center; line-height: 24px;
    font-weight: 700; font-size: 12px; }
  .variant.A { background: rgba(42,140,255,0.2); color: var(--accent); }
  .variant.B { background: rgba(201,165,74,0.2); color: #c9a54a; }
  .winner { font-weight: 700; color: var(--accent); }
  .empty { padding: 32px; color: var(--muted); text-align: center;
    font-style: italic; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .row-link { color: var(--muted); font-size: 12px; }
</style>
</head>
<body>
<h1>Crypto AutoPost -- Dashboard</h1>
<div class="meta" id="generated-at"></div>

<div class="cards" id="cards"></div>

<h2>A/B performance (X)</h2>
<div id="ab-section"></div>

<h2>Platform success rate</h2>
<table>
  <thead><tr><th>Platform</th><th>ok</th><th>dry_run</th><th>error</th>
  <th>blocked</th><th>missing</th></tr></thead>
  <tbody id="platform-rows"></tbody>
</table>

<h2>Recent slots</h2>
<table>
  <thead><tr><th>Date</th><th>Slot</th><th>Variant</th><th>X</th><th>TG</th>
  <th>Title</th><th>Impr.</th><th>Likes</th></tr></thead>
  <tbody id="slot-rows"></tbody>
</table>

<h2>Top tickers</h2>
<table>
  <thead><tr><th>Ticker</th><th>Mentions</th></tr></thead>
  <tbody id="ticker-rows"></tbody>
</table>

<script>
const DATA = __INLINE_DATA__;

function pill(s) {
  const cls = ["ok", "error", "blocked", "dry_run", "missing"].includes(s) ? s : "missing";
  return `<span class="pill ${cls}">${s}</span>`;
}
function variantBadge(v) {
  if (v !== "A" && v !== "B") return `<span class="pill missing">${v || "-"}</span>`;
  return `<span class="variant ${v}">${v}</span>`;
}

document.getElementById("generated-at").textContent =
  `Last refresh: ${DATA.generated_at || "-"}  -  Slots tracked: ${DATA.total_slots}`;

const cards = [
  { label: "Total slots", value: DATA.total_slots },
  { label: "Days tracked", value: Object.keys(DATA.by_day || {}).length },
  { label: "X ok", value: (DATA.by_platform_status?.x?.ok || 0) },
  { label: "TG ok", value: (DATA.by_platform_status?.telegram?.ok || 0) },
  { label: "Variant A", value: (DATA.by_variant?.A || 0) },
  { label: "Variant B", value: (DATA.by_variant?.B || 0) },
];
document.getElementById("cards").innerHTML = cards.map(c => `
  <div class="card"><div class="label">${c.label}</div>
    <div class="value">${c.value}</div></div>
`).join("");

document.getElementById("platform-rows").innerHTML =
  Object.entries(DATA.by_platform_status || {}).map(([plat, c]) => `
    <tr><td>${plat}</td>
    <td>${c.ok||0}</td><td>${c.dry_run||0}</td><td>${c.error||0}</td>
    <td>${c.blocked||0}</td><td>${c.missing||0}</td></tr>`).join("");

const ab = DATA.ab_x_performance || {};
const hasData = Object.values(ab).some(v => v.A_count || v.B_count);
if (hasData) {
  document.getElementById("ab-section").innerHTML = `
    <table>
      <thead><tr><th>Metric</th><th>A (n)</th><th>A mean</th>
      <th>B (n)</th><th>B mean</th><th>Winner</th></tr></thead>
      <tbody>${["impressions","likes","retweets"].map(k => {
        const v = ab[k] || {};
        const fmt = x => x == null ? "-" : x.toFixed(1);
        const w = v.winner || "-";
        return `<tr><td>${k}</td><td>${v.A_count||0}</td>
          <td>${fmt(v.A_mean)}</td><td>${v.B_count||0}</td>
          <td>${fmt(v.B_mean)}</td>
          <td><span class="winner">${w}</span></td></tr>`;
      }).join("")}</tbody>
    </table>`;
} else {
  document.getElementById("ab-section").innerHTML =
    `<div class="empty">No metrics.json files found yet.
    Populate one per slot to see A vs B performance.</div>`;
}

const slots = (DATA.slots || []).slice().sort((a, b) =>
  (b.date + b.slot).localeCompare(a.date + a.slot)).slice(0, 30);
document.getElementById("slot-rows").innerHTML = slots.map(s => {
  const mx = (s.metrics && s.metrics.x) || {};
  const title = (s.title || "").replace(/</g, "&lt;").slice(0, 80);
  const link = s.url ? ` <a href="${s.url}" target="_blank" class="row-link">[src]</a>` : "";
  return `<tr><td>${s.date}</td><td>${s.slot}</td>
    <td>${variantBadge(s.variant)}</td>
    <td>${pill(s.x_status)}</td><td>${pill(s.tg_status)}</td>
    <td>${title}${link}</td>
    <td>${mx.impressions ?? "-"}</td><td>${mx.likes ?? "-"}</td></tr>`;
}).join("") || `<tr><td colspan="8" class="empty">No slots yet.</td></tr>`;

document.getElementById("ticker-rows").innerHTML =
  (DATA.top_tickers || []).map(([t, n]) =>
    `<tr><td>${t}</td><td>${n}</td></tr>`).join("")
  || `<tr><td colspan="2" class="empty">-</td></tr>`;
</script>
</body>
</html>
"""


def build_dashboard(rollup_path: Path, out_path: Path) -> None:
    if not rollup_path.exists():
        data = {
            "generated_at":       datetime.now(timezone.utc).isoformat(),
            "total_slots":        0,
            "by_day":             {},
            "by_variant":         {},
            "by_platform_status": {"telegram": {}, "x": {}},
            "top_tickers":        [],
            "ab_x_performance":   {},
            "slots":              [],
            "_note":              f"rollup.json not found at {rollup_path}",
        }
    else:
        data = json.loads(rollup_path.read_text(encoding="utf-8"))

    inline = json.dumps(data, ensure_ascii=False, default=str)
    html = HTML_TEMPLATE.replace("__INLINE_DATA__", inline)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rollup",
                    default=str(_REPO_ROOT / "analytics" / "rollup.json"))
    ap.add_argument("--out",
                    default=str(_HERE / "index.html"))
    args = ap.parse_args()

    rollup = Path(args.rollup).resolve()
    out = Path(args.out).resolve()
    build_dashboard(rollup, out)
    print(f"[dashboard] wrote {out}  (rollup: {rollup})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
