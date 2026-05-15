"""Public image/video hosting helpers.

Instagram Graph API, TikTok and YouTube all need either a public URL or a
direct file upload. This module provides both options:

1. `upload_imgbb(path)` — fast, free, no account beyond IMGBB_API_KEY
2. `upload_catbox(path)` — free, anonymous, no key required
3. `upload_0x0(path)`   — free, anonymous, no key required (0x0.st)
4. `upload_litterbox(path)` — free, anonymous, 72h expiry (litterbox.catbox.moe)
5. `public_url_via_base(path)` — if PUBLIC_BASE_URL is set (e.g. ngrok)

`get_public_url(path)` tries them in order and returns the first URL that works.
"""
from __future__ import annotations

import os
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent


def upload_imgbb(path: Path) -> str | None:
    """Upload to imgbb.com (requires IMGBB_API_KEY).

    Free plan: 32 MB/file, no expiry unless you set one.
    Get key: https://api.imgbb.com/ → free account → dashboard.
    """
    key = os.environ.get("IMGBB_API_KEY", "").strip()
    if not key or not path.exists():
        return None
    try:
        import base64 as _b64
        import requests
        with open(path, "rb") as fh:
            data = _b64.b64encode(fh.read()).decode("ascii")
        r = requests.post(
            "https://api.imgbb.com/1/upload",
            data={"key": key, "image": data, "name": path.stem},
            timeout=60,
        )
        if r.status_code == 200:
            body = r.json()
            if body.get("success"):
                return body["data"]["url"]
    except Exception:
        return None
    return None


def upload_catbox(path: Path) -> str | None:
    """Upload to catbox.moe (anonymous, free, videos up to 200 MB, no expiry)."""
    if not path.exists():
        return None
    try:
        import requests
        with open(path, "rb") as fh:
            r = requests.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": (path.name, fh)},
                timeout=120,
            )
        if r.status_code == 200 and r.text.startswith("http"):
            return r.text.strip()
    except Exception:
        return None
    return None


def upload_0x0(path: Path) -> str | None:
    """Upload to 0x0.st (anonymous, free, no account, max 512 MB).

    Files expire based on size: small files (images) stay for months.
    No key required.
    """
    if not path.exists():
        return None
    try:
        import requests
        with open(path, "rb") as fh:
            r = requests.post(
                "https://0x0.st",
                files={"file": (path.name, fh)},
                timeout=120,
            )
        if r.status_code == 200 and r.text.strip().startswith("http"):
            return r.text.strip()
    except Exception:
        return None
    return None


def upload_litterbox(path: Path, hours: int = 72) -> str | None:
    """Upload to litterbox.catbox.moe (anonymous, free, temporary — default 72h).

    Good for Instagram which only needs the URL for a few seconds while it
    pulls the image from Meta's CDN during the media-create API call.
    """
    if not path.exists():
        return None
    try:
        import requests
        time_map = {1: "1h", 12: "12h", 24: "24h", 72: "72h"}
        time_val = time_map.get(hours, "72h")
        with open(path, "rb") as fh:
            r = requests.post(
                "https://litterbox.catbox.moe/resources/internals/api.php",
                data={"reqtype": "fileupload", "time": time_val},
                files={"fileToUpload": (path.name, fh)},
                timeout=120,
            )
        if r.status_code == 200 and r.text.strip().startswith("http"):
            return r.text.strip()
    except Exception:
        return None
    return None


def public_url_via_base(path: Path) -> str | None:
    """Build a URL under PUBLIC_BASE_URL (e.g. https://xxx.ngrok.io).

    The FastAPI dashboard serves /media/<user_id>/<slot>/<filename>.
    Set PUBLIC_BASE_URL to the publicly-accessible address of your server.
    """
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        return None
    try:
        rel = path.resolve().relative_to((_REPO_ROOT / "queue").resolve())
    except Exception:
        return None
    # rel is "<user_id>/<slot>/<file>" or "<slot>/<file>"
    return f"{base}/media/{rel.as_posix()}"


def get_public_url(path: Path) -> tuple[str | None, str]:
    """Return (url, provider) — tries providers in order, first success wins.

    Order:
      1. PUBLIC_BASE_URL — fastest, no upload needed, requires ngrok/public server
      2. imgbb           — fast CDN, requires IMGBB_API_KEY
      3. litterbox       — free, no key, 72h (enough for Meta API pull)
      4. catbox          — free, no key, permanent
      5. 0x0.st          — free, no key, long-lived for small files
    """
    for fn, name in (
        (public_url_via_base, "base_url"),
        (upload_imgbb,        "imgbb"),
        (upload_litterbox,    "litterbox"),
        (upload_catbox,       "catbox"),
        (upload_0x0,          "0x0.st"),
    ):
        try:
            url = fn(path)  # type: ignore[arg-type]
        except Exception:
            url = None
        if url:
            print(f"[media_host] hosted via {name}: {url}")
            return url, name
    print("[media_host] all providers failed — set IMGBB_API_KEY or PUBLIC_BASE_URL")
    return None, ""
