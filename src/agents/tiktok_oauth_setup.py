"""One-time OAuth bootstrap for TikTok Content Posting API.

Run ONCE to exchange an auth code for access_token + refresh_token.

Usage:
    python src/agents/tiktok_oauth_setup.py

Prereqs:
    1. Go to developers.tiktok.com → Manage apps → Create an app
    2. Add products: "Login Kit", "Content Posting API"
    3. Configure redirect URL: http://localhost:8765/callback
    4. Add scopes: video.upload, video.publish
    5. Copy Client Key + Client Secret into the prompt below
"""
from __future__ import annotations

import http.server
import os
import socketserver
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

AUTH_URL     = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL    = "https://open.tiktokapis.com/v2/oauth/token/"
REDIRECT_URI = "http://localhost:8765/callback"
SCOPES       = "user.info.basic,video.upload,video.publish"


def _capture_code() -> str:
    code_holder: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            code_holder["code"] = (params.get("code") or [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h1>OK!</h1><p>Close tab, back to terminal.</p>")

        def log_message(self, *_a):
            return

    with socketserver.TCPServer(("localhost", 8765), Handler) as httpd:
        while "code" not in code_holder:
            httpd.handle_request()
    return code_holder["code"]


def main() -> int:
    import requests

    ck = os.environ.get("TIKTOK_CLIENT_KEY", "").strip() or input("TIKTOK_CLIENT_KEY: ").strip()
    cs = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip() or input("TIKTOK_CLIENT_SECRET: ").strip()
    if not ck or not cs:
        print("Missing client key/secret.", file=sys.stderr)
        return 1

    params = {
        "client_key":    ck,
        "scope":         SCOPES,
        "response_type": "code",
        "redirect_uri":  REDIRECT_URI,
        "state":         "autopost",
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print(f"\nOpening browser:\n  {url}\n")
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    print("Log in with the TikTok account you want to post to, approve perms.\n")

    code = _capture_code()
    print("✅ Authorization code received.")

    r = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key":    ck,
            "client_secret": cs,
            "code":          code,
            "grant_type":    "authorization_code",
            "redirect_uri":  REDIRECT_URI,
        },
        timeout=30,
    )
    if r.status_code != 200:
        print(f"Token exchange failed HTTP {r.status_code}: {r.text}", file=sys.stderr)
        return 1
    d = r.json()
    access  = d.get("access_token")
    refresh = d.get("refresh_token")
    if not access or not refresh:
        print(f"No tokens in response: {d}", file=sys.stderr)
        return 1

    print("\n═════════════════════════════════════════════════════════════")
    print("  Add these to your .env  (or Settings → TikTok tab)")
    print("═════════════════════════════════════════════════════════════")
    print(f"TIKTOK_CLIENT_KEY={ck}")
    print(f"TIKTOK_CLIENT_SECRET={cs}")
    print(f"TIKTOK_ACCESS_TOKEN={access}")
    print(f"TIKTOK_REFRESH_TOKEN={refresh}")
    print("═════════════════════════════════════════════════════════════\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
