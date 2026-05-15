"""AutoPost Dashboard — multi-tenant FastAPI web interface."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import shutil
import signal
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent

if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_REPO / ".env")
except ImportError:
    pass

BASE_QUEUE = _REPO / "queue"
LOG_FILE   = _REPO / "scheduler.log"
PID_FILE   = _REPO / "scheduler.pid"
PAUSE_FILE = _REPO / "autopost_paused.flag"

import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.dashboard.database import (
    init_db, get_user_by_email, get_user_by_id, create_user,
    get_all_users, get_pending_users, approve_user, reject_user,
    delete_user, update_user, get_all_login_logs,
    get_user_settings, save_user_settings,
    create_session, get_session_user_id, delete_session, cleanup_sessions,
    get_competitors,
    log_heygen_usage, get_heygen_count,
    log_publish_history, get_publish_history,
    create_scheduled_post, get_scheduled_posts, delete_scheduled_post,
    get_due_scheduled_posts, update_scheduled_post_status,
)

init_db()

app = FastAPI(title="AutoPost Dashboard")
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")


@app.on_event("startup")
async def _startup() -> None:
    """Start the agent-brain background learning loop and scheduler on server boot."""
    try:
        from src.agents.agent_brain import manager as _brain_mgr
        _brain_mgr.start_background_loop(interval_minutes=30)
    except Exception as exc:
        print(f"[dashboard] agent_brain startup failed (non-fatal): {exc}")

    # Auto-launch scheduler.py if it is not already running
    try:
        if not _bot_status()["running"]:
            kwargs: dict = {"cwd": str(_REPO)}
            if platform.system() == "Windows":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            import time as _t
            proc = subprocess.Popen(
                [sys.executable, str(_REPO / "src" / "agents" / "scheduler.py")],
                **kwargs,
            )
            _t.sleep(1.5)
            if proc.poll() is None:
                PID_FILE.write_text(str(proc.pid))
                print(f"[dashboard] scheduler auto-started at boot (pid={proc.pid})")
            else:
                print("[dashboard] scheduler exited immediately at startup — check scheduler.log")
        else:
            print("[dashboard] scheduler already running at startup")
    except Exception as exc:
        print(f"[dashboard] scheduler auto-start failed (non-fatal): {exc}")


# Global safety net: convert any unhandled exception into a JSON response,
# so the frontend's `response.json()` parser never chokes on plain-text
# "Internal Server Error". This runs AFTER any endpoint-level handlers.
@app.exception_handler(Exception)
async def _json_exception_handler(request: Request, exc: Exception):
    import traceback
    tb = traceback.format_exc()
    print(f"[unhandled] {type(exc).__name__}: {exc}\n{tb}")
    path = getattr(getattr(request, "url", None), "path", "") or ""
    if path.startswith("/api/"):
        return JSONResponse(
            {"error": f"{type(exc).__name__}: {exc}",
             "traceback": tb[:2000]},
            status_code=500,
        )
    # Non-API route: return plain HTML error (don't raise — Starlette's
    # exception_handler contract expects a Response).
    return HTMLResponse(
        f"<h1>500 Internal Server Error</h1><pre>{type(exc).__name__}: {exc}</pre>",
        status_code=500,
    )

# 6 workers: room for parallel image gen, D-ID renders, publish, and background
# housekeeping without any job queuing when a couple of users are active.
_publish_executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="publisher")

_jinja = Environment(
    loader=FileSystemLoader(str(_HERE / "templates")),
    autoescape=select_autoescape(["html"]),
    auto_reload=True,
)


def _parse_ua(ua: str) -> str:
    """Convert raw User-Agent string to a short human-readable label."""
    if not ua:
        return "—"
    ua_lower = ua.lower()
    # Detect bot/curl/tool first
    if "curl/" in ua_lower:
        return "cURL (script/API)"
    if "python" in ua_lower:
        return "Python (script)"
    if "bot" in ua_lower or "spider" in ua_lower or "crawl" in ua_lower:
        return "Bot"
    # Browser
    browser = "Browser"
    if "edg/" in ua_lower or "edge/" in ua_lower:
        browser = "Edge"
    elif "opr/" in ua_lower or "opera" in ua_lower:
        browser = "Opera"
    elif "chrome/" in ua_lower and "chromium" not in ua_lower:
        browser = "Chrome"
    elif "firefox/" in ua_lower:
        browser = "Firefox"
    elif "safari/" in ua_lower and "chrome" not in ua_lower:
        browser = "Safari"
    # OS
    os_name = ""
    if "windows nt 10" in ua_lower:
        os_name = "Windows 10/11"
    elif "windows nt" in ua_lower:
        os_name = "Windows"
    elif "mac os x" in ua_lower or "macos" in ua_lower:
        os_name = "macOS"
    elif "iphone" in ua_lower:
        os_name = "iPhone"
    elif "ipad" in ua_lower:
        os_name = "iPad"
    elif "android" in ua_lower:
        os_name = "Android"
    elif "linux" in ua_lower:
        os_name = "Linux"
    return f"{browser} / {os_name}" if os_name else browser


_jinja.filters["parse_ua"] = _parse_ua


def _render(template_name: str, **ctx) -> HTMLResponse:
    return HTMLResponse(_jinja.get_template(template_name).render(**ctx))


# ---------------------------------------------------------------------------
# Email notifications helper
# ---------------------------------------------------------------------------

def _send_email_notification(user_id: int, event: str,
                              subject: str, body: str) -> None:
    """Send SMTP email if the user has SMTP configured and the event flag is on."""
    import smtplib, ssl
    from email.mime.text import MIMEText
    try:
        s = get_user_settings(user_id)
    except Exception:
        return
    smtp_host = (s.get("smtp_host") or "").strip()
    smtp_user = (s.get("smtp_user") or "").strip()
    smtp_pass = (s.get("smtp_pass") or "").strip()
    smtp_from = (s.get("smtp_from") or smtp_user or "").strip()
    if not smtp_host or not smtp_user or not smtp_pass or not smtp_from:
        return
    flag_map = {
        "heygen_done": "email_notify_heygen",
        "publish_fail": "email_notify_fail",
        "digest": "email_notify_digest",
    }
    flag_col = flag_map.get(event, "")
    if flag_col and not int(s.get(flag_col) or 0):
        return
    try:
        smtp_port = int(s.get("smtp_port") or 587)
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = smtp_from
        msg["To"]      = smtp_from
        ctx = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.starttls(context=ctx)
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, [smtp_from], msg.as_string())
        print(f"[email] sent '{subject}' to {smtp_from}")
    except Exception as exc:
        print(f"[email] send failed: {exc}")


# ---------------------------------------------------------------------------
# Session management (DB-backed: survives redeployments)
# ---------------------------------------------------------------------------

COOKIE = "ap_session"
COOKIE_AGE = 60 * 60 * 24 * 30  # 30 days


def _new_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    create_session(token, user_id)
    return token


def _end_session(token: str):
    delete_session(token)


def _session_user(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    uid = get_session_user_id(token)
    if not uid:
        return None
    return get_user_by_id(uid)


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

def _hash_pw(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 260_000)
    return f"{salt}:{h.hex()}"


def _verify_pw(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":", 1)
        h2 = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 260_000)
        return secrets.compare_digest(h2.hex(), h)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Security helpers — rate limiting, path safety, secure headers
# ---------------------------------------------------------------------------

import time as _time
from collections import defaultdict as _defaultdict
import hmac as _hmac
import random as _random

# CSRF / HMAC secret — set AUTOPOST_SECRET in .env for persistence across restarts
_SEC_SECRET: str = os.environ.get("AUTOPOST_SECRET") or secrets.token_hex(32)
if not os.environ.get("AUTOPOST_SECRET"):
    print("\n  ⚠  AUTOPOST_SECRET not set in .env — sessions will invalidate on restart.\n"
          "     Add: AUTOPOST_SECRET=" + _SEC_SECRET[:16] + "... to your .env file.\n",
          flush=True)

# ---------------------------------------------------------------------------
# Math CAPTCHA (register page)  — no external services required
# ---------------------------------------------------------------------------

def _captcha_generate() -> tuple[str, str]:
    """Return (question_text, signed_token).

    The token is HMAC-SHA256(secret, str(answer)) so the answer is never
    stored server-side.  A timestamp is mixed in so tokens expire after 15min.
    """
    ops = [
        lambda a, b: (f"Cât face {a} + {b}?", a + b),
        lambda a, b: (f"Cât face {a} × {b}?", a * b),
        lambda a, b: (f"Cât face {a} − {b}?", a - b),
    ]
    op = _random.choice(ops)
    a  = _random.randint(2, 12)
    b  = _random.randint(1, 9)
    question, answer = op(a, b)
    if answer < 0:   # ensure non-negative for subtraction
        question, answer = f"Cât face {b} + {a}?", a + b

    # Mix in 15-minute time bucket so tokens auto-expire
    bucket = int(_time.time()) // 900  # 900s = 15 min
    payload = f"{answer}:{bucket}"
    sig = _hmac.new(_SEC_SECRET.encode(), payload.encode(), "sha256").hexdigest()[:24]
    token = f"{payload}:{sig}"
    return question, token


def _captcha_verify(token: str, user_answer: str) -> bool:
    """Return True if user_answer matches the signed token (within 15 min window)."""
    try:
        parts = token.split(":")
        if len(parts) != 3:
            return False
        expected_answer_str, bucket_str, sig = parts
        bucket = int(bucket_str)
        now_bucket = int(_time.time()) // 900
        # Accept current bucket and the previous one (grace period)
        if now_bucket - bucket > 1:
            return False
        payload = f"{expected_answer_str}:{bucket_str}"
        expected_sig = _hmac.new(_SEC_SECRET.encode(), payload.encode(), "sha256").hexdigest()[:24]
        if not _hmac.compare_digest(sig, expected_sig):
            return False
        return str(int(user_answer.strip())) == expected_answer_str
    except Exception:
        return False


# Admin fields that regular users must never be able to set
_ADMIN_ONLY_FIELDS = frozenset({
    "telegram_alert_chat",
    "plan", "plan_expires_at", "stripe_customer_id", "monthly_clips_used",
})

# ---------------------------------------------------------------------------
# Rate limiters — login + API (per-IP, in-memory)
# ---------------------------------------------------------------------------

_login_attempts: dict = _defaultdict(list)
_LOGIN_MAX   = 5      # tightened: 5 failures
_LOGIN_WIN_S = 300    # in 5 minutes → then locked for the window

_api_hits: dict = _defaultdict(list)
_API_MAX   = 120   # 120 requests per IP
_API_WIN_S = 60    # per minute (general API)

_upload_hits: dict = _defaultdict(list)
_UPLOAD_MAX   = 15
_UPLOAD_WIN_S = 60

_SENSITIVE_PATHS = frozenset({"/api/avatar/upload-photo", "/api/upload-bgm",
                               "/api/upload-presenter", "/api/slot"})


def _login_rate_ok(ip: str) -> bool:
    now = _time.time()
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < _LOGIN_WIN_S]
    if len(_login_attempts[ip]) >= _LOGIN_MAX:
        return False
    _login_attempts[ip].append(now)
    return True


def _login_rate_clear(ip: str) -> None:
    _login_attempts.pop(ip, None)


def _api_rate_ok(ip: str, upload: bool = False) -> bool:
    now = _time.time()
    if upload:
        _upload_hits[ip] = [t for t in _upload_hits[ip] if now - t < _UPLOAD_WIN_S]
        if len(_upload_hits[ip]) >= _UPLOAD_MAX:
            return False
        _upload_hits[ip].append(now)
        return True
    _api_hits[ip] = [t for t in _api_hits[ip] if now - t < _API_WIN_S]
    if len(_api_hits[ip]) >= _API_MAX:
        return False
    _api_hits[ip].append(now)
    return True





def _safe_path(base: Path, *parts: str) -> Path:
    """Join base with parts; raise 400 if resolved path escapes base directory."""
    joined = base.joinpath(*parts)
    try:
        joined.resolve().relative_to(base.resolve())
    except ValueError:
        raise HTTPException(400, "Invalid path")
    return joined


def _csrf_token(session_token: str) -> str:
    """Derive a stable CSRF token from the session token via HMAC-SHA256."""
    return _hmac.new(_SEC_SECRET.encode(), session_token.encode(), "sha256").hexdigest()[:40]


_MAX_BODY_BYTES = 16 * 1024 * 1024  # 16 MB hard limit per request

_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https: blob:; "
    "media-src 'self' blob:; "
    "connect-src 'self' https://api.heygen.com https://api.telegram.org "
    "https://image.pollinations.ai https://api.elevenlabs.io; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self';"
)


@app.middleware("http")
async def _request_size_limit(request: Request, call_next):
    """Reject requests with Content-Length above threshold before reading body."""
    cl = request.headers.get("content-length")
    if cl and int(cl) > _MAX_BODY_BYTES:
        return JSONResponse({"error": "Request too large"}, status_code=413)
    return await call_next(request)


@app.middleware("http")
async def _api_rate_limit(request: Request, call_next):
    """Per-IP rate limiting on all /api/ routes."""
    path = request.url.path
    if path.startswith("/api/") and path not in ("/api/heygen/avatars",):
        ip       = _client_ip(request)
        is_upl   = any(path.startswith(p) for p in _SENSITIVE_PATHS)
        if not _api_rate_ok(ip, upload=is_upl):
            return JSONResponse(
                {"error": "Too many requests — slow down."},
                status_code=429,
                headers={"Retry-After": "60"},
            )
    return await call_next(request)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Add security response headers to every response."""
    response = await call_next(request)
    h = response.headers
    h["X-Content-Type-Options"]  = "nosniff"
    h["X-Frame-Options"]         = "DENY"
    h["X-XSS-Protection"]        = "1; mode=block"
    h["Referrer-Policy"]         = "strict-origin-when-cross-origin"
    h["Permissions-Policy"]      = "geolocation=(), microphone=(), camera=()"
    h["Content-Security-Policy"] = _CSP
    if request.url.scheme == "https" or request.headers.get("X-Forwarded-Proto") == "https":
        h["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
    return response


# ---------------------------------------------------------------------------
# Auth middleware — inject request.state.user on every protected request
# ---------------------------------------------------------------------------

_PUBLIC = {"/login", "/register", "/static", "/media", "/r",
           "/api/webhook/trigger"}


@app.middleware("http")
async def _auth(request: Request, call_next):
    path = request.url.path
    if any(path == p or path.startswith(p + "/") for p in _PUBLIC):
        return await call_next(request)
    user = _session_user(request)
    if not user:
        if path.startswith("/api/"):
            return JSONResponse({"error": "Not authenticated"}, status_code=401)
        return RedirectResponse("/login", status_code=302)
    request.state.user  = user
    session_tok          = request.cookies.get(COOKIE, "")
    request.state.csrf  = _csrf_token(session_tok)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Per-user queue root + credential injection
# ---------------------------------------------------------------------------

def _uqueue(request: Request) -> Path:
    q = BASE_QUEUE / str(request.state.user["id"])
    q.mkdir(parents=True, exist_ok=True)
    return q


def _ensure_x_oauth_fresh(user_id: int, settings: dict) -> dict:
    """If the user has an X OAuth token that's about to expire, refresh it.

    Mutates the returned settings dict with the fresh access_token + expiry,
    and persists the new values back to the DB. Silent no-op if the user
    hasn't connected X via OAuth.
    """
    access  = (settings.get("x_oauth_access_token")  or "").strip()
    refresh = (settings.get("x_oauth_refresh_token") or "").strip()
    expires = int(settings.get("x_oauth_expires_at") or 0)

    if not access or not refresh:
        return settings  # not connected

    import time as _t
    if expires and (expires - 90) > _t.time():
        return settings  # still fresh

    try:
        from src.agents.x_oauth import refresh_access_token
        tok = refresh_access_token(refresh)
        settings = dict(settings)
        settings["x_oauth_access_token"]  = tok["access_token"]
        settings["x_oauth_refresh_token"] = tok.get("refresh_token", refresh)
        settings["x_oauth_expires_at"]    = int(tok["_expires_at"])
        save_user_settings(
            user_id,
            x_oauth_access_token  = settings["x_oauth_access_token"],
            x_oauth_refresh_token = settings["x_oauth_refresh_token"],
            x_oauth_expires_at    = settings["x_oauth_expires_at"],
        )
    except Exception as exc:
        print(f"[dashboard] X token refresh failed for user {user_id}: {exc}")
        # Return original settings — publisher will fall back through the chain.
    return settings


@contextmanager
def _uenv(settings: dict):
    """Temporarily set user's API keys in os.environ (single publisher thread).

    CRITICAL for multi-tenant safety: we also set DATA_DIR to the user's own
    queue folder so that browser cookies land in a per-user jar instead of a
    shared file. Without this, two subscribers publishing at the same time
    would clobber each other's session cookies.
    """
    # Per-user data dir: cookies, caches, anything file-based stays isolated.
    user_data_dir = ""
    uid = settings.get("user_id")
    if uid:
        user_data_dir = str((BASE_QUEUE / str(uid)).resolve())
        (BASE_QUEUE / str(uid)).mkdir(parents=True, exist_ok=True)

    mapping = {
        "DATA_DIR":                user_data_dir,
        # User ID — used by publisher.py to attribute shortlinks/clicks.
        "AUTOPOST_USER_ID":        str(uid) if uid else "",
        # ipstack key + base URL flow into publisher's shortlink wrapper.
        "IPSTACK_API_KEY":         os.environ.get("IPSTACK_API_KEY", "").strip(),
        "TELEGRAM_BOT_TOKEN":      settings.get("telegram_token", ""),
        "TELEGRAM_CHAT_ID":        settings.get("telegram_channel", ""),
        "X_API_KEY":               settings.get("x_api_key", ""),
        "X_API_SECRET":            settings.get("x_api_secret", ""),
        "X_ACCESS_TOKEN":          settings.get("x_access_token", ""),
        "X_ACCESS_TOKEN_SECRET":   settings.get("x_access_secret", ""),
        "MAKE_X_WEBHOOK_URL":      settings.get("make_x_webhook_url", ""),
        "X_USERNAME":              settings.get("x_username", ""),
        "X_EMAIL":                 settings.get("x_email", ""),
        "X_PASSWORD":              settings.get("x_password", ""),
        # X OAuth 2.0 (priority #1 path in publisher.publish_x)
        "X_OAUTH_ACCESS_TOKEN":    settings.get("x_oauth_access_token", ""),
        "X_OAUTH_REFRESH_TOKEN":   settings.get("x_oauth_refresh_token", ""),
        "X_OAUTH_EXPIRES_AT":      str(settings.get("x_oauth_expires_at", "") or ""),
        # X cookies paste (priority #2 — zero-API, free)
        "X_COOKIES_JSON":          settings.get("x_cookies_json", ""),
        "DID_API_KEY":             settings.get("did_api_key", ""),
        "DID_API_EMAIL":           settings.get("did_email", ""),
        "DID_PRESENTER_URL":       settings.get("did_presenter_url", ""),
        # Replicate (new primary AI avatar provider — SadTalker / Wav2Lip)
        "REPLICATE_API_TOKEN":     settings.get("replicate_api_token", ""),
        "REPLICATE_PRESENTER_URL": settings.get("replicate_presenter_url", ""),
        "AVATAR_PROVIDER":         settings.get("avatar_provider", "auto"),
        "ELEVENLABS_API_KEY":      settings.get("elevenlabs_key", ""),
        "ANTHROPIC_API_KEY":       settings.get("anthropic_key", ""),
        # Native social publishers (no Make.com)
        "IG_USER_ID":              settings.get("ig_user_id", ""),
        "IG_ACCESS_TOKEN":         settings.get("ig_access_token", ""),
        "TIKTOK_CLIENT_KEY":       settings.get("tiktok_client_key", ""),
        "TIKTOK_CLIENT_SECRET":    settings.get("tiktok_client_secret", ""),
        "TIKTOK_ACCESS_TOKEN":     settings.get("tiktok_access_token", ""),
        "TIKTOK_REFRESH_TOKEN":    settings.get("tiktok_refresh_token", ""),
        "YT_CLIENT_ID":            settings.get("yt_client_id", ""),
        "YT_CLIENT_SECRET":        settings.get("yt_client_secret", ""),
        "YT_REFRESH_TOKEN":        settings.get("yt_refresh_token", ""),
        "IMGBB_API_KEY":           settings.get("imgbb_api_key", ""),
        "PUBLIC_BASE_URL":         settings.get("public_base_url", ""),
        # Video profile (read by avatar_video + video_builder). Empty strings
        # mean "use the default from config.video_profiles".
        "VIDEO_DURATION_TIKTOK":        str(settings.get("video_duration_tiktok", "") or ""),
        "VIDEO_DURATION_INSTAGRAM":     str(settings.get("video_duration_instagram", "") or ""),
        "VIDEO_DURATION_YOUTUBE_SHORTS":str(settings.get("video_duration_youtube_shorts", "") or ""),
        "VIDEO_STYLE":                  settings.get("video_style", "") or "",
        "VIDEO_INTRO_ENABLED":          str(settings.get("video_intro_enabled", "") or ""),
        "VIDEO_OUTRO_ENABLED":          str(settings.get("video_outro_enabled", "") or ""),
        "VIDEO_BGM_ENABLED":            str(settings.get("video_bgm_enabled", "") or ""),
        "VIDEO_BGM_VOLUME":             str(settings.get("video_bgm_volume", "") or ""),
        "VIDEO_BRAND_COLOR":            settings.get("video_brand_color", "") or "",
        "VIDEO_BRAND_HANDLE":           settings.get("video_brand_handle", "") or "",
        "VIDEO_BRAND_CTA":              settings.get("video_brand_cta", "") or "",
    }
    old = {k: os.environ.get(k) for k in mapping}
    for k, v in mapping.items():
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def get_slots(queue_root: Path) -> list[dict]:
    if not queue_root.exists():
        return []
    slots = []
    # Date-based slots (2026-...) sorted newest-first; non-date dirs (avatar_output etc.) at end
    def _slot_key(d: Path):
        is_date = d.name[:4].isdigit()
        return (1 if is_date else 2, -d.stat().st_mtime)
    for d in sorted(queue_root.iterdir(), key=_slot_key):
        if not d.is_dir():
            continue
        slot: dict[str, Any] = {
            "name":            d.name,
            "has_post_x":      (d / "post_x.txt").exists(),
            "has_post_tg":     (d / "post_telegram.md").exists(),
            "has_image_x":     (d / "image_x_1200x675.png").exists(),
            "has_image_tg":    (d / "image_tg_1080x1080.png").exists(),
            "has_reel_image":  (d / "image_reel_1080x1920.png").exists(),
            "has_reel_script": (d / "reel" / "01_script.txt").exists(),
            "has_reel_video":  (d / "reel_final.mp4").exists(),
            "published":       (d / "publish_log.json").exists(),
            "stories":         [],
            "story_count":     0,
            "post_x_preview":  "",
        }
        if (d / "top2.json").exists():
            try:
                data = json.loads((d / "top2.json").read_text(encoding="utf-8"))
                slot["stories"] = [s.get("title", "")[:90] for s in data.get("stories", [])]
                slot["story_count"] = data.get("total_candidates", 0)
            except Exception:
                pass
        if slot["has_post_x"]:
            slot["post_x_preview"] = (d / "post_x.txt").read_text(encoding="utf-8")[:140].strip()
        slots.append(slot)
    return slots


def get_slot_detail(name: str, queue_root: Path) -> dict[str, Any]:
    d = queue_root / name
    if not d.exists():
        raise HTTPException(status_code=404, detail=f"Slot '{name}' not found")

    def read(fname: str) -> str:
        p = d / fname
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""

    detail: dict[str, Any] = {
        "name":             name,
        "post_x":           read("post_x.txt"),
        "post_x_b":         read("post_x_b.txt"),
        "post_tg":          read("post_telegram.md"),
        "image_prompt":     read("image_prompt.txt"),
        "stories":          [],
        "total_candidates": 0,
        "picked_at":        "",
        "has_image_x":      (d / "image_x_1200x675.png").exists(),
        "has_image_tg":     (d / "image_tg_1080x1080.png").exists(),
        "publish_log":      None,
    }

    if (d / "top2.json").exists():
        try:
            data = json.loads((d / "top2.json").read_text(encoding="utf-8"))
            detail["stories"]          = data.get("stories", [])
            detail["total_candidates"] = data.get("total_candidates", 0)
            detail["picked_at"]        = data.get("picked_at", "")
        except Exception:
            pass

    for log_name in ("publish_log.json", "publish_log_dryrun.json"):
        if (d / log_name).exists():
            try:
                detail["publish_log"] = json.loads((d / log_name).read_text(encoding="utf-8"))
                break
            except Exception:
                pass

    detail["reel_video"]      = (d / "reel_avatar.mp4").exists() or (d / "reel_final.mp4").exists()
    detail["has_reel_script"] = (d / "reel" / "01_script.txt").exists()
    detail["has_reel_image"]  = (d / "image_reel_1080x1920.png").exists()

    # Avatar provider chain status — written by avatar_video.build_avatar_reel
    # so the slot page can show WHICH provider rendered the clip (or why
    # all of them failed and we fell back to the branded path).
    avatar_status_path = d / "_avatar_status.json"
    if avatar_status_path.exists():
        try:
            detail["avatar_status"] = json.loads(
                avatar_status_path.read_text(encoding="utf-8")
            )
        except Exception:
            detail["avatar_status"] = None
    else:
        detail["avatar_status"] = None

    return detail


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    return _render("login.html", error=error)


@app.post("/login")
async def login_submit(request: Request,
                       email: str = Form(...), password: str = Form(...)):
    ip = _client_ip(request) or "unknown"
    ua = (request.headers.get("user-agent") or "")[:255]
    if not _login_rate_ok(ip):
        return _render("login.html",
                       error="Prea multe încercări eșuate. Încearcă din nou în 5 minute.")
    user = get_user_by_email(email)
    if not user or not _verify_pw(password, user["password_hash"]):
        # Log failed attempt with geo so admin can spot brute-force from
        # specific countries.
        try:
            from src.dashboard.geo import geolocate_ip
            from src.dashboard.database import log_login as _db_log_login
            geo = geolocate_ip(ip) if user else {}
            if user:
                _db_log_login(user["id"], ip=ip,
                               country_code=geo.get("country_code", ""),
                               country_name=geo.get("country_name", ""),
                               city=geo.get("city", ""),
                               user_agent=ua, success=False)
        except Exception as exc:
            print(f"[login] failed-log skipped: {exc}")
        return _render("login.html", error="Email sau parolă incorectă.")
    if not user["is_approved"]:
        return _render("login.html", error="Contul tău așteaptă aprobarea adminului.")
    _login_rate_clear(ip)

    # ── Geo-log + suspicious-country alert ─────────────────────────────────
    try:
        from src.dashboard.geo import geolocate_ip, country_label
        from src.dashboard.database import (log_login as _db_log_login,
                                              previous_login_country)
        geo = geolocate_ip(ip)
        prev_cc = previous_login_country(user["id"])
        new_cc  = (geo.get("country_code") or "").upper()
        _db_log_login(user["id"], ip=ip,
                       country_code=new_cc,
                       country_name=geo.get("country_name", ""),
                       city=geo.get("city", ""),
                       user_agent=ua, success=True)
        # Alert when login happens from a different country than the prior one.
        if prev_cc and new_cc and prev_cc != new_cc:
            _alert_login_from_new_country(user, ip, geo, prev_cc)
    except Exception as exc:
        print(f"[login] geo-log skipped: {exc}")

    token  = _new_session(user["id"])
    secure = os.environ.get("HTTPS_ONLY", "").lower() in ("1", "true", "yes")
    resp   = RedirectResponse("/", status_code=302)
    resp.set_cookie(COOKIE, token, max_age=COOKIE_AGE,
                    httponly=True, samesite="lax", secure=secure)
    return resp


def _alert_login_from_new_country(user: dict, ip: str, geo: dict,
                                    prev_cc: str) -> None:
    """Send a Telegram alert to the configured alert channel.

    Picks credentials in this order:
      1) per-user telegram_alert_chat + telegram_token (Settings)
      2) env TELEGRAM_ALERT_CHAT + TELEGRAM_BOT_TOKEN
    Silent no-op if neither is configured.
    """
    try:
        s   = get_user_settings(user["id"]) or {}
        bot = (s.get("telegram_token") or
                os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
        chat = (s.get("telegram_alert_chat") or
                 os.environ.get("TELEGRAM_ALERT_CHAT", "")).strip()
        if not bot or not chat:
            return
        from src.dashboard.geo import country_label
        loc = country_label(geo) or "necunoscut"
        msg = (f"🛂 Login nou — {user.get('email', '?')}\n"
                f"Locație: {loc}\n"
                f"IP: {ip}\n"
                f"Țara anterioară: {prev_cc}\n"
                f"Dacă nu ai fost tu, schimbă parola imediat.")
        import urllib.request as _ureq, urllib.parse as _up, json as _j
        req = _ureq.Request(
            f"https://api.telegram.org/bot{bot}/sendMessage",
            data=_j.dumps({"chat_id": chat, "text": msg,
                            "disable_web_page_preview": True}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _ureq.urlopen(req, timeout=8) as resp:
            resp.read()
    except Exception as exc:
        print(f"[login] Telegram alert failed: {exc}")


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    captcha_q, captcha_token = _captcha_generate()
    return _render("register.html", captcha_q=captcha_q, captcha_token=captcha_token)


@app.post("/register")
async def register_submit(request: Request,
                          email: str = Form(...),
                          password: str = Form(...),
                          password2: str = Form(...),
                          display_name: str = Form(""),
                          captcha_answer: str = Form(""),
                          captcha_token: str = Form("")):
    # Regenerate CAPTCHA for any error response
    def _fresh_captcha():
        q, tok = _captcha_generate()
        return {"captcha_q": q, "captcha_token": tok}

    if not _captcha_verify(captcha_token, captcha_answer):
        return _render("register.html", error="Răspuns incorect la verificarea CAPTCHA. Încearcă din nou.",
                       **_fresh_captcha())
    if password != password2:
        return _render("register.html", error="Passwords do not match.", **_fresh_captcha())
    if len(password) < 8:
        return _render("register.html", error="Password must be at least 8 characters.", **_fresh_captcha())
    if get_user_by_email(email):
        return _render("register.html", error="Email already registered.", **_fresh_captcha())
    new_uid = create_user(email, _hash_pw(password), display_name)

    # Auto-approve all new users — they choose a plan on the next screen.
    from src.dashboard.database import approve_user as _approve
    _approve(new_uid)

    # ── Auto-detect timezone from signup IP and seed posting times ─────────
    try:
        from src.dashboard.geo import geolocate_ip, timezone_offset_hours
        ip = _client_ip(request)
        offset_h = timezone_offset_hours(ip)
        if offset_h is not None:
            geo = geolocate_ip(ip)
            from src.dashboard.database import save_user_settings as _save
            _save(new_uid, _is_admin=True,
                  timezone_id=(geo.get("timezone_id") or ""),
                  signup_country=(geo.get("country_code") or ""),
                  signup_city=(geo.get("city") or ""))
            print(f"[signup] {email}: geo={geo.get('country_code')} "
                  f"tz={geo.get('timezone_id')} offset={offset_h:+.1f}h")
    except Exception as exc:
        print(f"[signup] geolocate failed (non-fatal): {exc}")

    # Auto-login and send to pricing so the user can pick a plan immediately.
    secure = os.environ.get("HTTPS_ONLY", "").lower() in ("1", "true", "yes")
    token  = _new_session(new_uid)
    resp   = RedirectResponse("/pricing", status_code=302)
    resp.set_cookie(COOKIE, token, max_age=COOKIE_AGE,
                    httponly=True, samesite="lax", secure=secure)
    return resp


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(COOKIE)
    if token:
        _end_session(token)
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE)
    return resp


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: str = "", csrf_error: str = ""):
    user = request.state.user
    s = get_user_settings(user["id"])
    return _render("settings.html", user=user, s=s, saved=saved,
                   csrf_error=csrf_error,
                   csrf_token=getattr(request.state, "csrf", ""))


def _csrf_ok(request: Request, form_data) -> bool:
    """Return True if CSRF token in form matches the session-derived token."""
    expected  = getattr(request.state, "csrf", "")
    submitted = form_data.get("_csrf", "")
    if not expected:
        return True  # unauthenticated path — skip (auth middleware already blocked)
    return bool(submitted) and secrets.compare_digest(expected, submitted)


@app.post("/settings")
async def settings_save(request: Request):
    """Save user settings.

    Uses raw form data so we can add new fields (video profile, etc.) without
    touching this handler. save_user_settings() enforces a whitelist at the
    DB layer, so unknown fields are safely ignored.
    """
    form = await request.form()
    if not _csrf_ok(request, form):
        return RedirectResponse("/settings?csrf_error=1", status_code=303)
    data: dict[str, Any] = {}
    # Text/select/number fields — take as-is (empty string means "clear")
    text_keys = {
        "telegram_token", "telegram_channel",
        "x_api_key", "x_api_secret", "x_access_token", "x_access_secret",
        "make_x_webhook_url", "x_username", "x_email", "x_password",
        "did_api_key", "did_email", "did_presenter_url",
        "replicate_api_token", "replicate_presenter_url", "avatar_provider",
        "elevenlabs_key", "anthropic_key", "heygen_api_key",
        "video_style", "video_brand_color", "video_brand_handle", "video_brand_cta",
        "post_time_morning", "post_time_evening", "language", "niche",
        "telegram_alert_chat",
        # Facebook
        "fb_access_token", "fb_page_id",
        # Email SMTP
        "smtp_host", "smtp_user", "smtp_pass", "smtp_from",
        # Webhook inbound
        "webhook_secret",
    }
    int_keys = {
        "video_duration_tiktok", "video_duration_instagram",
        "video_duration_youtube_shorts", "video_bgm_volume",
        "smtp_port",
    }
    # Checkbox fields — present in form = 1, absent = 0
    checkbox_keys = {
        "video_intro_enabled", "video_outro_enabled", "video_bgm_enabled",
        "email_notify_heygen", "email_notify_fail", "email_notify_digest",
    }
    for k in text_keys:
        if k in form:
            data[k] = str(form[k])
    for k in int_keys:
        if k in form:
            try:
                data[k] = int(form[k])
            except (TypeError, ValueError):
                pass
    for k in checkbox_keys:
        data[k] = 1 if form.get(k) else 0

    is_admin = bool(request.state.user.get("is_admin"))
    # Strip admin-only fields for non-admin users — server-side enforcement
    if not is_admin:
        for f in _ADMIN_ONLY_FIELDS:
            data.pop(f, None)
    save_user_settings(request.state.user["id"], _is_admin=is_admin, **data)
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/api/set-language")
async def api_set_language(request: Request, lang: str = Form("")):
    from fastapi.responses import JSONResponse
    lang = lang.strip().lower()
    if lang not in ("en", "ro"):
        return JSONResponse({"ok": False})
    user_id = request.state.user.get("id") if hasattr(request.state, "user") else None
    if user_id:
        save_user_settings(user_id, language=lang)
    return JSONResponse({"ok": True, "lang": lang})


@app.post("/api/test-alert-chat")
async def test_alert_chat(request: Request):
    """Send a test message to the configured telegram_alert_chat. Admin only."""
    _require_admin(request)
    from fastapi.responses import JSONResponse
    settings = get_user_settings(request.state.user["id"])
    token   = (settings.get("telegram_token") or "").strip()
    chat_id = (settings.get("telegram_alert_chat") or "").strip()

    if not token:
        return JSONResponse({"ok": False, "error": "Telegram Bot Token nu e setat în Settings."})
    if not chat_id:
        return JSONResponse({"ok": False, "error": "Canal Alerte (telegram_alert_chat) nu e setat în Settings."})

    try:
        import urllib.parse, urllib.request
        text = "[AutoPost] ✅ Test canal alerte — funcționează corect! Erorile botului vor fi trimise aici, NU în canalul de știri."
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=8) as resp:
            result = json.loads(resp.read())
        if result.get("ok"):
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "error": str(result)})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})


