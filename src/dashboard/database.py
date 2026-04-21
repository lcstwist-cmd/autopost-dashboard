"""SQLite database for multi-tenant AutoPost."""
from __future__ import annotations
import os
import sqlite3
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# Persistent storage: use DATA_DIR env var, or /data if it exists (Railway volume),
# or fall back to repo root for local dev.
def _db_path() -> Path:
    env = os.environ.get("DATA_DIR", "").strip()
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p / "autopost.db"
    railway_volume = Path("/data")
    if railway_volume.exists():
        return railway_volume / "autopost.db"
    return _HERE.parent.parent / "autopost.db"

DB_PATH = _db_path()

SESSION_TTL = 60 * 60 * 24 * 30  # 30 days in seconds


def _conn():
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            email         TEXT    UNIQUE NOT NULL,
            password_hash TEXT    NOT NULL,
            display_name  TEXT    DEFAULT '',
            is_approved   INTEGER DEFAULT 0,
            is_admin      INTEGER DEFAULT 0,
            created_at    TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id           INTEGER PRIMARY KEY REFERENCES users(id),
            telegram_token    TEXT DEFAULT '',
            telegram_channel  TEXT DEFAULT '',
            x_api_key         TEXT DEFAULT '',
            x_api_secret      TEXT DEFAULT '',
            x_access_token    TEXT DEFAULT '',
            x_access_secret   TEXT DEFAULT '',
            did_api_key       TEXT DEFAULT '',
            did_email         TEXT DEFAULT '',
            elevenlabs_key    TEXT DEFAULT '',
            anthropic_key     TEXT DEFAULT '',
            updated_at        TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT    PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
        """)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def create_session(token: str, user_id: int):
    expires_at = time.time() + SESSION_TTL
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
            (token, user_id, expires_at),
        )


def get_session_user_id(token: str) -> int | None:
    with _conn() as c:
        row = c.execute(
            "SELECT user_id FROM sessions WHERE token=? AND expires_at>?",
            (token, time.time()),
        ).fetchone()
        return row["user_id"] if row else None


def delete_session(token: str):
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))


def cleanup_sessions():
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE expires_at<?", (time.time(),))


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

def get_user_by_email(email: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (email.lower().strip(),)).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def count_users() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(email: str, password_hash: str, display_name: str = "") -> int:
    is_first = count_users() == 0
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO users (email, password_hash, display_name, is_approved, is_admin)"
            " VALUES (?,?,?,?,?)",
            (email.lower().strip(), password_hash, display_name,
             1 if is_first else 0, 1 if is_first else 0),
        )
        user_id = cur.lastrowid
        c.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))
        return user_id


def get_all_users() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_pending_users() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM users WHERE is_approved=0 ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def approve_user(user_id: int):
    with _conn() as c:
        c.execute("UPDATE users SET is_approved=1 WHERE id=?", (user_id,))


def reject_user(user_id: int):
    with _conn() as c:
        c.execute("DELETE FROM user_settings WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM users WHERE id=?", (user_id,))


def get_user_settings(user_id: int) -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM user_settings WHERE user_id=?", (user_id,)
        ).fetchone()
        if row:
            return dict(row)
        c.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))
        return {"user_id": user_id}


def save_user_settings(user_id: int, **kwargs):
    fields = [
        "telegram_token", "telegram_channel",
        "x_api_key", "x_api_secret", "x_access_token", "x_access_secret",
        "did_api_key", "did_email", "elevenlabs_key", "anthropic_key",
    ]
    data = {k: kwargs.get(k, "") for k in fields}
    with _conn() as c:
        c.execute("""
            INSERT INTO user_settings
                (user_id, telegram_token, telegram_channel,
                 x_api_key, x_api_secret, x_access_token, x_access_secret,
                 did_api_key, did_email, elevenlabs_key, anthropic_key, updated_at)
            VALUES
                (:user_id, :telegram_token, :telegram_channel,
                 :x_api_key, :x_api_secret, :x_access_token, :x_access_secret,
                 :did_api_key, :did_email, :elevenlabs_key, :anthropic_key, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                telegram_token=excluded.telegram_token,
                telegram_channel=excluded.telegram_channel,
                x_api_key=excluded.x_api_key,
                x_api_secret=excluded.x_api_secret,
                x_access_token=excluded.x_access_token,
                x_access_secret=excluded.x_access_secret,
                did_api_key=excluded.did_api_key,
                did_email=excluded.did_email,
                elevenlabs_key=excluded.elevenlabs_key,
                anthropic_key=excluded.anthropic_key,
                updated_at=excluded.updated_at
        """, {"user_id": user_id, **data})
