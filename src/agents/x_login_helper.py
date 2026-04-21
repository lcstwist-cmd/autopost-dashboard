"""X/Twitter session helper — saves cookies for headless publishing.

Three ways to get cookies:

  1. --close-chrome  (fastest)
     Close Chrome, run this script, reopen Chrome.
     Reads cookies directly from Chrome's SQLite database.

  2. --cookie-file PATH
     Export cookies manually with the Cookie-Editor Chrome extension:
       - Install: https://chrome.google.com/webstore/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm
       - Go to x.com, open Cookie-Editor, click Export → saves JSON to clipboard
       - Paste into a .json file, then run:
           python src/agents/x_login_helper.py --cookie-file cookies.json

  3. (no flag) — opens a stealth Playwright browser for manual login.
     May be blocked by X on some accounts. Try option 1 or 2 first.

Cookies saved to: src/agents/.x_browser_cookies.json
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes
import json
import os
import sqlite3
import time
from pathlib import Path

COOKIES_FILE = Path(__file__).resolve().parent / ".x_browser_cookies.json"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _get_aes_key() -> bytes:
    chrome_dir = Path(os.environ["LOCALAPPDATA"]) / "Google/Chrome/User Data"
    local_state = json.loads((chrome_dir / "Local State").read_text(encoding="utf-8"))
    enc_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])[5:]

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    enc_blob = DATA_BLOB(len(enc_key), ctypes.cast(ctypes.c_char_p(enc_key), ctypes.POINTER(ctypes.c_char)))
    dec_blob = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(enc_blob), None, None, None, None, 0, ctypes.byref(dec_blob)
    )
    if not ok:
        raise RuntimeError("DPAPI decrypt failed — try running as the same user who owns Chrome")
    return ctypes.string_at(dec_blob.pbData, dec_blob.cbData)


def _decrypt_value(enc: bytes, aes_key: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if enc[:3] in (b"v10", b"v11"):
        try:
            return AESGCM(aes_key).decrypt(enc[3:15], enc[15:], None).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return enc.decode("utf-8", errors="replace")


def _save(cookies: list[dict]) -> None:
    COOKIES_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    has_auth = any(c["name"] == "auth_token" for c in cookies)
    print(f"[x_login_helper] Saved {len(cookies)} cookies -> {COOKIES_FILE}")
    if not has_auth:
        print("[x_login_helper] WARNING: auth_token not found — are you logged in to x.com in that browser?")
    else:
        print("[x_login_helper] auth_token found. Run: python src/agents/publisher.py --publish --platforms x")


# ---------------------------------------------------------------------------
# Mode 1: read Chrome cookies (Chrome must be CLOSED)
# ---------------------------------------------------------------------------

def mode_close_chrome() -> None:
    """Launch Chrome real (channel=chrome) with user's Default profile.

    Chrome must be CLOSED. Playwright opens it headlessly, navigates to x.com,
    and reads the cookies that Chrome decrypts internally.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("playwright not installed — run: pip install playwright && playwright install chromium")

    chrome_user_data = Path(os.environ["LOCALAPPDATA"]) / "Google/Chrome/User Data"
    profile_dir = chrome_user_data / "Default"

    if not profile_dir.exists():
        raise SystemExit(f"[x_login_helper] Chrome Default profile not found at {profile_dir}")

    print("[x_login_helper] Launching Chrome with your profile (Chrome must be closed)...")
    print("[x_login_helper] Navigating to x.com to extract cookies...")

    with sync_playwright() as pw:
        try:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(chrome_user_data),
                channel="chrome",
                headless=False,
                ignore_default_args=["--enable-automation", "--disable-sync"],
                args=[
                    "--profile-directory=Default",
                    "--disable-blink-features=AutomationControlled",
                    "--window-position=-32000,-32000",
                ],
            )
        except Exception as exc:
            err = str(exc).lower()
            if "already running" in err or "user data directory" in err:
                raise SystemExit(
                    "[x_login_helper] Chrome is still running — close ALL Chrome windows first, then retry."
                )
            raise SystemExit(f"[x_login_helper] Failed to launch Chrome: {exc}")

        page = ctx.new_page()
        page.goto("https://x.com", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

        cookies = ctx.cookies(["https://x.com", "https://twitter.com"])
        ctx.close()

    if not cookies:
        raise SystemExit("[x_login_helper] No x.com cookies found — are you logged in to x.com in Chrome?")

    _save(cookies)


# ---------------------------------------------------------------------------
# Mode 2: import from Cookie-Editor JSON export
# ---------------------------------------------------------------------------

def mode_cookie_file(path: str) -> None:
    src = Path(path)
    if not src.exists():
        raise SystemExit(f"[x_login_helper] File not found: {path}")

    raw = json.loads(src.read_text(encoding="utf-8"))

    # Cookie-Editor exports as a list of objects with name/value/domain/path/...
    out = []
    for c in raw:
        out.append({
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", ".x.com"),
            "path": c.get("path", "/"),
            "secure": bool(c.get("secure", True)),
            "httpOnly": bool(c.get("httpOnly", False)),
            "sameSite": c.get("sameSite", "None"),
        })

    _save(out)


# ---------------------------------------------------------------------------
# Mode 3: stealth Playwright browser (manual login)
# ---------------------------------------------------------------------------

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
window.chrome = {runtime: {}};
"""


def mode_playwright() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("playwright not installed — run: pip install playwright && playwright install chromium")

    print("[x_login_helper] Opening stealth browser — log in to X/Twitter manually.")
    print(f"[x_login_helper] Cookies will be saved to: {COOKIES_FILE}")
    print()
    print("  TIP: use your email (lcstwist@gmail.com) if username does not advance.")
    print()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        ctx.add_init_script(STEALTH_JS)
        page = ctx.new_page()
        page.goto("https://x.com/login", wait_until="domcontentloaded", timeout=30000)

        print("[x_login_helper] Waiting up to 3 minutes for login...")
        deadline = time.time() + 180
        current_url = ""
        while time.time() < deadline:
            try:
                current_url = page.url
            except Exception:
                print("[x_login_helper] Browser closed early.")
                return
            if "/home" in current_url:
                break
            time.sleep(2)
        else:
            print(f"[x_login_helper] Timed out. Last URL: {current_url!r}")
            browser.close()
            return

        time.sleep(1.5)
        _save(ctx.cookies())
        browser.close()


# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--close-chrome", action="store_true",
                     help="import from Chrome (Chrome must be closed first)")
    grp.add_argument("--cookie-file", metavar="PATH",
                     help="import from Cookie-Editor JSON export")
    args = ap.parse_args()

    if args.close_chrome:
        mode_close_chrome()
    elif args.cookie_file:
        mode_cookie_file(args.cookie_file)
    else:
        mode_playwright()


if __name__ == "__main__":
    main()
