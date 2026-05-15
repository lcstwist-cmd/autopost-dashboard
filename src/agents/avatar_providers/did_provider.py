"""D-ID avatar provider — wraps the existing /talks integration.

Demoted from primary to fallback (ordering decided in __init__.py).
The implementation lives in `src.agents.avatar_video.generate_avatar_clip`
which already handles auth-variant retries, presenter URL upload, and
result-URL polling. We expose it through the AvatarProvider interface so
the chain orchestrator can drive it the same way as Replicate.

Availability check: returns True only if DID_API_KEY (or DID_USERNAME +
DID_PASSWORD) is in the environment — that prevents the chain from
wasting an HTTP round-trip when the user hasn't configured D-ID at all.
"""
from __future__ import annotations

import os
from pathlib import Path


class DIDAvatarProvider:
    name = "did"

    def is_available(self) -> bool:
        # Either an API key OR a username/password pair counts as "configured"
        if (os.environ.get("DID_API_KEY") or "").strip():
            return True
        if (os.environ.get("DID_USERNAME") or "").strip() and \
           (os.environ.get("DID_PASSWORD") or "").strip():
            return True
        return False

    def generate(
        self,
        text: str,
        out_path: Path,
        presenter: str = "default",
        audio_path: Path | None = None,  # D-ID renders TTS internally
    ) -> Path:
        # Lazy import — keeps the chain orchestrator import light and avoids
        # circular imports if someone calls into avatar_providers from
        # within avatar_video itself.
        from src.agents.avatar_video import generate_avatar_clip
        return generate_avatar_clip(text, out_path, presenter=presenter)