@app.post("/api/test-did-key")
async def test_did_key(request: Request, did_api_key: str = Form("")):
    """Verify a D-ID API key by calling /credits."""
    from fastapi.responses import JSONResponse
    import base64 as _b64
    key = did_api_key.strip()
    if not key:
        return JSONResponse({"ok": False, "error": "Cheia e goală."})
    # Build auth header: if raw email:secret -> base64 encode it
    if ":" in key and "@" in key.split(":", 1)[0]:
        encoded = _b64.b64encode(key.encode("utf-8")).decode("ascii")
        auth = f"Basic {encoded}"
    else:
        auth = f"Basic {key}"
    try:
        import urllib.request as _ur
        req = _ur.Request(
            "https://api.d-id.com/credits",
            headers={"Authorization": auth, "Accept": "application/json"},
        )
        with _ur.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        remaining = data.get("remaining") or data.get("total") or "?"
        return JSONResponse({"ok": True, "message": f"D-ID OK — {remaining} credite rămase"})
    except Exception as exc:
        msg = str(exc)
        if "401" in msg:
            return JSONResponse({"ok": False, "error": "401 Unauthorized — cheie greșită sau expirată."})
        if "403" in msg:
            return JSONResponse({"ok": False, "error": "403 Forbidden — cheie validă dar fără permisiuni."})
        return JSONResponse({"ok": False, "error": msg[:200]})


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

def _require_admin(request: Request):
    if not request.state.user.get("is_admin"):
        raise HTTPException(403, "Admin access required")


@app.get("/admin/backoffice", response_class=HTMLResponse)
async def admin_backoffice(request: Request):
    _require_admin(request)

    all_users = get_all_users()
    today = datetime.now().strftime("%Y-%m-%d")

    # Enrich users with slot/publish counts
    for u in all_users:
        uq = BASE_QUEUE / str(u["id"])
        slots = list(uq.iterdir()) if uq.exists() else []
        u["slot_count"] = len([d for d in slots if d.is_dir()])
        u["published_count"] = len([
            d for d in slots
            if d.is_dir() and (d / "publish_log.json").exists()
        ])

    # System stats
    total_slots = sum(u["slot_count"] for u in all_users)
    published_today = sum(
        1 for u in all_users
        for uq in [(BASE_QUEUE / str(u["id"]))]
        if uq.exists()
        for d in uq.iterdir()
        if d.is_dir() and today in d.name and (d / "publish_log.json").exists()
    )

    log_tail: list[str] = []
    if LOG_FILE.exists():
        log_tail = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]

    # Collect the 6 most recent avatar reels across all users
    import json as _json_bo
    recent_videos: list[dict] = []
    for u in all_users:
        uq = BASE_QUEUE / str(u["id"])
        if not uq.exists():
            continue
        for slot_dir in uq.iterdir():
            if not slot_dir.is_dir():
                continue
            for vid_name in ("reel_avatar.mp4", "reel_final.mp4", "reel_branded.mp4"):
                vpath = slot_dir / vid_name
                if vpath.exists():
                    size_mb = round(vpath.stat().st_size / 1_048_576, 1)
                    topic = ""
                    try:
                        t2 = slot_dir / "top2.json"
                        if t2.exists():
                            d2 = _json_bo.loads(t2.read_text(encoding="utf-8", errors="replace"))
                            stories = d2 if isinstance(d2, list) else d2.get("stories", [])
                            if stories:
                                topic = (stories[0].get("title") or stories[0].get("headline") or "")[:60]
                    except Exception:
                        pass
                    import datetime as _dt_bo
                    mtime_str = _dt_bo.datetime.fromtimestamp(
                        vpath.stat().st_mtime
                    ).strftime("%Y-%m-%d %H:%M")
                    recent_videos.append({
                        "url": f"/media/{u['id']}/{slot_dir.name}/{vid_name}",
                        "slot": slot_dir.name,
                        "user": u.get("display_name") or u.get("email", "?").split("@")[0],
                        "size_mb": size_mb,
                        "mtime": mtime_str,
                        "topic": topic or slot_dir.name,
                        "type": vid_name.replace(".mp4", "").replace("reel_", ""),
                    })
                    break  # one video per slot
    recent_videos.sort(key=lambda x: x["mtime"], reverse=True)
    recent_videos = recent_videos[:6]

    stats = {
        "total_users":    len(all_users),
        "pending_users":  len([u for u in all_users if not u["is_approved"]]),
        "total_slots":    total_slots,
        "published_today": published_today,
    }

    return _render("admin_backoffice.html",
                   user=request.state.user,
                   bot=_bot_status(),
                   all_users=all_users,
                   stats=stats,
                   log_tail=log_tail,
                   recent_videos=recent_videos,
                   **_user_ctx(request))


@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request, msg: str = ""):
    _require_admin(request)
    return _render("admin_users.html",
                   user=request.state.user,
                   pending=get_pending_users(),
                   all_users=get_all_users(),
                   msg=msg)


@app.post("/admin/users/{user_id}/approve")
async def admin_approve(request: Request, user_id: int):
    _require_admin(request)
    approve_user(user_id)
    return RedirectResponse("/admin/users?msg=User+approved", status_code=303)


@app.post("/admin/users/{user_id}/reject")
async def admin_reject(request: Request, user_id: int):
    _require_admin(request)
    reject_user(user_id)
    return RedirectResponse("/admin/users?msg=User+removed", status_code=303)


@app.post("/admin/users/{user_id}/delete")
async def admin_delete_user(request: Request, user_id: int):
    """Permanently delete an approved user and all their data."""
    _require_admin(request)
    me = request.state.user
    if user_id == me["id"]:
        return RedirectResponse("/admin/users?msg=Nu+te+poti+sterge+pe+tine+insuti",
                                status_code=303)
    delete_user(user_id)
    return RedirectResponse("/admin/users?msg=User+sters+definitiv", status_code=303)


@app.post("/admin/users/{user_id}/edit")
async def admin_edit_user(request: Request, user_id: int,
                           display_name: str = Form(""),
                           email: str = Form(""),
                           is_admin: str = Form("0"),
                           is_approved: str = Form("1")):
    """Edit user profile fields from the admin panel."""
    _require_admin(request)
    me = request.state.user
    # Prevent removing own admin role
    if user_id == me["id"] and is_admin == "0":
        return RedirectResponse("/admin/users?msg=Nu+iti+poti+elimina+propriul+rol+de+admin",
                                status_code=303)
    update_user(
        user_id,
        display_name=display_name or None,
        email=email or None,
        is_admin=int(is_admin == "1"),
        is_approved=int(is_approved == "1"),
    )
    return RedirectResponse(f"/admin/users?msg=User+{user_id}+actualizat", status_code=303)


@app.get("/admin/login-logs", response_class=HTMLResponse)
async def admin_login_logs(request: Request, limit: int = 200):
    """Admin view of all login activity with IP + country + user agent."""
    _require_admin(request)
    logs = get_all_login_logs(limit=limit)
    return _render("admin_login_logs.html",
                   user=request.state.user,
                   logs=logs,
                   limit=limit)


@app.get("/api/admin/login-logs")
async def api_admin_login_logs(request: Request, limit: int = 200):
    """JSON endpoint for login logs (for admin dashboard widgets)."""
    _require_admin(request)
    return get_all_login_logs(limit=limit)


# ---------------------------------------------------------------------------
# Dashboard pages
# ---------------------------------------------------------------------------

def _user_ctx(request: Request) -> dict:
    """Common template context for all pages."""
    u = request.state.user
    pending_count = len(get_pending_users()) if u.get("is_admin") else 0
    return {"current_user": u, "pending_count": pending_count}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, triggered: str = ""):
    qr = _uqueue(request)
    slots = get_slots(qr)
    published_today = sum(
        1 for s in slots
        if s["published"] and datetime.now().strftime("%Y-%m-%d") in s["name"]
    )
    log_tail: list[str] = []
    if LOG_FILE.exists():
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        log_tail = lines[-40:]
    bot = _bot_status()
    _u_settings = get_user_settings(request.state.user["id"])
    user_niche = _u_settings.get("niche", "crypto") or "crypto"
    morning_time = (_u_settings.get("post_time_morning") or "08:00").strip() or "08:00"
    evening_time = (_u_settings.get("post_time_evening") or "18:00").strip() or "18:00"

    # Viral Scout summary for dashboard card
    viral_summary: dict = {"sample_size": 0, "hooks": 0, "hashtags": 0, "last_scan": ""}
    try:
        _bp_path = _REPO / "data" / "viral_blueprint.json"
        if _bp_path.exists():
            _bp = _json.loads(_bp_path.read_text(encoding="utf-8"))
            viral_summary = {
                "sample_size": _bp.get("sample_size", 0),
                "hooks":       len(_bp.get("hook_patterns") or []),
                "hashtags":    len(_bp.get("top_hashtags") or []),
                "last_scan":   (_bp.get("generated_at") or "")[:10],
            }
    except Exception:
        pass

    # Competitors summary for dashboard card
    comp_summary: dict = {"count": 0, "last_scanned": "Niciodată"}
    try:
        _comps = get_competitors(request.state.user["id"])
        _last = max(
            (c.get("last_scanned_at") or "" for c in _comps), default=""
        )
        comp_summary = {
            "count":        len(_comps),
            "last_scanned": _last[:10] if _last else "Niciodată",
        }
    except Exception:
        pass

    heygen_count = get_heygen_count(request.state.user["id"])
    pending_scheduled = get_scheduled_posts(request.state.user["id"], status="pending")

    return _render("index.html",
                   slots=slots, total=len(slots),
                   published_today=published_today,
                   log_tail=log_tail,
                   triggered=triggered,
                   bot=bot,
                   user_niche=user_niche,
                   morning_time=morning_time,
                   evening_time=evening_time,
                   viral_summary=viral_summary,
                   comp_summary=comp_summary,
                   heygen_count=heygen_count,
                   pending_scheduled=len(pending_scheduled),
                   **_user_ctx(request))


