"""Set default clip durations for all users.

Run this ONCE after the HeyGen pipeline fixes:
    python set_default_durations.py

It updates user_settings:
    video_duration_tiktok          -> 20 sec
    video_duration_instagram       -> 20 sec
    video_duration_youtube_shorts  -> 30 sec

If the server is running, retries until the WAL lock clears.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

DB = Path(__file__).resolve().parent / "autopost.db"

DEFAULTS = {
    "video_duration_tiktok":         20,
    "video_duration_instagram":      20,
    "video_duration_youtube_shorts": 30,
}


def main() -> int:
    if not DB.exists():
        print(f"[!] DB not found: {DB}", file=sys.stderr)
        return 2

    set_clause = ", ".join(f"{k}=?" for k in DEFAULTS)
    params = list(DEFAULTS.values())

    last_err = None
    for attempt in range(1, 11):
        try:
            con = sqlite3.connect(str(DB), timeout=15)
            con.execute("PRAGMA busy_timeout=15000")
            cur = con.cursor()
            cur.execute(f"UPDATE user_settings SET {set_clause}", params)
            con.commit()
            print(f"[ok] updated {cur.rowcount} user(s) -> {DEFAULTS}")
            print("[ok] now restart the dashboard so the running scheduler "
                  "picks up the new defaults.")
            con.close()
            return 0
        except sqlite3.OperationalError as exc:
            last_err = exc
            print(f"[retry {attempt}/10] {exc}", file=sys.stderr)
            time.sleep(2)
    print(f"[!] giving up after 10 retries: {last_err}", file=sys.stderr)
    print("    Stop the dashboard server (close the cmd window or "
          "stop start.bat) and run again.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
