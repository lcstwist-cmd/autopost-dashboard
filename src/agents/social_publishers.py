"""Native social publishers — Instagram, TikTok, YouTube.

Zero Make.com. Zero Zapier. Only official APIs.

Each publisher returns a dict:
    {"status": "ok"|"error"|"skipped", "error": "...", ...extra}

Environment variables expected:

  INSTAGRAM (Meta Graph API — Instagram Business/Creator account required):
    IG_USER_ID              — Instagram Business Account ID (numeric)
    IG_ACCESS_TOKEN         — long-lived Page Access Token

  TIKTOK (Content Posting API — TikTok for Developers):
    TIKTOK_ACCESS_TOKEN     — OAuth access token (user-scoped, refreshable)
    TIKTOK_REFRESH_TOKEN    — refresh token (optional; auto-rotates access)
    TIKTOK_CLIENT_KEY       — dev app key (needed for refresh)
    TIKTOK_CLIENT_SECRET    — dev app secret (needed for refresh)

  YOUTUBE (YouTube Data API v3 — OAuth 2.0 user scope):
    YT_CLIENT_ID            — OAuth client id
    YT_CLIENT_SECRET        — OAuth client secret
    YT_REFRESH_TOKEN        — long-lived refresh token (one-time auth)
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


# ═══════════════════════════════════════════════════════════════════════════
# INSTAGRAM — Graph API
# https://developers.facebook.com/docs/instagram-api/guides/content-publishing
# ═══════════════════════════════════════════════════════════════════════════

GRAPH_API = "https://graph.facebook.com/v21.0"


def publish_instagram_api(caption: str, image_url: str,
                          ig_user_id: str | None = None,
                          access_token: str | None = None) -> dict[str, Any]:
    """Publish a single photo to an Instagram Business/Creator account.

    Two-step flow:
      1. POST /{ig-user-id}/media       → media container id
      2. POST /{ig-user-id}/media_publish → creation id = published post
    """
    if requests is None:
        return {"status": "error", "error": "requests not installed"}

    ig_user_id   = (ig_user_id   or os.environ.get("IG_USER_ID",      "")).strip()
    access_token = (access_token or os.environ.get("IG_ACCESS_TOKEN", "")).strip()

    if not ig_user_id or not access_token:
        return {"status": "skipped",
                "error": "IG_USER_ID / IG_ACCESS_TOKEN not set"}
    if not image_url or not image_url.startswith("http"):
        return {"status": "error",
                "error": f"Instagram needs a public image URL, got: {image_url!r}"}

    caption = (caption or "")[:2200]  # Instagram hard limit

    # ── Step 1: create container ──────────────────────────────────────────
    try:
        r1 = requests.post(
            f"{GRAPH_API}/{ig_user_id}/media",
            data={"image_url": image_url, "caption": caption,
                  "access_token": access_token},
            timeout=60,
        )
    except Exception as exc:
        return {"status": "error", "error": f"IG create container failed: {exc}"}

    if r1.status_code != 200:
        return {"status": "error",
                "error": f"IG media create HTTP {r1.status_code}: {r1.text[:300]}"}

    creation_id = (r1.json() or {}).get("id")
    if not creation_id:
        return {"status": "error",
                "error": f"IG did not return creation id: {r1.text[:300]}"}

    # ── Step 1b: wait until container is FINISHED (usually instant) ───────
    for _ in range(10):
        try:
            rs = requests.get(
                f"{GRAPH_API}/{creation_id}",
                params={"fields": "status_code", "access_token": access_token},
                timeout=15,
            )
            status_code = (rs.json() or {}).get("status_code", "IN_PROGRESS")
        except Exception:
            status_code = "IN_PROGRESS"
        if status_code == "FINISHED":
            break
        if status_code == "ERROR":
            return {"status": "error",
                    "error": f"IG container error: {rs.text[:200]}"}
        time.sleep(2)

    # ── Step 2: publish ───────────────────────────────────────────────────
    try:
        r2 = requests.post(
            f"{GRAPH_API}/{ig_user_id}/media_publish",
            data={"creation_id": creation_id, "access_token": access_token},
            timeout=60,
        )
    except Exception as exc:
        return {"status": "error", "error": f"IG publish failed: {exc}"}

    if r2.status_code != 200:
        return {"status": "error",
                "error": f"IG media_publish HTTP {r2.status_code}: {r2.text[:300]}"}

    post_id = (r2.json() or {}).get("id")
    return {
        "status": "ok",
        "post_id": post_id,
        "url": f"https://www.instagram.com/p/{post_id}/" if post_id else None,
        "via": "graph_api",
    }


def publish_instagram_reel_api(caption: str, video_url: str,
                               cover_url: str | None = None,
                               ig_user_id: str | None = None,
                               access_token: str | None = None) -> dict[str, Any]:
    """Publish a vertical video as an Instagram Reel."""
    if requests is None:
        return {"status": "error", "error": "requests not installed"}

    ig_user_id   = (ig_user_id   or os.environ.get("IG_USER_ID",      "")).strip()
    access_token = (access_token or os.environ.get("IG_ACCESS_TOKEN", "")).strip()
    if not ig_user_id or not access_token:
        return {"status": "skipped", "error": "IG_USER_ID / IG_ACCESS_TOKEN not set"}
    if not video_url or not video_url.startswith("http"):
        return {"status": "error", "error": "IG reel needs a public video URL"}

    caption = (caption or "")[:2200]

    data = {
        "media_type":   "REELS",
        "video_url":    video_url,
        "caption":      caption,
        "access_token": access_token,
    }
    if cover_url:
        data["cover_url"] = cover_url

    try:
        r1 = requests.post(f"{GRAPH_API}/{ig_user_id}/media", data=data, timeout=60)
    except Exception as exc:
        return {"status": "error", "error": f"IG reel create failed: {exc}"}
    if r1.status_code != 200:
        return {"status": "error",
                "error": f"IG reel create HTTP {r1.status_code}: {r1.text[:300]}"}

    creation_id = (r1.json() or {}).get("id")
    if not creation_id:
        return {"status": "error",
                "error": f"IG reel no creation id: {r1.text[:200]}"}

    # Reels take longer to process — poll up to 3 min
    for _ in range(60):
        try:
            rs = requests.get(
                f"{GRAPH_API}/{creation_id}",
                params={"fields": "status_code", "access_token": access_token},
                timeout=15,
            )
            status_code = (rs.json() or {}).get("status_code", "IN_PROGRESS")
        except Exception:
            status_code = "IN_PROGRESS"
        if status_code == "FINISHED":
            break
        if status_code == "ERROR":
            return {"status": "error", "error": f"IG reel processing failed"}
        time.sleep(3)
    else:
        return {"status": "error", "error": "IG reel processing timeout (>3 min)"}

    try:
        r2 = requests.post(
            f"{GRAPH_API}/{ig_user_id}/media_publish",
            data={"creation_id": creation_id, "access_token": access_token},
            timeout=60,
        )
    except Exception as exc:
        return {"status": "error", "error": f"IG reel publish failed: {exc}"}

    if r2.status_code != 200:
        return {"status": "error",
                "error": f"IG reel publish HTTP {r2.status_code}: {r2.text[:300]}"}
    post_id = (r2.json() or {}).get("id")
    return {"status": "ok", "post_id": post_id,
            "url": f"https://www.instagram.com/reel/{post_id}/" if post_id else None,
            "via": "graph_api_reel"}


# ═══════════════════════════════════════════════════════════════════════════
# TIKTOK — Content Posting API
# https://developers.tiktok.com/doc/content-posting-api-get-started
# ═══════════════════════════════════════════════════════════════════════════

TIKTOK_API = "https://open.tiktokapis.com"


def _tiktok_refresh_token() -> str | None:
    """Refresh the TikTok access token if we have a refresh_token + client creds."""
    if requests is None:
        return None
    refresh  = os.environ.get("TIKTOK_REFRESH_TOKEN", "").strip()
    ck       = os.environ.get("TIKTOK_CLIENT_KEY", "").strip()
    cs       = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip()
    if not (refresh and ck and cs):
        return None
    try:
        r = requests.post(
            "https://open.tiktokapis.com/v2/oauth/token/",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_key":    ck,
                "client_secret": cs,
                "grant_type":    "refresh_token",
                "refresh_token": refresh,
            },
            timeout=30,
        )
        if r.status_code == 200:
            tok = (r.json() or {}).get("access_token")
            if tok:
                os.environ["TIKTOK_ACCESS_TOKEN"] = tok
                return tok
    except Exception:
        pass
    return None


def publish_tiktok_api(caption: str, video_path: Path,
                      access_token: str | None = None) -> dict[str, Any]:
    """Upload a local video to TikTok using FILE_UPLOAD strategy.

    Flow:
      1. POST /v2/post/publish/video/init/   with source=FILE_UPLOAD
      2. PUT  upload_url (chunks)             with the binary video
      3. GET  /v2/post/publish/status/fetch/  until SEND_TO_USER_INBOX or PUBLISH_COMPLETE
    """
    if requests is None:
        return {"status": "error", "error": "requests not installed"}

    if not video_path.exists():
        return {"status": "error", "error": f"video not found: {video_path}"}

    tok = (access_token or os.environ.get("TIKTOK_ACCESS_TOKEN", "")).strip()
    if not tok:
        tok = _tiktok_refresh_token() or ""
    if not tok:
        return {"status": "skipped",
                "error": "TIKTOK_ACCESS_TOKEN not set (and cannot refresh)"}

    video_size = video_path.stat().st_size
    chunk_size = min(video_size, 10 * 1024 * 1024)  # 10 MB
    total_chunks = (video_size + chunk_size - 1) // chunk_size

    # ── Step 1: init upload ───────────────────────────────────────────────
    init_payload = {
        "post_info": {
            "title":                    (caption or "")[:2200],
            "description":              (caption or "")[:2200],
            "privacy_level":            "SELF_ONLY",  # safe default; user can flip later
            "disable_duet":             False,
            "disable_comment":          False,
            "disable_stitch":           False,
            "video_cover_timestamp_ms": 1000,
        },
        "source_info": {
            "source":             "FILE_UPLOAD",
            "video_size":         video_size,
            "chunk_size":         chunk_size,
            "total_chunk_count":  total_chunks,
        },
    }
    # TikTok recommends privacy_level=PUBLIC_TO_EVERYONE only for accounts
    # that passed the audit. New apps default to SELF_ONLY.
    privacy_env = os.environ.get("TIKTOK_PRIVACY", "").strip().upper()
    if privacy_env in ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS",
                       "FOLLOWER_OF_CREATOR", "SELF_ONLY"):
        init_payload["post_info"]["privacy_level"] = privacy_env

    try:
        ri = requests.post(
            f"{TIKTOK_API}/v2/post/publish/video/init/",
            headers={"Authorization": f"Bearer {tok}",
                     "Content-Type":  "application/json; charset=UTF-8"},
            json=init_payload,
            timeout=30,
        )
    except Exception as exc:
        return {"status": "error", "error": f"TikTok init failed: {exc}"}

    if ri.status_code != 200:
        return {"status": "error",
                "error": f"TikTok init HTTP {ri.status_code}: {ri.text[:300]}"}

    data = ri.json().get("data", {})
    publish_id = data.get("publish_id")
    upload_url = data.get("upload_url")
    if not publish_id or not upload_url:
        return {"status": "error",
                "error": f"TikTok init no upload_url: {ri.text[:300]}"}

    # ── Step 2: upload chunks ─────────────────────────────────────────────
    try:
        with open(video_path, "rb") as fh:
            for idx in range(total_chunks):
                start = idx * chunk_size
                end   = min(start + chunk_size, video_size) - 1
                fh.seek(start)
                chunk_data = fh.read(end - start + 1)
                ru = requests.put(
                    upload_url,
                    data=chunk_data,
                    headers={
                        "Content-Range":  f"bytes {start}-{end}/{video_size}",
                        "Content-Length": str(len(chunk_data)),
                        "Content-Type":   "video/mp4",
                    },
                    timeout=300,
                )
                if ru.status_code not in (200, 201, 206):
                    return {"status": "error",
                            "error": f"TikTok chunk {idx+1}/{total_chunks} HTTP "
                                     f"{ru.status_code}: {ru.text[:200]}"}
    except Exception as exc:
        return {"status": "error", "error": f"TikTok upload failed: {exc}"}

    # ── Step 3: poll until ready ──────────────────────────────────────────
    deadline = time.time() + 300
    last_state = "PROCESSING_UPLOAD"
    while time.time() < deadline:
        try:
            rs = requests.post(
                f"{TIKTOK_API}/v2/post/publish/status/fetch/",
                headers={"Authorization": f"Bearer {tok}",
                         "Content-Type":  "application/json; charset=UTF-8"},
                json={"publish_id": publish_id},
                timeout=30,
            )
            if rs.status_code == 200:
                sd = rs.json().get("data", {})
                last_state = sd.get("status", last_state)
                if last_state in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
                    return {"status":     "ok",
                            "publish_id": publish_id,
                            "final":      last_state,
                            "via":        "tiktok_api"}
                if last_state in ("FAILED", "PUBLISH_FAILED"):
                    return {"status": "error",
                            "error":  f"TikTok publish failed: {sd.get('fail_reason','')}"}
        except Exception:
            pass
        time.sleep(5)

    return {"status": "error",
            "error": f"TikTok timeout (last state: {last_state})"}


# ═══════════════════════════════════════════════════════════════════════════
# YOUTUBE — Data API v3 (resumable upload)
# https://developers.google.com/youtube/v3/docs/videos/insert
# ═══════════════════════════════════════════════════════════════════════════

YT_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
YT_TOKEN_URL  = "https://oauth2.googleapis.com/token"


def _youtube_access_token() -> str | None:
    """Swap refresh_token → short-lived access_token via Google OAuth."""
    if requests is None:
        return None
    ci  = os.environ.get("YT_CLIENT_ID", "").strip()
    cs  = os.environ.get("YT_CLIENT_SECRET", "").strip()
    rt  = os.environ.get("YT_REFRESH_TOKEN", "").strip()
    if not (ci and cs and rt):
        return None
    try:
        r = requests.post(
            YT_TOKEN_URL,
            data={"client_id": ci, "client_secret": cs,
                  "refresh_token": rt, "grant_type": "refresh_token"},
            timeout=30,
        )
        if r.status_code == 200:
            return (r.json() or {}).get("access_token")
    except Exception:
        return None
    return None


def publish_youtube_api(title: str, description: str, video_path: Path,
                        tags: list[str] | None = None,
                        privacy: str = "public",
                        made_for_kids: bool = False) -> dict[str, Any]:
    """Upload a video to YouTube (Shorts-compatible if vertical + <= 60s).

    Uses resumable upload so connection drops don't kill the upload.
    """
    if requests is None:
        return {"status": "error", "error": "requests not installed"}
    if not video_path.exists():
        return {"status": "error", "error": f"video not found: {video_path}"}

    tok = _youtube_access_token()
    if not tok:
        return {"status": "skipped",
                "error": "YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN not set"}

    title       = (title or "Crypto Update")[:100]
    description = (description or "")[:5000]
    tags        = (tags or [])[:500]

    metadata = {
        "snippet": {
            "title":       title,
            "description": description,
            "tags":        tags,
            "categoryId":  "25",  # News & Politics
        },
        "status": {
            "privacyStatus":      privacy,
            "selfDeclaredMadeForKids": made_for_kids,
        },
    }

    video_size = video_path.stat().st_size

    # ── Step 1: init resumable upload ─────────────────────────────────────
    try:
        r1 = requests.post(
            YT_UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization":          f"Bearer {tok}",
                "Content-Type":           "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(video_size),
                "X-Upload-Content-Type":   "video/*",
            },
            data=json.dumps(metadata),
            timeout=30,
        )
    except Exception as exc:
        return {"status": "error", "error": f"YT init failed: {exc}"}

    if r1.status_code != 200:
        return {"status": "error",
                "error": f"YT init HTTP {r1.status_code}: {r1.text[:300]}"}

    upload_url = r1.headers.get("Location")
    if not upload_url:
        return {"status": "error", "error": "YT init: no resumable Location header"}

    # ── Step 2: upload file in one PUT (simple — YT allows whole-file) ───
    try:
        with open(video_path, "rb") as fh:
            r2 = requests.put(
                upload_url,
                data=fh,
                headers={"Content-Length": str(video_size),
                         "Content-Type":   "video/*"},
                timeout=1800,
            )
    except Exception as exc:
        return {"status": "error", "error": f"YT upload failed: {exc}"}

    if r2.status_code not in (200, 201):
        return {"status": "error",
                "error": f"YT upload HTTP {r2.status_code}: {r2.text[:400]}"}

    video_id = (r2.json() or {}).get("id")
    if not video_id:
        return {"status": "error", "error": f"YT no video id: {r2.text[:300]}"}

    result = {
        "status":   "ok",
        "video_id": video_id,
        "url":      f"https://www.youtube.com/watch?v={video_id}",
        "shorts":   f"https://www.youtube.com/shorts/{video_id}",
        "via":      "youtube_data_v3",
    }

    # Upload custom thumbnail if present (requires channel verification on YT)
    thumb_path = video_path.parent / "image_yt_thumb_1280x720.png"
    if not thumb_path.exists():
        thumb_path = video_path.parent / "image_x_1200x675.png"  # fallback: X card
    if thumb_path.exists():
        try:
            r3 = requests.post(
                "https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
                params={"videoId": video_id, "uploadType": "media"},
                headers={"Authorization": f"Bearer {tok}",
                         "Content-Type": "image/png"},
                data=thumb_path.read_bytes(),
                timeout=60,
            )
            if r3.status_code in (200, 204):
                result["thumbnail"] = "uploaded"
            else:
                result["thumbnail_warn"] = f"HTTP {r3.status_code}"
        except Exception as te:
            result["thumbnail_warn"] = str(te)[:120]

    return result