@app.get("/slot/{name}", response_class=HTMLResponse)
async def slot_page(request: Request, name: str,
                    saved: str = "", regenerated: str = "", published: str = ""):
    detail = get_slot_detail(name, _uqueue(request))
    return _render("slot.html", **detail,
                   saved=saved, regenerated=regenerated, published=published,
                   **_user_ctx(request))


# ---------------------------------------------------------------------------
# Slot actions
# ---------------------------------------------------------------------------

@app.post("/slot/{name}/save-x")
async def save_x(request: Request, name: str,
                 content: str = Form(...), variant: str = Form("a")):
    d = _uqueue(request) / name
    fname = "post_x_b.txt" if variant == "b" else "post_x.txt"
    (d / fname).write_text(content + "\n", encoding="utf-8")
    return RedirectResponse(f"/slot/{name}?saved=x_{variant}", status_code=303)


@app.post("/slot/{name}/save-tg")
async def save_tg(request: Request, name: str, content: str = Form(...)):
    d = _uqueue(request) / name
    (d / "post_telegram.md").write_text(content + "\n", encoding="utf-8")
    return RedirectResponse(f"/slot/{name}?saved=tg", status_code=303)


@app.post("/slot/{name}/regenerate")
async def regenerate(request: Request, name: str):
    d = _uqueue(request) / name
    top2 = d / "top2.json"
    if not top2.exists():
        raise HTTPException(400, "No top2.json found in this slot")
    try:
        from src.agents.copywriter import write_outputs
        data = json.loads(top2.read_text(encoding="utf-8"))
        stories = data.get("stories", [])
        if not stories:
            raise HTTPException(400, "No stories in top2.json")
        write_outputs(stories[0], d)
        return RedirectResponse(f"/slot/{name}?regenerated=1", status_code=303)
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/slot/{name}/delete")
async def delete_slot(request: Request, name: str):
    d = _uqueue(request) / name
    if d.exists():
        shutil.rmtree(d)
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# X OAuth 2.0 — per-user "Connect X" flow (SaaS multi-tenant path)
# ---------------------------------------------------------------------------

# In-memory state ↔ verifier map. Keyed by random `state` produced in /x/connect.
# Small TTL is fine: the callback happens within seconds of the redirect.
_x_oauth_states: dict[str, dict] = {}


def _gc_oauth_states():
    """Drop OAuth pending states older than 10 minutes."""
    import time as _t
    now = _t.time()
    dead = [k for k, v in _x_oauth_states.items() if now - v.get("ts", 0) > 600]
    for k in dead:
        _x_oauth_states.pop(k, None)


@app.get("/x/connect")
async def x_connect(request: Request):
    """Kick off the X OAuth 2.0 PKCE flow for the logged-in user."""
    _gc_oauth_states()
    uid = request.state.user["id"]
    try:
        from src.agents.x_oauth import build_authorize_url
    except ImportError as exc:
        return JSONResponse({"error": f"x_oauth import failed: {exc}"}, status_code=500)

    try:
        state = secrets.token_urlsafe(24)
        url, verifier = build_authorize_url(state)
    except Exception as exc:
        return HTMLResponse(
            f"<h1>X OAuth not configured</h1>"
            f"<p><b>Eroare:</b> {exc}</p>"
            f"<p>Admin-ul trebuie să seteze în <code>.env</code>:</p>"
            f"<pre>X_OAUTH_CLIENT_ID=...\nX_OAUTH_CLIENT_SECRET=...\n"
            f"X_OAUTH_REDIRECT_URI=https://domeniu/x/callback</pre>"
            f"<p>Creează un app la "
            f"<a href='https://developer.x.com/en/portal/projects-and-apps'>developer.x.com</a> "
            f"și activează OAuth 2.0 cu scopes "
            f"<code>tweet.read tweet.write users.read offline.access media.write</code>.</p>"
            f"<p><a href='/settings'>← Înapoi la Settings</a></p>",
            status_code=500,
        )

    import time as _t
    _x_oauth_states[state] = {"user_id": uid, "verifier": verifier, "ts": _t.time()}
    return RedirectResponse(url, status_code=302)


@app.get("/x/callback")
async def x_callback(request: Request, code: str = "", state: str = "",
                     error: str = "", error_description: str = ""):
    """Receive the auth code from X, exchange for tokens, store per-user."""
    _gc_oauth_states()
    if error:
        return HTMLResponse(
            f"<h1>Autorizarea X a fost refuzată</h1>"
            f"<p><b>{error}</b>: {error_description}</p>"
            f"<p><a href='/settings'>← Înapoi la Settings</a></p>",
            status_code=400,
        )

    ctx = _x_oauth_states.pop(state, None)
    if not ctx:
        return HTMLResponse(
            "<h1>State invalid sau expirat</h1>"
            "<p>Procesul OAuth a durat prea mult. Reîncearcă.</p>"
            "<p><a href='/settings'>← Settings</a></p>",
            status_code=400,
        )

    # Sanity: the user who started /x/connect must be the one hitting /x/callback.
    current_uid = request.state.user["id"]
    if ctx["user_id"] != current_uid:
        return HTMLResponse(
            "<h1>User mismatch</h1><p>Conectează-te cu contul care a început autorizarea.</p>",
            status_code=403,
        )

    try:
        from src.agents.x_oauth import exchange_code, get_me
        tok = exchange_code(code, ctx["verifier"])
    except Exception as exc:
        return HTMLResponse(
            f"<h1>Schimbul de token X a eșuat</h1>"
            f"<p><code>{exc}</code></p>"
            f"<p><a href='/x/connect'>↻ Reîncearcă</a> | "
            f"<a href='/settings'>← Settings</a></p>",
            status_code=500,
        )

    # Fetch user identity for the UI ("Connected as @handle")
    handle = ""
    x_user_id = ""
    try:
        me = get_me(tok["access_token"])
        handle    = me.get("username", "")
        x_user_id = me.get("id", "")
    except Exception as exc:
        print(f"[x_callback] /users/me failed (non-fatal): {exc}")

    save_user_settings(
        current_uid,
        x_oauth_access_token  = tok["access_token"],
        x_oauth_refresh_token = tok.get("refresh_token", ""),
        x_oauth_expires_at    = int(tok["_expires_at"]),
        x_oauth_scope         = tok.get("scope", ""),
        x_oauth_username      = handle,
        x_oauth_user_id       = x_user_id,
    )

    return HTMLResponse(
        f"<h1>✓ X conectat cu succes</h1>"
        f"<p>Contul <b>@{handle or 'necunoscut'}</b> poate acum posta automat "
        f"prin aplicația ta.</p>"
        f"<p>Tokenul se reînnoiește singur — nu mai trebuie să faci nimic.</p>"
        f"<p><a href='/settings'>← Înapoi la Settings</a></p>",
        status_code=200,
    )


@app.post("/x/disconnect")
async def x_disconnect(request: Request):
    """Revoke the stored X OAuth tokens for the current user."""
    uid = request.state.user["id"]
    save_user_settings(
        uid,
        x_oauth_access_token  = "",
        x_oauth_refresh_token = "",
        x_oauth_expires_at    = 0,
        x_oauth_scope         = "",
        x_oauth_username      = "",
        x_oauth_user_id       = "",
    )
    return RedirectResponse("/settings", status_code=303)


# ---------------------------------------------------------------------------
# X "paste cookies" — zero-API, free, 10-sec setup
# ---------------------------------------------------------------------------

@app.post("/api/x/cookies-save")
async def x_cookies_save(request: Request, cookies_json: str = Form("")):
    """Validate + save pasted cookies. Whoami is best-effort (not blocking)."""
    uid = request.state.user["id"]
    try:
        from src.agents.x_cookies import validate_cookies, whoami_via_cookies
    except ImportError as exc:
        return JSONResponse({"error": f"x_cookies import failed: {exc}"}, status_code=500)

    # STAGE A — pure offline validation. If this fails, the paste is broken.
    cookies, err = validate_cookies(cookies_json)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    # STAGE B — best-effort identity check. DO NOT block save if this fails:
    # the whoami endpoint/bearer may be rate-limited, blocked by antivirus,
    # or temporarily rejecting. The cookies can still be perfectly good —
    # the user can confirm with the 🩺 Test button, or just publish.
    handle = ""
    whoami_warning = ""
    try:
        h, why_err = whoami_via_cookies(cookies)
        if h:
            handle = h
        elif why_err:
            whoami_warning = why_err
    except Exception as exc:
        whoami_warning = str(exc)

    # Persist the cookies regardless of whoami result.
    import json as _json
    save_user_settings(
        uid,
        x_cookies_json       = _json.dumps(cookies, ensure_ascii=False),
        x_cookies_handle     = handle,
        x_cookies_updated_at = datetime.now(timezone.utc).isoformat(),
    )
    return JSONResponse({
        "ok":           True,
        "handle":       handle,
        "cookie_count": len(cookies),
        "warning":      whoami_warning,   # UI can display this as a gentle note
    })


@app.post("/api/x/cookies-test")
async def x_cookies_test(request: Request):
    """Ping X with the saved cookies — returns the detected @handle."""
    uid = request.state.user["id"]
    s = get_user_settings(uid)
    raw = (s.get("x_cookies_json") or "").strip()
    if not raw:
        return JSONResponse({"error": "no cookies saved"}, status_code=400)

    try:
        from src.agents.x_cookies import validate_cookies, whoami_via_cookies
    except ImportError as exc:
        return JSONResponse({"error": f"import failed: {exc}"}, status_code=500)

    cookies, err = validate_cookies(raw)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    handle, why_err = whoami_via_cookies(cookies)
    if handle:
        return JSONResponse({"ok": True, "handle": handle})
    # Handle empty. Distinguish hard-fail (explicit 401/403 from X) from
    # soft-fail (X web API moved/blocked us — cookies might still work).
    msg = (why_err or "").lower()
    if "rejected" in msg or "expired" in msg or " 401" in msg or " 403" in msg:
        return JSONResponse({"error": why_err}, status_code=400)
    # Soft fail — return 200 so the UI shows a warning, not an error card.
    return JSONResponse({
        "ok":      True,
        "handle":  "",
        "warning": why_err or "Could not verify handle. Cookies look OK; "
                             "the real test is publishing a post.",
    })


@app.post("/x/cookies-clear")
async def x_cookies_clear(request: Request):
    uid = request.state.user["id"]
    save_user_settings(
        uid,
        x_cookies_json       = "",
        x_cookies_handle     = "",
        x_cookies_updated_at = "",
    )
    return RedirectResponse("/settings", status_code=303)


@app.get("/api/x/status")
async def x_status(request: Request):
    """Return the user's current X connection status (for the settings page)."""
    uid = request.state.user["id"]
    s = get_user_settings(uid)
    connected = bool((s.get("x_oauth_access_token") or "").strip())
    return JSONResponse({
        "connected":  connected,
        "username":   s.get("x_oauth_username") or "",
        "expires_at": int(s.get("x_oauth_expires_at") or 0),
        "scope":      s.get("x_oauth_scope") or "",
    })


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------

