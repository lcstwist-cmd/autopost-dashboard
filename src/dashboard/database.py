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
    c = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")  # faster writes, still crash-safe with WAL
    c.execute("PRAGMA cache_size=-8000")    # 8 MB page cache
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
            created_at    TEXT    DEFAULT (datetime('now')),
            last_login_at TEXT    DEFAULT '',
            last_login_ip TEXT    DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id               INTEGER PRIMARY KEY REFERENCES users(id),
            telegram_token        TEXT DEFAULT '',
            telegram_channel      TEXT DEFAULT '',
            x_api_key             TEXT DEFAULT '',
            x_api_secret          TEXT DEFAULT '',
            x_access_token        TEXT DEFAULT '',
            x_access_secret       TEXT DEFAULT '',
            make_x_webhook_url    TEXT DEFAULT '',
            x_username            TEXT DEFAULT '',
            x_email               TEXT DEFAULT '',
            x_password            TEXT DEFAULT '',
            did_api_key           TEXT DEFAULT '',
            did_email             TEXT DEFAULT '',
            did_presenter_url     TEXT DEFAULT '',
            elevenlabs_key        TEXT DEFAULT '',
            anthropic_key         TEXT DEFAULT '',
            -- Instagram Graph API
            ig_user_id            TEXT DEFAULT '',
            ig_access_token       TEXT DEFAULT '',
            meta_app_id           TEXT DEFAULT '',
            meta_app_secret       TEXT DEFAULT '',
            -- TikTok Content Posting API
            tiktok_client_key     TEXT DEFAULT '',
            tiktok_client_secret  TEXT DEFAULT '',
            tiktok_access_token   TEXT DEFAULT '',
            tiktok_refresh_token  TEXT DEFAULT '',
            tiktok_privacy        TEXT DEFAULT 'SELF_ONLY',
            -- YouTube Data API v3
            yt_client_id          TEXT DEFAULT '',
            yt_client_secret      TEXT DEFAULT '',
            yt_refresh_token      TEXT DEFAULT '',
            -- Image hosting (for Instagram)
            imgbb_api_key         TEXT DEFAULT '',
            public_base_url       TEXT DEFAULT '',
            -- Subscription + billing (SaaS)
            plan                  TEXT DEFAULT 'trial',   -- trial|free|pro|business
            plan_expires_at       TEXT DEFAULT '',
            stripe_customer_id    TEXT DEFAULT '',
            monthly_clips_used    INTEGER DEFAULT 0,
            -- X OAuth 2.0 tokens (per-user, multi-tenant safe)
            x_oauth_access_token  TEXT DEFAULT '',
            x_oauth_refresh_token TEXT DEFAULT '',
            x_oauth_expires_at    INTEGER DEFAULT 0,
            x_oauth_scope         TEXT DEFAULT '',
            x_oauth_username      TEXT DEFAULT '',
            x_oauth_user_id       TEXT DEFAULT '',
            -- X cookies paste (zero-API, no developer account needed)
            x_cookies_json        TEXT DEFAULT '',
            x_cookies_updated_at  TEXT DEFAULT '',
            x_cookies_handle      TEXT DEFAULT '',
            updated_at            TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT    PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
        """)
        # Migrate existing databases — add columns added after initial deploy
        _migrate(c)


def _migrate(c):
    """Add new columns to existing databases without dropping data."""
    new_cols = [
        ("user_settings", "make_x_webhook_url",   "TEXT DEFAULT ''"),
        ("user_settings", "x_username",           "TEXT DEFAULT ''"),
        ("user_settings", "x_email",              "TEXT DEFAULT ''"),
        ("user_settings", "x_password",           "TEXT DEFAULT ''"),
        # Social media native APIs (added 2026-04)
        ("user_settings", "ig_user_id",           "TEXT DEFAULT ''"),
        ("user_settings", "ig_access_token",      "TEXT DEFAULT ''"),
        ("user_settings", "meta_app_id",          "TEXT DEFAULT ''"),
        ("user_settings", "meta_app_secret",      "TEXT DEFAULT ''"),
        ("user_settings", "tiktok_client_key",    "TEXT DEFAULT ''"),
        ("user_settings", "tiktok_client_secret", "TEXT DEFAULT ''"),
        ("user_settings", "tiktok_access_token",  "TEXT DEFAULT ''"),
        ("user_settings", "tiktok_refresh_token", "TEXT DEFAULT ''"),
        ("user_settings", "tiktok_privacy",       "TEXT DEFAULT 'SELF_ONLY'"),
        ("user_settings", "yt_client_id",         "TEXT DEFAULT ''"),
        ("user_settings", "yt_client_secret",     "TEXT DEFAULT ''"),
        ("user_settings", "yt_refresh_token",     "TEXT DEFAULT ''"),
        ("user_settings", "imgbb_api_key",        "TEXT DEFAULT ''"),
        ("user_settings", "public_base_url",      "TEXT DEFAULT ''"),
        # SaaS / billing columns (added 2026-04)
        ("user_settings", "plan",                 "TEXT DEFAULT 'trial'"),
        ("user_settings", "plan_expires_at",      "TEXT DEFAULT ''"),
        ("user_settings", "stripe_customer_id",   "TEXT DEFAULT ''"),
        ("user_settings", "monthly_clips_used",   "INTEGER DEFAULT 0"),
        # X OAuth 2.0 tokens (added 2026-04-24) — per-user SaaS-safe
        ("user_settings", "x_oauth_access_token",  "TEXT DEFAULT ''"),
        ("user_settings", "x_oauth_refresh_token", "TEXT DEFAULT ''"),
        ("user_settings", "x_oauth_expires_at",    "INTEGER DEFAULT 0"),
        ("user_settings", "x_oauth_scope",         "TEXT DEFAULT ''"),
        ("user_settings", "x_oauth_username",      "TEXT DEFAULT ''"),
        ("user_settings", "x_oauth_user_id",       "TEXT DEFAULT ''"),
        # X cookies paste (no X developer account needed)
        ("user_settings", "x_cookies_json",        "TEXT DEFAULT ''"),
        ("user_settings", "x_cookies_updated_at",  "TEXT DEFAULT ''"),
        ("user_settings", "x_cookies_handle",      "TEXT DEFAULT ''"),
        # Per-platform video profile (added 2026-04-25)
        ("user_settings", "video_duration_tiktok",         "INTEGER DEFAULT 25"),
        ("user_settings", "video_duration_instagram",      "INTEGER DEFAULT 25"),
        ("user_settings", "video_duration_youtube_shorts", "INTEGER DEFAULT 45"),
        ("user_settings", "video_style",           "TEXT DEFAULT 'full_ai'"),
        ("user_settings", "video_intro_enabled",   "INTEGER DEFAULT 1"),
        ("user_settings", "video_outro_enabled",   "INTEGER DEFAULT 1"),
        ("user_settings", "video_bgm_enabled",     "INTEGER DEFAULT 1"),
        ("user_settings", "video_bgm_volume",      "INTEGER DEFAULT 15"),
        ("user_settings", "video_brand_color",     "TEXT DEFAULT '#F59E0B'"),
        ("user_settings", "video_brand_handle",    "TEXT DEFAULT ''"),
        ("user_settings", "video_brand_cta",       "TEXT DEFAULT 'Follow for more'"),
        ("user_settings", "video_bgm_path",        "TEXT DEFAULT ''"),
        ("user_settings", "use_viral_blueprint",   "INTEGER DEFAULT 1"),
        ("user_settings", "youtube_api_key",       "TEXT DEFAULT ''"),
        # Separate alert channel — bot errors/health go here, NOT to the news channel
        ("user_settings", "telegram_alert_chat",   "TEXT DEFAULT ''"),
        # Replicate (new primary AI avatar provider — added 2026-04-25)
        ("user_settings", "replicate_api_token",     "TEXT DEFAULT ''"),
        ("user_settings", "replicate_presenter_url", "TEXT DEFAULT ''"),
        ("user_settings", "avatar_provider",         "TEXT DEFAULT 'auto'"),
        # Global posting schedule times (added 2026-04-27)
        ("user_settings", "post_time_morning", "TEXT DEFAULT '08:00'"),
        ("user_settings", "post_time_evening", "TEXT DEFAULT '18:00'"),
        # UI language preference (added 2026-04-28)
        ("user_settings", "language",          "TEXT DEFAULT 'en'"),
        # HeyGen API key (added 2026-04-28)
        ("user_settings", "heygen_api_key",    "TEXT DEFAULT ''"),
        # Content niche (added 2026-05-04)
        ("user_settings", "niche",             "TEXT DEFAULT 'crypto'"),
        # Geolocation metadata (added 2026-05-07, ipstack-powered)
        ("user_settings", "timezone_id",       "TEXT DEFAULT ''"),
        ("user_settings", "signup_country",    "TEXT DEFAULT ''"),
        ("user_settings", "signup_city",       "TEXT DEFAULT ''"),
        # HeyGen voice override (added 2026-05-07)
        ("user_settings", "heygen_voice_id",   "TEXT DEFAULT ''"),
        # Facebook Page (added 2026-05-11)
        ("user_settings", "fb_access_token",   "TEXT DEFAULT ''"),
        ("user_settings", "fb_page_id",        "TEXT DEFAULT ''"),
        # Email / SMTP notifications (added 2026-05-11)
        ("user_settings", "smtp_host",              "TEXT DEFAULT ''"),
        ("user_settings", "smtp_port",              "INTEGER DEFAULT 587"),
        ("user_settings", "smtp_user",              "TEXT DEFAULT ''"),
        ("user_settings", "smtp_pass",              "TEXT DEFAULT ''"),
        ("user_settings", "smtp_from",              "TEXT DEFAULT ''"),
        ("user_settings", "email_notify_heygen",    "INTEGER DEFAULT 1"),
        ("user_settings", "email_notify_fail",      "INTEGER DEFAULT 1"),
        ("user_settings", "email_notify_digest",    "INTEGER DEFAULT 0"),
        # Webhook inbound secret key (added 2026-05-11)
        ("user_settings", "webhook_secret",    "TEXT DEFAULT ''"),
        # Per-platform deferred post times (added 2026-05-08)
        # TikTok peaks at 21:00, YouTube at 15:00 — posted from already-generated content
        ("user_settings", "post_time_tiktok",   "TEXT DEFAULT '21:00'"),
        ("user_settings", "post_time_youtube",  "TEXT DEFAULT '15:00'"),
        # Login tracking columns (added 2026-05-08)
        ("users", "last_login_at", "TEXT DEFAULT ''"),
        ("users", "last_login_ip", "TEXT DEFAULT ''"),
    ]
    # Crypto payments — USDC on Solana (added 2026-05-11)
    c.execute("""
        CREATE TABLE IF NOT EXISTS crypto_payments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            tx_signature TEXT    NOT NULL UNIQUE,
            plan         TEXT    NOT NULL,
            billing      TEXT    NOT NULL DEFAULT 'monthly',
            amount_usdc  INTEGER NOT NULL DEFAULT 0,
            activated_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_crypto_tx ON crypto_payments(tx_signature)")
    # Bootstrap viral_patterns table (once)
    c.execute("""
        CREATE TABLE IF NOT EXISTS viral_patterns (
            user_id    INTEGER NOT NULL,
            topic      TEXT    NOT NULL,
            blueprint  TEXT    NOT NULL,
            created_at TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, topic)
        )
    """)
    # Competitor tracker tables (added 2026-05-04)
    c.execute("""
        CREATE TABLE IF NOT EXISTS competitor_accounts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            platform   TEXT    NOT NULL DEFAULT 'x',
            handle     TEXT    NOT NULL,
            label      TEXT    DEFAULT '',
            rss_url    TEXT    DEFAULT '',
            x_user_id  TEXT    DEFAULT '',
            is_active  INTEGER DEFAULT 1,
            last_scanned_at TEXT DEFAULT '',
            created_at TEXT    DEFAULT (datetime('now'))
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_competitors_user ON competitor_accounts(user_id)
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS competitor_posts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES competitor_accounts(id) ON DELETE CASCADE,
            post_id    TEXT    NOT NULL DEFAULT '',
            content    TEXT    DEFAULT '',
            url        TEXT    DEFAULT '',
            likes      INTEGER DEFAULT 0,
            shares     INTEGER DEFAULT 0,
            views      INTEGER DEFAULT 0,
            posted_at  TEXT    DEFAULT '',
            fetched_at TEXT    DEFAULT (datetime('now')),
            UNIQUE(account_id, post_id)
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_comp_posts_account ON competitor_posts(account_id)
    """)

    # ── Audience tracker shortlinks (ipstack-powered) ───────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS shortlinks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            code        TEXT    UNIQUE NOT NULL,
            target_url  TEXT    NOT NULL,
            label       TEXT    DEFAULT '',
            platform    TEXT    DEFAULT '',
            slot_name   TEXT    DEFAULT '',
            click_count INTEGER DEFAULT 0,
            created_at  TEXT    DEFAULT (datetime('now'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_shortlinks_user ON shortlinks(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_shortlinks_code ON shortlinks(code)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS shortlink_clicks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            link_id      INTEGER NOT NULL REFERENCES shortlinks(id) ON DELETE CASCADE,
            ip           TEXT    DEFAULT '',
            country_code TEXT    DEFAULT '',
            country_name TEXT    DEFAULT '',
            city         TEXT    DEFAULT '',
            user_agent   TEXT    DEFAULT '',
            referer      TEXT    DEFAULT '',
            clicked_at   TEXT    DEFAULT (datetime('now'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_clicks_link ON shortlink_clicks(link_id)")

    # ── Login security log ─────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS login_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            ip           TEXT    DEFAULT '',
            country_code TEXT    DEFAULT '',
            country_name TEXT    DEFAULT '',
            city         TEXT    DEFAULT '',
            user_agent   TEXT    DEFAULT '',
            success      INTEGER DEFAULT 1,
            logged_at    TEXT    DEFAULT (datetime('now'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_login_user ON login_log(user_id)")

    # ── HeyGen usage counter ────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS heygen_usage (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            slot_name  TEXT    NOT NULL DEFAULT '',
            platform   TEXT    NOT NULL DEFAULT '',
            words      INTEGER NOT NULL DEFAULT 0,
            status     TEXT    NOT NULL DEFAULT 'done',
            created_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_heygen_user ON heygen_usage(user_id)")

    # ── Publish history (populated from publish_log.json on each publish) ──
    c.execute("""
        CREATE TABLE IF NOT EXISTS publish_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            slot_name    TEXT    NOT NULL DEFAULT '',
            title        TEXT    NOT NULL DEFAULT '',
            platforms    TEXT    NOT NULL DEFAULT '[]',
            results      TEXT    NOT NULL DEFAULT '{}',
            published_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_pubhist_user ON publish_history(user_id)")

    # ── Scheduled posts ─────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_posts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            slot_name    TEXT    NOT NULL DEFAULT '',
            platforms    TEXT    NOT NULL DEFAULT '[]',
            scheduled_at TEXT    NOT NULL,
            status       TEXT    NOT NULL DEFAULT 'pending',
            created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            ran_at       TEXT    DEFAULT ''
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_sched_user ON scheduled_posts(user_id)")

    for table, col, col_def in new_cols:
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
        except Exception:
            pass  # column already exists


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


def delete_user(user_id: int):
    """Permanently delete a user and all their data."""
    with _conn() as c:
        c.execute("DELETE FROM user_settings WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM login_log WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM users WHERE id=?", (user_id,))


def update_user(user_id: int, display_name: str | None = None,
                email: str | None = None, is_admin: int | None = None,
                is_approved: int | None = None) -> bool:
    """Update editable user fields. Returns True if the row was found."""
    fields: dict[str, object] = {}
    if display_name is not None:
        fields["display_name"] = display_name.strip()[:120]
    if email is not None:
        fields["email"] = email.strip().lower()
    if is_admin is not None:
        fields["is_admin"] = int(bool(is_admin))
    if is_approved is not None:
        fields["is_approved"] = int(bool(is_approved))
    if not fields:
        return True
    set_clause = ", ".join(f"{k}=:{k}" for k in fields)
    with _conn() as c:
        cur = c.execute(
            f"UPDATE users SET {set_clause} WHERE id=:user_id",
            {"user_id": user_id, **fields},
        )
        return cur.rowcount > 0


def get_user_settings(user_id: int) -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM user_settings WHERE user_id=?", (user_id,)
        ).fetchone()
        if row:
            return dict(row)
        c.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))
        return {"user_id": user_id}


_DB_ADMIN_ONLY_FIELDS = frozenset({
    "telegram_alert_chat",
    "plan", "plan_expires_at", "stripe_customer_id", "monthly_clips_used",
})


def save_user_settings(user_id: int, _is_admin: bool = False, **kwargs):
    """Dynamic UPSERT — only updates columns the caller passed in kwargs.
    Other columns keep their existing values. Unknown columns are ignored.
    Admin-only fields are silently dropped for non-admin callers.
    """
    # Whitelist of known columns (protects against SQL injection via kwargs).
    allowed = {
        "telegram_token", "telegram_channel",
        "x_api_key", "x_api_secret", "x_access_token", "x_access_secret",
        "make_x_webhook_url", "x_username", "x_email", "x_password",
        "did_api_key", "did_email", "did_presenter_url",
        "elevenlabs_key", "anthropic_key",
        "ig_user_id", "ig_access_token", "meta_app_id", "meta_app_secret",
        "tiktok_client_key", "tiktok_client_secret",
        "tiktok_access_token", "tiktok_refresh_token", "tiktok_privacy",
        "yt_client_id", "yt_client_secret", "yt_refresh_token",
        "imgbb_api_key", "public_base_url",
        "plan", "plan_expires_at", "stripe_customer_id", "monthly_clips_used",
        # X OAuth 2.0 per-user tokens
        "x_oauth_access_token", "x_oauth_refresh_token", "x_oauth_expires_at",
        "x_oauth_scope", "x_oauth_username", "x_oauth_user_id",
        # X cookies paste
        "x_cookies_json", "x_cookies_updated_at", "x_cookies_handle",
        # Per-platform video profile
        "video_duration_tiktok", "video_duration_instagram",
        "video_duration_youtube_shorts",
        "video_style", "video_intro_enabled", "video_outro_enabled",
        "video_bgm_enabled", "video_bgm_volume",
        "video_brand_color", "video_brand_handle", "video_brand_cta",
        "video_bgm_path", "use_viral_blueprint", "youtube_api_key",
        # Replicate (new primary avatar provider) + provider preference
        "replicate_api_token", "replicate_presenter_url", "avatar_provider",
        # Global posting schedule (admin only — applies to all users)
        "post_time_morning", "post_time_evening",
        # Per-platform deferred post times
        "post_time_tiktok", "post_time_youtube",
        # Separate alert channel
        "telegram_alert_chat",
        # UI language preference
        "language",
        # HeyGen
        "heygen_api_key", "heygen_voice_id",
        # Content niche
        "niche",
        # Geolocation metadata (ipstack)
        "timezone_id", "signup_country", "signup_city",
        # Facebook Page
        "fb_access_token", "fb_page_id",
        # Email / SMTP notifications
        "smtp_host", "smtp_port", "smtp_user", "smtp_pass", "smtp_from",
        "email_notify_heygen", "email_notify_fail", "email_notify_digest",
        # Webhook inbound secret
        "webhook_secret",
    }
    # Drop admin-only fields if caller is not admin (defence-in-depth)
    if not _is_admin:
        kwargs = {k: v for k, v in kwargs.items() if k not in _DB_ADMIN_ONLY_FIELDS}
    data = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    with _conn() as c:
        # Ensure the row exists
        c.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))
        if not data:
            return
        set_clauses = ", ".join(f"{k}=:{k}" for k in data)
        c.execute(
            f"UPDATE user_settings SET {set_clauses}, updated_at=datetime('now') "
            f"WHERE user_id=:user_id",
            {"user_id": user_id, **data},
        )


# ---------------------------------------------------------------------------
# Viral pattern cache (24h TTL)
# ---------------------------------------------------------------------------

def save_viral_blueprint(user_id: int, topic: str, blueprint: dict) -> None:
    """Persist a scan result so the copywriter doesn't re-scrape on every
    pipeline run."""
    import json as _json
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO viral_patterns "
            "(user_id, topic, blueprint, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (user_id, topic.lower(), _json.dumps(blueprint, ensure_ascii=False)),
        )


def get_viral_blueprint(user_id: int, topic: str,
                        max_age_hours: int = 24) -> dict | None:
    """Return the cached blueprint if younger than `max_age_hours`, else None."""
    import json as _json
    with _conn() as c:
        row = c.execute(
            "SELECT blueprint, created_at FROM viral_patterns "
            "WHERE user_id=? AND topic=? "
            "AND datetime(created_at) > datetime('now', ?)",
            (user_id, topic.lower(), f"-{max_age_hours} hours"),
        ).fetchone()
    if not row:
        return None
    try:
        return _json.loads(row[0])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Competitor tracker helpers
# ---------------------------------------------------------------------------

def add_competitor(user_id: int, platform: str, handle: str,
                   label: str = "", rss_url: str = "") -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO competitor_accounts (user_id, platform, handle, label, rss_url) "
            "VALUES (?,?,?,?,?)",
            (user_id, platform, handle.strip(), label.strip(), rss_url.strip()),
        )
        return cur.lastrowid


def remove_competitor(comp_id: int, user_id: int) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM competitor_accounts WHERE id=? AND user_id=?",
            (comp_id, user_id),
        )


def get_competitors(user_id: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM competitor_accounts WHERE user_id=? AND is_active=1 "
            "ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_competitors() -> list[dict]:
    """All active competitors across all users — used by scheduler."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM competitor_accounts WHERE is_active=1"
        ).fetchall()
        return [dict(r) for r in rows]


def save_competitor_posts(account_id: int, posts: list[dict]) -> int:
    """Upsert competitor posts; returns number of new posts inserted."""
    inserted = 0
    with _conn() as c:
        for p in posts:
            try:
                c.execute(
                    "INSERT OR IGNORE INTO competitor_posts "
                    "(account_id, post_id, content, url, likes, shares, views, posted_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (account_id, p.get("post_id", ""), p.get("content", ""),
                     p.get("url", ""), p.get("likes", 0), p.get("shares", 0),
                     p.get("views", 0), p.get("posted_at", "")),
                )
                if c.execute("SELECT changes()").fetchone()[0]:
                    inserted += 1
            except Exception:
                pass
        c.execute(
            "UPDATE competitor_accounts SET last_scanned_at=datetime('now') WHERE id=?",
            (account_id,),
        )
        # Keep only last 50 posts per account
        c.execute(
            "DELETE FROM competitor_posts WHERE account_id=? AND id NOT IN "
            "(SELECT id FROM competitor_posts WHERE account_id=? "
            "ORDER BY posted_at DESC, id DESC LIMIT 50)",
            (account_id, account_id),
        )
    return inserted


def get_competitor_posts(account_id: int, limit: int = 10) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM competitor_posts WHERE account_id=? "
            "ORDER BY posted_at DESC, id DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def update_competitor_x_id(comp_id: int, x_user_id: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE competitor_accounts SET x_user_id=? WHERE id=?",
            (x_user_id, comp_id),
        )


# ---------------------------------------------------------------------------
# Audience tracker — shortlinks + clicks (uses geo.py for ipstack lookups)
# ---------------------------------------------------------------------------

def _new_shortcode() -> str:
    """Random url-safe 7-char code (~3.5 trillion possibilities)."""
    import secrets, string
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(7))


def create_shortlink(user_id: int, target_url: str,
                      label: str = "", platform: str = "",
                      slot_name: str = "") -> dict:
    """Create a new shortlink. Retries on collision (extremely rare)."""
    target_url = (target_url or "").strip()
    if not target_url:
        raise ValueError("target_url required")
    with _conn() as c:
        for _ in range(10):
            code = _new_shortcode()
            try:
                cur = c.execute(
                    "INSERT INTO shortlinks (user_id, code, target_url, label, "
                    "platform, slot_name) VALUES (?,?,?,?,?,?)",
                    (user_id, code, target_url, label.strip(),
                     platform.strip(), slot_name.strip()),
                )
                return {"id": cur.lastrowid, "code": code,
                        "target_url": target_url}
            except sqlite3.IntegrityError:
                continue
    raise RuntimeError("could not generate unique shortcode")


def get_shortlink_by_code(code: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM shortlinks WHERE code=?", (code,),
        ).fetchone()
        return dict(row) if row else None


def list_shortlinks(user_id: int, limit: int = 100) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM shortlinks WHERE user_id=? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_shortlink(link_id: int, user_id: int) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM shortlinks WHERE id=? AND user_id=?",
            (link_id, user_id),
        )


def log_shortlink_click(link_id: int, ip: str = "",
                         country_code: str = "", country_name: str = "",
                         city: str = "", user_agent: str = "",
                         referer: str = "") -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO shortlink_clicks (link_id, ip, country_code, "
            "country_name, city, user_agent, referer) VALUES (?,?,?,?,?,?,?)",
            (link_id, ip[:64], country_code[:8], country_name[:64],
             city[:80], user_agent[:255], referer[:255]),
        )
        c.execute(
            "UPDATE shortlinks SET click_count = click_count + 1 WHERE id=?",
            (link_id,),
        )


def get_shortlink_clicks(link_id: int, user_id: int,
                          limit: int = 200) -> list[dict]:
    """Recent clicks for a single link (scoped to the owner)."""
    with _conn() as c:
        # Verify ownership first
        own = c.execute(
            "SELECT 1 FROM shortlinks WHERE id=? AND user_id=?",
            (link_id, user_id),
        ).fetchone()
        if not own:
            return []
        rows = c.execute(
            "SELECT * FROM shortlink_clicks WHERE link_id=? "
            "ORDER BY clicked_at DESC LIMIT ?",
            (link_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_audience_breakdown(user_id: int, days: int = 30) -> dict:
    """Aggregate clicks across ALL the user's shortlinks for the last N days."""
    with _conn() as c:
        rows = c.execute("""
            SELECT cl.country_code, cl.country_name, COUNT(*) AS hits
            FROM shortlink_clicks cl
            JOIN shortlinks sl ON sl.id = cl.link_id
            WHERE sl.user_id = ?
              AND cl.clicked_at >= datetime('now', ?)
            GROUP BY cl.country_code
            ORDER BY hits DESC
        """, (user_id, f"-{int(days)} days")).fetchall()
        countries = [
            {"country_code": r["country_code"] or "??",
             "country_name": r["country_name"] or "Unknown",
             "hits":         r["hits"]}
            for r in rows
        ]
        total = sum(r["hits"] for r in countries)
        return {"days": days, "total_clicks": total, "countries": countries}


# ---------------------------------------------------------------------------
# Login security log
# ---------------------------------------------------------------------------

def log_login(user_id: int, ip: str = "",
                country_code: str = "", country_name: str = "",
                city: str = "", user_agent: str = "",
                success: bool = True) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO login_log (user_id, ip, country_code, country_name, "
            "city, user_agent, success) VALUES (?,?,?,?,?,?,?)",
            (user_id, ip[:64], country_code[:8], country_name[:64],
             city[:80], user_agent[:255], 1 if success else 0),
        )
        if success:
            c.execute(
                "UPDATE users SET last_login_at=datetime('now'), last_login_ip=? "
                "WHERE id=?",
                (ip[:64], user_id),
            )


def previous_login_country(user_id: int) -> str:
    """Country code of the most recent successful login BEFORE this one."""
    with _conn() as c:
        # Skip the very last row (which we likely just inserted) — we want the
        # country of the prior session, not the one in progress.
        row = c.execute(
            "SELECT country_code FROM login_log WHERE user_id=? AND success=1 "
            "ORDER BY id DESC LIMIT 1 OFFSET 1",
            (user_id,),
        ).fetchone()
        return row["country_code"] if row and row["country_code"] else ""


def recent_logins(user_id: int, limit: int = 20) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM login_log WHERE user_id=? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_login_logs(limit: int = 200) -> list[dict]:
    """Admin-wide login log with user email + country."""
    with _conn() as c:
        rows = c.execute(
            "SELECT l.*, u.email, u.display_name "
            "FROM login_log l LEFT JOIN users u ON u.id=l.user_id "
            "ORDER BY l.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# HeyGen usage counter
# ---------------------------------------------------------------------------

def log_heygen_usage(user_id: int, slot_name: str, platform: str,
                     words: int, status: str = "done") -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO heygen_usage (user_id, slot_name, platform, words, status) "
            "VALUES (?,?,?,?,?)",
            (user_id, slot_name, platform, words, status),
        )


def get_heygen_count(user_id: int) -> int:
    """Total successful HeyGen generations for this user."""
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM heygen_usage WHERE user_id=? AND status='done'",
            (user_id,),
        ).fetchone()
        return row[0] if row else 0


def get_heygen_usage_log(user_id: int, limit: int = 20) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM heygen_usage WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Publish history
# ---------------------------------------------------------------------------

def log_publish_history(user_id: int, slot_name: str, title: str,
                        platforms: list, results: dict) -> None:
    import json as _json
    with _conn() as c:
        c.execute(
            "INSERT INTO publish_history (user_id, slot_name, title, platforms, results) "
            "VALUES (?,?,?,?,?)",
            (user_id, slot_name, title,
             _json.dumps(platforms, ensure_ascii=False),
             _json.dumps(results, ensure_ascii=False)),
        )


def get_publish_history(user_id: int, limit: int = 50) -> list[dict]:
    import json as _json
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM publish_history WHERE user_id=? "
            "ORDER BY published_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["platforms"] = _json.loads(d["platforms"])
        except Exception:
            d["platforms"] = []
        try:
            d["results"] = _json.loads(d["results"])
        except Exception:
            d["results"] = {}
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Scheduled posts
# ---------------------------------------------------------------------------

def create_scheduled_post(user_id: int, slot_name: str,
                          platforms: list, scheduled_at: str) -> int:
    import json as _json
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO scheduled_posts (user_id, slot_name, platforms, scheduled_at) "
            "VALUES (?,?,?,?)",
            (user_id, slot_name, _json.dumps(platforms), scheduled_at),
        )
        return cur.lastrowid


def get_scheduled_posts(user_id: int, status: str = "pending") -> list[dict]:
    import json as _json
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM scheduled_posts WHERE user_id=? AND status=? "
            "ORDER BY scheduled_at ASC",
            (user_id, status),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["platforms"] = _json.loads(d["platforms"])
        except Exception:
            d["platforms"] = []
        out.append(d)
    return out


def get_due_scheduled_posts() -> list[dict]:
    """All pending posts whose scheduled_at <= now (for all users)."""
    import json as _json
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM scheduled_posts WHERE status='pending' "
            "AND scheduled_at <= datetime('now')",
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["platforms"] = _json.loads(d["platforms"])
        except Exception:
            d["platforms"] = []
        out.append(d)
    return out


def update_scheduled_post_status(post_id: int, status: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE scheduled_posts SET status=?, ran_at=datetime('now') WHERE id=?",
            (status, post_id),
        )


def delete_scheduled_post(post_id: int, user_id: int) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM scheduled_posts WHERE id=? AND user_id=?",
            (post_id, user_id),
        )
