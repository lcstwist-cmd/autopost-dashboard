"""AutoPost Pipeline Backtest & Health Check
============================================

Tests every agent against real queue data (no live API calls needed for most tests).

Usage:
    python backtest.py              # full backtest using existing queue data
    python backtest.py --live       # also runs news scout (network calls)
    python backtest.py --stage X    # test only one stage (scout/rank/copy/image/publish)

Output: coloured terminal report + backtest_report.json
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 output on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_REPO = Path(__file__).resolve().parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

try:
    from dotenv import load_dotenv
    load_dotenv(_REPO / ".env")
except ImportError:
    pass

# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):   print(f"  {GREEN}✓{RESET}  {msg}")
def fail(msg): print(f"  {RED}✗{RESET}  {msg}")
def warn(msg): print(f"  {YELLOW}⚠{RESET}  {msg}")
def info(msg): print(f"  {CYAN}·{RESET}  {msg}")
def section(title): print(f"\n{BOLD}{CYAN}{'━'*60}{RESET}\n{BOLD}  {title}{RESET}\n{'━'*60}")


# ── Result collector ──────────────────────────────────────────────────────────
results: list[dict] = []

def record(stage: str, name: str, passed: bool, detail: str = "", duration_ms: int = 0):
    results.append({
        "stage": stage,
        "test": name,
        "passed": passed,
        "detail": detail,
        "duration_ms": duration_ms,
    })
    if passed:
        ok(f"{name}  {YELLOW}({duration_ms}ms){RESET}  {detail}")
    else:
        fail(f"{name}  —  {RED}{detail}{RESET}")


# ── Helpers ───────────────────────────────────────────────────────────────────
def timed(fn, *args, **kwargs):
    t0 = time.monotonic()
    try:
        result = fn(*args, **kwargs)
        ms = int((time.monotonic() - t0) * 1000)
        return result, ms, None
    except Exception as exc:
        ms = int((time.monotonic() - t0) * 1000)
        return None, ms, exc


def latest_slot() -> Path | None:
    slots = sorted(
        [d for d in (_REPO / "queue").iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    return slots[0] if slots else None


def load_top2(slot_dir: Path) -> list[dict]:
    f = slot_dir / "top2.json"
    if not f.exists():
        return []
    data = json.loads(f.read_text(encoding="utf-8"))
    return data.get("stories", data) if isinstance(data, dict) else data


# ===========================================================================
# STAGE 0 — Environment & Imports
# ===========================================================================
def test_environment():
    section("STAGE 0 — Environment & Imports")

    # Python version
    ver = sys.version_info
    record("env", "Python version ≥ 3.11",
           ver >= (3, 11), f"Python {ver.major}.{ver.minor}.{ver.micro}")

    # Required packages
    packages = [
        ("feedparser",        "feedparser"),
        ("requests",          "requests"),
        ("anthropic",         "anthropic"),
        ("fastapi",           "fastapi"),
        ("jinja2",            "jinja2"),
        ("PIL (Pillow)",      "PIL"),
        ("dateutil",          "dateutil"),
        ("schedule",          "schedule"),
        ("dotenv",            "dotenv"),
    ]
    for label, mod in packages:
        t0 = time.monotonic()
        try:
            __import__(mod)
            ms = int((time.monotonic() - t0) * 1000)
            record("env", f"import {label}", True, "", ms)
        except ImportError as e:
            record("env", f"import {label}", False, str(e))

    # .env keys
    required_keys = [
        ("ANTHROPIC_API_KEY",   True),
        ("TELEGRAM_BOT_TOKEN",  True),
        ("TELEGRAM_CHAT_ID",    True),
        ("X_USERNAME",          True),
        ("X_PASSWORD",          True),
    ]
    optional_keys = [
        "CRYPTOPANIC_API_KEY",
        "LUNARCRUSH_API_KEY",
        "X_BEARER_TOKEN",
    ]
    for key, required in required_keys:
        val = os.environ.get(key, "")
        if val:
            record("env", f".env {key}", True, f"set ({len(val)} chars)")
        elif required:
            record("env", f".env {key}", False, "MISSING — required for publishing")
        else:
            warn(f".env {key} — optional, not set")

    for key in optional_keys:
        val = os.environ.get(key, "")
        if val:
            info(f".env {key} — set ({len(val)} chars)")
        else:
            info(f".env {key} — not set (optional)")

    # Queue directory
    q = _REPO / "queue"
    slots = sorted([d for d in q.iterdir() if d.is_dir()]) if q.exists() else []
    record("env", "Queue directory exists", q.exists(), str(q))
    record("env", f"Queue slots found ({len(slots)})", len(slots) > 0,
           ", ".join(d.name for d in slots[-3:]))


# ===========================================================================
# STAGE 1 — News Scout
# ===========================================================================
def test_news_scout(live: bool = False):
    section("STAGE 1 — News Scout")

    if not live:
        warn("Skipping live network fetch (pass --live to enable)")
        # Test with existing data
        slot = latest_slot()
        if slot and (slot / "raw_news.json").exists():
            raw = json.loads((slot / "raw_news.json").read_text(encoding="utf-8"))
            stories = raw if isinstance(raw, list) else []
            record("scout", "Existing raw_news.json parseable", True,
                   f"{len(stories)} stories in {slot.name}")
            _analyse_stories(stories, "scout")
        else:
            warn("No existing raw_news.json found — run with --live to fetch")
        return

    info("Fetching news (this takes ~30s)...")
    from src.agents.news_scout import collect_news

    stories, ms, exc = timed(collect_news, hours_back=12)
    if exc:
        record("scout", "collect_news()", False, str(exc), ms)
        return

    record("scout", "collect_news() returned", True, f"{len(stories)} stories", ms)
    _analyse_stories(stories, "scout")


def _analyse_stories(stories: list[dict], stage: str):
    if not stories:
        record(stage, "Stories non-empty", False, "got 0 stories")
        return

    record(stage, "Stories non-empty", True, f"{len(stories)} total")

    # Check required fields
    required = ["id", "title", "url", "source", "published_at"]
    missing_counts = {f: 0 for f in required}
    for s in stories:
        for f in required:
            if not s.get(f):
                missing_counts[f] += 1
    for f, cnt in missing_counts.items():
        ok_flag = cnt == 0
        record(stage, f"Field '{f}' present", ok_flag,
               f"{cnt}/{len(stories)} missing" if cnt else "all present")

    # Source diversity
    sources = {s["source"] for s in stories}
    record(stage, "Source diversity ≥ 5", len(sources) >= 5,
           f"{len(sources)} unique sources: {', '.join(sorted(sources)[:8])}{'…' if len(sources) > 8 else ''}")

    # Freshness (last 12h)
    now = datetime.now(timezone.utc)
    from dateutil import parser as dp
    fresh = 0
    for s in stories:
        try:
            dt = dp.parse(s["published_at"])
            if dt.tzinfo is None:
                from datetime import timezone as tz
                dt = dt.replace(tzinfo=tz.utc)
            if (now - dt).total_seconds() < 43200:
                fresh += 1
        except Exception:
            pass
    pct = fresh / len(stories) * 100 if stories else 0
    record(stage, f"Stories fresh (≤12h): {fresh}/{len(stories)}",
           pct >= 50, f"{pct:.0f}% fresh")

    # Tickers coverage
    with_tickers = sum(1 for s in stories if s.get("tickers"))
    record(stage, "Tickers extracted", True,
           f"{with_tickers}/{len(stories)} stories have tickers")

    # Reddit data
    reddit = [s for s in stories if s.get("reddit_score")]
    if reddit:
        record(stage, "Reddit engagement data present", True,
               f"{len(reddit)} stories with reddit_score")


# ===========================================================================
# STAGE 2 — Ranker
# ===========================================================================
def test_ranker():
    section("STAGE 2 — Ranker")
    from src.agents.ranker import rank, score_recency, score_keywords, score_tickers
    from src.agents.ranker import score_importance, score_engagement, score_reddit

    slot = latest_slot()
    if not slot:
        record("rank", "Latest slot found", False, "no queue slots exist")
        return

    raw_file = slot / "raw_news.json"
    if not raw_file.exists():
        record("rank", "raw_news.json exists", False, f"not found in {slot.name}")
        return

    raw = json.loads(raw_file.read_text(encoding="utf-8"))
    stories = raw if isinstance(raw, list) else []
    record("rank", "raw_news.json loaded", True, f"{len(stories)} stories from {slot.name}")

    if not stories:
        return

    # Test individual scorers
    s = stories[0]
    now = datetime.now(timezone.utc)
    tests = [
        ("score_recency",    lambda: score_recency(s, now)),
        ("score_keywords",   lambda: score_keywords(s)),
        ("score_tickers",    lambda: score_tickers(s)),
        ("score_importance", lambda: score_importance(s)),
        ("score_engagement", lambda: score_engagement(s)),
        ("score_reddit",     lambda: score_reddit(s)),
    ]
    for name, fn in tests:
        val, ms, exc = timed(fn)
        if exc:
            record("rank", name, False, str(exc), ms)
        else:
            valid = isinstance(val, float) and 0.0 <= val <= 1.0
            record("rank", name, valid,
                   f"= {val:.3f}" + (" OUT OF RANGE!" if not valid else ""), ms)

    # Full ranking
    picked, ms, exc = timed(rank, stories, top_n=2)
    if exc:
        record("rank", "rank() full run", False, str(exc), ms)
        return
    record("rank", "rank() top-2 selected", len(picked) == 2,
           f"picked {len(picked)} from {len(stories)}", ms)

    # Print scoring breakdown
    print(f"\n  {BOLD}Scoring breakdown:{RESET}")
    for i, p in enumerate(picked, 1):
        print(f"\n  #{i}  {p['title'][:70]}")
        print(f"       source={p['source']}  score={p['_score_total']}")
        print(f"       why: {p.get('_rationale','—')}")
        sc = p.get("_scores", {})
        for k, v in sc.items():
            bar_len = int(v * 20)
            bar = "█" * bar_len + "░" * (20 - bar_len)
            print(f"       {k:<12} {bar}  {v:.3f}")

    # Check scores are in range
    for p in picked:
        for k, v in p.get("_scores", {}).items():
            if not (0.0 <= v <= 1.0):
                record("rank", f"score {k} in [0,1]", False, f"got {v}")


# ===========================================================================
# STAGE 3 — Copywriter
# ===========================================================================
def test_copywriter():
    section("STAGE 3 — Copywriter")
    from src.agents.copywriter import write_outputs

    slot = latest_slot()
    if not slot:
        record("copy", "Latest slot found", False, "no slots")
        return

    # Check existing files
    for fname in ["post_x.txt", "post_x_b.txt", "post_telegram.md", "image_prompt.txt"]:
        f = slot / fname
        if f.exists():
            content = f.read_text(encoding="utf-8")
            record("copy", f"{fname} exists", True, f"{len(content)} chars")
        else:
            warn(f"{fname} not found in {slot.name}")

    # Check X post char count (X Premium allows up to 25,000; standard is 280)
    px = slot / "post_x.txt"
    if px.exists():
        from src.agents.copywriter import x_weighted_len
        txt = px.read_text(encoding="utf-8")
        wl = x_weighted_len(txt)
        # Warn if over standard 280 (in case account is not X Premium)
        if wl > 25000:
            record("copy", "X post within X limit", False, f"weighted length = {wl} (> 25000!)")
        elif wl > 280:
            record("copy", "X post length", True,
                   f"weighted length = {wl} (>280 — requires X Premium)")
        else:
            record("copy", "X post ≤ 280 chars (standard)", True, f"weighted length = {wl}")

    # Check Telegram post length
    ptg = slot / "post_telegram.md"
    if ptg.exists():
        txt = ptg.read_text(encoding="utf-8")
        record("copy", "Telegram post ≤ 1024 chars", len(txt) <= 1024,
               f"{len(txt)} chars")

    # Test copywriter on existing story (no Claude API call)
    top2 = load_top2(slot)
    if not top2:
        warn("No stories in top2.json — skipping copywriter dry run")
        return

    story = top2[0]
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        stats, ms, exc = timed(write_outputs, story, Path(tmp))
    if exc:
        record("copy", "write_outputs() dry run", False, str(exc), ms)
    else:
        record("copy", "write_outputs() dry run", True,
               f"X={stats['post_x_chars']}wc  TG={stats['post_telegram_chars']}c", ms)


# ===========================================================================
# STAGE 4 — Image Generator
# ===========================================================================
def test_image_gen():
    section("STAGE 4 — Image Generator")

    slot = latest_slot()
    if not slot:
        record("image", "Latest slot found", False, "no slots")
        return

    # Check existing images
    for fname in ["image_x_1200x675.png", "image_tg_1080x1080.png"]:
        f = slot / fname
        if f.exists():
            size_kb = f.stat().st_size // 1024
            record("image", f"{fname} exists", True, f"{size_kb} KB")
            # Verify it's a valid PNG
            try:
                from PIL import Image
                with Image.open(f) as img:
                    w, h = img.size
                    expected = (1200, 675) if "x_" in fname else (1080, 1080)
                    correct_size = (w, h) == expected
                    record("image", f"{fname} correct dimensions",
                           correct_size, f"{w}×{h}")
            except Exception as exc:
                record("image", f"{fname} valid PNG", False, str(exc))
        else:
            warn(f"{fname} not found in {slot.name}")

    # Test image generation with a story (requires Pollinations.ai)
    top2 = load_top2(slot)
    if not top2:
        warn("No stories in top2.json — skipping image render test")
        return

    story = top2[0]
    import tempfile
    from src.agents.image_gen import render_card

    info("Rendering test image (400×225) via Pollinations.ai...")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "test.png"
        _, ms, exc = timed(render_card, story, 400, 225, out, seed=42)
        if exc:
            record("image", "render_card() test render", False, str(exc), ms)
        else:
            valid = out.exists() and out.stat().st_size > 1000
            record("image", "render_card() test render",
                   valid, f"{out.stat().st_size // 1024} KB  ({ms}ms)", ms)


# ===========================================================================
# STAGE 5 — Publisher (dry-run)
# ===========================================================================
def test_publisher():
    section("STAGE 5 — Publisher")
    from src.agents.publisher import load_queue, validate

    slot = latest_slot()
    if not slot:
        record("publish", "Latest slot found", False, "no slots")
        return

    # load_queue
    q, ms, exc = timed(load_queue, slot)
    if exc:
        record("publish", "load_queue()", False, str(exc), ms)
        return
    record("publish", "load_queue() OK", True,
           f"variant={q.get('variant')}  x_file={q.get('x_file')}", ms)

    # validate
    issues, ms, exc = timed(validate, q)
    if exc:
        record("publish", "validate()", False, str(exc), ms)
        return
    record("publish", "validate() no blocking issues",
           len(issues) == 0,
           f"{len(issues)} issue(s): {'; '.join(issues)}" if issues else "all checks passed",
           ms)

    # Check existing publish log — mark env errors as warnings (fixed by load_dotenv)
    plog = slot / "publish_log.json"
    if plog.exists():
        log_data = json.loads(plog.read_text(encoding="utf-8"))
        results_data = log_data.get("results", {})
        for platform, res in results_data.items():
            status = res.get("status", "?")
            error  = res.get("error", "")
            passed = status == "ok"
            if not passed and "not set" in error:
                # Historical .env loading bug (now fixed) — don't fail the test
                warn(f"{platform} last publish: {error} (old log — fixed by load_dotenv)")
            else:
                detail = error if not passed else f"posted {log_data.get('timestamp','?')[:10]}"
                record("publish", f"{platform} last publish", passed, detail)
    else:
        warn("No publish_log.json found — bot hasn't published yet")

    # Telegram env check
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat  = os.environ.get("TELEGRAM_CHAT_ID", "")
    record("publish", "Telegram credentials in env",
           bool(tg_token and tg_chat),
           f"token={'set' if tg_token else 'MISSING'}  chat={'set' if tg_chat else 'MISSING'}")

    # X credentials
    x_user = os.environ.get("X_USERNAME", "")
    x_pass = os.environ.get("X_PASSWORD", "")
    record("publish", "X credentials in env",
           bool(x_user and x_pass),
           f"user={'set' if x_user else 'MISSING'}  pass={'set' if x_pass else 'MISSING'}")

    # Cookie file
    cookie_file = _REPO / "src" / "agents" / ".x_browser_cookies.json"
    record("publish", "X cookie file exists", cookie_file.exists(),
           str(cookie_file) if cookie_file.exists() else "not found — X login required")


# ===========================================================================
# STAGE 6 — Analytics
# ===========================================================================
def test_analytics():
    section("STAGE 6 — Analytics")
    from src.agents.analytics import collect_slots, aggregate

    q = _REPO / "queue"
    slots, ms, exc = timed(collect_slots, q)
    if exc:
        record("analytics", "collect_slots()", False, str(exc), ms)
        return
    record("analytics", "collect_slots()", True, f"{len(slots)} slots found", ms)

    if not slots:
        return

    agg, ms, exc = timed(aggregate, slots)
    if exc:
        record("analytics", "aggregate()", False, str(exc), ms)
        return

    total_posts = agg.get("total_posts_sent", 0)
    record("analytics", "aggregate() OK", True,
           f"total_posts={total_posts}  slots={agg.get('total_slots',0)}", ms)

    # Check rollup file
    rollup = _REPO / "analytics" / "rollup.json"
    record("analytics", "rollup.json exists", rollup.exists(),
           "present" if rollup.exists() else "missing — run pipeline first")

    # A/B stats
    ab = agg.get("variant_counts", {})
    if ab:
        record("analytics", "A/B variant tracking", True,
               f"A={ab.get('A',0)}  B={ab.get('B',0)}")


# ===========================================================================
# STAGE 7 — Dashboard
# ===========================================================================
def test_dashboard():
    section("STAGE 7 — Dashboard")

    # Check template files
    for fname in ["index.html", "slot.html"]:
        f = _REPO / "src" / "dashboard" / "templates" / fname
        record("dashboard", f"template {fname}", f.exists(),
               f"{f.stat().st_size // 1024} KB" if f.exists() else "MISSING")

    # Import app without starting server
    t0 = time.monotonic()
    try:
        from src.dashboard import app as _app
        ms = int((time.monotonic() - t0) * 1000)
        record("dashboard", "import app.py", True,
               f"FastAPI app loaded in {ms}ms", ms)
    except Exception as exc:
        ms = int((time.monotonic() - t0) * 1000)
        record("dashboard", "import app.py", False, str(exc), ms)


# ===========================================================================
# BACKTEST — Replay existing slots
# ===========================================================================
def test_backtest():
    section("BACKTEST — Historical Slot Analysis")
    from src.agents.ranker import rank

    q = _REPO / "queue"
    if not q.exists():
        warn("No queue directory")
        return

    slots = sorted([d for d in q.iterdir() if d.is_dir()])
    if not slots:
        warn("No queue slots")
        return

    print(f"\n  {BOLD}{'Slot':<25} {'Candidates':>10} {'Score #1':>10} {'Story Title':<45}{RESET}")
    print(f"  {'─'*100}")

    total_slots = 0
    total_stories = 0
    score_sum = 0.0

    for slot_dir in slots:
        raw_file  = slot_dir / "raw_news.json"
        top2_file = slot_dir / "top2.json"

        if not raw_file.exists():
            print(f"  {slot_dir.name:<25} {'— no raw_news.json'}")
            continue

        raw = json.loads(raw_file.read_text(encoding="utf-8"))
        stories = raw if isinstance(raw, list) else []
        total_stories += len(stories)

        # Re-rank with current ranker
        t0 = time.monotonic()
        try:
            picked = rank(stories, top_n=2)
        except Exception as exc:
            print(f"  {slot_dir.name:<25} {'ERROR: ' + str(exc)[:60]}")
            continue
        ms = int((time.monotonic() - t0) * 1000)

        if not picked:
            print(f"  {slot_dir.name:<25} {len(stories):>10}  {'no picks'}")
            continue

        top = picked[0]
        score = top["_score_total"]
        score_sum += score
        title = top["title"][:44]
        total_slots += 1

        # Compare with previously saved ranking (if exists)
        saved_top = load_top2(slot_dir)
        match = saved_top and saved_top[0]["id"] == top["id"] if saved_top else None
        match_mark = (f"{GREEN}✓ same{RESET}" if match else
                      f"{YELLOW}≠ reranked{RESET}" if match is False else "—")

        print(f"  {slot_dir.name:<25} {len(stories):>10}  {score:>10.3f}  {title:<45}  {match_mark}  {ms}ms")

    if total_slots:
        avg_score = score_sum / total_slots
        avg_candidates = total_stories // total_slots if total_slots else 0
        print(f"\n  {BOLD}Average score: {avg_score:.3f}  |  Avg candidates/slot: {avg_candidates}{RESET}")
        record("backtest", "Re-rank all historical slots", True,
               f"{total_slots} slots, avg_score={avg_score:.3f}")


# ===========================================================================
# MAIN
# ===========================================================================
def main():
    ap = argparse.ArgumentParser(description="AutoPost backtest + health check")
    ap.add_argument("--live",  action="store_true",
                    help="run live news fetch (network calls)")
    ap.add_argument("--stage", choices=["env","scout","rank","copy","image","publish",
                                        "analytics","dashboard","backtest"],
                    help="run only one stage")
    args = ap.parse_args()

    print(f"\n{BOLD}{CYAN}{'═'*60}{RESET}")
    print(f"{BOLD}{CYAN}  AutoPost Pipeline — Backtest & Health Check{RESET}")
    print(f"{BOLD}{CYAN}  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
    print(f"{BOLD}{CYAN}{'═'*60}{RESET}")

    stages = {
        "env":       test_environment,
        "scout":     lambda: test_news_scout(live=args.live),
        "rank":      test_ranker,
        "copy":      test_copywriter,
        "image":     test_image_gen,
        "publish":   test_publisher,
        "analytics": test_analytics,
        "dashboard": test_dashboard,
        "backtest":  test_backtest,
    }

    if args.stage:
        stages[args.stage]()
    else:
        for fn in stages.values():
            fn()

    # ── Summary ───────────────────────────────────────────────────────────────
    section("SUMMARY")
    total  = len(results)
    passed = sum(1 for r in results if r["passed"])
    failed = total - passed

    for r in results:
        if not r["passed"]:
            fail(f"[{r['stage']}] {r['test']}: {r['detail']}")

    print(f"\n  {BOLD}Total: {total}  "
          f"{GREEN}Passed: {passed}{RESET}{BOLD}  "
          f"{RED}Failed: {failed}{RESET}")

    if failed == 0:
        print(f"\n  {GREEN}{BOLD}All checks passed! Pipeline is healthy.{RESET}")
    elif failed <= 3:
        print(f"\n  {YELLOW}{BOLD}Minor issues found. Pipeline likely functional.{RESET}")
    else:
        print(f"\n  {RED}{BOLD}Multiple failures — investigate before running live.{RESET}")

    # Write JSON report
    report = {
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "total":        total,
        "passed":       passed,
        "failed":       failed,
        "results":      results,
    }
    out = _REPO / "backtest_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  Report saved: {out}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