@app.post("/api/slot/{name}/publish-live")
async def publish_live_api(request: Request, name: str,
                           platforms: str = Form("telegram,x"),
                           variant: str = Form(""),
                           dry_run: str = Form("false")):
    # EVERYTHING wrapped in try/except so we always return JSON, never
    # FastAPI's default plain-text 500 (which breaks the frontend JSON.parse).
    try:
        d = _uqueue(request) / name
        if not d.exists():
            return JSONResponse({"error": f"Slot '{name}' not found"}, status_code=404)

        plat_set = {p.strip() for p in platforms.split(",") if p.strip()}
        if not plat_set:
            return JSONResponse({"error": "No platforms selected"}, status_code=400)

        # Pre-flight: if any required image is missing, try to render it NOW
        # (synchronously, with local gradient fallback). Background render may
        # have failed silently (Pollinations down, etc.) — we'd rather recover
        # and publish than block the user.
        needs_x = "x" in plat_set
        needs_tg = "telegram" in plat_set
        img_x = d / "image_x_1200x675.png"
        img_tg = d / "image_tg_1080x1080.png"

        def _ensure_image(path: Path, w: int, h: int) -> bool:
            """Try to render a missing image via render_card. Returns True on success."""
            if path.exists():
                return True
            try:
                from src.agents.image_gen import render_card
                story: dict = {}
                top2 = d / "top2.json"
                if top2.exists():
                    try:
                        stories = json.loads(top2.read_text(encoding="utf-8")).get("stories") or []
                        if stories:
                            story = stories[0]
                    except Exception:
                        pass
                render_card(story, w, h, path, seed=42)
                return path.exists()
            except Exception as exc:
                print(f"[publish] on-demand render of {path.name} failed: {exc}")
                return False

        pending = []
        if needs_x and not _ensure_image(img_x, 1200, 675):
            pending.append("X")
        if needs_tg and not _ensure_image(img_tg, 1080, 1080):
            pending.append("Telegram")
        if pending:
            return JSONResponse(
                {"error": f"Nu s-a putut genera imaginea pentru {', '.join(pending)}. "
                          f"Click dreapta pe imagine → Regenerează, sau retry."},
                status_code=500,
            )

        is_dry = dry_run.lower() in ("true", "1", "yes")
        variant_arg = variant.strip().upper() if variant.strip() in ("A", "B") else None

        try:
            uid = request.state.user["id"]
        except (AttributeError, KeyError, TypeError) as exc:
            return JSONResponse({"error": f"Auth required: {exc}"}, status_code=401)

        try:
            settings = get_user_settings(uid)
        except Exception as exc:
            return JSONResponse(
                {"error": f"Failed to load user settings: {exc}"},
                status_code=500,
            )

        # Refresh X OAuth 2.0 access_token if it's about to expire — avoids a
        # 401 mid-publish and keeps the publisher stateless.
        if "x" in plat_set and not is_dry:
            try:
                settings = _ensure_x_oauth_fresh(uid, settings)
            except Exception as exc:
                return JSONResponse(
                    {"error": f"X OAuth refresh failed: {exc}. "
                              f"Please reconnect X in Settings."},
                    status_code=400,
                )

        try:
            from src.agents.publisher import publish
        except Exception as exc:
            import traceback
            return JSONResponse(
                {"error": f"Publisher import failed: {exc}",
                 "traceback": traceback.format_exc()[:1000]},
                status_code=500,
            )

        loop = asyncio.get_event_loop()

        def _run():
            with _uenv(settings):
                return publish(queue_dir=d, platforms=plat_set,
                               dry_run=is_dry, variant=variant_arg)

        try:
            result = await loop.run_in_executor(_publish_executor, _run)
        except (Exception, SystemExit) as exc:
            import traceback
            # Log publish failure + send email notification
            try:
                _send_email_notification(uid, "publish_fail",
                    subject=f"Publicare eșuată — {name}",
                    body=f"Slot '{name}' nu a putut fi publicat pe {', '.join(plat_set)}.\nEroare: {exc}")
            except Exception:
                pass
            return JSONResponse(
                {"error": f"Publish failed: {exc}",
                 "traceback": traceback.format_exc()[:1500]},
                status_code=500,
            )

        # Log to publish_history DB
        if not is_dry:
            try:
                title = ""
                top2p = d / "top2.json"
                if top2p.exists():
                    try:
                        td = json.loads(top2p.read_text(encoding="utf-8"))
                        sts = td if isinstance(td, list) else td.get("stories", [])
                        title = (sts[0].get("title") or sts[0].get("headline") or "")[:120] if sts else ""
                    except Exception:
                        pass
                successes = [p for p, v in result.items()
                             if isinstance(v, dict) and v.get("ok")]
                failures = [p for p, v in result.items()
                            if isinstance(v, dict) and not v.get("ok")]
                log_publish_history(uid, name, title, list(plat_set), result)
                if failures:
                    _send_email_notification(uid, "publish_fail",
                        subject=f"Publicare parțial eșuată — {name}",
                        body=f"Publicat pe: {', '.join(successes)}\nEșuat pe: {', '.join(failures)}")
            except Exception as _pe:
                print(f"[publish] history log failed: {_pe}")

        return JSONResponse(result)
    except (Exception, SystemExit) as exc:
        # Last-resort catch-all — guarantees JSON response.
        # Catches SystemExit too (legacy raise SystemExit in library code).
        import traceback
        return JSONResponse(
            {"error": f"Unexpected error: {exc}",
             "traceback": traceback.format_exc()[:1500]},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Bot control (admin only)
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Cross-platform process existence check."""
    if platform.system() == "Windows":
        # tasklist is more reliable than os.kill on Windows
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in r.stdout
        except Exception:
            pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        # PermissionError = process exists but owned by another user → still alive
        return True


def _kill_pid(pid: int) -> None:
    """Cross-platform process kill."""
    try:
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


def _bot_status() -> dict[str, Any]:
    if not PID_FILE.exists():
        return {"running": False, "pid": None}
    try:
        pid = int(PID_FILE.read_text().strip())
    except Exception:
        PID_FILE.unlink(missing_ok=True)
        return {"running": False, "pid": None}
    if _pid_alive(pid):
        return {"running": True, "pid": pid}
    PID_FILE.unlink(missing_ok=True)
    return {"running": False, "pid": None}


@app.get("/api/bot-status")
async def api_bot_status(request: Request):
    return JSONResponse(_bot_status())


@app.post("/bot/start")
async def bot_start(request: Request):
    _require_admin(request)
    try:
        if _bot_status()["running"]:
            return RedirectResponse("/admin/backoffice?bot=already_running", status_code=303)
        kwargs: dict = {"cwd": str(_REPO)}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(
            [sys.executable, str(_REPO / "src" / "agents" / "scheduler.py")],
            **kwargs,
        )
        import time as _t; _t.sleep(1)   # give process 1s to start
        if proc.poll() is not None:       # already exited = startup crash
            raise RuntimeError(f"Scheduler exited immediately (code {proc.poll()}). Check scheduler.log.")
        PID_FILE.write_text(str(proc.pid))
    except Exception as exc:
        return RedirectResponse(f"/admin/backoffice?err={exc}", status_code=303)
    return RedirectResponse("/admin/backoffice?bot=started", status_code=303)


@app.post("/bot/stop")
async def bot_stop(request: Request):
    _require_admin(request)
    status = _bot_status()
    if not status["running"]:
        return RedirectResponse("/?bot=not_running", status_code=303)
    _kill_pid(status["pid"])
    PID_FILE.unlink(missing_ok=True)
    return RedirectResponse("/?bot=stopped", status_code=303)


@app.post("/bot/restart")
async def bot_restart(request: Request):
    """Stop the scheduler (if running) and start it again."""
    _require_admin(request)
    import time as _t
    status = _bot_status()
    if status["running"]:
        _kill_pid(status["pid"])
        PID_FILE.unlink(missing_ok=True)
        _t.sleep(1)
    try:
        kwargs: dict = {"cwd": str(_REPO)}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(
            [sys.executable, str(_REPO / "src" / "agents" / "scheduler.py")],
            **kwargs,
        )
        _t.sleep(1)
        if proc.poll() is not None:
            raise RuntimeError(f"Scheduler exited immediately (code {proc.poll()}).")
        PID_FILE.write_text(str(proc.pid))
    except Exception as exc:
        return RedirectResponse(f"/admin/backoffice?err={exc}", status_code=303)
    return RedirectResponse("/admin/backoffice?bot=restarted", status_code=303)


def _kill_all_schedulers() -> list[int]:
    """Kill every Python process that has scheduler.py in its command line.

    Returns list of PIDs killed.  Uses psutil for precise targeting so the
    dashboard server process itself is never touched.
    """
    killed: list[int] = []
    own_pid = os.getpid()
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if proc.pid == own_pid:
                    continue
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if "scheduler.py" in cmdline or "autopost" in cmdline.lower():
                    proc.kill()
                    killed.append(proc.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except ImportError:
        # psutil not available — fall back to killing the PID in PID_FILE
        status = _bot_status()
        if status["running"]:
            _kill_pid(status["pid"])
            killed.append(status["pid"])
    # Also kill whatever's in PID_FILE even if psutil missed it
    status = _bot_status()
    if status["running"] and status["pid"] not in killed:
        _kill_pid(status["pid"])
        killed.append(status["pid"])
    PID_FILE.unlink(missing_ok=True)
    return killed


@app.post("/api/emergency-stop")
async def emergency_stop(request: Request):
    """Write pause flag + kill all scheduler processes immediately."""
    _require_admin(request)
    PAUSE_FILE.write_text(
        f"Paused at {datetime.now(timezone.utc).isoformat()} by admin",
        encoding="utf-8",
    )
    killed = _kill_all_schedulers()
    return JSONResponse({"status": "stopped", "killed_pids": killed,
                         "message": f"Sistem oprit. {len(killed)} proces(e) terminate."})


@app.post("/api/emergency-resume")
async def emergency_resume(request: Request):
    """Remove pause flag (and optionally restart scheduler)."""
    _require_admin(request)
    PAUSE_FILE.unlink(missing_ok=True)
    # Auto-restart scheduler after resume
    import time as _t
    try:
        kwargs: dict = {"cwd": str(_REPO)}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(
            [sys.executable, str(_REPO / "src" / "agents" / "scheduler.py")],
            **kwargs,
        )
        _t.sleep(1)
        if proc.poll() is None:
            PID_FILE.write_text(str(proc.pid))
            return JSONResponse({"status": "resumed", "scheduler_pid": proc.pid,
                                 "message": "Flag șters. Scheduler repornit."})
    except Exception as exc:
        return JSONResponse({"status": "resumed_no_scheduler",
                             "message": f"Flag șters. Scheduler nu a pornit: {exc}"})
    return JSONResponse({"status": "resumed", "message": "Flag șters."})


@app.get("/api/health")
async def api_health(request: Request, force: bool = False):
    """Return cached platform health report (refreshes if stale or force=true)."""
    _require_admin(request)
    try:
        from src.agents.health_monitor import run_checks, load_cache, is_stale
        if force or is_stale():
            report = run_checks()
        else:
            report = load_cache()
        return JSONResponse(report or {"overall": "unknown", "platforms": {}})
    except Exception as exc:
        return JSONResponse({"overall": "error", "error": str(exc), "platforms": {}})


@app.get("/api/system-status")
async def system_status(request: Request):
    """Returns paused state + bot running state."""
    _require_admin(request)
    bot = _bot_status()
    return JSONResponse({
        "paused": PAUSE_FILE.exists(),
        "bot_running": bot["running"],
        "bot_pid": bot.get("pid"),
    })


@app.post("/api/system-restart")
async def system_restart(request: Request):
    """Restart the entire app server (uvicorn + scheduler).

    Launches a detached helper that waits 2 s, kills the current server PID,
    then starts a fresh instance — so the HTTP response is delivered before
    the process dies.
    """
    _require_admin(request)
    import time as _t

    current_pid = os.getpid()
    app_script  = str(_HERE / "app.py")
    python_exe  = sys.executable

    # Build a one-liner that: waits 2s → kills old PID → starts new server
    if platform.system() == "Windows":
        bat = _REPO / "_restart_helper.bat"
        bat.write_text(
            f"@echo off\r\n"
            f"timeout /t 2 /nobreak >nul\r\n"
            f"taskkill /PID {current_pid} /F /T >nul 2>&1\r\n"
            f"start \"\" \"{python_exe}\" \"{app_script}\"\r\n",
            encoding="utf-8",
        )
        subprocess.Popen(
            ["cmd.exe", "/c", str(bat)],
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            close_fds=True,
        )
    else:
        subprocess.Popen(
            ["bash", "-c",
             f"sleep 2 && kill {current_pid} && {python_exe} {app_script}"],
            start_new_session=True,
        )

    return JSONResponse({"status": "restarting", "message": "Server va reporni în 2 secunde. Reîncarcă pagina după 5 secunde."})


@app.get("/api/trend-forecast")
async def api_trend_forecast(request: Request):
    """Return cached market trend forecast (auto-refreshes every hour)."""
    _require_admin(request)
    try:
        from src.agents.trend_predictor import get_forecast
        return JSONResponse(get_forecast(max_age_seconds=3600))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/engagement-insights")
async def api_engagement_insights(request: Request):
    """Return engagement analysis and recommendations."""
    _require_admin(request)
    try:
        from src.agents.engagement_optimizer import get_insights
        return JSONResponse(get_insights())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/ab-test")
async def api_ab_test(request: Request):
    """Return A/B test results (winner, hook analysis, feature wins)."""
    _require_admin(request)
    try:
        from src.agents.ab_tester import get_results
        return JSONResponse(get_results())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/security-log")
async def api_security_log(request: Request):
    """Return recent login attempts with geo data for security monitoring."""
    _require_admin(request)
    try:
        from src.dashboard.database import get_all_users
        import sqlite3, json as _json
        users = {u["id"]: u for u in get_all_users()}
        # Read last 100 login events across all users
        from src.dashboard.database import _conn
        with _conn() as c:
            rows = c.execute(
                "SELECT l.*, u.email FROM login_log l "
                "LEFT JOIN users u ON u.id = l.user_id "
                "ORDER BY l.id DESC LIMIT 100"
            ).fetchall()
        events = []
        for r in rows:
            d = dict(r)
            # Flag suspicious: failed attempts or country changes
            is_suspicious = not d.get("success")
            events.append({
                "id":           d.get("id"),
                "user_email":   d.get("email", "?"),
                "ip":           d.get("ip", ""),
                "country_code": d.get("country_code", ""),
                "country_name": d.get("country_name", ""),
                "city":         d.get("city", ""),
                "success":      bool(d.get("success")),
                "suspicious":   is_suspicious,
                "created_at":   d.get("created_at", ""),
                "user_agent_short": (d.get("user_agent") or "")[:60],
            })
        # Country summary
        from collections import Counter
        country_counts = Counter(e["country_code"] for e in events if e["country_code"])
        failed_ips = Counter(e["ip"] for e in events if not e["success"])
        return JSONResponse({
            "events": events[:50],
            "country_summary": dict(country_counts.most_common(10)),
            "failed_attempts": len([e for e in events if not e["success"]]),
            "suspicious_ips": [ip for ip, cnt in failed_ips.most_common(5) if cnt >= 2],
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/platform-timing")
async def api_platform_timing(request: Request):
    """Return optimal posting hours per platform."""
    _require_admin(request)
    try:
        from src.agents.platform_timing import get_platform_timing
        return JSONResponse(get_platform_timing())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/market-pulse")
async def api_market_pulse(request: Request):
    """Return latest market intelligence pulse."""
    _require_admin(request)
    try:
        data_file = _REPO / "data" / "market_pulse.json"
        if data_file.exists():
            import json as _json
            return JSONResponse(_json.loads(data_file.read_text(encoding="utf-8")))
        return JSONResponse({"error": "No pulse data yet — market intelligence hasn't run"})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/feedback-summary")
async def api_feedback_summary(request: Request):
    """Return engagement feedback loop summary."""
    _require_admin(request)
    try:
        data_file = _REPO / "data" / "feedback_summary.json"
        if data_file.exists():
            import json as _json
            return JSONResponse(_json.loads(data_file.read_text(encoding="utf-8")))
        return JSONResponse({"error": "No feedback data yet"})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/run-now/{slot_type}")
async def run_now(request: Request, slot_type: str, dry_run: bool = False):
    _require_admin(request)
    if slot_type not in ("morning", "evening"):
        raise HTTPException(400, "slot_type must be morning or evening")
    cmd = [sys.executable, str(_REPO / "src" / "agents" / "scheduler.py"),
           "--now", slot_type]
    if dry_run:
        cmd.append("--dry-run")
    subprocess.Popen(cmd, cwd=str(_REPO))
    return RedirectResponse("/?triggered=1", status_code=303)


# ---------------------------------------------------------------------------
# AI rewrite
# ---------------------------------------------------------------------------

@app.post("/api/slot/{name}/ai-rewrite")
async def ai_rewrite(request: Request, name: str,
                     platform: str = Form(...), instructions: str = Form("")):
    d = _uqueue(request) / name
    settings = get_user_settings(request.state.user["id"])
    story_text = ""
    top2 = d / "top2.json"
    if top2.exists():
        try:
            data = json.loads(top2.read_text(encoding="utf-8"))
            stories = data.get("stories", [])
            if stories:
                s = stories[0]
                story_text = (
                    f"Title: {s.get('title','')}\n"
                    f"Summary: {(s.get('summary') or '')[:600]}\n"
                    f"Source: {s.get('source','')}\n"
                    f"Tickers: {', '.join(s.get('tickers') or [])}"
                )
        except Exception:
            pass

    if platform == "x_b":
        post_file = d / "post_x_b.txt"
        fmt_hint = "X/Twitter post variant B (question hook style). Include 10+ hashtags at the end."
    elif platform == "tg":
        post_file = d / "post_telegram.md"
        fmt_hint = ("Telegram post. Use **bold** for the headline. Write 2 punchy paragraphs. "
                    "End with: 🌐 https://www.elitemargindesk.io?ref=7UBN23I0\n"
                    "💬 Support: @CryptohEMD\n⚠️ Not financial advice. DYOR.")
    else:
        post_file = d / "post_x.txt"
        fmt_hint = "X/Twitter post variant A (factual hook style). Include 10+ hashtags at the end."

    current = post_file.read_text(encoding="utf-8").strip() if post_file.exists() else ""
    api_key = settings.get("anthropic_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return JSONResponse({"error": "Anthropic API key not set in Settings"}, status_code=500)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2500,
            messages=[{"role": "user", "content": (
                f"You are a crypto social media copywriter. "
                f"Rewrite as a {fmt_hint}.\n\n"
                f"--- STORY ---\n{story_text}\n\n"
                f"--- CURRENT POST ---\n{current}\n\n"
                + (f"--- EXTRA ---\n{instructions}\n\n" if instructions else "")
                + "Return ONLY the post text."
            )}],
        )
        return JSONResponse({"text": msg.content[0].text})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# News search + generate from story
# ---------------------------------------------------------------------------

@app.post("/api/search-news")
async def search_news(request: Request, hours_back: int = Form(12)):
    try:
        from src.agents.news_scout import collect_news
        from src.agents.ranker import rank
        user_niche = get_user_settings(request.state.user["id"]).get("niche", "crypto") or "crypto"
        raw = collect_news(hours_back=hours_back, niche=user_niche)
        top = rank(raw, top_n=15, niche=user_niche)
        slim = []
        for s in top:
            slim.append({
                "id":           s.get("id", ""),
                "title":        s.get("title", ""),
                "url":          s.get("url", ""),
                "source":       s.get("source", ""),
                "published_at": s.get("published_at", ""),
                "summary":      (s.get("summary") or "")[:400],
                "tickers":      s.get("tickers") or [],
                "_score_total": s.get("_score_total", 0),
                "_rationale":   s.get("_rationale", ""),
                "_full":        s,
            })
        return JSONResponse({"stories": slim, "total": len(raw)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/generate-from-story")
async def generate_from_story(request: Request,
                               story_json: str = Form(...),
                               slot_suffix: str = Form("manual")):
    """Generate copy + images for a picked story.

    Strategy:
      - Copy (post_x / post_tg / etc.) is instant — done synchronously.
      - Images (3 Pollinations.ai renders) run in PARALLEL inside a
        background thread; the request returns as soon as the slot dir
        exists with top2.json + copy files. The user is redirected to
        /slot/<name>, which lazily loads the images once they land.
    Before we returned only after all 3 images finished (90-180s). Now
    we return in ~1-2s and the images appear within ~30-60s.
    """
    try:
        story = json.loads(story_json)
        full_story = story.get("_full", story)
        qr = _uqueue(request)
        today = datetime.now().strftime("%Y-%m-%d")
        slot_name = f"{today}_{slot_suffix}_{int(datetime.now().timestamp())}"
        out_dir = qr / slot_name
        out_dir.mkdir(parents=True, exist_ok=True)

        top2_data = {
            "picked_at": datetime.now(timezone.utc).isoformat(),
            "total_candidates": 1,
            "stories": [full_story],
        }
        (out_dir / "top2.json").write_text(
            json.dumps(top2_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        from src.agents.copywriter import write_outputs
        write_outputs(full_story, out_dir)

        # Kick off image generation in the background; don't block the HTTP
        # response on it. The slot page polls for file presence and shows
        # placeholders until the PNGs land. Pollinations.ai sometimes 502s,
        # so retry each missing image up to 3 times.
        def _bg_render() -> None:
            import time as _time
            from src.agents.image_gen import render_all, render_card, classify_mood
            mood = classify_mood(full_story)
            # First pass: parallel render of all 3 sizes.
            try:
                render_all(full_story, out_dir, seed=42, sizes=["x", "tg", "reel"])
            except Exception as exc:
                print(f"[generate-from-story] first pass failed: {exc}")

            # Verify + retry any missing file.
            expected = {
                "image_x_1200x675.png":  (1200, 675),
                "image_tg_1080x1080.png": (1080, 1080),
                "image_reel_1080x1920.png": (1080, 1920),
            }
            for fname, (w, h) in expected.items():
                dest = out_dir / fname
                if dest.exists():
                    continue
                for attempt in range(3):
                    try:
                        print(f"[generate-from-story] retry {fname} attempt {attempt+1}")
                        render_card(full_story, w, h, dest, seed=42 + attempt)
                        if dest.exists():
                            break
                    except Exception as exc:
                        print(f"[generate-from-story] retry {fname} failed: {exc}")
                        _time.sleep(2)
            missing_after = [f for f in expected if not (out_dir / f).exists()]
            if missing_after:
                print(f"[generate-from-story] STILL MISSING after retries: {missing_after}")
            else:
                print(f"[generate-from-story] all images ready for {slot_name}")

        _publish_executor.submit(_bg_render)

        return JSONResponse({"slot": slot_name, "url": f"/slot/{slot_name}"})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Slot status (used by slot.html polling while images are being generated)
# ---------------------------------------------------------------------------

@app.get("/api/slot/{name}/status")
async def slot_status(request: Request, name: str):
    try:
        d = _uqueue(request) / name
        if not d.exists():
            return JSONResponse({"error": f"Slot '{name}' not found"}, status_code=404)
        return JSONResponse({
            "name": name,
            "has_image_x":  (d / "image_x_1200x675.png").exists(),
            "has_image_tg": (d / "image_tg_1080x1080.png").exists(),
            "has_reel":     (d / "image_reel_1080x1920.png").exists(),
            "has_avatar":   (d / "avatar_clip.mp4").exists(),
            "has_video":    (d / "clip.mp4").exists(),
            "has_post_x":   (d / "post_x.txt").exists(),
            "has_post_tg":  (d / "post_telegram.md").exists(),
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Image editing
# ---------------------------------------------------------------------------

@app.post("/api/slot/{name}/regenerate-image")
async def regenerate_image(request: Request, name: str,
                           mood: str = Form("auto"), seed: int = Form(-1),
                           custom_prompt: str = Form(""), target: str = Form("both")):
    d = _uqueue(request) / name
    if not d.exists():
        return JSONResponse({"error": f"Slot '{name}' not found"}, status_code=404)

    import random as _random
    actual_seed = _random.randint(1, 99999) if seed < 0 else seed

    try:
        from src.agents.image_gen import render_card, classify_mood
        story: dict[str, Any] = {}
        top2 = d / "top2.json"
        if top2.exists():
            data = json.loads(top2.read_text(encoding="utf-8"))
            stories = data.get("stories") or []
            if stories:
                story = stories[0]

        actual_mood = classify_mood(story) if mood == "auto" else mood

        if custom_prompt.strip():
            from src.agents import image_gen as _ig
            _orig = _ig.BG_PROMPTS.copy()
            _ig.BG_PROMPTS[actual_mood] = custom_prompt.strip()

        sizes = []
        if target in ("x", "both"):
            sizes.append((1200, 675, "image_x_1200x675.png"))
        if target in ("tg", "both"):
            sizes.append((1080, 1080, "image_tg_1080x1080.png"))

        for w, h, fname in sizes:
            render_card(story, w, h, d / fname, seed=actual_seed)

        if custom_prompt.strip():
            _ig.BG_PROMPTS.clear()
            _ig.BG_PROMPTS.update(_orig)

        return JSONResponse({"ok": True, "seed": actual_seed, "mood": actual_mood,
                             "files": [f for _, _, f in sizes]})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/slot/{name}/upload-image")
async def upload_image(request: Request, name: str,
                       target: str = Form("both"), file: UploadFile = File(...)):
    d = _uqueue(request) / name
    if not d.exists():
        return JSONResponse({"error": f"Slot '{name}' not found"}, status_code=404)

    if file.content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        return JSONResponse({"error": f"Unsupported type: {file.content_type}"}, status_code=400)

    try:
        from PIL import Image as PILImage
        from io import BytesIO
        data = await file.read()
        img = PILImage.open(BytesIO(data)).convert("RGB")
        saved = []
        sizes = []
        if target in ("x", "both"):
            sizes.append((1200, 675, "image_x_1200x675.png"))
        if target in ("tg", "both"):
            sizes.append((1080, 1080, "image_tg_1080x1080.png"))
        for w, h, fname in sizes:
            img.resize((w, h), PILImage.LANCZOS).save(str(d / fname), "PNG")
            saved.append(fname)
        return JSONResponse({"ok": True, "saved": saved})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Avatar / reel / video generation
# ---------------------------------------------------------------------------

@app.post("/api/slot/{name}/generate-avatar")
async def generate_avatar(request: Request, name: str, use_llm: str = Form("false")):
    d = _uqueue(request) / name
    if not d.exists():
        return JSONResponse({"error": f"Slot '{name}' not found"}, status_code=404)
    top2 = d / "top2.json"
    if not top2.exists():
        return JSONResponse({"error": "No top2.json in slot"}, status_code=400)
    data    = json.loads(top2.read_text(encoding="utf-8"))
    stories = data.get("stories") or []
    if not stories:
        return JSONResponse({"error": "No stories in top2.json"}, status_code=400)
    try:
        from src.agents.avatar_writer import write_package
        result = await asyncio.get_event_loop().run_in_executor(
            _publish_executor,
            lambda: write_package(stories[0], d / "avatar",
                                  use_llm=use_llm.lower() in ("true","1","yes")),
        )
        return JSONResponse({"ok": True, "files": result["files"],
                             "mood": result["mood"], "hook": result["hook"]})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/slot/{name}/heygen-video")
async def slot_heygen_video(
    request: Request, name: str,
    heygen_avatar_id: str = Form(""),
    platform: str = Form("tiktok"),
):
    """One-click: read story → auto-script → HeyGen video → save as reel_avatar.mp4."""
    uid      = request.state.user["id"]
    settings = get_user_settings(uid)

    heygen_key = settings.get("heygen_api_key", "").strip()
    if not heygen_key:
        return JSONResponse({"error": "HeyGen API Key nu este configurat în Settings → AI Keys."}, status_code=400)
    if not heygen_avatar_id.strip():
        return JSONResponse({"error": "Selectează un avatar HeyGen."}, status_code=400)

    slot_dir = _uqueue(request) / name
    if not slot_dir.exists():
        return JSONResponse({"error": f"Slot '{name}' not found."}, status_code=404)

    # Step 1: Generate avatar script if missing
    script_path = slot_dir / "avatar" / "script_20s.txt"
    # Resolve target duration from user settings (per-platform, default 20).
    _dur_key = f"video_duration_{platform}"
    try:
        target_seconds = int(float(settings.get(_dur_key) or
                                    settings.get("video_duration_tiktok") or 20))
    except (TypeError, ValueError):
        target_seconds = 20
    target_seconds = max(10, min(target_seconds, 60))

    # Use LLM-enhanced scripts when an Anthropic key is configured — these are
    # tighter, more punchy, and fix the CONTEXT==KEY_FACT repetition bug.
    _use_llm = bool((settings.get("anthropic_key") or
                      settings.get("anthropic_api_key") or "").strip())

    # Force regenerate when the existing package was built for a different
    # duration. Without this, switching Settings → Duration silently kept the
    # old 20s script forever.
    avatar_dir   = slot_dir / "avatar"
    stamp_path   = avatar_dir / ".target_seconds"
    stale_script = False
    if script_path.exists():
        try:
            prev = int((stamp_path.read_text(encoding="utf-8").strip()
                        if stamp_path.exists() else "0") or 0)
        except (TypeError, ValueError):
            prev = 0
        if prev != target_seconds:
            stale_script = True

    if (not script_path.exists()) or stale_script:
        top2_path = slot_dir / "top2.json"
        if not top2_path.exists():
            return JSONResponse({"error": "Nu există top2.json. Rulează pipeline-ul mai întâi."}, status_code=400)
        data    = json.loads(top2_path.read_text(encoding="utf-8"))
        stories = data.get("stories") or (data if isinstance(data, list) else [])
        if not stories:
            return JSONResponse({"error": "Nu există știri în slot."}, status_code=400)

        # If top2.json only has 1 story, pull 2 more from raw_news.json so we
        # always have 3 headlines for the roundup. Falls back to single-story
        # if raw_news is missing.
        if len(stories) < 3:
            try:
                raw_path = slot_dir / "raw_news.json"
                if raw_path.exists():
                    raw = json.loads(raw_path.read_text(encoding="utf-8"))
                    extras = raw if isinstance(raw, list) else (
                        raw.get("articles") or raw.get("items")
                        or raw.get("stories") or [])
                    seen_titles = {(s.get("title") or "").strip().lower()
                                    for s in stories}
                    for ex in extras:
                        t = (ex.get("title") or "").strip()
                        if not t or t.lower() in seen_titles:
                            continue
                        stories.append(ex)
                        seen_titles.add(t.lower())
                        if len(stories) >= 3:
                            break
            except Exception as exc:
                print(f"[heygen] raw_news fallback failed: {exc}")

        # Use roundup mode (3 headlines) when we have multiple stories, else single.
        story_arg = stories[:3] if len(stories) >= 2 else stories[0]

        try:
            from src.agents.avatar_writer import write_package
            _settings_snapshot = dict(settings)
            _ts = target_seconds
            _story_arg = story_arg

            def _run_writer():
                with _uenv(_settings_snapshot):
                    return write_package(_story_arg, avatar_dir,
                                          use_llm=_use_llm,
                                          target_seconds=_ts)

            await asyncio.get_event_loop().run_in_executor(
                _publish_executor, _run_writer,
            )
        except Exception as exc:
            return JSONResponse({"error": f"Generare script eșuată: {exc}"}, status_code=500)

    if not script_path.exists():
        return JSONResponse({"error": "Scriptul nu a putut fi generat."}, status_code=500)

    script = script_path.read_text(encoding="utf-8").strip()

    # Step 2: Background image — roundup mode renders 3 headlines on a single
    # 9:16, otherwise we fall back to the standard single-story image.
    bg_image  = slot_dir / "image_reel_1080x1920.png"
    headlines_path = avatar_dir / "headlines.json"
    is_roundup_pkg = headlines_path.exists()

    # Whenever the script package is in roundup mode, regenerate the bg with
    # all 3 headlines stamped on it (existing single-story image gets overwritten).
    if is_roundup_pkg:
        try:
            roundup_stories: list[dict] = []
            top2_path = slot_dir / "top2.json"
            if top2_path.exists():
                _td = json.loads(top2_path.read_text(encoding="utf-8"))
                roundup_stories = (_td.get("stories") if isinstance(_td, dict)
                                    else (_td if isinstance(_td, list) else []))
            if len(roundup_stories) < 3:
                raw_path = slot_dir / "raw_news.json"
                if raw_path.exists():
                    _raw = json.loads(raw_path.read_text(encoding="utf-8"))
                    extras = _raw if isinstance(_raw, list) else (
                        _raw.get("articles") or _raw.get("items")
                        or _raw.get("stories") or [])
                    seen = {(s.get("title") or "").strip().lower()
                             for s in roundup_stories}
                    for ex in extras:
                        t = (ex.get("title") or "").strip()
                        if not t or t.lower() in seen:
                            continue
                        roundup_stories.append(ex); seen.add(t.lower())
                        if len(roundup_stories) >= 3: break
            if len(roundup_stories) >= 2:
                from src.agents.image_gen import render_roundup_image
                _settings_snapshot = dict(settings)
                _stories = roundup_stories[:3]
                def _run_roundup():
                    with _uenv(_settings_snapshot):
                        render_roundup_image(_stories, bg_image)
                await asyncio.get_event_loop().run_in_executor(
                    _publish_executor, _run_roundup,
                )
        except Exception as exc:
            print(f"[heygen] roundup image render failed (falling back): {exc}")

    if not bg_image.exists():
        err = await asyncio.get_event_loop().run_in_executor(
            _publish_executor,
            lambda: _ensure_reel_assets(slot_dir, dict(settings)),
        )
        if err:
            return JSONResponse({"error": f"Auto-generare imagine eșuată: {err}"}, status_code=400)
    if not bg_image.exists():
        return JSONResponse({"error": "Imaginea 9:16 nu a putut fi generată. Verifică image_gen."}, status_code=500)

    # Step 3: Launch HeyGen → composite → save directly as reel_avatar.mp4
    from src.agents.avatar_studio import _create_job, _update_job
    work_dir   = slot_dir / "_heygen_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    final_path = slot_dir / "reel_avatar.mp4"

    job_id = _create_job(uid, None, platform, script, str(bg_image))
    _update_job(job_id, status="running")

    import threading
    threading.Thread(
        target=_heygen_generate_async,
        args=(job_id, heygen_key, heygen_avatar_id.strip(), script, platform,
              work_dir, bg_image,
              settings.get("video_brand_handle", ""),
              settings.get("video_brand_color", "#00FF7A"),
              uid, dict(settings), final_path),
        daemon=True,
    ).start()

    return JSONResponse({"job_id": job_id, "status": "running", "script": script[:200]})


@app.post("/api/slot/{name}/generate-reel")
async def generate_reel_scripts(request: Request, name: str):
    d = _uqueue(request) / name
    if not d.exists():
        return JSONResponse({"error": f"Slot '{name}' not found"}, status_code=404)
    top2 = d / "top2.json"
    if not top2.exists():
        return JSONResponse({"error": "No top2.json — run pipeline first"}, status_code=400)
    try:
        data = json.loads(top2.read_text(encoding="utf-8"))
        stories = data.get("stories") or []
        if not stories:
            return JSONResponse({"error": "No stories in top2.json"}, status_code=400)
        from src.agents.reel_writer import write_package
        await asyncio.get_event_loop().run_in_executor(
            _publish_executor,
            lambda: write_package(stories[0], d / "reel"),
        )
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


def _ensure_reel_assets(d: Path, settings: dict | None = None) -> str | None:
    top2 = d / "top2.json"
    if not top2.exists():
        return "No top2.json — run pipeline first"
    try:
        data = json.loads(top2.read_text(encoding="utf-8"))
    except Exception as exc:
        return f"Could not read top2.json: {exc}"
    stories = data.get("stories") or []
    if not stories:
        return "No stories in top2.json"

    _s = settings or {}

    if not (d / "image_reel_1080x1920.png").exists():
        try:
            from src.agents.image_gen import render_all
            with _uenv(_s):
                render_all(stories[0], d, seed=42, sizes=["reel"])
        except Exception as exc:
            return f"Could not generate background image: {exc}"

    if not (d / "reel" / "01_script.txt").exists():
        try:
            from src.agents.reel_writer import write_package
            with _uenv(_s):
                write_package(stories[0], d / "reel")
        except Exception as exc:
            return f"Could not generate reel script: {exc}"

    return None


@app.post("/api/slot/{name}/build-video")
async def build_video_route(request: Request, name: str,
                            use_elevenlabs: str = Form("false")):
    d = _uqueue(request) / name
    if not d.exists():
        return JSONResponse({"error": f"Slot '{name}' not found"}, status_code=404)

    settings = get_user_settings(request.state.user["id"])
    err = await asyncio.get_event_loop().run_in_executor(
        _publish_executor, lambda: _ensure_reel_assets(d, settings)
    )
    if err:
        return JSONResponse({"error": err}, status_code=400)

    try:
        from src.agents.video_builder import build_video
        use_el = use_elevenlabs.lower() in ("true", "1", "yes")

        def _run_build():
            with _uenv(settings):
                return build_video(d, use_elevenlabs=use_el)

        out_path = await asyncio.get_event_loop().run_in_executor(_publish_executor, _run_build)
        size_mb = round(out_path.stat().st_size / 1_048_576, 1)
        return JSONResponse({"ok": True, "file": out_path.name, "size_mb": size_mb})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/slot/{name}/build-avatar-video")
async def build_avatar_video_route(request: Request, name: str,
                                   presenter: str = Form("alex_suit"),
                                   platform: str = Form("tiktok")):
    """Build an avatar reel sized for the given platform.

    `platform` = 'tiktok' | 'instagram' | 'youtube_shorts'. Settings from
    the user's Video Profile (Settings page) are injected via _uenv so the
    duration/intro/outro/BGM preferences take effect.
    """
    d = _uqueue(request) / name
    if not d.exists():
        return JSONResponse({"error": f"Slot '{name}' not found"}, status_code=404)

    settings = get_user_settings(request.state.user["id"])
    did_key = settings.get("did_api_key") or os.environ.get("DID_API_KEY", "")
    replicate_key = settings.get("replicate_api_token") or os.environ.get("REPLICATE_API_TOKEN", "")
    if not did_key and not replicate_key:
        return JSONResponse(
            {"error": "Niciun provider AI Avatar configurat. Adaugă D-ID sau Replicate API key în Settings → AI Avatar."},
            status_code=400,
        )

    # Validate platform up-front for a clean error
    if platform not in ("tiktok", "instagram", "youtube_shorts"):
        return JSONResponse(
            {"error": f"Unknown platform {platform!r}. "
                      f"Use tiktok | instagram | youtube_shorts."},
            status_code=400,
        )

    err = await asyncio.get_event_loop().run_in_executor(
        _publish_executor, lambda: _ensure_reel_assets(d, settings)
    )
    if err:
        return JSONResponse({"error": err}, status_code=400)

    try:
        from src.agents.avatar_video import build_avatar_reel
        out_path = await asyncio.get_event_loop().run_in_executor(
            _publish_executor,
            lambda: _with_did(settings, lambda: build_avatar_reel(
                d, presenter=presenter, platform=platform,
            )),
        )
        size_mb = round(out_path.stat().st_size / 1_048_576, 1)
        return JSONResponse({
            "ok": True, "file": out_path.name, "size_mb": size_mb,
            "platform": platform,
        })
    except Exception as exc:
        import traceback
        return JSONResponse(
            {"error": str(exc), "traceback": traceback.format_exc()[:1000]},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Video profile preview (for slot page UI)
# ---------------------------------------------------------------------------

@app.get("/api/video-profiles")
async def api_video_profiles(request: Request):
    """Return the resolved video profiles for the current user.

    Used by the slot page to show "TikTok: 25s, Instagram: 25s, Shorts: 45s"
    next to the Build buttons.
    """
    try:
        from src.config.video_profiles import get_all_profiles
        settings = get_user_settings(request.state.user["id"])
        profiles = get_all_profiles(settings)
        # Strip non-serializable bits (tuples)
        for p in profiles.values():
            p["aspect_ratio"] = list(p["aspect_ratio"])
        return JSONResponse({"profiles": profiles})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


def _with_did(settings: dict, fn):
    """Run fn() with user's avatar-provider credentials injected.

    Despite the historical name, this now also injects the Replicate
    credentials (the new primary provider) so the avatar provider chain
    has access to all configured backends.
    """
    mapping = {
        "DID_API_KEY":             settings.get("did_api_key", ""),
        "DID_API_EMAIL":           settings.get("did_email", ""),
        "DID_PRESENTER_URL":       settings.get("did_presenter_url", ""),
        "REPLICATE_API_TOKEN":     settings.get("replicate_api_token", ""),
        "REPLICATE_PRESENTER_URL": settings.get("replicate_presenter_url", ""),
        "AVATAR_PROVIDER":         settings.get("avatar_provider", "auto") or "auto",
    }
    # Video profile env vars so avatar_video reads the right duration.
    for key in ("video_duration_tiktok", "video_duration_instagram",
                "video_duration_youtube_shorts", "video_style",
                "video_intro_enabled", "video_outro_enabled",
                "video_bgm_enabled", "video_bgm_volume",
                "video_brand_color", "video_brand_handle", "video_brand_cta"):
        val = settings.get(key, "")
        if val != "" and val is not None:
            mapping[key.upper()] = str(val)
    # Per-user uploaded BGM file overrides the bundled default.
    bgm_path = (settings.get("video_bgm_path") or "").strip()
    if bgm_path and Path(bgm_path).exists():
        mapping["BGM_PATH"] = bgm_path

    old = {k: os.environ.get(k) for k in mapping}
    for k, v in mapping.items():
        if v:
            os.environ[k] = v
    try:
        return fn()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# File serving
# ---------------------------------------------------------------------------

@app.get("/media/{user_id}/{slot_name}/{filename}")
async def serve_public_media(user_id: str, slot_name: str, filename: str):
    """Unauthenticated public endpoint used by Instagram/TikTok/YouTube APIs
    when they fetch the image/video by URL. Requires knowing user_id +
    slot_name + filename — low-risk obscurity since queue paths aren't enumerable.
    """
    f = _safe_path(BASE_QUEUE, user_id, slot_name, filename)
    if not f.exists():
        raise HTTPException(404, "Media not found")
    ext = f.suffix.lower()
    mime = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
        ".mp4": "video/mp4", ".mov": "video/quicktime",
    }.get(ext, "application/octet-stream")
    return FileResponse(str(f), media_type=mime)


@app.get("/video/{slot_name}/{filename}")
async def serve_video(request: Request, slot_name: str, filename: str):
    f = _safe_path(_uqueue(request), slot_name, filename)
    if not f.exists():
        raise HTTPException(404, "Video not found")
    return FileResponse(str(f), media_type="video/mp4")


@app.get("/image/{slot_name}/{filename}")
async def serve_image(request: Request, slot_name: str, filename: str):
    img = _safe_path(_uqueue(request), slot_name, filename)
    if not img.exists():
        raise HTTPException(404, "Image not found")
    return FileResponse(str(img))


@app.get("/avatar-file/{slot_name}/{filename}")
async def serve_avatar_file(request: Request, slot_name: str, filename: str):
    f = _safe_path(_uqueue(request), slot_name, "avatar", filename)
    if not f.exists():
        raise HTTPException(404, "Avatar file not found")
    return FileResponse(str(f))


@app.get("/api/agent-brains")
async def api_agent_brains(request: Request):
    """Return all agent brain states for the backoffice real-time panel."""
    try:
        from src.agents.agent_brain import manager as _brain_mgr
        return JSONResponse({"brains": _brain_mgr.all_brains()})
    except Exception as exc:
        return JSONResponse({"brains": [], "error": str(exc)})


@app.get("/api/supervisor/report")
async def api_supervisor_report(request: Request):
    """Return latest supervisor health report. Admin only."""
    _require_admin(request)
    try:
        from src.agents.agent_supervisor import get_last_report, run_supervision, is_stale
        if is_stale(max_age_min=16):
            import threading as _th
            _th.Thread(target=run_supervision, daemon=True).start()
        return JSONResponse(get_last_report())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/supervisor/run-now")
async def api_supervisor_run_now(request: Request):
    """Force an immediate supervisor check cycle. Admin only."""
    _require_admin(request)
    import threading as _th
    from src.agents.agent_supervisor import run_supervision
    _th.Thread(target=run_supervision, daemon=True).start()
    return JSONResponse({"ok": True, "msg": "Supervisor check started"})


@app.post("/api/web-learner/run-now")
async def api_web_learner_run_now(request: Request):
    """Force an immediate web learning cycle for all agents. Admin only."""
    _require_admin(request)
    import threading as _th
    from src.agents.web_learner import run_web_learning
    _th.Thread(target=run_web_learning, kwargs={"force": True}, daemon=True).start()
    return JSONResponse({"ok": True, "msg": "Web learning cycle started (all agents)"})


@app.post("/api/agent-brains/run-cycle")
async def api_agent_brains_run(request: Request):
    """Manually trigger a learning cycle (admin only)."""
    user = request.state.user
    if not user or not user.get("is_admin"):
        raise HTTPException(403, "Admin only")
    import threading as _th
    from src.agents.agent_brain import manager as _brain_mgr
    _th.Thread(target=_brain_mgr.run_learning_cycle, daemon=True).start()
    return JSONResponse({"ok": True, "msg": "Learning cycle started in background"})


@app.get("/api/logs")
async def api_logs(request: Request, lines: int = 60, file: str = "scheduler"):
    _log_map = {
        "scheduler": _REPO / "scheduler.log",
        "watchdog":  _REPO / "watchdog.log",
        "server":    _REPO / "server.log",
    }
    log_path = _log_map.get(file, LOG_FILE)
    if not log_path.exists():
        return JSONResponse({"lines": [f"(log {file!r} nu exista inca)"]})
    all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return JSONResponse({"lines": all_lines[-lines:]})


# ---------------------------------------------------------------------------
# Scheduler status + manual run
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Viral analyzer endpoints
# ---------------------------------------------------------------------------

@app.post("/api/viral-scan")
async def viral_scan(request: Request,
                     topic: str = Form("crypto"),
                     refresh: bool = Form(False)):
    """Run the viral analyzer on `topic` for this user. Returns the blueprint.

    By default we serve from the 24h cache. Pass refresh=true to force a
    fresh scrape.
    """
    user_id = request.state.user["id"]
    settings = get_user_settings(user_id)
    from src.dashboard.database import get_viral_blueprint, save_viral_blueprint
    from src.agents.viral_analyzer import scan_viral

    topic = (topic or "").strip().lower() or "crypto"
    user_niche = (settings.get("niche") or "").strip().lower() or None

    if not refresh:
        cached = get_viral_blueprint(user_id, topic)
        # Only honour the cache if it matches the user's current niche;
        # otherwise it is stale (e.g. user changed niche after the scan).
        if cached and (cached.get("niche") or "") == (user_niche or ""):
            return JSONResponse({"ok": True, "cached": True, "blueprint": cached})

    yt_key = settings.get("youtube_api_key", "") or os.environ.get("YOUTUBE_API_KEY", "")

    def _go():
        return scan_viral(
            topic=topic,
            platforms=("youtube", "tiktok", "instagram", "x", "facebook"),
            limit_per_platform=20,
            youtube_api_key=yt_key or None,
            niche=user_niche,
        )

    try:
        bp = await asyncio.get_event_loop().run_in_executor(_publish_executor, _go)
        save_viral_blueprint(user_id, topic, bp)
        return JSONResponse({"ok": True, "cached": False, "blueprint": bp})
    except Exception as exc:
        import traceback
        return JSONResponse(
            {"error": str(exc), "traceback": traceback.format_exc()[:1000]},
            status_code=500,
        )


@app.get("/api/viral-blueprint")
async def viral_blueprint_get(request: Request, topic: str = "crypto"):
    """Return the cached blueprint without scanning. None if no fresh cache."""
    from src.dashboard.database import get_viral_blueprint
    bp = get_viral_blueprint(request.state.user["id"], topic.lower())
    return JSONResponse({"blueprint": bp})


@app.post("/api/upload-bgm")
async def upload_bgm(request: Request, audio: UploadFile = File(...)):
    """Save user-uploaded background music to src/assets/bgm/user_{id}.<ext>
    and persist the absolute path to that user's video_bgm_path setting."""
    user_id = request.state.user["id"]
    ext = Path(audio.filename or "").suffix.lower()
    if ext not in (".mp3", ".wav", ".m4a", ".ogg"):
        return JSONResponse(
            {"error": f"Format nesuportat: {ext}. Foloseste mp3/wav/m4a/ogg."},
            status_code=400,
        )
    repo_root = Path(__file__).resolve().parent.parent.parent
    bgm_dir = repo_root / "src" / "assets" / "bgm"
    bgm_dir.mkdir(parents=True, exist_ok=True)
    out = bgm_dir / f"user_{user_id}{ext}"
    data = await audio.read()
    if len(data) > 20 * 1024 * 1024:
        return JSONResponse({"error": "Fisier prea mare (>20 MB)"}, status_code=400)
    out.write_bytes(data)
    save_user_settings(user_id, video_bgm_path=str(out))
    return JSONResponse({"ok": True, "path": str(out), "size_kb": len(data) // 1024})


@app.post("/api/upload-presenter")
async def upload_presenter(request: Request, photo: UploadFile = File(...)):
    """Upload a JPG/PNG presenter photo to D-ID /images and save the returned URL.

    The URL is stored as `did_presenter_url` in user settings so every subsequent
    avatar build uses this photo automatically.  No D-ID key = returns 400 with
    a helpful message instead of crashing.
    """
    uid = request.state.user["id"]
    settings = get_user_settings(uid)
    did_key = settings.get("did_api_key") or os.environ.get("DID_API_KEY", "")
    if not did_key:
        return JSONResponse(
            {"error": "D-ID API key not set — add it in Settings → D-ID Avatar first."},
            status_code=400,
        )

    ext = Path(photo.filename or "photo.jpg").suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png"):
        return JSONResponse({"error": f"Format nesuportat {ext} — folosiți JPG sau PNG."}, status_code=400)

    data = await photo.read()
    if len(data) > 10 * 1024 * 1024:
        return JSONResponse({"error": "Fișier prea mare (>10 MB)."}, status_code=400)

    # Save to a temp file so avatar_video.upload_photo_to_did can read it.
    import tempfile as _tmp
    with _tmp.NamedTemporaryFile(suffix=ext, delete=False) as f:
        f.write(data)
        tmp_path = Path(f.name)

    try:
        from src.agents.avatar_video import upload_photo_to_did
        url = _with_did(settings, lambda: upload_photo_to_did(tmp_path))
        save_user_settings(uid, did_presenter_url=url)
        return JSONResponse({"ok": True, "url": url})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/api/upload-bgm/clear")
async def upload_bgm_clear(request: Request):
    """Remove any user-saved BGM track."""
    user_id = request.state.user["id"]
    settings = get_user_settings(user_id)
    p = settings.get("video_bgm_path") or ""
    if p:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass
    save_user_settings(user_id, video_bgm_path="")
    return JSONResponse({"ok": True})


@app.get("/api/scheduler/status")
async def scheduler_status(request: Request):
    """Return whether the scheduler is alive (heartbeat <120s old) and last-run
    info for both slots."""
    import json as _json
    from datetime import datetime as _dt, timezone as _tz
    repo_root = Path(__file__).resolve().parent.parent.parent
    hb = repo_root / "scheduler_heartbeat.txt"
    state_file = repo_root / "scheduler_state.json"

    alive = False
    hb_age = None
    if hb.exists():
        try:
            ts = _dt.fromisoformat(hb.read_text(encoding="utf-8").strip())
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_tz.utc)
            hb_age = (_dt.now(_tz.utc) - ts).total_seconds()
            alive = hb_age < 120  # tolerate up to 2 min between heartbeats
        except Exception:
            pass

    state = {}
    if state_file.exists():
        try:
            state = _json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    return JSONResponse({
        "alive": alive,
        "heartbeat_age_seconds": hb_age,
        "last_morning":      state.get("last_morning"),
        "last_morning_ok":   state.get("last_morning_ok"),
        "last_evening":      state.get("last_evening"),
        "last_evening_ok":   state.get("last_evening_ok"),
    })


@app.get("/api/scheduler/platforms")
async def scheduler_platforms(request: Request):
    """Return which platforms WILL fire at the next scheduled slot, based
    on the credentials currently saved in this user's settings.

    Useful for the Scheduler Status card — answers the question
    'why didn't X post yesterday?' without running the slot.
    """
    s = get_user_settings(request.state.user["id"])

    def has(*keys):
        return all(bool((s.get(k) or "").strip()) for k in keys)

    checks = {
        "telegram":  {
            "ready":  has("telegram_token", "telegram_channel"),
            "needs":  "telegram_token + telegram_channel",
        },
        "x": {
            "ready":  bool((s.get("x_cookies_json") or "").strip())
                      or bool((s.get("x_oauth_access_token") or "").strip())
                      or has("x_api_key", "x_access_token", "x_api_secret", "x_access_secret")
                      or bool((s.get("make_x_webhook_url") or "").strip()),
            "needs":  "paste cookies (preferred) sau OAuth/API/Make webhook",
            "preferred_path":
                "cookies" if (s.get("x_cookies_json") or "").strip()
                else "OAuth" if (s.get("x_oauth_access_token") or "").strip()
                else "API"   if has("x_api_key", "x_access_token")
                else "make"  if (s.get("make_x_webhook_url") or "").strip()
                else "none",
        },
        "instagram": {
            "ready":  has("ig_user_id", "ig_access_token")
                      and (bool((s.get("imgbb_api_key") or "").strip())
                           or bool((s.get("public_base_url") or "").strip())),
            "needs":  "ig_user_id + ig_access_token + (imgbb_api_key sau public_base_url)",
        },
        "tiktok":    {
            "ready":  bool((s.get("tiktok_access_token") or "").strip()),
            "needs":  "tiktok_access_token (din OAuth flow)",
        },
        "youtube":   {
            "ready":  has("youtube_refresh_token", "youtube_client_id",
                          "youtube_client_secret")
                      or has("yt_refresh_token", "yt_client_id", "yt_client_secret"),
            "needs":  "youtube_refresh_token + client_id + client_secret",
        },
    }

    will_fire = sorted([p for p, c in checks.items() if c["ready"]])
    blocked   = sorted([p for p, c in checks.items() if not c["ready"]])
    return JSONResponse({
        "ok":         True,
        "will_fire":  will_fire,
        "blocked":    blocked,
        "checks":     checks,
    })


@app.post("/api/scheduler/run-now")
async def scheduler_run_now(request: Request, slot: str = Form("morning")):
    """Trigger one slot immediately (non-blocking). Useful when the scheduler
    process wasn't running at the scheduled time."""
    if slot not in ("morning", "evening", "both"):
        return JSONResponse({"error": f"Unknown slot {slot!r}"}, status_code=400)

    settings = get_user_settings(request.state.user["id"])

    def _go():
        try:
            from src.agents.scheduler import run_slot
            slots = ["morning", "evening"] if slot == "both" else [slot]
            results = {}
            for s in slots:
                ok = _with_did(settings, lambda s=s: run_slot(s, publish=True))
                results[s] = bool(ok)
            return results
        except Exception as e:
            import traceback
            return {"error": str(e), "traceback": traceback.format_exc()[:1000]}

    try:
        result = await asyncio.get_event_loop().run_in_executor(_publish_executor, _go)
        return JSONResponse({"ok": True, "result": result})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Pricing / Plans page + Billing (Stripe)
# ---------------------------------------------------------------------------

@app.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request):
    user = request.state.user
    from src.dashboard.database import get_user_settings
    from src.agents.billing_agent import stripe_configured, get_plan_limits
    settings = get_user_settings(user["id"])
    plan     = settings.get("plan", "trial")
    return _render("pricing.html",
        current_user     = user,
        current_plan     = plan,
        plan_expires_at  = settings.get("plan_expires_at", ""),
        stripe_ok        = stripe_configured(),
        plan_limits      = get_plan_limits(plan),
    )


@app.get("/billing/checkout/{plan}/{billing}", response_class=HTMLResponse)
async def billing_checkout(request: Request, plan: str, billing: str):
    user = request.state.user
    from src.agents.billing_agent import create_checkout_session, PAID_PLANS
    if plan not in PAID_PLANS:
        return HTMLResponse("<h3>Invalid plan.</h3>", status_code=400)
    if billing not in ("monthly", "annual"):
        return HTMLResponse("<h3>Invalid billing period.</h3>", status_code=400)
    base = str(request.base_url).rstrip("/")
    try:
        url = create_checkout_session(
            user_id     = user["id"],
            user_email  = user["email"],
            plan        = plan,
            billing     = billing,
            success_url = f"{base}/billing/success",
            cancel_url  = f"{base}/pricing",
        )
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url, status_code=303)
    except Exception as exc:
        return _render("billing_error.html",
            current_user = user,
            error        = str(exc),
        )


