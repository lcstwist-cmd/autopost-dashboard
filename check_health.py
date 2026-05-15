"""Health check — validates credentials + external services.

Run:
    python check_health.py

Exits 0 if everything critical is configured, 1 otherwise.
The script never uploads credits / tweets — every check is read-only.
"""
from __future__ import annotations

import base64
import io
import os
import sys
from pathlib import Path

# Force UTF-8 output on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent

# ── Colors ─────────────────────────────────────────────────────────────────
_USE_COLOR = sys.stdout.isatty()
def _c(text: str, code: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"

def ok(msg: str)    -> None: print("  " + _c("✅", "92") + " " + msg)
def warn(msg: str)  -> None: print("  " + _c("⚠ ", "93") + " " + msg)
def err(msg: str)   -> None: print("  " + _c("❌", "91") + " " + msg)
def head(msg: str)  -> None: print("\n" + _c("▶ " + msg, "96;1"))


# ── Load .env ───────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    warn("python-dotenv not installed — reading env only from OS")


FAILURES   = 0
WARNINGS   = 0

def _fail(msg: str) -> None:
    global FAILURES
    FAILURES += 1
    err(msg)

def _warn(msg: str) -> None:
    global WARNINGS
    WARNINGS += 1
    warn(msg)


# ── 1. Python deps ──────────────────────────────────────────────────────────

def check_deps() -> None:
    head("1. Python dependencies")
    required = {
        "fastapi":      "dashboard",
        "uvicorn":      "dashboard",
        "jinja2":       "dashboard templates",
        "feedparser":   "RSS news scout",
        "requests":     "HTTP client",
        "dateutil":     "date parsing",
        "anthropic":    "Claude copywriter (optional if using other copy engine)",
        "moviepy":      "video composition (optional)",
        "PIL":          "image generation (package: Pillow)",
        "playwright":   "X browser automation",
        "tweepy":       "X API posting (optional)",
        "telegram":     "Telegram Bot (package: python-telegram-bot)",
    }
    for mod, purpose in required.items():
        try:
            __import__(mod)
            ok(f"{mod:15s}  → {purpose}")
        except ImportError:
            if "optional" in purpose:
                _warn(f"{mod:15s}  → {purpose}  (not installed)")
            else:
                _fail(f"{mod:15s}  → {purpose}  (MISSING — pip install -r requirements.txt)")


# ── 2. Required env vars ────────────────────────────────────────────────────

def check_env() -> None:
    head("2. Environment variables")

    critical = [
        ("TELEGRAM_BOT_TOKEN", "Telegram Bot Token from @BotFather"),
        ("TELEGRAM_CHAT_ID",   "Channel id (@channel) or numeric chat id"),
    ]
    for key, note in critical:
        v = os.environ.get(key, "").strip()
        if v:
            ok(f"{key:28s} set ({len(v)} chars) — {note}")
        else:
            _fail(f"{key:28s} MISSING — {note}")

    recommended = [
        ("ANTHROPIC_API_KEY",       "Claude API key (for copywriter)"),
        ("IG_USER_ID",              "Instagram Business Account ID (native API)"),
        ("IG_ACCESS_TOKEN",         "Instagram long-lived Page Access Token"),
        ("TIKTOK_CLIENT_KEY",       "TikTok Dev app client key"),
        ("TIKTOK_REFRESH_TOKEN",    "TikTok OAuth refresh token"),
        ("YT_CLIENT_ID",            "YouTube OAuth client ID"),
        ("YT_REFRESH_TOKEN",        "YouTube OAuth refresh token"),
        ("IMGBB_API_KEY",           "imgbb (host image for Instagram fetch) — optional"),
        ("PUBLIC_BASE_URL",         "Public base URL (ngrok) — optional fallback"),
        ("X_BEARER_TOKEN",          "X API v2 Bearer (read tweets — Basic tier)"),
        ("CRYPTOPANIC_API_KEY",     "CryptoPanic developer API"),
        ("LUNARCRUSH_API_KEY",      "LunarCrush social-sentiment API"),
        ("DID_API_KEY",             "D-ID /talks API (AI avatar)"),
        ("ELEVENLABS_API_KEY",      "ElevenLabs TTS (optional)"),
    ]
    for key, note in recommended:
        v = os.environ.get(key, "").strip()
        if v:
            ok(f"{key:28s} set — {note}")
        else:
            _warn(f"{key:28s} not set — {note}")

    # X browser automation credentials
    x_user = os.environ.get("X_USERNAME", "").strip()
    x_mail = os.environ.get("X_EMAIL", "").strip()
    x_pass = os.environ.get("X_PASSWORD", "").strip()
    if x_user and x_mail and x_pass:
        ok(f"X_USERNAME/EMAIL/PASSWORD all set — browser fallback available")
    else:
        _warn("X_USERNAME / X_EMAIL / X_PASSWORD incomplete — browser fallback disabled")


# ── 3. Telegram bot ────────────────────────────────────────────────────────

def check_telegram() -> None:
    head("3. Telegram bot reachability")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        _warn("Skipped (TELEGRAM_BOT_TOKEN not set)")
        return
    try:
        import requests
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        if r.status_code == 200 and r.json().get("ok"):
            info = r.json()["result"]
            ok(f"Bot @{info.get('username')} online  id={info.get('id')}")
        else:
            _fail(f"getMe HTTP {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        _fail(f"Telegram API call failed: {exc}")


# ── 4. Anthropic API ────────────────────────────────────────────────────────

def check_anthropic() -> None:
    head("4. Anthropic / Claude API")
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        _warn("Skipped (ANTHROPIC_API_KEY not set)")
        return
    if not key.startswith("sk-ant-"):
        _warn(f"Key format unusual (expected 'sk-ant-...'); got {key[:10]}...")
    try:
        import requests
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":          key,
                "anthropic-version":  "2023-06-01",
                "content-type":       "application/json",
            },
            json={
                "model":    "claude-3-5-haiku-20241022",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=15,
        )
        if r.status_code == 200:
            ok("Claude API key valid (test ping succeeded)")
        elif r.status_code == 401:
            _fail("Claude API 401 — key invalid or revoked")
        else:
            _warn(f"Claude API HTTP {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        _fail(f"Claude API call failed: {exc}")


# ── 5. D-ID ─────────────────────────────────────────────────────────────────

def check_did() -> None:
    head("5. D-ID talking-avatar API")
    key = os.environ.get("DID_API_KEY", "").strip()
    if not key:
        _warn("Skipped (DID_API_KEY not set)")
        return

    # Use the avatar_video pre-flight helper so the check matches
    # what the pipeline actually does.
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from agents.avatar_video import check_did_credentials  # type: ignore
        ok_state, msg = check_did_credentials()
        if ok_state:
            ok(msg)
        else:
            _fail(msg)
    except Exception as exc:
        _warn(f"Could not invoke D-ID pre-flight: {exc} — doing raw check")
        try:
            import requests
            r = requests.get(
                "https://api.d-id.com/credits",
                headers={"Authorization": f"Basic {key}"},
                timeout=15,
            )
            if r.status_code == 200:
                ok(f"D-ID auth OK (HTTP 200)")
            elif r.status_code == 401:
                _fail("D-ID 401 — key is wrong")
            else:
                _warn(f"D-ID HTTP {r.status_code}")
        except Exception as exc2:
            _fail(f"D-ID call failed: {exc2}")


# ── 6. X / Twitter ─────────────────────────────────────────────────────────

def check_x() -> None:
    head("6. X / Twitter posting pathways")

    tweepy_ok = False
    api_key     = os.environ.get("X_API_KEY", "").strip()
    api_secret  = os.environ.get("X_API_SECRET", "").strip()
    acc_token   = os.environ.get("X_ACCESS_TOKEN", "").strip()
    acc_secret  = os.environ.get("X_ACCESS_TOKEN_SECRET", "").strip()
    if all([api_key, api_secret, acc_token, acc_secret]):
        ok("X API keys all 4 set — tweepy path available (needs Basic $100/mo to WRITE)")
        tweepy_ok = True
    else:
        _warn("X API keys incomplete — tweepy path disabled")

    webhook = os.environ.get("MAKE_X_WEBHOOK_URL", "").strip()
    if webhook:
        ok("MAKE_X_WEBHOOK_URL set — Make.com path available (recommended, free)")
    else:
        _warn("MAKE_X_WEBHOOK_URL not set — Make.com path disabled")

    # Pasted cookies (preferred X path) live in DB as X_COOKIES_JSON env var
    pasted_cookies = bool(os.environ.get("X_COOKIES_JSON", "").strip())
    if pasted_cookies:
        ok("X cookies pasted in Settings (preferred path)")
    else:
        _warn("X cookies NOT pasted — open Settings → X / Twitter and paste cookies for automatic posting")

    if not tweepy_ok and not webhook and not pasted_cookies:
        _fail("ALL X pathways disabled — no way to post to X")


# ── 7. Queue dir ───────────────────────────────────────────────────────────

def check_queue() -> None:
    head("7. Queue directory")
    q = ROOT / "queue"
    if not q.exists():
        _warn("queue/ does not exist yet — will be created on first pipeline run")
        return

    slots = sorted([d for d in q.iterdir() if d.is_dir()])
    if not slots:
        _warn("queue/ is empty — no slots generated yet")
        return

    ok(f"{len(slots)} slot(s) in queue/")
    latest = slots[-1]
    print(f"      Latest: {latest.name}")
    for f in ("post_x.txt", "post_telegram.md",
              "image_x_1200x675.png", "image_tg_1080x1080.png"):
        if (latest / f).exists():
            ok(f"  {f}")
        else:
            _warn(f"  {f} — missing in latest slot")


# ── 8. DB + dashboard ──────────────────────────────────────────────────────

def check_db() -> None:
    head("8. SQLite dashboard DB")
    db = ROOT / "autopost.db"
    if not db.exists():
        _warn("autopost.db not found — run: python seed_db.py")
        return
    size_kb = db.stat().st_size / 1024
    ok(f"autopost.db exists ({size_kb:.1f} KB)")

    try:
        import sqlite3
        conn = sqlite3.connect(str(db))
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        conn.close()
        if tables:
            ok(f"Tables: {', '.join(tables)}")
        else:
            _warn("DB has no tables — run: python seed_db.py")
    except Exception as exc:
        _fail(f"DB read failed: {exc}")


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> int:
    print()
    print(_c("╔══════════════════════════════════════════════╗", "96"))
    print(_c("║      AutoPost — Health Check                 ║", "96"))
    print(_c("╚══════════════════════════════════════════════╝", "96"))

    check_deps()
    check_env()
    check_telegram()
    check_anthropic()
    check_did()
    check_x()
    check_queue()
    check_db()

    print()
    print(_c("═" * 50, "96"))
    if FAILURES == 0 and WARNINGS == 0:
        print(_c("  ✅ ALL OK — sistemul este complet functional", "92;1"))
    elif FAILURES == 0:
        print(_c(f"  ⚠  OK cu {WARNINGS} avertisment(e) — "
                 "poate rula, dar unele canale sunt dezactivate", "93;1"))
    else:
        print(_c(f"  ❌ {FAILURES} eroare(i) critice, {WARNINGS} avertisment(e) — "
                 "rezolva erorile inainte de publicare live", "91;1"))
    print(_c("═" * 50, "96"))
    print()
    return 0 if FAILURES == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
