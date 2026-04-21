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

import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.dashboard.database import (
    init_db, get_user_by_email, get_user_by_id, create_user,
    get_all_users, get_pending_users, approve_user, reject_user,
    get_user_settings, save_user_settings,
    create_session, get_session_user_id, delete_session, cleanup_sessions,
)

init_db()

app = FastAPI(title="AutoPost Dashboard")
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

_publish_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="publisher")

_jinja = Environment(
    loader=FileSystemLoader(str(_HERE / "templates")),
    autoescape=select_autoescape(["html"]),
    auto_reload=True,
)


def _render(template_name: str, **ctx) -> HTMLResponse:
    return HTMLResponse(_jinja.get_template(template_name).render(**ctx))


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
# Auth middleware — inject request.state.user on every protected request
# ---------------------------------------------------------------------------

_PUBLIC = {"/login", "/register", "/static"}


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
    request.state.user = user
    return await call_next(request)


# ---------------------------------------------------------------------------
# Per-user queue root + credential injection
# ---------------------------------------------------------------------------

def _uqueue(request: Request) -> Path:
    q = BASE_QUEUE / str(request.state.user["id"])
    q.mkdir(parents=True, exist_ok=True)
    return q


@contextmanager
def _uenv(settings: dict):
    """Temporarily set user's API keys in os.environ (single publisher thread)."""
    mapping = {
        "TELEGRAM_BOT_TOKEN":      settings.get("telegram_token", ""),
        "TELEGRAM_CHAT_ID":        settings.get("telegram_channel", ""),
        "X_API_KEY":               settings.get("x_api_key", ""),
        "X_API_SECRET":            settings.get("x_api_secret", ""),
        "X_ACCESS_TOKEN":          settings.get("x_access_token", ""),
        "X_ACCESS_TOKEN_SECRET":   settings.get("x_access_secret", ""),
        "DID_API_KEY":             settings.get("did_api_key", ""),
        "DID_API_EMAIL":           settings.get("did_email", ""),
        "DID_PRESENTER_URL":       settings.get("did_presenter_url", ""),
        "ELEVENLABS_API_KEY":      settings.get("elevenlabs_key", ""),
        "ANTHROPIC_API_KEY":       settings.get("anthropic_key", ""),
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
    for d in sorted(queue_root.iterdir(), reverse=True):
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
    user = get_user_by_email(email)
    if not user or not _verify_pw(password, user["password_hash"]):
        return _render("login.html", error="Invalid email or password.")
    if not user["is_approved"]:
        return _render("login.html", error="Your account is pending admin approval.")
    token = _new_session(user["id"])
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(COOKIE, token, max_age=COOKIE_AGE, httponly=True, samesite="lax")
    return resp


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return _render("register.html")


@app.post("/register")
async def register_submit(request: Request,
                          email: str = Form(...),
                          password: str = Form(...),
                          password2: str = Form(...),
                          display_name: str = Form("")):
    if password != password2:
        return _render("register.html", error="Passwords do not match.")
    if len(password) < 8:
        return _render("register.html", error="Password must be at least 8 characters.")
    if get_user_by_email(email):
        return _render("register.html", error="Email already registered.")
    create_user(email, _hash_pw(password), display_name)
    return _render("register.html", pending=True)


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
async def settings_page(request: Request, saved: str = ""):
    user = request.state.user
    s = get_user_settings(user["id"])
    return _render("settings.html", user=user, s=s, saved=saved)


@app.post("/settings")
async def settings_save(request: Request,
                        telegram_token: str = Form(""),
                        telegram_channel: str = Form(""),
                        x_api_key: str = Form(""),
                        x_api_secret: str = Form(""),
                        x_access_token: str = Form(""),
                        x_access_secret: str = Form(""),
                        did_api_key: str = Form(""),
                        did_email: str = Form(""),
                        did_presenter_url: str = Form(""),
                        elevenlabs_key: str = Form(""),
                        anthropic_key: str = Form("")):
    save_user_settings(
        request.state.user["id"],
        telegram_token=telegram_token,
        telegram_channel=telegram_channel,
        x_api_key=x_api_key,
        x_api_secret=x_api_secret,
        x_access_token=x_access_token,
        x_access_secret=x_access_secret,
        did_api_key=did_api_key,
        did_email=did_email,
        did_presenter_url=did_presenter_url,
        elevenlabs_key=elevenlabs_key,
        anthropic_key=anthropic_key,
    )
    return RedirectResponse("/settings?saved=1", status_code=303)


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
    return _render("index.html",
                   slots=slots, total=len(slots),
                   published_today=published_today,
                   log_tail=log_tail,
                   triggered=triggered,
                   bot=bot,
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
# Publish
# ---------------------------------------------------------------------------

@app.post("/api/slot/{name}/publish-live")
async def publish_live_api(request: Request, name: str,
                           platforms: str = Form("telegram,x"),
                           variant: str = Form(""),
                           dry_run: str = Form("false")):
    d = _uqueue(request) / name
    if not d.exists():
        return JSONResponse({"error": f"Slot '{name}' not found"}, status_code=404)

    plat_set = {p.strip() for p in platforms.split(",") if p.strip()}
    if not plat_set:
        return JSONResponse({"error": "No platforms selected"}, status_code=400)

    is_dry = dry_run.lower() in ("true", "1", "yes")
    variant_arg = variant.strip().upper() if variant.strip() in ("A", "B") else None
    settings = get_user_settings(request.state.user["id"])

    try:
        from src.agents.publisher import publish
        loop = asyncio.get_event_loop()

        def _run():
            with _uenv(settings):
                return publish(queue_dir=d, platforms=plat_set,
                               dry_run=is_dry, variant=variant_arg)

        result = await loop.run_in_executor(_publish_executor, _run)
        return JSONResponse(result)
    except Exception as exc:
        import traceback
        return JSONResponse({"error": str(exc), "traceback": traceback.format_exc()},
                            status_code=500)


# ---------------------------------------------------------------------------
# Bot control (admin only)
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Cross-platform process existence check."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


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
        proc = subprocess.Popen(
            [sys.executable, str(_REPO / "src" / "agents" / "scheduler.py")],
            cwd=str(_REPO),
        )
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
        raw = collect_news(hours_back=hours_back)
        top = rank(raw, top_n=15)
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

        try:
            from src.agents.image_gen import render_all
            render_all(full_story, out_dir, seed=42, sizes=["x", "tg", "reel"])
        except Exception:
            pass

        return JSONResponse({"slot": slot_name, "url": f"/slot/{slot_name}"})
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
                                   presenter: str = Form("alex_suit")):
    d = _uqueue(request) / name
    if not d.exists():
        return JSONResponse({"error": f"Slot '{name}' not found"}, status_code=404)

    settings = get_user_settings(request.state.user["id"])
    did_key = settings.get("did_api_key") or os.environ.get("DID_API_KEY", "")
    if not did_key:
        return JSONResponse({"error": "D-ID API key not set — go to Settings"}, status_code=400)

    err = await asyncio.get_event_loop().run_in_executor(
        _publish_executor, lambda: _ensure_reel_assets(d, settings)
    )
    if err:
        return JSONResponse({"error": err}, status_code=400)

    try:
        from src.agents.avatar_video import build_avatar_reel
        out_path = await asyncio.get_event_loop().run_in_executor(
            _publish_executor,
            lambda: _with_did(settings, lambda: build_avatar_reel(d, presenter=presenter)),
        )
        size_mb = round(out_path.stat().st_size / 1_048_576, 1)
        return JSONResponse({"ok": True, "file": out_path.name, "size_mb": size_mb})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


def _with_did(settings: dict, fn):
    """Run fn() with user's D-ID credentials injected."""
    mapping = {
        "DID_API_KEY":       settings.get("did_api_key", ""),
        "DID_API_EMAIL":     settings.get("did_email", ""),
        "DID_PRESENTER_URL": settings.get("did_presenter_url", ""),
    }
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

@app.get("/video/{slot_name}/{filename}")
async def serve_video(request: Request, slot_name: str, filename: str):
    f = _uqueue(request) / slot_name / filename
    if not f.exists():
        raise HTTPException(404, "Video not found")
    return FileResponse(str(f), media_type="video/mp4")


@app.get("/image/{slot_name}/{filename}")
async def serve_image(request: Request, slot_name: str, filename: str):
    img = _uqueue(request) / slot_name / filename
    if not img.exists():
        raise HTTPException(404, "Image not found")
    return FileResponse(str(img))


@app.get("/avatar-file/{slot_name}/{filename}")
async def serve_avatar_file(request: Request, slot_name: str, filename: str):
    f = _uqueue(request) / slot_name / "avatar" / filename
    if not f.exists():
        raise HTTPException(404, "Avatar file not found")
    return FileResponse(str(f))


@app.get("/api/logs")
async def api_logs(request: Request, lines: int = 60):
    if not LOG_FILE.exists():
        return JSONResponse({"lines": ["(no log file yet)"]})
    all_lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    return JSONResponse({"lines": all_lines[-lines:]})


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