@app.get("/billing/portal", response_class=HTMLResponse)
async def billing_portal(request: Request):
    user = request.state.user
    from src.agents.billing_agent import create_portal_session
    base = str(request.base_url).rstrip("/")
    try:
        url = create_portal_session(user["id"], return_url=f"{base}/pricing")
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url, status_code=303)
    except Exception as exc:
        return _render("billing_error.html", current_user=user, error=str(exc))


@app.get("/billing/success", response_class=HTMLResponse)
async def billing_success(request: Request):
    user = request.state.user
    from src.dashboard.database import get_user_settings
    settings = get_user_settings(user["id"])
    return _render("billing_success.html",
        current_user = user,
        current_plan = settings.get("plan", "trial"),
    )


@app.post("/billing/crypto/confirm")
async def billing_crypto_confirm(request: Request):
    """Verify USDC Solana tx and activate plan instantly."""
    user = request.state.user
    form = await request.form()
    tx_sig  = str(form.get("tx_sig",  "")).strip()
    plan    = str(form.get("plan",    "")).strip()
    billing = str(form.get("billing", "monthly")).strip()
    from src.agents.billing_agent import activate_crypto_plan, PAID_PLANS
    if plan not in PAID_PLANS:
        return {"status": "error", "error": "Invalid plan."}
    result = activate_crypto_plan(user["id"], tx_sig, plan, billing)
    return result


