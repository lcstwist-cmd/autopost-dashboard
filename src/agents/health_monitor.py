"""health_monitor.py — Monitors platform connection health for all admin users.

Checks every platform's credentials and reports:
  - ok       : connection working fine
  - warning  : token expires soon (< 7 days) or soft error
  - error    : connection broken / credentials missing / API down

Runs:
  - At scheduler startup (immediate check)
  - Every 30 minutes via scheduler loop
  - On-demand via dashboard API /api/health

Alerts via Telegram when:
  - Any platform transitions to error state
  - Token expires in < 7 days (once per day max)
  - Platform recovers from error

Results cached in: data/health_status.json
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

_HERE       = Path(__file__).resolve().parent
_REPO       = _HERE.parent.parent
_CACHE_FILE = _REPO / "data" / "health_status.json"
_ALERT_FILE = _REPO / "data" / "health_alert_sent.json"   # tracks last alert per platform

CHECK_INTERVAL_SECONDS = 1800   # 30 minutes
WARN_DAYS_BEFORE_EXPIRY = 7


# ---------------------------------------------------------------------------
# Individual platform checks
# ---------------------------------------------------------------------------

def _check_telegram(token: str, chat_id: str) -> dict[str, Any]:
    if not token or not chat_id:
        return {"status": "error", "message": "Credențiale lipsă (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)"}
    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        with urllib.request.urlopen(urllib.request.Request(url), timeout=8) as r:
            data = json.loads(r.read())
        if data.get("ok"):
            bot_name = data["result"].get("username", "?")
            return {"status": "ok", "message": f"Bot @{bot_name} activ"}
        return {"status": "error", "message": str(data.get("description", "API error"))}
    except Exception as exc:
        return {"status": "error", "message": f"Telegram API error: {exc}"}


def _check_x_cookies(cookies_json: str) -> dict[str, Any]:
    if not cookies_json:
        return {"status": "error", "message": "X cookies nepaste — Settings > X > Paste Cookies"}
    try:
        cookies = json.loads(cookies_json)
        # Find auth_token cookie expiry
        auth = next((c for c in cookies if c.get("name") == "auth_token"), None)
        if not auth:
            return {"status": "error", "message": "auth_token lipsă din cookies — re-paste cookies"}
        expiry = auth.get("expirationDate") or auth.get("expires")
        if expiry:
            exp_dt = datetime.fromtimestamp(float(expiry), tz=timezone.utc)
            days_left = (exp_dt - datetime.now(timezone.utc)).days
            if days_left < 0:
                return {"status": "error",   "message": f"Cookies expirate acum {abs(days_left)} zile — re-paste"}
            if days_left < WARN_DAYS_BEFORE_EXPIRY:
                return {"status": "warning", "message": f"Cookies expiră în {days_left} zile",
                        "expires_in_days": days_left}
            return {"status": "ok", "message": f"Cookies valide, expiră în {days_left} zile",
                    "expires_in_days": days_left}
        return {"status": "ok", "message": "Cookies prezente (fără dată expirare)"}
    except Exception as exc:
        return {"status": "error", "message": f"Cookies invalide (JSON parse error): {exc}"}


def _check_x_oauth(access_token: str, expires_at: str) -> dict[str, Any]:
    if not access_token:
        return {"status": "error", "message": "OAuth 2.0 neconectat"}
    try:
        exp_ts = float(expires_at) if expires_at else 0
        if exp_ts:
            days_left = (datetime.fromtimestamp(exp_ts, tz=timezone.utc) - datetime.now(timezone.utc)).days
            if days_left < 0:
                return {"status": "error",   "message": f"OAuth token expirat — reconectează din Settings"}
            if days_left < WARN_DAYS_BEFORE_EXPIRY:
                return {"status": "warning", "message": f"OAuth token expiră în {days_left} zile",
                        "expires_in_days": days_left}
        return {"status": "ok", "message": "OAuth 2.0 token valid"}
    except Exception as exc:
        return {"status": "error", "message": f"OAuth check error: {exc}"}


def _check_instagram(user_id: str, access_token: str) -> dict[str, Any]:
    if not user_id or not access_token:
        return {"status": "error", "message": "Credențiale lipsă (IG_USER_ID / IG_ACCESS_TOKEN)"}
    try:
        url = (f"https://graph.facebook.com/v19.0/{user_id}"
               f"?fields=name,instagram_business_account&access_token={access_token}")
        with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as r:
            data = json.loads(r.read())
        if "error" in data:
            err = data["error"]
            code = err.get("code", 0)
            if code in (190, 102):
                return {"status": "error", "message": "Token Instagram expirat — regenerează din Meta Business"}
            return {"status": "error", "message": err.get("message", "API error")}
        # Check token expiry via debug_token
        debug_url = (f"https://graph.facebook.com/debug_token"
                     f"?input_token={access_token}&access_token={access_token}")
        with urllib.request.urlopen(urllib.request.Request(debug_url), timeout=10) as r:
            dbg = json.loads(r.read()).get("data", {})
        exp_ts = dbg.get("expires_at", 0)
        if exp_ts and exp_ts != 0:
            days_left = (datetime.fromtimestamp(exp_ts, tz=timezone.utc) - datetime.now(timezone.utc)).days
            if days_left < WARN_DAYS_BEFORE_EXPIRY:
                return {"status": "warning", "message": f"Token Instagram expiră în {days_left} zile",
                        "expires_in_days": days_left}
        name = data.get("name", "?")
        return {"status": "ok", "message": f"Cont @{name} conectat"}
    except Exception as exc:
        return {"status": "error", "message": f"Instagram API error: {exc}"}


def _check_tiktok(access_token: str, refresh_token: str) -> dict[str, Any]:
    if not access_token and not refresh_token:
        return {"status": "error", "message": "TikTok neconectat — Settings > TikTok"}
    if not access_token:
        return {"status": "warning", "message": "Access token lipsă — refresh necesar"}
    try:
        url = "https://open.tiktokapis.com/v2/user/info/?fields=display_name,follower_count"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        err_code = data.get("error", {}).get("code", "ok")
        if err_code in ("access_token_invalid", "access_token_expired"):
            if refresh_token:
                return {"status": "warning", "message": "Access token expirat — se va reîmprospăta automat"}
            return {"status": "error", "message": "Token TikTok expirat și nu există refresh token"}
        if err_code != "ok":
            return {"status": "warning", "message": f"TikTok API: {err_code}"}
        name = data.get("data", {}).get("user", {}).get("display_name", "?")
        return {"status": "ok", "message": f"Cont @{name} conectat"}
    except Exception as exc:
        return {"status": "error", "message": f"TikTok API error: {exc}"}


def _check_youtube(client_id: str, client_secret: str, refresh_token: str) -> dict[str, Any]:
    if not all([client_id, client_secret, refresh_token]):
        return {"status": "error", "message": "YouTube neconectat — Settings > YouTube"}
    try:
        data = urllib.parse.urlencode({
            "client_id":     client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type":    "refresh_token",
        }).encode()
        req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
        if "error" in resp:
            return {"status": "error", "message": f"YouTube OAuth error: {resp['error']} — {resp.get('error_description','')}"}
        return {"status": "ok", "message": "YouTube OAuth funcțional, token reîmprospătat"}
    except Exception as exc:
        return {"status": "error", "message": f"YouTube API error: {exc}"}


# ---------------------------------------------------------------------------
# Main check function
# ---------------------------------------------------------------------------

def run_checks(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run all platform checks. Returns full health report.

    If `settings` is None, loads from the first admin user in the database.
    """
    if settings is None:
        settings = _load_admin_settings()

    results: dict[str, Any] = {}
    ts = datetime.now(timezone.utc).isoformat()

    results["telegram"] = _check_telegram(
        settings.get("telegram_token", "") or os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        settings.get("telegram_channel", "") or os.environ.get("TELEGRAM_CHAT_ID", ""),
    )

    # X — cookies (primary) or OAuth
    cookies_json = settings.get("x_cookies_json", "") or os.environ.get("X_COOKIES_JSON", "")
    oauth_token  = settings.get("x_oauth_access_token", "") or os.environ.get("X_OAUTH_ACCESS_TOKEN", "")
    oauth_exp    = str(settings.get("x_oauth_expires_at", "") or os.environ.get("X_OAUTH_EXPIRES_AT", "") or "")

    if cookies_json:
        results["x"] = _check_x_cookies(cookies_json)
        results["x"]["method"] = "cookies"
    elif oauth_token:
        results["x"] = _check_x_oauth(oauth_token, oauth_exp)
        results["x"]["method"] = "oauth2"
    else:
        results["x"] = {"status": "error", "message": "X neconectat — Settings > X"}

    results["instagram"] = _check_instagram(
        settings.get("ig_user_id", "") or os.environ.get("IG_USER_ID", ""),
        settings.get("ig_access_token", "") or os.environ.get("IG_ACCESS_TOKEN", ""),
    )

    results["tiktok"] = _check_tiktok(
        settings.get("tiktok_access_token", "") or os.environ.get("TIKTOK_ACCESS_TOKEN", ""),
        settings.get("tiktok_refresh_token", "") or os.environ.get("TIKTOK_REFRESH_TOKEN", ""),
    )

    results["youtube"] = _check_youtube(
        settings.get("youtube_client_id", "") or os.environ.get("YOUTUBE_CLIENT_ID", ""),
        settings.get("youtube_client_secret", "") or os.environ.get("YOUTUBE_CLIENT_SECRET", ""),
        settings.get("youtube_refresh_token", "") or os.environ.get("YOUTUBE_REFRESH_TOKEN", ""),
    )

    overall = "ok"
    for r in results.values():
        if r["status"] == "error":
            overall = "error"
            break
        if r["status"] == "warning" and overall == "ok":
            overall = "warning"

    report = {
        "checked_at": ts,
        "checked_at_ts": time.time(),
        "overall": overall,
        "platforms": results,
    }

    _save_cache(report)
    _send_alerts(report)
    return report


