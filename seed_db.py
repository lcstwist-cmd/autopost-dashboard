"""
seed_db.py — Create the local database and pre-fill the admin user.

Run once before starting the server:
    python seed_db.py

If the database already exists it will only update the settings
(will NOT delete existing data or overwrite the password if user exists).
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Make sure src/ is importable
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load .env so we can read the credentials
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def _hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def main() -> None:
    from src.dashboard.database import (
        init_db, get_user_by_email, create_user,
        save_user_settings, get_user_settings,
    )
    import sqlite3, time

    print("  Initializare baza de date...")
    init_db()
    print("  ✅ Schema OK")

    # ── Admin account ──────────────────────────────────────────────────────
    ADMIN_EMAIL    = "lcstwist@gmail.com"
    ADMIN_PASSWORD = "Admin1234!"          # change after first login if you want
    ADMIN_NAME     = "Admin"

    user = get_user_by_email(ADMIN_EMAIL)
    if user is None:
        user_id = create_user(ADMIN_EMAIL, _hash(ADMIN_PASSWORD), ADMIN_NAME)
        print(f"  ✅ User creat: {ADMIN_EMAIL}  (parola: {ADMIN_PASSWORD})")
    else:
        user_id = user["id"]
        print(f"  ℹ  User exista deja: {ADMIN_EMAIL}  (parola neschimbata)")

    # ── Pre-fill settings from .env ────────────────────────────────────────
    existing = get_user_settings(user_id)

    def _env(key: str, fallback: str = "") -> str:
        return os.environ.get(key, "").strip() or fallback

    # Build settings dict — keep existing value if .env has nothing
    def _pick(env_key: str, db_key: str, fallback: str = "") -> str:
        from_env = _env(env_key)
        from_db  = existing.get(db_key, "") or ""
        return from_env or from_db or fallback

    settings = {
        "telegram_token":     _pick("TELEGRAM_BOT_TOKEN", "telegram_token"),
        "telegram_channel":   _pick("TELEGRAM_CHAT_ID",   "telegram_channel"),
        "x_username":         _pick("X_USERNAME",          "x_username"),
        "x_email":            _pick("X_EMAIL",             "x_email"),
        "x_password":         _pick("X_PASSWORD",          "x_password"),
        "x_api_key":          _pick("X_API_KEY",           "x_api_key"),
        "x_api_secret":       _pick("X_API_SECRET",        "x_api_secret"),
        "x_access_token":     _pick("X_ACCESS_TOKEN",      "x_access_token"),
        "x_access_secret":    _pick("X_ACCESS_TOKEN_SECRET", "x_access_secret"),
        "make_x_webhook_url": _pick("MAKE_X_WEBHOOK_URL",  "make_x_webhook_url"),
        "did_api_key":        _pick("DID_API_KEY",         "did_api_key"),
        "did_presenter_url":  _pick("DID_PRESENTER_URL",   "did_presenter_url"),
        "elevenlabs_key":     _pick("ELEVENLABS_API_KEY",  "elevenlabs_key"),
        "anthropic_key":      _pick("ANTHROPIC_API_KEY",   "anthropic_key"),
    }

    save_user_settings(user_id, **settings)
    print("  ✅ Setarile au fost salvate din .env")

    # ── Summary ────────────────────────────────────────────────────────────
    print()
    print("  ╔══════════════════════════════════════════════╗")
    print("  ║          Baza de date e gata!                ║")
    print("  ╚══════════════════════════════════════════════╝")
    print()
    print(f"  Email:   {ADMIN_EMAIL}")
    print(f"  Parola:  {ADMIN_PASSWORD}")
    print()
    print("  Porneste serverul cu:  python run_local.py")
    print()


if __name__ == "__main__":
    main()