@app.post("/billing/webhook")
async def billing_webhook(request: Request):
    """Stripe webhook — no auth, verified by signature."""
    from src.agents.billing_agent import handle_webhook
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        result = handle_webhook(payload, sig_header)
        return result
    except Exception as exc:
        return {"status": "error", "msg": str(exc)}


# Admin: manually set plan for a user
@app.post("/api/admin/set-plan")
async def admin_set_plan(request: Request):
    _require_admin(request)
    form    = await request.form()
    user_id = int(form.get("user_id", 0))
    plan    = str(form.get("plan", "free"))
    days    = int(form.get("days", 30))
    from src.agents.billing_agent import admin_set_plan as _set
    _set(user_id, plan, days)
    return {"status": "ok", "user_id": user_id, "plan": plan, "days": days}


# ---------------------------------------------------------------------------
# Competitor Tracker
# ---------------------------------------------------------------------------

@app.get("/viral", response_class=HTMLResponse)
async def viral_page(request: Request):
    user = request.state.user
    from src.dashboard.database import get_viral_blueprint
    from src.agents.viral_scout import topics_for_niche, DEFAULT_TOPICS
    import json as _json

    # Derive Viral Scout topics from the user's selected niche.
    # Manual override via `viral_topics` setting still wins.
    settings = get_user_settings(user["id"]) or {}
    override = (settings.get("viral_topics") or "").strip()
    if override:
        topics = [t.strip().lower() for t in override.split(",") if t.strip()]
    else:
        niche_id = (settings.get("niche") or "").strip().lower()
        topics = topics_for_niche(niche_id) if niche_id else list(DEFAULT_TOPICS)

    blueprints = {}
    current_niche = (settings.get("niche") or "").strip().lower()
    for t in topics:
        bp = get_viral_blueprint(user["id"], t)
        if not bp:
            continue
        # Drop blueprints scanned under a different niche so the user doesn't
        # see crypto results after switching to fitness, etc.
        bp_niche = (bp.get("niche") or "").strip().lower()
        if current_niche and bp_niche and bp_niche != current_niche:
            continue
        blueprints[t] = bp
    return _render("viral_scout.html",
        user=user,
        topics=topics,
        blueprints=blueprints,
        blueprints_json=_json.dumps(blueprints, ensure_ascii=False),
        topics_json=_json.dumps(topics, ensure_ascii=False),
    )


@app.get("/competitor-tracker")
async def competitor_tracker_page(request: Request):
    from src.agents.competitor_tracker import get_user_report
    from src.dashboard.database import add_competitor as _db_add
    uid    = request.state.user["id"]
    report = get_user_report(uid)
    token  = request.cookies.get(COOKIE, "")
    return _render(
        "competitor_tracker.html",
        user=request.state.user,
        is_admin=bool(request.state.user.get("is_admin")),
        report=report,
        csrf_token=_csrf_token(token),
    )


@app.post("/api/competitor/add")
async def competitor_add(
    request: Request,
    platform: str = Form("x"),
    handle:   str = Form(""),
    label:    str = Form(""),
    csrf:     str = Form("", alias="_csrf"),
):
    token = request.cookies.get(COOKIE, "")
    if not csrf or csrf != _csrf_token(token):
        return JSONResponse({"error": "Invalid CSRF token"}, status_code=403)
    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "Handle / URL is required"}, status_code=400)
    allowed_platforms = {"x", "instagram", "tiktok", "website", "youtube"}
    if platform not in allowed_platforms:
        return JSONResponse({"error": "Invalid platform"}, status_code=400)
    uid = request.state.user["id"]
    from src.dashboard.database import get_competitors, add_competitor as _add_comp
    existing = get_competitors(uid)
    if len(existing) >= 20:
        return JSONResponse({"error": "Max 20 competitors per account"}, status_code=400)
    if any(c["handle"].lower() == handle.lower() and c["platform"] == platform
           for c in existing):
        return JSONResponse({"error": "Already tracking this account"}, status_code=400)
    comp_id = _add_comp(uid, platform, handle, label=label)
    return JSONResponse({"ok": True, "comp_id": comp_id})


@app.post("/api/competitor/remove")
async def competitor_remove(
    request: Request,
    comp_id: int = Form(...),
    csrf:    str = Form("", alias="_csrf"),
):
    token = request.cookies.get(COOKIE, "")
    if not csrf or csrf != _csrf_token(token):
        return JSONResponse({"error": "Invalid CSRF token"}, status_code=403)
    uid = request.state.user["id"]
    from src.dashboard.database import remove_competitor as _rm
    _rm(comp_id, uid)
    return JSONResponse({"ok": True})


@app.post("/api/competitor/scan")
async def competitor_scan(request: Request, csrf: str = Form("", alias="_csrf")):
    token = request.cookies.get(COOKIE, "")
    if not csrf or csrf != _csrf_token(token):
        return JSONResponse({"error": "Invalid CSRF token"}, status_code=403)
    if not request.state.user.get("is_admin"):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    def _go():
        from src.agents.competitor_tracker import run_all_users
        return run_all_users(force=True)
    result = await asyncio.get_event_loop().run_in_executor(_publish_executor, _go)
    return JSONResponse({"ok": True, **result})


# ---------------------------------------------------------------------------
# Avatar Studio — AI avatar creation, multi-profile, per-user
# ---------------------------------------------------------------------------

@app.get("/ad-studio")
async def ad_studio_page(request: Request):
    return _render(
        "ad_studio.html",
        user=request.state.user,
        industries=sorted(_AD_ALLOWED_INDUSTRIES),
        formulas=sorted(_AD_ALLOWED_FORMULAS),
        goals=sorted(_AD_ALLOWED_GOALS),
        voices=sorted(_AD_ALLOWED_VOICES),
    )


@app.get("/avatar-studio")
async def avatar_studio_page(request: Request):
    from src.agents.avatar_studio import get_profiles, get_jobs, PLATFORM_PRESETS, studio
    uid      = request.state.user["id"]
    settings = get_user_settings(uid)
    profiles = get_profiles(uid)
    jobs     = get_jobs(uid, limit=5)
    engines  = studio.available_engines()
    return _render(
        "avatar_studio.html",
        user=request.state.user,
        profiles=[vars(p) if hasattr(p, "__dict__") else p for p in profiles],
        jobs=jobs,
        engines=engines,
        platforms=PLATFORM_PRESETS,
        has_heygen_key=bool(settings.get("heygen_api_key", "").strip()),
        has_did_key=bool(settings.get("did_api_key", "").strip()),
    )


@app.get("/api/avatar/profiles")
async def api_avatar_profiles(request: Request):
    from src.agents.avatar_studio import get_profiles
    profiles = get_profiles(request.state.user["id"])
    return JSONResponse({"profiles": [
        {"id": p.id, "name": p.name, "photo_path": p.photo_path,
         "voice_id": p.voice_id, "voice_name": p.voice_name,
         "settings": p.settings, "created_at": p.created_at}
        for p in profiles
    ]})


_DID_PRESENTER_URL_CACHE = BASE_QUEUE.parent / ".did_presenter_urls.json"

_DID_PRESENTER_META = [
    {"key": "jenny",  "name": "Jenny",  "accent": "EN-US", "gender": "female", "voice_id": "en-US-JennyNeural",  "pravatar": "https://i.pravatar.cc/512?img=47"},
    {"key": "aria",   "name": "Aria",   "accent": "EN-US", "gender": "female", "voice_id": "en-US-AriaNeural",   "pravatar": "https://i.pravatar.cc/512?img=44"},
    {"key": "sonia",  "name": "Sonia",  "accent": "EN-GB", "gender": "female", "voice_id": "en-GB-SoniaNeural",  "pravatar": "https://i.pravatar.cc/512?img=32"},
    {"key": "guy",    "name": "Guy",    "accent": "EN-US", "gender": "male",   "voice_id": "en-US-GuyNeural",    "pravatar": "https://i.pravatar.cc/512?img=68"},
    {"key": "ryan",   "name": "Ryan",   "accent": "EN-GB", "gender": "male",   "voice_id": "en-GB-RyanNeural",   "pravatar": "https://i.pravatar.cc/512?img=53"},
    {"key": "davis",  "name": "Davis",  "accent": "EN-US", "gender": "male",   "voice_id": "en-US-DavisNeural",  "pravatar": "https://i.pravatar.cc/512?img=60"},
]


def _load_did_presenter_urls() -> dict:
    try:
        if _DID_PRESENTER_URL_CACHE.exists():
            import json as _j
            return _j.loads(_DID_PRESENTER_URL_CACHE.read_text())
    except Exception:
        pass
    return {}


@app.get("/api/did/presenters")
async def api_did_presenters(request: Request):
    """Return D-ID presenter list — avatare uploadate pe D-ID cu voci diferite.

    Photo URLs sunt s3:// interne D-ID (uploadate o singură dată via /images).
    Dacă cache-ul lipsește, preview-ul e din pravatar.cc (HTTPS, doar pentru UI).
    """
    cached_urls = _load_did_presenter_urls()
    presenters = []
    for p in _DID_PRESENTER_META:
        did_url = cached_urls.get(p["key"], "")
        presenters.append({
            "key":         p["key"],
            "name":        p["name"],
            "accent":      p["accent"],
            "gender":      p["gender"],
            "voice_id":    p["voice_id"],
            "did_url":     did_url,           # s3:// URL pt /talks
            "preview_url": p["pravatar"],     # HTTPS pt img preview în UI
        })
    return JSONResponse({"presenters": presenters})


@app.post("/api/avatar/upload-photo")
async def api_avatar_upload_photo(request: Request, photo: UploadFile = File(...)):
    uid = request.state.user["id"]
    if photo.content_type not in {"image/jpeg", "image/png", "image/webp"}:
        return JSONResponse({"error": "Only JPG/PNG/WebP allowed"}, status_code=400)
    data = await photo.read()
    if len(data) > 15 * 1024 * 1024:
        return JSONResponse({"error": "File too large (>15 MB)"}, status_code=400)
    from src.agents.avatar_studio import studio as _studio
    filename = _studio.upload_photo(uid, data, photo.filename or "photo.jpg")
    return JSONResponse({"ok": True, "filename": filename,
                         "url": f"/api/avatar/photo/{uid}/{filename}"})


@app.get("/api/avatar/photo/{user_id}/{filename}")
async def api_avatar_photo(request: Request, user_id: int, filename: str):
    if ".." in filename:
        raise HTTPException(400, "Invalid path")
    uid = request.state.user["id"]
    if uid != user_id and not request.state.user.get("is_admin"):
        raise HTTPException(403, "Forbidden")
    from src.agents.avatar_studio import _AVATARS_ROOT
    f = _AVATARS_ROOT / str(user_id) / filename
    if not f.exists():
        raise HTTPException(404, "Photo not found")
    return FileResponse(str(f))


@app.post("/api/avatar/profile")
async def api_avatar_create_profile(
    request: Request,
    name: str       = Form(...),
    photo_filename: str = Form(""),
    voice_id: str   = Form(""),
    voice_name: str = Form(""),
    profile_id: int = Form(0),
):
    uid = request.state.user["id"]
    from src.agents.avatar_studio import studio as _studio
    if profile_id:
        _studio.update_profile(
            profile_id=profile_id, user_id=uid,
            name=name, photo_path=photo_filename,
            voice_id=voice_id, voice_name=voice_name,
        )
        return JSONResponse({"ok": True, "profile_id": profile_id})
    pid = _studio.create_profile(
        user_id=uid, name=name, photo_path=photo_filename,
        voice_id=voice_id, voice_name=voice_name,
    )
    return JSONResponse({"ok": True, "profile_id": pid})


@app.post("/api/avatar/profile/{profile_id}/delete")
async def api_avatar_delete_profile(request: Request, profile_id: int):
    uid = request.state.user["id"]
    from src.agents.avatar_studio import delete_profile
    delete_profile(profile_id, uid)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# HeyGen voice selector — match avatar gender to voice gender
# ---------------------------------------------------------------------------
# Cache (avatar_id, voice_id, voice_id) per HeyGen API key, refreshed every
# 24h. Without this we'd hit /v2/avatars + /v2/voices on every clip render
# (≈ 800 ms × 2 = 1.6s wasted, plus rate-limit risk).
_HEYGEN_VOICE_CACHE: dict[str, dict] = {}
_HEYGEN_VOICE_CACHE_TTL = 24 * 60 * 60  # 24h

def _pick_heygen_voice_for_avatar(api_key: str, avatar_id: str,
                                    user_voice_id: str = "") -> str:
    """Return a voice_id whose gender matches the avatar's gender.

    Order of preference:
      1) explicit user_voice_id (passed from UI / settings / DB)
      2) env override HEYGEN_VOICE_ID_MALE / HEYGEN_VOICE_ID_FEMALE
      3) live API lookup: /v2/avatars → gender → /v2/voices → first English
         voice with matching gender (deterministic: same avatar always picks
         the same voice across runs).

    Cache disabled for now — when the cache had stale data from before this
    fix, users reported female voices on male avatars persisting forever.
    The two API calls add ~1.5s but only run when generation is queued.
    """
    if user_voice_id and user_voice_id.strip():
        print(f"[heygen voice] using explicit voice_id={user_voice_id[:10]}")
        return user_voice_id.strip()

    import urllib.request as _ureq

    # 1) Detect avatar gender via /v2/avatars
    gender = ""
    avatar_name = ""
    try:
        req = _ureq.Request(
            "https://api.heygen.com/v2/avatars",
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
        )
        with _ureq.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read())
        for av in data.get("data", {}).get("avatars", []) or []:
            if av.get("avatar_id") == avatar_id:
                gender = (av.get("gender") or "").strip().lower()
                avatar_name = av.get("avatar_name") or av.get("name") or ""
                break
    except Exception as exc:
        print(f"[heygen voice] /v2/avatars lookup failed: {exc}")

    # If we couldn't determine gender from API, infer from name as last resort.
    # Common English/Western names — covers ~80% of HeyGen's catalogue.
    if not gender and avatar_name:
        nm = avatar_name.lower().split()[0] if avatar_name else ""
        male_names = {
            "aaron", "adam", "alex", "andrew", "anthony", "ben", "benjamin",
            "brian", "carl", "chris", "christopher", "daniel", "david", "edward",
            "ethan", "george", "henry", "ian", "jack", "james", "jason",
            "jeff", "john", "jonathan", "joseph", "joshua", "kevin", "kyle",
            "leo", "luke", "mark", "matthew", "michael", "nathan", "nicholas",
            "oliver", "patrick", "paul", "peter", "richard", "robert", "ryan",
            "samuel", "scott", "sean", "stephen", "thomas", "tom", "tyler",
            "william",
        }
        female_names = {
            "alice", "amanda", "amelia", "amy", "anna", "ashley", "barbara",
            "bella", "betty", "carol", "catherine", "charlotte", "cynthia",
            "deborah", "diana", "donna", "dorothy", "elizabeth", "emily",
            "emma", "eva", "grace", "hannah", "helen", "isabella", "jane",
            "janet", "jennifer", "jessica", "joan", "julia", "karen",
            "katherine", "kathleen", "kelly", "kimberly", "laura", "linda",
            "lisa", "margaret", "maria", "mary", "melissa", "michelle",
            "mia", "monica", "nancy", "natalie", "olivia", "patricia",
            "rachel", "rebecca", "ruth", "sandra", "sarah", "sharon",
            "shirley", "sophia", "stephanie", "susan", "victoria",
        }
        if nm in male_names:
            gender = "male"
        elif nm in female_names:
            gender = "female"
        if gender:
            print(f"[heygen voice] gender inferred from name '{avatar_name}': {gender}")

    # Default to male when truly unknown (most news avatars are male-presenting).
    if not gender:
        gender = "male"
        print(f"[heygen voice] gender unknown for avatar {avatar_id[:10]} — defaulting to male")

    # 2) Env override (lets the user pin a known-good voice if API lookup misbehaves)
    env_male   = (os.environ.get("HEYGEN_VOICE_ID_MALE")   or "").strip()
    env_female = (os.environ.get("HEYGEN_VOICE_ID_FEMALE") or "").strip()
    if gender == "male" and env_male:
        print(f"[heygen voice] env override -> {env_male[:10]} (male)")
        return env_male
    if gender == "female" and env_female:
        print(f"[heygen voice] env override -> {env_female[:10]} (female)")
        return env_female

    # 3) Live lookup of HeyGen's voice catalogue → first English voice with matching gender
    voice_id = ""
    voice_name = ""
    try:
        req = _ureq.Request(
            "https://api.heygen.com/v2/voices",
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
        )
        with _ureq.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read())
        voices = data.get("data", {}).get("voices", []) or []
        def _score(v: dict) -> int:
            lang = (v.get("language") or "").lower()
            g    = (v.get("gender") or "").lower()
            if g != gender:
                return -1
            score = 0
            if lang.startswith("en-us") or lang == "english":
                score += 100
            elif lang.startswith("en-gb"):
                score += 80
            elif lang.startswith("en"):
                score += 60
            else:
                return -1
            # Prefer voices flagged as "support_pause" / premium tier
            if v.get("support_pause"):
                score += 5
            return score
        scored = [(_score(v), v) for v in voices]
        scored = [t for t in scored if t[0] > 0]
        scored.sort(key=lambda t: t[0], reverse=True)
        if scored:
            voice_id   = scored[0][1].get("voice_id", "")
            voice_name = scored[0][1].get("name", "")
    except Exception as exc:
        print(f"[heygen voice] /v2/voices lookup failed: {exc}")

    # 4) Last-resort: if API didn't return any voices, raise a clear error
    # rather than send a guessed/invalid voice_id (which would burn API credit).
    if not voice_id:
        raise RuntimeError(
            f"HeyGen /v2/voices returned no English {gender} voice. "
            "Set HEYGEN_VOICE_ID_MALE / HEYGEN_VOICE_ID_FEMALE in .env, "
            "or paste a voice_id in Settings → AI Keys → HeyGen voice ID."
        )

    print(f"[heygen voice] avatar={avatar_id[:10]} '{avatar_name}' "
          f"gender={gender} -> voice={voice_id[:10]} '{voice_name}'")
    return voice_id


