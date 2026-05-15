"""One-time OAuth bootstrap for YouTube Data API v3.

Run this ONCE to obtain a long-lived refresh token, then drop it in .env.

Usage:
    python src/agents/youtube_oauth_setup.py

Prereqs:
    1. Google Cloud Console → Create project
    2. APIs & Services → Library → enable 'YouTube Data API v3'
    3. APIs & Services → Credentials → Create OAuth Client ID → Desktop app
    4. Download the JSON, or copy client_id + client_secret
    5. Add your Google account as a Test user on the OAuth consent screen
       (no verification needed while in Testing mode — tokens are valid 7 days
       unless you Publish the app, in which case they're permanent)
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

SCOPES      = "https://www.googleapis.com/auth/youtube.upload"
AUTH_URL    = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL   = "https://oauth2.googleapis.com/token"
REDIRECT_URI = "http://localhost:8765/callback"


def _capture_code() -> str:
    """Spin up a tiny localhost server to capture the OAuth redirect."""
    code_holder: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            code_holder["code"] = (params.get("code") or [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<h1>OK!</h1><p>You can close this tab and return to the terminal.</p>"
            )

        def log_message(self, *_a):  # suppress console spam
            return

    with socketserver.TCPServer(("localhost", 8765), Handler) as httpd:
        while "code" not in code_holder:
            httpd.handle_request()
    return code_holder["code"]


def main() -> int:
    import requests  # late import so import error has pip hint

    client_id     = os.environ.get("YT_CLIENT_ID", "").strip()
    client_secret = os.environ.get("YT_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        client_id     = input("YT_CLIENT_ID: ").strip()
        client_secret = input("YT_CLIENT_SECRET: ").strip()
    if not client_id or not client_secret:
        print("Missing client id / secret. Aborting.", file=sys.stderr)
        return 1

    # Open consent screen
    params = {
        "client_id":     client_id,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         SCOPES,
        "access_type":   "offline",
        "prompt":        "consent",
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print(f"\nOpening browser to:\n  {url}\n")
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    print("Log in with the Google account that owns the YouTube channel,")
    print("grant the requested permissions, then come back here.\n")

    code = _capture_code()
    print("✅ Authorization code received.")

    # Exchange for refresh token
    r = requests.post(TOKEN_URL, data={
        "code":          code,
        "client_id":     client_id,
        "client_secret": client_secret,
        "redirect_uri":  REDIRECT_URI,
        "grant_type":    "authorization_code",
    }, timeout=30)
    if r.status_code != 200:
        print(f"Token exchange failed HTTP {r.status_code}: {r.text}", file=sys.stderr)
        return 1

    data = r.json()
    refresh = data.get("refresh_token")
    access  = data.get("access_token")

    if not refresh:
        print("No refresh_token returned. Go to "
              "https://myaccount.google.com/permissions and REVOKE this app, "
              "then re-run this script.", file=sys.stderr)
        return 1

    print("\n═════════════════════════════════════════════════════════════")
    print("  Add these to your .env  (Settings → YouTube tab in dashboard)")
    print("═════════════════════════════════════════════════════════════")
    print(f"YT_CLIENT_ID={client_id}")
    print(f"YT_CLIENT_SECRET={client_secret}")
    print(f"YT_REFRESH_TOKEN={refresh}")
    print("═════════════════════════════════════════════════════════════\n")
    print(f"(short-lived access_token for testing: {access[:40]}...)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
