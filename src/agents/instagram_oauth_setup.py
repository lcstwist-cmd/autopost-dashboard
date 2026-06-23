"""One-time setup for Instagram Graph API.

Unlike TikTok/YT this doesn't do an OAuth flow — Meta's Graph API Explorer
already hands you a short-lived User Token. This script:
  1. Takes a short-lived User Access Token (you paste it)
  2. Exchanges it for a long-lived (60-day) Page Access Token
  3. Finds the IG Business Account ID linked to the Facebook Page
  4. Prints what to put in .env

Prereqs:
  1. developers.facebook.com → My Apps → Create App → "Business"
  2. Add product: "Instagram Graph API"
  3. Link your Instagram Business/Creator account to a Facebook Page
     (Instagram app → Settings → Account → Switch to Professional Account)
  4. On Facebook Page settings, link the IG account
  5. Graph API Explorer → select your app → Get User Access Token with scopes:
       pages_show_list, pages_read_engagement, instagram_basic,
       instagram_content_publish, business_management
  6. Copy that short-lived token here.

Usage:
    python src/agents/instagram_oauth_setup.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

GRAPH = "https://graph.facebook.com/v22.0"


def main() -> int:
    import requests

    app_id     = os.environ.get("META_APP_ID", "").strip()     or input("META_APP_ID: ").strip()
    app_secret = os.environ.get("META_APP_SECRET", "").strip() or input("META_APP_SECRET: ").strip()
    short_tok  = input("Short-lived User Access Token from Graph API Explorer: ").strip()

    if not (app_id and app_secret and short_tok):
        print("Missing input. Aborting.", file=sys.stderr)
        return 1

    # ── Step 1: short-lived → long-lived user token ─────────────────────────
    r = requests.get(f"{GRAPH}/oauth/access_token", params={
        "grant_type":          "fb_exchange_token",
        "client_id":           app_id,
        "client_secret":       app_secret,
        "fb_exchange_token":   short_tok,
    }, timeout=30)
    if r.status_code != 200:
        print(f"Exchange failed HTTP {r.status_code}: {r.text}", file=sys.stderr)
        return 1
    long_user_tok = r.json().get("access_token", "")
    if not long_user_tok:
        print("No long-lived token returned.", file=sys.stderr)
        return 1
    print("✅ Long-lived user token obtained.")

    # ── Step 2: list pages the user manages ─────────────────────────────────
    r = requests.get(f"{GRAPH}/me/accounts",
                     params={"access_token": long_user_tok}, timeout=30)
    if r.status_code != 200:
        print(f"me/accounts failed HTTP {r.status_code}: {r.text}", file=sys.stderr)
        return 1
    pages = r.json().get("data", [])
    if not pages:
        print("No Facebook Pages on this account.", file=sys.stderr)
        return 1

    if len(pages) == 1:
        chosen = pages[0]
    else:
        print("Pages:")
        for i, p in enumerate(pages):
            print(f"  [{i}] {p['name']}  (id={p['id']})")
        idx = int(input("Choose page #: "))
        chosen = pages[idx]

    page_id        = chosen["id"]
    page_token     = chosen["access_token"]  # this is ALREADY long-lived
    print(f"✅ Page token for '{chosen['name']}' retrieved.")

    # ── Step 3: find the IG business account linked to the page ────────────
    r = requests.get(f"{GRAPH}/{page_id}", params={
        "fields":       "instagram_business_account",
        "access_token": page_token,
    }, timeout=30)
    ig = (r.json() or {}).get("instagram_business_account", {})
    ig_user_id = ig.get("id")
    if not ig_user_id:
        print("This Facebook Page has no linked Instagram Business account. "
              "Link it in the Page settings first.", file=sys.stderr)
        return 1

    print(f"✅ Instagram Business Account ID: {ig_user_id}")

    print("\n═════════════════════════════════════════════════════════════")
    print("  Add these to your .env  (or Settings → Instagram tab)")
    print("═════════════════════════════════════════════════════════════")
    print(f"IG_USER_ID={ig_user_id}")
    print(f"IG_ACCESS_TOKEN={page_token}")
    print("═════════════════════════════════════════════════════════════\n")
    print("Note: Page tokens on long-lived user tokens DO NOT EXPIRE — you're good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
