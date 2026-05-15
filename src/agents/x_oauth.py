"""X OAuth 2.0 PKCE flow — the SaaS-grade path for posting tweets.

Why this module exists
----------------------
For a multi-tenant SaaS (many subscribers posting through one app), we cannot:
  - share a single cookies file (everyone would post as the same account)
  - require each user to run a Playwright browser helper from the CLI

The clean solution is X's OAuth 2.0 User Context with PKCE. Each subscriber
clicks "Connect X" in the dashboard, authorizes our single X Developer app,
and we store their per-user refresh/access tokens. We then post on their
behalf with plain HTTPS calls to `api.x.com/2/tweets`.

Setup (one-time, for you as app owner)
--------------------------------------
1. Go to https://developer.x.com/en/portal/projects-and-apps
2. Create a Project → App (Free tier is fine for development).
3. In "User authentication settings":
     App permissions   = Read and write  (Tweet + Media)
     Type of App       = Web App
     Callback URI      = https://your.domain.com/x/callback
                       + http://localhost:8000/x/callback  (for local dev)
     Website URL       = https://your.domain.com
4. Copy the OAuth 2.0 Client ID and Client Secret into:
     .env   →   X_OAUTH_CLIENT_ID=...
                X_OAUTH_CLIENT_SECRET=...
                X_OAUTH_REDIRECT_URI=https://your.domain.com/x/callback

Scopes we request
-----------------
- tweet.read        (required so /users/me works)
- tweet.write       (post tweets)
- users.read        (identify the connected account)
- offline.access    (receive a refresh_token → we can post forever without re-auth)
- media.write       (upload images/videos)

Rate limits (Free tier, as of May 2025)
---------------------------------------
- POST /2/tweets: 17 requests / 24h per user × 500 requests / month per app.
  For a typical subscriber posting a few times a day this is plenty.
  Upgrade the app to Basic ($100/mo) for 3K/month, Pro ($5K/mo) for 300K.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    requests = None  # type: ignore


AUTH_URL   = "https://x.com/i/oauth2/authorize"
TOKEN_URL  = "https://api.x.com/2/oauth2/token"
TWEETS_URL = "https://api.x.com/2/tweets"
ME_URL     = "https://api.x.com/2/users/me"
MEDIA_UPLOAD_V2 = "https://api.x.com/2/media/upload"

DEFAULT_SCOPES = "tweet.read tweet.write users.read offline.access media.write"


# ---------------------------------------------------------------------------
# Credentials discovery
# ---------------------------------------------------------------------------

def _client_creds() -> tuple[str, str, str]:
    """Return (client_id, client_secret, redirect_uri) or raise."""
    cid    = os.environ.get("X_OAUTH_CLIENT_ID", "").strip()
    secret = os.environ.get("X_OAUTH_CLIENT_SECRET", "").strip()
    uri    = os.environ.get("X_OAUTH_REDIRECT_URI", "").strip()
    if not cid:
        raise RuntimeError(
            "X_OAUTH_CLIENT_ID missing in environment. "
            "Create an X Developer app at developer.x.com and set it in .env"
        )
    if not uri:
        # Sensible default for local dev
        uri = "http://localhost:8000/x/callback"
    return cid, secret, uri


# ---------------------------------------------------------------------------
# PKCE helpers (no external deps)
# ---------------------------------------------------------------------------

def _gen_code_verifier() -> str:
    return secrets.token_urlsafe(64)[:128]


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


# ---------------------------------------------------------------------------
# Step 1: build the authorize URL (called from /x/connect)
# ---------------------------------------------------------------------------

def build_authorize_url(state: str, scopes: str = DEFAULT_SCOPES) -> tuple[str, str]:
    """Return (authorize_url, code_verifier).

    The caller MUST persist `state` + `code_verifier` in the user's session
    so that the /x/callback handler can pair them back together.
    """
    cid, _secret, redirect_uri = _client_creds()
    verifier  = _gen_code_verifier()
    challenge = _code_challenge(verifier)

    params = {
        "response_type":         "code",
        "client_id":             cid,
        "redirect_uri":          redirect_uri,
        "scope":                 scopes,
        "state":                 state,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}", verifier


# ---------------------------------------------------------------------------
# Step 2: exchange the callback code for access_token + refresh_token
# ---------------------------------------------------------------------------

def exchange_code(code: str, code_verifier: str) -> dict[str, Any]:
    """Trade an authorization code for an access token + refresh token."""
    if requests is None:
        raise RuntimeError("`requests` library not installed")

    cid, secret, redirect_uri = _client_creds()
    data = {
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  redirect_uri,
        "code_verifier": code_verifier,
        "client_id":     cid,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    auth = (cid, secret) if secret else None

    r = requests.post(TOKEN_URL, data=data, headers=headers, auth=auth, timeout=30)
    if r.status_code >= 400:
        try:
            detail = r.json()
        except Exception:
            detail = {"raw": r.text[:400]}
        raise RuntimeError(f"X token exchange {r.status_code}: {detail}")

    tok = r.json()
    # Normalize
    tok["_obtained_at"] = int(time.time())
    tok["_expires_at"]  = int(time.time()) + int(tok.get("expires_in", 7200))
    return tok


# ---------------------------------------------------------------------------
# Step 3: refresh tokens (when access_token is close to expiry)
# ---------------------------------------------------------------------------

def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    """Use a refresh_token to get a new access_token (and possibly a new refresh_token)."""
    if requests is None:
        raise RuntimeError("`requests` library not installed")
    if not refresh_token:
        raise RuntimeError("no refresh_token provided — user must re-authorize")

    cid, secret, _ = _client_creds()
    data = {
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
        "client_id":     cid,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    auth = (cid, secret) if secret else None

    r = requests.post(TOKEN_URL, data=data, headers=headers, auth=auth, timeout=30)
    if r.status_code >= 400:
        try:
            detail = r.json()
        except Exception:
            detail = {"raw": r.text[:400]}
        raise RuntimeError(f"X refresh {r.status_code}: {detail}")

    tok = r.json()
    tok["_obtained_at"] = int(time.time())
    tok["_expires_at"]  = int(time.time()) + int(tok.get("expires_in", 7200))
    # X does not always return a new refresh_token — keep the old one.
    tok.setdefault("refresh_token", refresh_token)
    return tok


def ensure_fresh_token(access_token: str,
                       refresh_token: str,
                       expires_at: int,
                       skew_seconds: int = 90) -> dict[str, Any]:
    """Return a dict with a guaranteed-fresh access_token.

    If the current token is still valid (with a skew margin), return it as-is.
    Otherwise refresh and return the new pair.
    """
    if access_token and expires_at and (expires_at - skew_seconds) > time.time():
        return {
            "access_token":  access_token,
            "refresh_token": refresh_token,
            "_expires_at":   expires_at,
            "refreshed":     False,
        }
    tok = refresh_access_token(refresh_token)
    tok["refreshed"] = True
    return tok


# ---------------------------------------------------------------------------
# Step 4: identify the connected account (nice to show in UI)
# ---------------------------------------------------------------------------

def get_me(access_token: str) -> dict[str, Any]:
    if requests is None:
        raise RuntimeError("`requests` library not installed")
    r = requests.get(
        ME_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    if r.status_code >= 400:
        try:
            detail = r.json()
        except Exception:
            detail = {"raw": r.text[:400]}
        raise RuntimeError(f"X /users/me {r.status_code}: {detail}")
    data = r.json().get("data") or {}
    return data  # {"id": "...", "name": "...", "username": "..."}


# ---------------------------------------------------------------------------
# Step 5: upload media (image) via v2 endpoint — OAuth 2.0 compatible
# ---------------------------------------------------------------------------

def upload_image_v2(access_token: str, image_path: Path) -> str | None:
    """Upload an image to X using /2/media/upload (simple, non-chunked).

    Returns the media_id string, or None on failure (caller falls back to text-only).
    This endpoint supports OAuth 2.0 user context — unlike v1.1 media/upload
    which needs OAuth 1.0a.
    """
    if requests is None:
        return None
    image_path = Path(image_path)
    if not image_path.exists():
        return None
    mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
    try:
        with open(image_path, "rb") as f:
            files = {"media": (image_path.name, f, mime)}
            data  = {"media_category": "tweet_image"}
            r = requests.post(
                MEDIA_UPLOAD_V2,
                headers={"Authorization": f"Bearer {access_token}"},
                files=files,
                data=data,
                timeout=120,
            )
        if r.status_code >= 400:
            # Some X plans still only have v1.1 for media upload. In that case we
            # simply return None and the caller posts text-only.
            print(f"[x_oauth] media upload {r.status_code}: {r.text[:200]}")
            return None
        j = r.json()
        # Response shape: {"data": {"id": "...", "media_key": "..."}}
        if isinstance(j, dict):
            data_obj = j.get("data") or j
            mid = data_obj.get("id") or data_obj.get("media_id_string") or data_obj.get("media_id")
            if mid:
                return str(mid)
        return None
    except Exception as exc:
        print(f"[x_oauth] media upload crashed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Step 6: post a tweet (+ optional media)
# ---------------------------------------------------------------------------

def post_tweet(access_token: str, text: str,
               media_ids: list[str] | None = None) -> dict[str, Any]:
    if requests is None:
        raise RuntimeError("`requests` library not installed")

    payload: dict[str, Any] = {"text": text}
    if media_ids:
        payload["media"] = {"media_ids": [str(m) for m in media_ids]}

    r = requests.post(
        TWEETS_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type":  "application/json",
        },
        json=payload,
        timeout=30,
    )
    if r.status_code >= 400:
        try:
            detail = r.json()
        except Exception:
            detail = {"raw": r.text[:400]}
        raise RuntimeError(f"X POST /2/tweets {r.status_code}: {detail}")

    data = r.json().get("data") or {}
    tweet_id = data.get("id") or ""
    return {
        "status":   "ok",
        "tweet_id": tweet_id,
        "url":      f"https://x.com/i/web/status/{tweet_id}" if tweet_id else "",
        "via":      "oauth2",
    }
