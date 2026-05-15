"""AI Avatar provider chain — pluggable talking-head backends.

Provider order (preference, top wins):
  1. Replicate (SadTalker / Wav2Lip) — open-source, ~$0.01-0.05 per video
  2. D-ID /talks API                  — commercial, ~$0.20+ per video
  3. branded-no-avatar                — graceful fallback handled by avatar_video

Each provider exposes:
    name: str
    is_available() -> bool
    generate(text: str, out_path: Path, presenter: str = "default",
             audio_path: Path | None = None) -> Path

`audio_path` is optional — providers that synthesize audio internally
(D-ID) ignore it; providers that need a pre-rendered TTS track (Replicate
SadTalker) will create one if not supplied.

Selection:
    AVATAR_PROVIDER env var ("replicate" / "did" / "auto") — "auto" is the
    default and walks the chain in order.
"""
from __future__ import annotations

import os
import traceback
from pathlib import Path
from typing import Iterable, Protocol


class AvatarProvider(Protocol):
    name: str
    def is_available(self) -> bool: ...
    def generate(
        self,
        text: str,
        out_path: Path,
        presenter: str = "default",
        audio_path: Path | None = None,
    ) -> Path: ...


def _load_providers() -> list[AvatarProvider]:
    """Instantiate the provider chain, skipping ones that fail to import."""
    providers: list[AvatarProvider] = []
    # 1) Replicate (SadTalker — best quality, needs API token)
    try:
        from .replicate_provider import ReplicateAvatarProvider
        providers.append(ReplicateAvatarProvider())
    except Exception as exc:
        print(f"[avatar_providers] replicate import failed: {exc}")
    # 2) D-ID (commercial, needs API key)
    try:
        from .did_provider import DIDAvatarProvider
        providers.append(DIDAvatarProvider())
    except Exception as exc:
        print(f"[avatar_providers] d-id import failed: {exc}")
    # 3) Local (always available — PIL + numpy + FFmpeg, no API needed)
    try:
        from .local_provider import LocalAvatarProvider
        providers.append(LocalAvatarProvider())
    except Exception as exc:
        print(f"[avatar_providers] local import failed: {exc}")
    return providers


def get_provider_chain() -> list[AvatarProvider]:
    """Return providers ordered by user preference + availability.

    AVATAR_PROVIDER env var:
      "replicate"  -> replicate first, then everything else as fallback
      "did"        -> d-id first, then everything else as fallback
      "auto"/""    -> default order (replicate -> d-id)
    """
    chain = _load_providers()
    pref = (os.environ.get("AVATAR_PROVIDER") or "auto").strip().lower()
    if pref in ("auto", ""):
        return chain
    # Move the preferred provider to the front, keep the rest in order.
    name_to_provider = {p.name: p for p in chain}
    if pref in name_to_provider:
        head = name_to_provider[pref]
        rest = [p for p in chain if p.name != pref]
        return [head, *rest]
    print(f"[avatar_providers] unknown AVATAR_PROVIDER={pref!r}, using auto order")
    return chain


def generate_with_chain(
    text: str,
    out_path: Path,
    presenter: str = "default",
    audio_path: Path | None = None,
    status: dict | None = None,
) -> tuple[Path, str] | None:
    """Try each available provider in order. Return (path, provider_name) on
    success, or None if every provider failed/was unavailable.

    `status` (optional dict) — if provided, the chain populates it with per-
    provider attempt records so the caller can persist diagnostics:
        status['attempts'] = [
            {'name': 'replicate', 'available': True, 'ok': False,
             'error': 'auth: 401 Unauthorized', 'elapsed_s': 1.2},
            {'name': 'did', 'available': True, 'ok': True, 'elapsed_s': 18.4},
        ]
        status['winner']    = 'did'        # or None
        status['summary']   = 'replicate skipped — REPLICATE_API_TOKEN not set; ...'

    The caller can then route to the branded-no-avatar fallback when this
    returns None — that keeps the slot from going dark when both APIs fail.
    """
    import time as _time

    if status is not None:
        status.setdefault("attempts", [])

    chain = get_provider_chain()
    if not chain:
        print("[avatar_providers] no providers loaded")
        if status is not None:
            status["winner"] = None
            status["summary"] = "no avatar providers loaded (import errors?)"
        return None
    last_error: str | None = None
    summary_parts: list[str] = []
    for provider in chain:
        attempt: dict = {"name": provider.name, "available": False, "ok": False}
        if not provider.is_available():
            reason = _explain_unavailability(provider.name)
            attempt["error"] = reason
            print(f"[avatar_providers] skipping {provider.name}: {reason}")
            summary_parts.append(f"{provider.name}: skipped ({reason})")
            if status is not None:
                status["attempts"].append(attempt)
            continue
        attempt["available"] = True
        print(f"[avatar_providers] trying provider: {provider.name}")
        t0 = _time.time()
        try:
            result = provider.generate(
                text, out_path,
                presenter=presenter,
                audio_path=audio_path,
            )
            attempt["ok"] = True
            attempt["elapsed_s"] = round(_time.time() - t0, 2)
            print(f"[avatar_providers] success via {provider.name} "
                  f"({attempt['elapsed_s']}s)")
            if status is not None:
                status["attempts"].append(attempt)
                status["winner"] = provider.name
                status["summary"] = (
                    "; ".join(summary_parts + [f"{provider.name}: ok"])
                )
            return (result, provider.name)
        except Exception as exc:
            err_msg = str(exc)[:300]
            attempt["error"] = err_msg
            attempt["elapsed_s"] = round(_time.time() - t0, 2)
            last_error = f"{provider.name}: {err_msg}"
            print(f"[avatar_providers] {provider.name} failed: {err_msg}")
            traceback.print_exc()
            summary_parts.append(f"{provider.name}: FAIL ({err_msg[:120]})")
            if status is not None:
                status["attempts"].append(attempt)
            continue
    print(f"[avatar_providers] all providers failed (last error: {last_error})")
    if status is not None:
        status["winner"] = None
        status["summary"] = "; ".join(summary_parts) or "no providers configured"
    return None


def _explain_unavailability(provider_name: str) -> str:
    """Human-readable hint for why a provider was skipped.

    The dashboard surfaces this in the avatar status card so the user can
    see exactly which env var they still need to set.
    """
    name = (provider_name or "").lower()
    if name == "replicate":
        return ("REPLICATE_API_TOKEN not set — add it via Settings → "
                "AI Avatar (or .env)")
    if name == "did":
        return ("D-ID credentials missing — set DID_API_KEY (or "
                "DID_USERNAME + DID_PASSWORD) via Settings → AI Avatar")
    if name == "local":
        return "PIL or numpy not installed (pip install Pillow numpy)"
    return "not configured"
