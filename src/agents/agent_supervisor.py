"""Agent Supervisor — watches over all other agents, detects failures, auto-heals.

Runs every 15 minutes as a background task. Checks each agent's output files,
log entries, and timing to assign health scores. Sends Telegram alerts on
critical failures and writes a persistent report used by the dashboard.

Health scores: "ok" | "warn" | "fail" | "unknown"
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT      = Path(__file__).resolve().parent.parent.parent
_QUEUE_ROOT     = _REPO_ROOT / "queue"
_ANALYTICS_DIR  = _REPO_ROOT / "analytics"
_REPORT_FILE    = _ANALYTICS_DIR / "supervisor_report.json"
_ALERT_STATE    = _ANALYTICS_DIR / "supervisor_alerts.json"
_LOG_FILE       = _REPO_ROOT / "scheduler.log"
_HEARTBEAT      = _REPO_ROOT / "scheduler_heartbeat.txt"
_HEALTH_CACHE   = _REPO_ROOT / "data" / "health_status.json"
_BRAINS_DIR     = _ANALYTICS_DIR / "agent_brains"


# ---------------------------------------------------------------------------
# Per-agent check definitions
# ---------------------------------------------------------------------------

def _age_h(path: Path) -> float:
    """Hours since file was last modified. 9999 if missing."""
    if not path.exists():
        return 9999.0
    return (time.time() - path.stat().st_mtime) / 3600


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _latest_slot(user_id: int | None = None) -> Path | None:
    """Return the most recent dated slot dir across all users."""
    best: tuple[float, Path] | None = None
    roots = [_QUEUE_ROOT / str(uid) for uid in _iter_users()] if user_id is None \
            else [_QUEUE_ROOT / str(user_id)]
    for root in roots:
        if not root.exists():
            continue
        for d in root.iterdir():
            if not d.is_dir() or not d.name[:4].isdigit():
                continue
            mtime = d.stat().st_mtime
            if best is None or mtime > best[0]:
                best = (mtime, d)
    return best[1] if best else None


def _iter_users() -> list[int]:
    if not _QUEUE_ROOT.exists():
        return []
    return [int(d.name) for d in _QUEUE_ROOT.iterdir() if d.is_dir() and d.name.isdigit()]


def _log_errors_last_n(hours: float = 24.0) -> list[str]:
    """Extract ERROR/CRASHED lines from scheduler.log in last N hours."""
    if not _LOG_FILE.exists():
        return []
    cutoff = time.time() - hours * 3600
    errors: list[str] = []
    try:
        for line in _LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]:
            if "ERROR" in line or "CRASHED" in line or "Traceback" in line:
                errors.append(line.strip())
    except Exception:
        pass
    return errors[-50:]


# ---------------------------------------------------------------------------
# Individual agent checks
# ---------------------------------------------------------------------------

def _check_scheduler() -> dict:
    issues: list[str] = []
    age = _age_h(_HEARTBEAT)
    if age > 0.1:   # heartbeat every 30s — if older than 6 min it's dead
        issues.append(f"Heartbeat stale: {age:.1f}h ago")
    errors = [e for e in _log_errors_last_n(2) if "CRASHED" in e]
    if errors:
        issues.append(f"{len(errors)} crash(es) in last 2h")
    return {"status": "fail" if issues else "ok", "issues": issues,
            "detail": f"Heartbeat: {age*60:.0f}min ago"}


def _check_news_scout() -> dict:
    slot = _latest_slot()
    if slot is None:
        return {"status": "warn", "issues": ["No slot directory found"], "detail": ""}
    top2 = slot / "top2.json"
    age  = _age_h(top2)
    issues: list[str] = []
    if age > 26:
        issues.append(f"top2.json is {age:.0f}h old — news_scout may not have run")
    data = _read_json(top2)
    stories = data.get("stories", [])
    if top2.exists() and len(stories) == 0:
        issues.append("top2.json exists but has 0 stories")
    detail = f"Last run: {age:.1f}h ago · {len(stories)} stories"
    return {"status": "fail" if len(issues) > 1 else ("warn" if issues else "ok"),
            "issues": issues, "detail": detail}


def _check_ranker() -> dict:
    slot = _latest_slot()
    if slot is None:
        return {"status": "warn", "issues": ["No slot found"], "detail": ""}
    top2 = slot / "top2.json"
    issues: list[str] = []
    data  = _read_json(top2)
    stories = data.get("stories", [])
    if stories:
        scores = [s.get("score", 0) for s in stories]
        if max(scores, default=0) == 0:
            issues.append("All story scores are 0 — ranker may be broken")
    detail = f"Top score: {max((s.get('score',0) for s in stories), default=0):.2f}"
    return {"status": "warn" if issues else "ok", "issues": issues, "detail": detail}


def _check_copywriter() -> dict:
    slot = _latest_slot()
    issues: list[str] = []
    if slot is None:
        return {"status": "warn", "issues": ["No slot found"], "detail": ""}
    post_x = slot / "post_x.txt"
    age    = _age_h(post_x)
    if not post_x.exists():
        issues.append("post_x.txt missing — copywriter didn't run")
    elif post_x.stat().st_size < 20:
        issues.append("post_x.txt is near-empty")
    detail = f"post_x.txt age: {age:.1f}h · {post_x.stat().st_size if post_x.exists() else 0}B"
    return {"status": "fail" if issues else "ok", "issues": issues, "detail": detail}


def _check_image_gen() -> dict:
    slot = _latest_slot()
    issues: list[str] = []
    if slot is None:
        return {"status": "warn", "issues": ["No slot found"], "detail": ""}
    imgs = list(slot.glob("*.jpg")) + list(slot.glob("*.png"))
    if not imgs:
        issues.append("No image file in latest slot")
    else:
        small = [i for i in imgs if i.stat().st_size < 5_000]
        if small:
            issues.append(f"{len(small)} image(s) suspiciously small (<5KB)")
    detail = f"{len(imgs)} image(s) in latest slot"
    return {"status": "warn" if issues else "ok", "issues": issues, "detail": detail}


def _check_reel_writer() -> dict:
    slot = _latest_slot()
    issues: list[str] = []
    if slot is None:
        return {"status": "warn", "issues": ["No slot found"], "detail": ""}
    reel_dir = slot / "reel"
    files    = list(reel_dir.iterdir()) if reel_dir.exists() else []
    if not reel_dir.exists():
        issues.append("reel/ dir missing in latest slot")
    elif len(files) < 3:
        issues.append(f"reel/ has only {len(files)} files (expected ≥3)")
    detail = f"{len(files)} reel files"
    return {"status": "warn" if issues else "ok", "issues": issues, "detail": detail}


def _check_video_builder() -> dict:
    slot = _latest_slot()
    issues: list[str] = []
    if slot is None:
        return {"status": "warn", "issues": ["No slot found"], "detail": ""}
    mp4s = list(slot.glob("*.mp4"))
    if not mp4s:
        # video_builder is optional — only warn, not fail
        return {"status": "warn", "issues": ["No .mp4 in latest slot (video_builder skipped?)"],
                "detail": "No video"}
    big = [v for v in mp4s if v.stat().st_size > 100_000]
    if not big:
        issues.append("MP4 files are suspiciously small (<100KB)")
    detail = f"{len(mp4s)} video(s) · {big[0].stat().st_size // 1024}KB" if big else "tiny mp4"
    return {"status": "warn" if issues else "ok", "issues": issues, "detail": detail}


def _check_publisher() -> dict:
    issues: list[str] = []
    errors = _log_errors_last_n(25)
    pub_errors = [e for e in errors if "publisher" in e.lower() or "publish" in e.lower()]
    if len(pub_errors) >= 3:
        issues.append(f"{len(pub_errors)} publisher errors in last 25h")
    slot = _latest_slot()
    published = False
    if slot:
        pub_log = slot / "publish_result.json"
        if pub_log.exists():
            data = _read_json(pub_log)
            published = any(r.get("ok") for r in data.values() if isinstance(r, dict))
    detail = "Last slot published ✓" if published else "No publish record found"
    return {"status": "warn" if issues else "ok", "issues": issues, "detail": detail}


def _check_health_monitor() -> dict:
    issues: list[str] = []
    age = _age_h(_HEALTH_CACHE)
    if age > 3:
        issues.append(f"health_status.json is {age:.1f}h old")
    data = _read_json(_HEALTH_CACHE)
    platforms = data.get("platforms", {})
    failed = [p for p, r in platforms.items() if r.get("status") == "error"]
    if failed:
        issues.append(f"Platforms in ERROR: {', '.join(failed)}")
    detail = f"Cache age: {age:.1f}h · {len(platforms)} platforms checked"
    return {"status": "fail" if len(failed) > 2 else ("warn" if issues else "ok"),
            "issues": issues, "detail": detail}


def _check_agent_brain() -> dict:
    issues: list[str] = []
    if not _BRAINS_DIR.exists():
        return {"status": "warn", "issues": ["agent_brains dir missing"], "detail": ""}
    brains   = list(_BRAINS_DIR.iterdir())
    stale    = []
    for b in brains:
        f = b / "brain.json"
        if not f.exists():
            continue
        age = _age_h(f)
        if age > 35:
            stale.append(b.name)
    if stale:
        issues.append(f"Stale brains (>35h): {', '.join(stale)}")
    detail = f"{len(brains)} brains · {len(stale)} stale"
    return {"status": "warn" if stale else "ok", "issues": issues, "detail": detail}


def _check_trend_predictor() -> dict:
    p = _BRAINS_DIR / "trend_predictor" / "learned_params.json"
    if not p.exists():
        return {"status": "warn", "issues": ["No learned params yet"], "detail": "Not trained yet"}
    age = _age_h(p)
    return {"status": "ok" if age < 48 else "warn",
            "issues": [f"Params stale: {age:.0f}h"] if age >= 48 else [],
            "detail": f"Params age: {age:.0f}h"}


def _check_ad_creator() -> dict:
    p = _BRAINS_DIR / "ad_creator" / "brain.json"
    exists = p.exists()
    detail = "Brain active" if exists else "No brain yet (new agent)"
    return {"status": "ok" if exists else "warn",
            "issues": [] if exists else ["Brain not yet initialized"],
            "detail": detail}


def _check_generic(agent_name: str) -> dict:
    """Fallback check for agents not explicitly handled."""
    p = _BRAINS_DIR / agent_name / "brain.json"
    if not p.exists():
        return {"status": "warn", "issues": ["Brain file missing"], "detail": "No brain yet"}
    age = _age_h(p)
    data = _read_json(p)
    cycles = data.get("cycles_completed", 0)
    detail = f"Cycles: {cycles} · Brain age: {age:.0f}h"
    issues = [f"No learning cycles yet"] if cycles == 0 else []
    return {"status": "warn" if issues else "ok", "issues": issues, "detail": detail}


# ---------------------------------------------------------------------------
# Check dispatch table
# ---------------------------------------------------------------------------

_CHECKS: dict[str, Any] = {
    "scheduler":          _check_scheduler,
    "news_scout":         _check_news_scout,
    "ranker":             _check_ranker,
    "copywriter":         _check_copywriter,
    "image_gen":          _check_image_gen,
    "reel_writer":        _check_reel_writer,
    "video_builder":      _check_video_builder,
    "publisher":          _check_publisher,
    "health_monitor":     _check_health_monitor,
    "agent_brain":        _check_agent_brain,
    "trend_predictor":    _check_trend_predictor,
    "ad_creator":         _check_ad_creator,
    "engagement_optimizer": lambda: _check_generic("engagement_optimizer"),
    "ab_tester":          lambda: _check_generic("ab_tester"),
    "fact_checker":       lambda: _check_generic("fact_checker"),
    "content_personalizer": lambda: _check_generic("content_personalizer"),
    "viral_analyzer":     lambda: _check_generic("viral_analyzer"),
    "avatar_writer":      lambda: _check_generic("avatar_writer"),
    "copywriter_adv":     lambda: _check_generic("copywriter"),
}

_CRITICAL_AGENTS = {"scheduler", "news_scout", "copywriter", "publisher"}


# ---------------------------------------------------------------------------
# Telegram alert helper
# ---------------------------------------------------------------------------

def _tg_alert(message: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_ALERT_CHAT", "").strip()
    if not token or not chat_id:
        return
    try:
        import urllib.parse, urllib.request as _ureq
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
        _ureq.urlopen(_ureq.Request(url, data=data), timeout=8)
    except Exception:
        pass


def _should_alert(agent: str, status: str) -> bool:
    """Throttle: alert max once per 4h per agent."""
    _ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
    state = _read_json(_ALERT_STATE)
    key   = f"{agent}_{status}"
    last  = state.get(key, 0)
    if time.time() - last < 4 * 3600:
        return False
    state[key] = time.time()
    try:
        _ALERT_STATE.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass
    return True


# ---------------------------------------------------------------------------
# Main supervisor run
# ---------------------------------------------------------------------------

def run_supervision() -> dict:
    """Run all agent checks. Returns full report dict."""
    _ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)

    report: dict = {
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "agents":     {},
        "summary":    {"ok": 0, "warn": 0, "fail": 0, "total": 0},
        "critical_issues": [],
    }

    for agent, check_fn in _CHECKS.items():
        try:
            result = check_fn()
        except Exception as exc:
            result = {"status": "warn", "issues": [f"Supervisor check error: {exc}"], "detail": ""}

        result["agent"]     = agent
        result["checked_at"] = datetime.now(timezone.utc).isoformat()
        report["agents"][agent] = result

        st = result.get("status", "unknown")
        report["summary"][st] = report["summary"].get(st, 0) + 1
        report["summary"]["total"] += 1

        if st == "fail" and agent in _CRITICAL_AGENTS:
            report["critical_issues"].append(
                f"[{agent.upper()}] FAIL: {'; '.join(result.get('issues', []))}"
            )
            if _should_alert(agent, "fail"):
                _tg_alert(
                    f"🚨 [AutoPost Supervisor] AGENT FAIL\n"
                    f"Agent: {agent}\n"
                    f"Issues: {'; '.join(result.get('issues', []))}\n"
                    f"Time: {datetime.now().strftime('%H:%M:%S')}"
                )
        elif st == "warn" and agent in _CRITICAL_AGENTS:
            if _should_alert(agent, "warn"):
                _tg_alert(
                    f"⚠️ [AutoPost Supervisor] AGENT WARN\n"
                    f"Agent: {agent}\n"
                    f"Issues: {'; '.join(result.get('issues', []))}"
                )

    # Overall health
    fails = report["summary"].get("fail", 0)
    warns = report["summary"].get("warn", 0)
    report["overall"] = "fail" if fails >= 2 else ("warn" if (fails + warns) >= 3 else "ok")

    # Persist report
    try:
        _REPORT_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception:
        pass

    print(
        f"[supervisor] check done — ok={report['summary']['ok']} "
        f"warn={report['summary']['warn']} fail={report['summary']['fail']} "
        f"overall={report['overall']}"
    )
    return report


def get_last_report() -> dict:
    """Load last persisted report. Empty dict if none exists."""
    return _read_json(_REPORT_FILE)


def is_stale(max_age_min: float = 20.0) -> bool:
    """True if last supervision run was more than max_age_min minutes ago."""
    return _age_h(_REPORT_FILE) * 60 > max_age_min