# ---------------------------------------------------------------------------
# Alert logic — throttled (max 1 alert per platform per day)
# ---------------------------------------------------------------------------

def _send_alerts(report: dict[str, Any]) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = (os.environ.get("TELEGRAM_ALERT_CHAT", "").strip()
               or os.environ.get("TELEGRAM_CHAT_ID", "").strip())
    if not token or not chat_id:
        return

    prev_alerts = _load_alert_state()
    new_alerts  = dict(prev_alerts)
    now         = time.time()
    messages    = []

    for plat, res in report["platforms"].items():
        status = res["status"]
        key    = f"{plat}_{status}"
        last   = prev_alerts.get(f"{plat}_last_alerted", 0)

        # Alert on error — max once every 6 hours
        if status == "error" and (now - last) > 21600:
            messages.append(f"❌ {plat.upper()}: {res['message']}")
            new_alerts[f"{plat}_last_alerted"] = now
            new_alerts[f"{plat}_last_status"]  = status

        # Alert on warning — max once per day
        elif status == "warning" and (now - last) > 86400:
            messages.append(f"⚠️ {plat.upper()}: {res['message']}")
            new_alerts[f"{plat}_last_alerted"] = now
            new_alerts[f"{plat}_last_status"]  = status

        # Recovery alert — was error/warning, now ok
        elif status == "ok" and prev_alerts.get(f"{plat}_last_status") in ("error", "warning"):
            messages.append(f"✅ {plat.upper()}: conexiune restaurată")
            new_alerts[f"{plat}_last_status"] = "ok"

    if messages:
        text = "[AutoPost Health]\n" + "\n".join(messages)
        try:
            url  = f"https://api.telegram.org/bot{token}/sendMessage"
            data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=8)
        except Exception:
            pass

    _save_alert_state(new_alerts)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def load_cache() -> dict[str, Any] | None:
    if not _CACHE_FILE.exists():
        return None
    try:
        return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_cache(report: dict[str, Any]) -> None:
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_alert_state() -> dict[str, Any]:
    if not _ALERT_FILE.exists():
        return {}
    try:
        return json.loads(_ALERT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_alert_state(state: dict[str, Any]) -> None:
    _ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ALERT_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _load_admin_settings() -> dict[str, Any]:
    try:
        import sys
        if str(_REPO) not in sys.path:
            sys.path.insert(0, str(_REPO))
        from src.dashboard.database import get_all_users, get_user_settings
        admins = [u for u in get_all_users() if u.get("is_admin") and u.get("is_approved")]
        if admins:
            return get_user_settings(admins[0]["id"])
    except Exception:
        pass
    return {}


def is_stale(max_age_seconds: int = CHECK_INTERVAL_SECONDS) -> bool:
    cache = load_cache()
    if not cache:
        return True
    return (time.time() - cache.get("checked_at_ts", 0)) > max_age_seconds


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.path.insert(0, str(_REPO))
    report = run_checks()
    print(f"Overall: {report['overall'].upper()}  |  Checked at: {report['checked_at']}\n")
    for plat, res in report["platforms"].items():
        icon = {"ok": "OK", "warning": "WARN", "error": "ERR "}[res["status"]]
        print(f"  [{icon}] {plat:<12} {res['message']}")