def _heygen_generate_async(
    job_id: int, key: str, avatar_id: str,
    script: str, platform: str,
    out_dir: "Path", bg_image: "Path | None",
    brand_handle: str, brand_color: str,
    user_id: int = 0, settings: "dict | None" = None,
    final_path_override: "Path | None" = None,
) -> None:
    """Background thread: submit to HeyGen, poll, download, composite with news image."""
    import time
    import uuid as _uuid
    import urllib.request as _ureq
    from src.agents.avatar_studio import (
        _update_job, _audio_duration, composite_for_platform, PLATFORM_PRESETS,
    )

    preset = PLATFORM_PRESETS.get(platform, PLATFORM_PRESETS["tiktok"])
    dim_w = min(preset["width"],  1080)
    dim_h = min(preset["height"], 1920)

    # CRITICAL FIX: strip [HOOK — 0:00–0:02] / [CONTEXT] / [CTA] markers
    # before sending to HeyGen. Without this, the avatar literally reads
    # "open bracket HOOK em-dash zero colon zero zero..." which destroys
    # the entire video and burns API credits for garbage output.
    try:
        from src.agents.video_builder import extract_narration
        clean_script = extract_narration(script) or script
    except Exception:
        clean_script = script
    if not clean_script.strip():
        clean_script = script  # never send empty payload to HeyGen

    try:
        # ── Pre-flight validation — never burn HeyGen credits on garbage ──
        # HeyGen charges for any /v2/video/generate that returns a video_id,
        # even if the resulting clip is unusable. We catch obvious problems
        # BEFORE the API call:
        #   • script too short (< 10 words → HeyGen produces a 2-second clip)
        #   • script too long (> 500 words → exceeds free-tier limit)
        #   • avatar_id wrong format (HeyGen IDs are 32-char hex)
        _word_count = len(clean_script.split())
        if _word_count < 5:
            raise RuntimeError(
                f"Script prea scurt ({_word_count} cuvinte). HeyGen va genera "
                "un clip de 2-3 sec inutilizabil. Verifică `script_20s.txt`."
            )
        if _word_count > 500:
            raise RuntimeError(
                f"Script prea lung ({_word_count} cuvinte). Reduceți la <500 "
                "sau bifați Settings → Video Duration mai scurt."
            )
        if not avatar_id or len(avatar_id.strip()) < 16:
            raise RuntimeError(
                f"Avatar ID invalid: {avatar_id!r}. Selectează un avatar din "
                "grid sau copiază ID-ul din heygen.com → Avatars."
            )

        # Pick a voice whose gender matches the avatar — fixes "female voice
        # on a male avatar" issue. Honours an explicit voice_id from settings.
        # Wrapped in try so a voice-lookup failure marks the job as error
        # cleanly instead of crashing the worker thread.
        _user_voice = ""
        if settings:
            _user_voice = (settings.get("heygen_voice_id") or "").strip()
        voice_id_for_hg = _pick_heygen_voice_for_avatar(key, avatar_id, _user_voice)
        if not voice_id_for_hg or len(voice_id_for_hg) < 16:
            raise RuntimeError(
                f"Voice ID invalid returnat de picker: {voice_id_for_hg!r}. "
                "Setează HEYGEN_VOICE_ID_MALE/FEMALE in .env."
            )

        # Final guard: log exactly what we're about to send (for debugging).
        print(f"[heygen] job {job_id} → avatar={avatar_id[:10]} "
              f"voice={voice_id_for_hg[:10]} words={_word_count} "
              f"dim={dim_w}x{dim_h}")

        # Step 1: Submit video generation to HeyGen
        # Green background (#00FF00) → FFmpeg chromakey removes it perfectly
        # leaving only the avatar on transparent, then overlaid on news image.
        payload = json.dumps({
            "video_inputs": [{
                "character": {
                    "type": "avatar",
                    "avatar_id": avatar_id,
                    "avatar_style": "normal",
                },
                "voice": {
                    "type": "text",
                    "input_text": clean_script,
                    "voice_id": voice_id_for_hg,
                },
                "background": {
                    "type": "color",
                    "value": "#00FF00",
                },
            }],
            "dimension": {"width": dim_w, "height": dim_h},
        }).encode()

        req = _ureq.Request(
            "https://api.heygen.com/v2/video/generate",
            data=payload,
            headers={"X-Api-Key": key, "Content-Type": "application/json"},
            method="POST",
        )
        with _ureq.urlopen(req, timeout=30) as resp:
            r_data = json.loads(resp.read())

        video_id = r_data.get("data", {}).get("video_id")
        if not video_id:
            raise RuntimeError(f"HeyGen didn't return video_id: {r_data}")
        print(f"[heygen] job {job_id}: video_id={video_id}")
        _update_job(job_id, progress=10, error="HeyGen: video trimis, se renderizează...")

        # Step 2: Poll status until completed (max 10 min)
        video_url = ""
        for attempt in range(120):
            time.sleep(5)
            poll_req = _ureq.Request(
                f"https://api.heygen.com/v1/video_status.get?video_id={video_id}",
                headers={"X-Api-Key": key},
            )
            with _ureq.urlopen(poll_req, timeout=15) as resp:
                status_data = json.loads(resp.read())
            vdata = status_data.get("data", {})
            vstat = vdata.get("status", "")
            print(f"[heygen] job {job_id}: poll #{attempt + 1} status={vstat}")
            # Update progress every 5 polls so UI shows real advancement
            if attempt % 5 == 0:
                pct = min(10 + attempt, 65)
                _update_job(job_id, progress=pct,
                            error=f"HeyGen rendering... ({(attempt+1)*5}s)")
            if vstat == "completed":
                video_url = vdata.get("video_url") or vdata.get("video_url_caption", "")
                break
            if vstat in ("failed", "error"):
                raise RuntimeError(f"HeyGen failed: {vdata.get('error', vdata)}")

        if not video_url:
            raise RuntimeError("HeyGen timeout — video not ready after 10 minutes")

        # Step 3: Download raw HeyGen video
        _update_job(job_id, progress=70, error="HeyGen gata — se descarcă videoclipul...")
        slug    = f"{platform}_heygen_{_uuid.uuid4().hex[:8]}"
        raw_out = out_dir / f"{slug}_raw.mp4"
        # Download with explicit headers (some CDNs require User-Agent)
        try:
            dl_req = _ureq.Request(video_url, headers={"User-Agent": "Mozilla/5.0"})
            with _ureq.urlopen(dl_req, timeout=120) as resp, open(str(raw_out), "wb") as f:
                f.write(resp.read())
        except Exception as dl_exc:
            # Fallback to urlretrieve
            _ureq.urlretrieve(video_url, str(raw_out))
        print(f"[heygen] job {job_id}: downloaded -> {raw_out.name} ({round(raw_out.stat().st_size/1_048_576,1)} MB)")
        _update_job(job_id, progress=80, error="Composite: avatar + fundal + subtitrări...")

        # Step 4: Generate word-level SRT (3 words/chunk, karaoke feel)
        from src.agents.avatar_video import _composite_ffmpeg
        from src.agents.avatar_studio import _audio_duration as _av_dur
        raw_duration = _av_dur(raw_out)
        srt_out = out_dir / f"{slug}_caps.srt"
        _words = clean_script.split()
        _chunk_sz = 3
        _chunks = [' '.join(_words[i:i+_chunk_sz]) for i in range(0, len(_words), _chunk_sz)]
        _cd = raw_duration / max(len(_chunks), 1)
        def _srt_fmt(t):
            hh=int(t//3600); mm=int((t%3600)//60); ss=int(t%60); ms=int((t%1)*1000)
            return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"
        _srt_lines = []
        for _i, _c in enumerate(_chunks):
            _s = _i * _cd
            _e = min((_i + 1) * _cd, raw_duration - 0.05)
            _srt_lines += [str(_i+1), f"{_srt_fmt(_s)} --> {_srt_fmt(_e)}", _c, ""]
        srt_out.write_text('\n'.join(_srt_lines), encoding='utf-8')

        # Step 5: Ensure background exists
        if not (bg_image and bg_image.exists()):
            bg_image = out_dir / "_dark_bg.png"
            if not bg_image.exists():
                try:
                    from PIL import Image as _Img
                    _Img.new("RGB", (1080, 1920), color=(6, 12, 9)).save(bg_image)
                except Exception:
                    pass

        # Step 6: Premium composite — TV news anchor layout
        final_out = Path(final_path_override) if final_path_override else (out_dir / f"{slug}_final.mp4")
        _hg_profile = {
            "duration_seconds": int(raw_duration) + 1,
            "style": "simple",
            "brand": {"color": brand_color, "handle": brand_handle},
            "intro_enabled": False,
            "outro_enabled": False,
            "bgm_enabled": False,
        }
        # Extract headline from slot's top2.json — look in slot_dir (parent of _heygen_work)
        _hg_headline = ""
        _hg_excerpt = ""
        try:
            import json as _jj, re as _rr
            # out_dir = slot_dir/_heygen_work → parent is slot_dir
            _top2 = out_dir.parent / "top2.json"
            if not _top2.exists():
                _top2 = out_dir / "top2.json"  # legacy fallback
            if _top2.exists():
                _td = _jj.loads(_top2.read_text(encoding="utf-8"))
                _sts = _td if isinstance(_td, list) else _td.get("stories") or []
                if _sts:
                    _hg_headline = (_sts[0].get("title") or _sts[0].get("headline") or "").strip()
            if not _hg_headline:
                _hg_headline = (clean_script.splitlines()[0] if clean_script.strip() else "")[:120]
            _sents = _rr.split(r"(?<=[.!?])\s+", clean_script.strip())
            _hg_excerpt = " ".join(_sents[:3])
        except Exception:
            pass

        # Try composite — if FFmpeg fails, fall back to raw video (user still gets their clip)
        composite_ok = False
        try:
            _composite_ffmpeg(
                bg_path=bg_image,
                avatar_path=raw_out,
                out_path=final_out,
                srt_path=srt_out,
                profile=_hg_profile,
                headline=_hg_headline or None,
                script_excerpt=_hg_excerpt or None,
            )
            composite_ok = True
        except Exception as ffmpeg_exc:
            print(f"[heygen] job {job_id}: composite failed ({ffmpeg_exc}) — using raw video as output")
            _update_job(job_id, progress=88,
                        error="⚠️ Composite indisponibil (FFmpeg lipsă?) — se salvează video brut HeyGen...")
            import shutil as _sh
            _sh.copy2(str(raw_out), str(final_out))

        for _p in (srt_out,):
            try: _p.unlink(missing_ok=True)
            except Exception: pass
        if composite_ok:
            try: raw_out.unlink(missing_ok=True)
            except Exception: pass

        _update_job(job_id, progress=95, error="Salvare reel_avatar.mp4...")
        duration = _audio_duration(final_out)
        size_mb  = round(final_out.stat().st_size / 1_048_576, 2)
        engine_tag = "heygen" if composite_ok else "heygen-raw"
        _update_job(job_id, status="done", progress=100,
                    output_path=str(final_out), duration_s=round(duration, 1),
                    engine=engine_tag, error="")
        print(f"[heygen] job {job_id} done [{engine_tag}] -> {final_out.name} ({size_mb} MB)")

        # Track HeyGen usage + send email notification
        if user_id:
            try:
                log_heygen_usage(user_id, out_dir.parent.name, platform,
                                 len(clean_script.split()), "done")
            except Exception as _e:
                print(f"[heygen] usage log failed: {_e}")
            try:
                _send_email_notification(user_id, "heygen_done",
                    subject="Avatar video gata!",
                    body=f"Video-ul pentru slot '{out_dir.parent.name}' a fost generat ({round(duration,1)}s, {size_mb} MB).")
            except Exception as _e:
                print(f"[heygen] email notify failed: {_e}")

    except Exception as exc:
        import traceback as _tb
        err_str = str(exc)
        print(f"[heygen] job {job_id} failed: {exc}\n{_tb.format_exc()}")
        from src.agents.avatar_studio import _update_job as _upd

        # Auto-fallback to D-ID when HeyGen has no API credits
        _credit_err = any(k in err_str for k in (
            "MOVIO_PAYMENT_INSUFFICIENT_CREDIT", "Insufficient credit",
            "insufficient_credit", "api' credits", "api credits",
        ))
        if _credit_err and user_id and settings:
            print(f"[heygen] job {job_id}: no API credits → auto-fallback to D-ID premium pipeline")
            _upd(job_id, status="running",
                 error="HeyGen: credite API insuficiente — se foloseste D-ID fallback...")
            try:
                import shutil
                from src.agents.avatar_video import build_avatar_reel

                avatar_sub = out_dir / "avatar"
                avatar_sub.mkdir(parents=True, exist_ok=True)
                (avatar_sub / "script_20s.txt").write_text(script, encoding="utf-8")

                bg_dest = out_dir / "image_reel_1080x1920.png"
                if bg_image and bg_image.exists() and bg_image.resolve() != bg_dest.resolve():
                    shutil.copy2(bg_image, bg_dest)
                elif not bg_dest.exists():
                    try:
                        from PIL import Image as _Img
                        img = _Img.new("RGB", (1080, 1920), color=(6, 12, 9))
                        img.save(bg_dest)
                    except Exception:
                        pass

                with _uenv(settings):
                    out_path = build_avatar_reel(out_dir, presenter="default", platform=platform)

                size_mb = round(out_path.stat().st_size / 1_048_576, 2)
                _upd(job_id, status="done", output_path=str(out_path),
                     duration_s=0, engine="did (heygen-fallback)")
                print(f"[heygen] job {job_id}: D-ID fallback done -> {out_path.name} ({size_mb} MB)")
            except Exception as fallback_exc:
                print(f"[heygen] job {job_id}: D-ID fallback failed: {fallback_exc}")
                _upd(job_id, status="failed",
                     error=f"HeyGen: credite API insuficiente. D-ID fallback a esuat: {str(fallback_exc)[:300]}")
        else:
            _upd(job_id, status="failed", error=err_str[:500])


@app.post("/api/avatar/generate")
async def api_avatar_generate(
    request: Request,
    script:              str = Form(...),
    platform:            str = Form("tiktok"),
    profile_id:          int = Form(0),
    heygen_avatar_id:    str = Form(""),
    did_presenter_name:  str = Form(""),
    did_presenter_url:   str = Form(""),
    did_voice_id:        str = Form(""),
):
    uid      = request.state.user["id"]
    settings = get_user_settings(uid)
    from src.agents.avatar_studio import studio as _studio, PLATFORM_PRESETS, _create_job, _update_job
    if platform not in PLATFORM_PRESETS:
        return JSONResponse({"error": f"Unknown platform {platform!r}"}, status_code=400)
    if not script.strip():
        return JSONResponse({"error": "Script cannot be empty"}, status_code=400)

    out_dir = BASE_QUEUE / str(uid) / "avatar_output"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Find latest reel background image if available (date slots first)
    bg_image: Path | None = None
    _uid_queue = BASE_QUEUE / str(uid)
    if _uid_queue.exists():
        for slot_dir in sorted(_uid_queue.iterdir(),
                               key=lambda d: (1 if d.name[:4].isdigit() else 2, -d.stat().st_mtime)):
            candidate = slot_dir / "image_reel_1080x1920.png"
            if candidate.exists():
                bg_image = candidate
                break

    # ── D-ID path: sync executor (same as slot build) ─────────────────────────
    did_name = did_presenter_name.strip() or ""
    if did_name:
        did_key = settings.get("did_api_key", "").strip()
        if not did_key:
            return JSONResponse(
                {"error": "D-ID API Key nu este configurat în Settings → AI Keys."},
                status_code=400,
            )
        import time as _time
        t0 = _time.time()
        job_id = _create_job(uid, profile_id or None, platform, script.strip(), str(bg_image or ""))
        _update_job(job_id, status="running")

        def _run_did():
            import shutil
            from src.agents.avatar_video import build_avatar_reel
            # Prepare slot-like structure that build_avatar_reel expects
            avatar_sub = out_dir / "avatar"
            avatar_sub.mkdir(parents=True, exist_ok=True)
            (avatar_sub / "script_20s.txt").write_text(script.strip(), encoding="utf-8")
            # Copy or symlink background image
            bg_dest = out_dir / "image_reel_1080x1920.png"
            if bg_image and bg_image.exists() and bg_image.resolve() != bg_dest.resolve():
                shutil.copy2(bg_image, bg_dest)
            elif not bg_dest.exists():
                # Generate a neutral dark background if no slot image found
                try:
                    from PIL import Image as _Img
                    img = _Img.new("RGB", (1080, 1920), color=(6, 12, 9))
                    img.save(bg_dest)
                except Exception:
                    pass
            voice    = did_voice_id.strip() or "en-US-GuyNeural"
            # Use pre-uploaded s3:// URL if provided, else fall back to alice.jpg
            src_url  = did_presenter_url.strip() or settings.get("did_presenter_url", "").strip()
            with _uenv(settings):
                os.environ["DID_VOICE_ID"] = voice
                if src_url:
                    os.environ["DID_PRESENTER_URL"] = src_url
                return build_avatar_reel(
                    out_dir,
                    presenter=did_name,
                    platform=platform,
                )

        try:
            out_path = await asyncio.get_event_loop().run_in_executor(_publish_executor, _run_did)
            elapsed  = round(_time.time() - t0, 1)
            size_mb  = round(out_path.stat().st_size / 1_048_576, 2)
            duration = elapsed  # approximate
            _update_job(job_id, status="done", output_path=str(out_path),
                        duration_s=round(duration, 1), engine="did")
            return JSONResponse({
                "status": "done", "job_id": job_id,
                "output_path": str(out_path), "platform": platform,
                "size_mb": size_mb, "elapsed_s": elapsed,
            })
        except Exception as exc:
            import traceback
            _update_job(job_id, status="failed", error=str(exc)[:500])
            return JSONResponse(
                {"error": str(exc), "traceback": traceback.format_exc()[:1500]},
                status_code=500,
            )

    # ── HeyGen path: async background thread ──────────────────────────────────
    heygen_id = heygen_avatar_id.strip()
    if heygen_id:
        heygen_key = settings.get("heygen_api_key", "").strip()
        if not heygen_key:
            return JSONResponse(
                {"error": "HeyGen API Key nu este configurat în Settings → AI Keys."},
                status_code=400,
            )
        job_id = _create_job(uid, profile_id or None, platform, script.strip(), str(bg_image or ""))
        _update_job(job_id, status="running")

        import threading
        threading.Thread(
            target=_heygen_generate_async,
            args=(job_id, heygen_key, heygen_id, script.strip(), platform, out_dir, bg_image,
                  settings.get("video_brand_handle", ""), settings.get("video_brand_color", "#F59E0B"),
                  uid, dict(settings)),
            daemon=True,
        ).start()
        return JSONResponse({"job_id": job_id, "status": "running"})

    # ── Local engines path: synchronous ──────────────────────────────────────
    def _run():
        with _uenv(settings):
            return _studio.generate_video(
                user_id=uid,
                script=script,
                platform=platform,
                profile_id=profile_id or None,
                bg_image=bg_image,
                output_dir=out_dir,
                brand_handle=settings.get("video_brand_handle", ""),
                brand_color=settings.get("video_brand_color", "#F59E0B"),
            )

    try:
        result = await asyncio.get_event_loop().run_in_executor(_publish_executor, _run)
        return JSONResponse(result)
    except Exception as exc:
        import traceback
        return JSONResponse(
            {"error": str(exc), "traceback": traceback.format_exc()[:1500]},
            status_code=500,
        )


@app.get("/api/avatar/jobs")
async def api_avatar_jobs(request: Request):
    from src.agents.avatar_studio import get_jobs
    jobs = get_jobs(request.state.user["id"], limit=5)
    return JSONResponse({"jobs": jobs})


@app.get("/api/avatar/job/{job_id}")
async def api_avatar_job(request: Request, job_id: int):
    from src.agents.avatar_studio import get_job
    job = get_job(job_id)
    if not job or job["user_id"] != request.state.user["id"]:
        raise HTTPException(404, "Job not found")
    return JSONResponse(job)


@app.get("/api/avatar/engines")
async def api_avatar_engines(request: Request):
    from src.agents.avatar_studio import studio as _studio
    return JSONResponse({"engines": _studio.available_engines()})


@app.get("/api/heygen/avatars")
async def api_heygen_avatars(request: Request):
    uid = request.state.user["id"]
    settings = get_user_settings(uid)
    key = settings.get("heygen_api_key", "").strip()
    if not key:
        return JSONResponse(
            {"error": "HeyGen API Key nu este configurat în Settings → AI Keys."},
            status_code=400,
        )
    try:
        import urllib.request as _ureq
        req = _ureq.Request(
            "https://api.heygen.com/v2/avatars",
            headers={"X-Api-Key": key, "Accept": "application/json"},
        )
        with _ureq.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        avatars = data.get("data", {}).get("avatars", [])
        return JSONResponse({"avatars": avatars})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/schedule-times")
async def get_schedule_times(request: Request):
    uid = request.state.user["id"]
    s   = get_user_settings(uid)
    return {
        "post_time_morning": s.get("post_time_morning") or "08:00",
        "post_time_evening": s.get("post_time_evening") or "18:00",
    }


@app.post("/api/schedule-times")
async def save_schedule_times(request: Request):
    body = await request.json()
    morning = (body.get("post_time_morning") or "").strip()
    evening = (body.get("post_time_evening") or "").strip()
    import re as _re
    if not _re.match(r"^\d{2}:\d{2}$", morning) or not _re.match(r"^\d{2}:\d{2}$", evening):
        raise HTTPException(400, "Format invalid — folositi HH:MM")
    m_h, m_m = int(morning.split(":")[0]), int(morning.split(":")[1])
    e_h, e_m = int(evening.split(":")[0]), int(evening.split(":")[1])
    if not (0 <= m_h <= 23 and 0 <= m_m <= 59 and 0 <= e_h <= 23 and 0 <= e_m <= 59):
        raise HTTPException(400, "Ora invalida")
    if m_h >= e_h:
        raise HTTPException(400, "Ora de dimineata trebuie sa fie inaintea orei de seara")
    uid = request.state.user["id"]
    save_user_settings(uid, post_time_morning=morning, post_time_evening=evening)
    return {"ok": True, "post_time_morning": morning, "post_time_evening": evening}


@app.get("/api/heygen/voices")
async def api_heygen_voices(request: Request):
    uid = request.state.user["id"]
    settings = get_user_settings(uid)
    key = settings.get("heygen_api_key", "").strip()
    if not key:
        return JSONResponse({"error": "HeyGen API Key nu este configurat."}, status_code=400)
    try:
        import urllib.request as _ureq
        req = _ureq.Request(
            "https://api.heygen.com/v2/voices",
            headers={"X-Api-Key": key, "Accept": "application/json"},
        )
        with _ureq.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        voices = data.get("data", {}).get("voices", [])
        # Filter to English + keep relevant fields
        filtered = [
            {
                "voice_id":    v.get("voice_id", ""),
                "name":        v.get("name", ""),
                "language":    v.get("language", ""),
                "gender":      v.get("gender", ""),
                "preview_url": v.get("preview_audio_url", ""),
            }
            for v in voices
            if "english" in (v.get("language") or "").lower()
            or (v.get("language") or "").startswith("en")
        ][:60]
        return JSONResponse({"voices": filtered})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


def _ad_generate_video_async(
    job_id: int, uid: int, key: str, avatar_id: str, voice_id: str,
    script: str, image_prompt: str, out_dir: "Path",
    brand_handle: str, brand_color: str,
) -> None:
    """Background thread: generate 9:16 bg image → HeyGen ad video → composite."""
    import time, uuid as _uuid, urllib.request as _ureq
    from src.agents.avatar_studio import _update_job

    def _upd(**kw): _update_job(job_id, **kw)

    try:
        _upd(status="running", progress=5)
        out_dir.mkdir(parents=True, exist_ok=True)

        # Step 1 — Generate 9:16 background via Pollinations.ai
        _upd(progress=10)
        bg_path = out_dir / "ad_background.jpg"
        try:
            from src.agents.image_gen import _download_background
            img = _download_background(image_prompt, width=1080, height=1920,
                                       seed=abs(hash(script)) % 9999, mood="neutral")
            img.save(str(bg_path), "JPEG", quality=92)
            print(f"[ad_video] bg image saved: {bg_path.name}")
        except Exception as e:
            print(f"[ad_video] bg image failed ({e}), proceeding without bg")
            bg_path = None

        _upd(progress=25)

        # Step 2 — Submit to HeyGen
        payload = json.dumps({
            "video_inputs": [{
                "character": {
                    "type": "avatar",
                    "avatar_id": avatar_id,
                    "avatar_style": "normal",
                },
                "voice": {
                    "type": "text",
                    "input_text": script,
                    "voice_id": voice_id,
                },
                "background": {
                    "type": "color",
                    "value": "#00FF00",
                },
            }],
            "dimension": {"width": 1080, "height": 1920},
        }).encode()

        req = _ureq.Request(
            "https://api.heygen.com/v2/video/generate",
            data=payload,
            headers={"X-Api-Key": key, "Content-Type": "application/json"},
            method="POST",
        )
        with _ureq.urlopen(req, timeout=30) as resp:
            r_data = json.loads(resp.read())

        video_id = r_data.get("data", {}).get("video_id")
        if not video_id:
            raise RuntimeError(f"HeyGen no video_id: {r_data}")

        _upd(progress=35, status="running")
        print(f"[ad_video] HeyGen video_id={video_id}")

        # Step 3 — Poll until done
        video_url = ""
        for attempt in range(120):
            time.sleep(5)
            poll_req = _ureq.Request(
                f"https://api.heygen.com/v1/video_status.get?video_id={video_id}",
                headers={"X-Api-Key": key},
            )
            with _ureq.urlopen(poll_req, timeout=15) as resp:
                s_data = json.loads(resp.read())
            vstat = s_data.get("data", {}).get("status", "")
            pct = min(35 + attempt, 80)
            _upd(progress=pct)
            print(f"[ad_video] poll #{attempt+1} status={vstat}")
            if vstat == "completed":
                video_url = s_data["data"].get("video_url") or s_data["data"].get("video_url_caption", "")
                break
            if vstat in ("failed", "error"):
                raise RuntimeError(f"HeyGen failed: {s_data.get('data', {}).get('error')}")

        if not video_url:
            raise RuntimeError("HeyGen timeout after 10 minutes")

        # Step 4 — Download raw video
        slug    = f"ad_{_uuid.uuid4().hex[:8]}"
        raw_out = out_dir / f"{slug}_raw.mp4"
        _ureq.urlretrieve(video_url, str(raw_out))
        _upd(progress=85)

        # Step 5 — Composite avatar on background
        final_out = out_dir / f"{slug}_final.mp4"
        try:
            from src.agents.avatar_studio import composite_for_platform
            composite_for_platform(
                raw_out, bg_path, "tiktok", final_out,
                caption_text=script[:100],
                brand_handle=brand_handle,
                brand_color=brand_color,
            )
            raw_out.unlink(missing_ok=True)
        except Exception as e:
            print(f"[ad_video] composite failed ({e}), using raw video")
            raw_out.rename(final_out)

        size_mb   = round(final_out.stat().st_size / 1_048_576, 2)
        # Build a web-servable relative URL: /ad-video/{ts}/{filename}
        ts_folder = out_dir.name
        video_url = f"/ad-video/{ts_folder}/{final_out.name}"
        _upd(status="done", progress=100,
             video_path=str(final_out),
             video_filename=final_out.name,
             video_url=video_url,
             file_size_mb=size_mb)
        print(f"[ad_video] done: {final_out.name} ({size_mb} MB)")

    except Exception as exc:
        print(f"[ad_video] ERROR job {job_id}: {exc}")
        _update_job(job_id, status="failed", error=str(exc)[:500])


@app.post("/api/heygen/ad-video/generate")
async def api_ad_video_generate(request: Request):
    uid      = request.state.user["id"]
    settings = get_user_settings(uid)
    key      = settings.get("heygen_api_key", "").strip()
    if not key:
        raise HTTPException(400, "HeyGen API Key nu este configurat în Settings → AI Keys.")

    body      = await request.json()
    avatar_id = (body.get("avatar_id") or "").strip()
    voice_id  = (body.get("voice_id") or "").strip()
    script    = (body.get("script") or "").strip()
    img_prompt = (body.get("image_prompt") or "").strip()

    if not avatar_id:
        raise HTTPException(400, "avatar_id este obligatoriu")
    if not script:
        raise HTTPException(400, "script este obligatoriu")
    if not voice_id:
        # Auto-pick a voice whose gender matches the avatar's gender.
        voice_id = _pick_heygen_voice_for_avatar(key, avatar_id)

    import datetime as _dt, threading
    ts      = _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_dir = BASE_QUEUE / str(uid) / "ad_videos" / ts

    from src.agents.avatar_studio import _create_job, _update_job as _av_upd
    job_id = _create_job(
        user_id=uid, profile_id=None, platform="tiktok",
        script=script[:500],
    )
    _av_upd(job_id, engine="heygen")

    brand_handle = settings.get("video_brand_handle", "")
    brand_color  = settings.get("video_brand_color", "#F43F5E")

    threading.Thread(
        target=_ad_generate_video_async,
        args=(job_id, uid, key, avatar_id, voice_id, script, img_prompt,
              out_dir, brand_handle, brand_color),
        daemon=True,
    ).start()

    return {"ok": True, "job_id": job_id}


@app.get("/api/heygen/ad-video/job/{job_id}")
async def api_ad_video_job(request: Request, job_id: int):
    from src.agents.avatar_studio import get_job
    job = get_job(job_id)
    if not job or job["user_id"] != request.state.user["id"]:
        raise HTTPException(404, "Job not found")
    return JSONResponse(job)


@app.get("/avatar-video/{filename}")
async def serve_avatar_studio_video(request: Request, filename: str):
    if ".." in filename:
        raise HTTPException(400, "Invalid path")
    uid = request.state.user["id"]
    f = BASE_QUEUE / str(uid) / "avatar_output" / filename
    if not f.exists():
        raise HTTPException(404, "Video not found")
    return FileResponse(str(f), media_type="video/mp4")


@app.get("/ad-video/{job_ts}/{filename}")
async def serve_ad_video(request: Request, job_ts: str, filename: str):
    if ".." in job_ts or ".." in filename:
        raise HTTPException(400, "Invalid path")
    uid = request.state.user["id"]
    f   = BASE_QUEUE / str(uid) / "ad_videos" / job_ts / filename
    if not f.exists():
        raise HTTPException(404, "Video not found")
    return FileResponse(str(f), media_type="video/mp4")


# ---------------------------------------------------------------------------
# Ad Script Writer — viral ad generation for any company / industry
# ---------------------------------------------------------------------------

_AD_ALLOWED_INDUSTRIES = {
    "crypto", "real_estate", "fitness", "ecommerce", "finance",
    "tech", "food", "fashion", "travel", "education", "healthcare", "marketing",
}
_AD_ALLOWED_FORMULAS = {"auto", "aida", "pas", "bab"}
_AD_ALLOWED_GOALS    = {"brand_awareness", "sales", "lead_gen", "app_download", "engagement"}
_AD_ALLOWED_VOICES   = {"premium", "casual", "authoritative", "energetic", "friendly", "professional"}


@app.post("/api/ad-script/generate")
async def api_ad_script_generate(request: Request):
    user = request.state.user
    uid  = user["id"]

    body = await request.json()

    company   = (body.get("company") or "").strip()[:80]
    product   = (body.get("product") or "").strip()[:80]
    industry  = (body.get("industry") or "crypto").strip().lower()
    target    = (body.get("target_audience") or "").strip()[:200]
    goal      = (body.get("goal") or "brand_awareness").strip().lower()
    price     = (body.get("price_point") or "").strip()[:30]
    benefit   = (body.get("key_benefit") or "").strip()[:200]
    proof     = (body.get("proof_point") or "").strip()[:200]
    cta       = (body.get("cta") or "").strip()[:200]
    voice     = (body.get("brand_voice") or "professional").strip().lower()
    formula   = (body.get("formula") or "auto").strip().lower()

    if not company:
        raise HTTPException(400, "company is required")
    if not product:
        raise HTTPException(400, "product is required")
    if industry not in _AD_ALLOWED_INDUSTRIES:
        raise HTTPException(400, f"industry must be one of: {', '.join(sorted(_AD_ALLOWED_INDUSTRIES))}")
    if formula not in _AD_ALLOWED_FORMULAS:
        raise HTTPException(400, f"formula must be one of: {', '.join(_AD_ALLOWED_FORMULAS)}")
    if goal not in _AD_ALLOWED_GOALS:
        raise HTTPException(400, f"goal must be one of: {', '.join(_AD_ALLOWED_GOALS)}")

    brief = {
        "company":         company,
        "product":         product,
        "industry":        industry,
        "target_audience": target or f"{industry} enthusiasts",
        "goal":            goal,
        "price_point":     price,
        "key_benefit":     benefit or f"The best {product} solution",
        "proof_point":     proof or f"Trusted by thousands",
        "cta":             cta or f"Try {product} today",
        "brand_voice":     voice if voice in _AD_ALLOWED_VOICES else "professional",
        "formula":         formula,
    }

    import datetime as _dt
    ts      = _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    slug    = re.sub(r"[^a-z0-9]", "_", company.lower())[:20]
    out_dir = BASE_QUEUE / str(uid) / "ad_scripts" / f"{ts}_{slug}"

    try:
        from src.agents.ad_script_writer import write_package
        result = write_package(brief, out_dir)
    except Exception as exc:
        raise HTTPException(500, f"Ad script generation failed: {exc}")

    # Read generated files back
    def _read(fname: str) -> str:
        p = out_dir / fname
        return p.read_text(encoding="utf-8") if p.exists() else ""

    return {
        "ok":       True,
        "formula":  result["formula"],
        "industry": result["industry"],
        "company":  result["company"],
        "product":  result["product"],
        "hooks":    result["hooks"],
        "scenes":   result["scenes"],
        "out_dir":  str(out_dir),
        "script":         _read("01_ad_script.txt"),
        "hook_variants":  _read("02_hook_variants.txt"),
        "platform_meta":  json.loads(_read("05_platform_metadata.json") or "{}"),
        "image_prompt":   _read("06_image_prompt.txt"),
        "heygen_brief":   json.loads(_read("07_heygen_brief.json") or "{}"),
    }


@app.get("/api/ad-script/jobs")
async def api_ad_script_jobs(request: Request):
    uid = request.state.user["id"]
    base = BASE_QUEUE / str(uid) / "ad_scripts"
    if not base.exists():
        return {"jobs": []}
    jobs = []
    for d in sorted(base.iterdir(), reverse=True)[:20]:
        if not d.is_dir():
            continue
        brief_file = d / "brief.json"
        if not brief_file.exists():
            continue
        try:
            b = json.loads(brief_file.read_text(encoding="utf-8"))
            jobs.append({
                "dir":      d.name,
                "company":  b.get("company", ""),
                "product":  b.get("product", ""),
                "industry": b.get("industry", ""),
                "formula":  b.get("formula", ""),
                "goal":     b.get("goal", ""),
            })
        except Exception:
            pass
    return {"jobs": jobs}


# ---------------------------------------------------------------------------
# Audience tracker — shortlinks + ipstack-powered geolocation
# ---------------------------------------------------------------------------

def _client_ip(request: Request) -> str:
    """Best-effort real IP. Honours X-Forwarded-For (trusted via ngrok)."""
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        return xff.split(",")[0].strip()
    real = (request.headers.get("x-real-ip") or "").strip()
    if real:
        return real
    return getattr(getattr(request, "client", None), "host", "") or ""


@app.post("/api/shortlink")
async def api_shortlink_create(request: Request):
    """Create a tracker shortlink for a target URL.

    Body: {"target_url": "...", "label": "...", "platform": "telegram", "slot_name": "..."}
    Returns: {"code": "abc123x", "short_url": "https://.../r/abc123x", "id": 42}
    """
    from src.dashboard.database import create_shortlink as _db_create
    body = await request.json()
    target_url = (body.get("target_url") or "").strip()
    if not target_url:
        raise HTTPException(400, "target_url required")
    if not (target_url.startswith("http://") or target_url.startswith("https://")):
        raise HTTPException(400, "target_url must start with http:// or https://")
    uid = request.state.user["id"]
    info = _db_create(
        user_id=uid,
        target_url=target_url,
        label=(body.get("label") or "").strip(),
        platform=(body.get("platform") or "").strip(),
        slot_name=(body.get("slot_name") or "").strip(),
    )
    settings = get_user_settings(uid)
    base = (settings.get("public_base_url") or "").strip().rstrip("/")
    short_url = f"{base}/r/{info['code']}" if base else f"/r/{info['code']}"
    return JSONResponse({"ok": True, "short_url": short_url, **info})


@app.get("/api/shortlinks")
async def api_shortlinks_list(request: Request):
    from src.dashboard.database import list_shortlinks as _db_list
    uid = request.state.user["id"]
    settings = get_user_settings(uid)
    base = (settings.get("public_base_url") or "").strip().rstrip("/")
    items = _db_list(uid)
    for it in items:
        it["short_url"] = (f"{base}/r/{it['code']}" if base
                            else f"/r/{it['code']}")
    return JSONResponse({"shortlinks": items})


@app.delete("/api/shortlink/{link_id}")
async def api_shortlink_delete(request: Request, link_id: int):
    from src.dashboard.database import delete_shortlink as _db_del
    uid = request.state.user["id"]
    _db_del(link_id, uid)
    return JSONResponse({"ok": True})


@app.get("/api/shortlink/{link_id}/clicks")
async def api_shortlink_clicks(request: Request, link_id: int):
    from src.dashboard.database import get_shortlink_clicks as _db_clicks
    uid = request.state.user["id"]
    clicks = _db_clicks(link_id, uid)
    return JSONResponse({"clicks": clicks})


@app.get("/api/audience/breakdown")
async def api_audience_breakdown(request: Request, days: int = 30):
    """Aggregated country breakdown across all the user's shortlinks."""
    from src.dashboard.database import get_audience_breakdown as _db_breakdown
    days = max(1, min(int(days or 30), 365))
    uid = request.state.user["id"]
    return JSONResponse(_db_breakdown(uid, days=days))


@app.get("/audience", response_class=HTMLResponse)
async def audience_page(request: Request):
    """Audience tracker dashboard — country breakdown + shortlink manager."""
    return _render("audience.html", user=request.state.user)


# Public redirect endpoint — NO auth required (it's the consumer-facing URL).
# This is added BEFORE the auth middleware via _PUBLIC_PATHS in get_current_user.
@app.get("/r/{code}")
async def shortlink_redirect(code: str, request: Request):
    from src.dashboard.database import (get_shortlink_by_code,
                                          log_shortlink_click)
    from src.dashboard.geo import geolocate_ip

    code = (code or "").strip()
    link = get_shortlink_by_code(code)
    if not link:
        return HTMLResponse("Link not found", status_code=404)

    ip   = _client_ip(request)
    ua   = (request.headers.get("user-agent") or "")[:255]
    ref  = (request.headers.get("referer") or "")[:255]
    geo  = geolocate_ip(ip) if ip else {}
    try:
        log_shortlink_click(
            link_id=link["id"],
            ip=ip,
            country_code=geo.get("country_code", ""),
            country_name=geo.get("country_name", ""),
            city=geo.get("city", ""),
            user_agent=ua,
            referer=ref,
        )
    except Exception as exc:
        print(f"[shortlink] click log failed: {exc}")

    return RedirectResponse(link["target_url"], status_code=302)


# ---------------------------------------------------------------------------
# Feature: Preview / edit avatar script before HeyGen
# ---------------------------------------------------------------------------

@app.get("/api/slot/{name}/avatar-script")
async def get_avatar_script(request: Request, name: str):
    """Return clean narration text (after extract_narration) for preview."""
    slot_dir = _uqueue(request) / name
    if not slot_dir.exists():
        return JSONResponse({"error": "Slot not found"}, status_code=404)
    avatar_dir = slot_dir / "avatar"
    for candidate in (
        avatar_dir / "script_20s.txt",
        avatar_dir / "script_30s.txt",
        slot_dir / "reel" / "01_script.txt",
    ):
        if candidate.exists():
            raw = candidate.read_text(encoding="utf-8")
            try:
                from src.agents.video_builder import extract_narration
                clean = extract_narration(raw)
            except Exception:
                clean = raw
            return JSONResponse({"raw": raw, "clean": clean,
                                  "words": len(clean.split()),
                                  "source": candidate.name})
    return JSONResponse({"error": "Script not found. Run pipeline first."}, status_code=404)


@app.post("/api/slot/{name}/avatar-script")
async def save_avatar_script(request: Request, name: str,
                              script: str = Form(...)):
    """Save user-edited clean script (written to avatar/script_20s.txt)."""
    script = script.strip()
    if not script:
        return JSONResponse({"error": "Script cannot be empty"}, status_code=400)
    slot_dir = _uqueue(request) / name
    if not slot_dir.exists():
        return JSONResponse({"error": "Slot not found"}, status_code=404)
    avatar_dir = slot_dir / "avatar"
    avatar_dir.mkdir(parents=True, exist_ok=True)
    (avatar_dir / "script_20s.txt").write_text(script, encoding="utf-8")
    return JSONResponse({"ok": True, "words": len(script.split())})


# ---------------------------------------------------------------------------
# Feature: HeyGen usage stats
# ---------------------------------------------------------------------------

@app.get("/api/heygen/stats")
async def heygen_stats(request: Request):
    uid = request.state.user["id"]
    count = get_heygen_count(uid)
    return JSONResponse({"total_generations": count})


# ---------------------------------------------------------------------------
# Feature: Publish history
# ---------------------------------------------------------------------------

@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    uid = request.state.user["id"]
    history = get_publish_history(uid, limit=100)
    return _render("history.html", current_user=request.state.user, user=request.state.user, history=history)


@app.get("/api/publish-history")
async def api_publish_history(request: Request, limit: int = 50):
    uid = request.state.user["id"]
    limit = max(1, min(int(limit), 200))
    return JSONResponse({"history": get_publish_history(uid, limit=limit)})


# ---------------------------------------------------------------------------
# Feature: Schedule posts
# ---------------------------------------------------------------------------

@app.post("/api/slot/{name}/schedule")
async def schedule_post(request: Request, name: str,
                        platforms: str = Form(...),
                        scheduled_at: str = Form(...)):
    """Schedule a post for a future datetime (ISO format: 2026-05-11T18:00)."""
    uid = request.state.user["id"]
    slot_dir = _uqueue(request) / name
    if not slot_dir.exists():
        return JSONResponse({"error": "Slot not found"}, status_code=404)
    plat_list = [p.strip() for p in platforms.split(",") if p.strip()]
    if not plat_list:
        return JSONResponse({"error": "No platforms selected"}, status_code=400)
    try:
        from datetime import datetime as _dt
        _dt.fromisoformat(scheduled_at.replace("Z", ""))
    except ValueError:
        return JSONResponse({"error": "Invalid datetime format. Use YYYY-MM-DDTHH:MM"}, status_code=400)
    post_id = create_scheduled_post(uid, name, plat_list, scheduled_at)
    return JSONResponse({"ok": True, "id": post_id, "scheduled_at": scheduled_at})


@app.get("/api/schedule")
async def list_scheduled(request: Request):
    uid = request.state.user["id"]
    posts = get_scheduled_posts(uid, status="pending")
    return JSONResponse({"scheduled": posts})


@app.delete("/api/schedule/{post_id}")
async def delete_scheduled(request: Request, post_id: int):
    uid = request.state.user["id"]
    delete_scheduled_post(post_id, uid)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Feature: Webhook inbound trigger
# ---------------------------------------------------------------------------

@app.post("/api/webhook/trigger")
async def webhook_trigger(request: Request):
    """Inbound webhook — trigger pipeline for a slot via Make.com/n8n/Zapier.
    Expects JSON: {"secret": "...", "slot": "2026-05-11_morning", "action": "publish|pipeline"}
    """
    import hmac as _hmac
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    secret = (body.get("secret") or "").strip()
    slot   = (body.get("slot") or "").strip()
    action = (body.get("action") or "publish").strip()

    # Find the user by webhook secret
    from src.dashboard.database import _conn as _db_conn
    with _db_conn() as _c:
        row = _c.execute(
            "SELECT user_id FROM user_settings WHERE webhook_secret=? AND webhook_secret!=''",
            (secret,),
        ).fetchone()
    if not row:
        return JSONResponse({"error": "Invalid secret"}, status_code=401)

    uid = row["user_id"]
    slot_dir = BASE_QUEUE / str(uid) / slot if slot else None

    if action == "publish" and slot_dir and slot_dir.exists():
        platforms_str = (body.get("platforms") or "telegram,x")
        try:
            settings = get_user_settings(uid)
            from src.agents.publisher import publish
            def _do():
                with _uenv(settings):
                    return publish(queue_dir=slot_dir,
                                   platforms={p.strip() for p in platforms_str.split(",")},
                                   dry_run=False)
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(_publish_executor, _do)
            return JSONResponse({"ok": True, "result": result})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"ok": True, "message": f"Webhook received. action={action} slot={slot}"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()
    print(f"\n  AutoPost Dashboard -> http://{args.host}:{args.port}\n")
    uvicorn.run("src.dashboard.app:app", host=args.host, port=args.port,
                reload=args.reload, app_dir=str(_REPO))
