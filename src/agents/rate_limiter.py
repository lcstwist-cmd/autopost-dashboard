"""rate_limiter.py — Per-platform rate limiting to prevent API bans.

Tracks the last successful post time per platform per user and enforces
minimum delays. Uses a file-based state so it works across processes/threads.

Used by publisher.py before each platform publish call.

Usage:
    from src.agents.rate_limiter import RateLimiter
    rl = RateLimiter(user_id=1)
    wait_secs = rl.wait_if_needed("x")     # blocks if too soon, returns actual wait
    # ... publish ...
    rl.mark_posted("x")                    # call after successful post
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_HERE  = Path(__file__).resolve().parent
_REPO  = _HERE.parent.parent

# Minimum seconds between posts per platform (per user account)
MIN_DELAY: dict[str, int] = {
    "telegram":  10,
    "x":         300,    # 5 min — X is strict about burst posting
    "instagram": 600,    # 10 min
    "tiktok":    600,    # 10 min
    "youtube":   900,    # 15 min
}

_STATE_DIR = _REPO / "data" / "rate_limits"


class RateLimiter:
    def __init__(self, user_id: int = 0):
        self.user_id   = user_id
        self._state_file = _STATE_DIR / f"user_{user_id}.json"
        _STATE_DIR.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, Any]:
        if not self._state_file.exists():
            return {}
        try:
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(self, state: dict[str, Any]) -> None:
        self._state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def seconds_until_ready(self, platform: str) -> float:
        """Return how many seconds to wait before this platform is ready. 0 = ready now."""
        state   = self._load()
        last_ts = state.get(f"last_{platform}", 0)
        delay   = MIN_DELAY.get(platform, 60)
        elapsed = time.time() - last_ts
        wait    = delay - elapsed
        return max(0.0, wait)

    def wait_if_needed(self, platform: str) -> float:
        """Block until platform is ready. Returns seconds waited."""
        wait = self.seconds_until_ready(platform)
        if wait > 0:
            print(f"[rate_limiter] waiting {wait:.0f}s before posting to {platform} (user {self.user_id})")
            time.sleep(wait)
        return wait

    def mark_posted(self, platform: str) -> None:
        """Record a successful post — resets the cooldown timer."""
        state = self._load()
        state[f"last_{platform}"] = time.time()
        self._save(state)

    def is_ready(self, platform: str) -> bool:
        return self.seconds_until_ready(platform) == 0

    def status(self) -> dict[str, Any]:
        """Return readiness status for all platforms."""
        out = {}
        for plat, delay in MIN_DELAY.items():
            secs = self.seconds_until_ready(plat)
            out[plat] = {
                "ready":        secs == 0,
                "wait_seconds": round(secs),
                "min_delay":    delay,
            }
        return out


# Global convenience — used by publisher when user_id isn't easily accessible
_default_limiter: dict[int, RateLimiter] = {}

def get_limiter(user_id: int = 0) -> RateLimiter:
    if user_id not in _default_limiter:
        _default_limiter[user_id] = RateLimiter(user_id)
    return _default_limiter[user_id]


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(_REPO))
    rl = RateLimiter(user_id=0)
    print("Rate limit status (user 0):")
    for plat, info in rl.status().items():
        ready = "READY" if info["ready"] else f"wait {info['wait_seconds']}s"
        print(f"  {plat:<12} {ready}  (min delay: {info['min_delay']}s)")
