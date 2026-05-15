"""Replicate AI avatar provider — SadTalker / Wav2Lip.

Why Replicate is the new primary:
  * ~$0.01-0.05 per video (vs D-ID's $0.20+)
  * Free credits on signup ($1-5 → ~25-500 clips)
  * Open-source models (no vendor lock-in)
  * Public REST API — same auth pattern as D-ID

Setup (one-time):
  1. Create account at https://replicate.com
  2. Generate API token at https://replicate.com/account/api-tokens
  3. Add to .env:  REPLICATE_API_TOKEN=r8_xxxxxxxx

Model selection:
  - default: cjwbw/sadtalker (talking-head from photo+audio, full head)
  - alt:     devxpy/cog-wav2lip (lip-sync only — faster, less expressive)

We pre-render the narration via edge-tts (free, no extra cost), upload it
to a public temp host (or pass a hosted URL), then call SadTalker which
returns an MP4 of the photo speaking the audio.

Audio hosting trick: Replicate's API accepts data: URIs for small files
(< 5 MB), so we base64-inline the TTS mp3. No need for S3/imgbb.
"""
from __future__ import annotations

import asyncio
import base64
import os
import time
from pathlib import Path
from typing import Any

import requests


# ---------------------------------------------------------------------------
# Model versions — update these when Replicate publishes newer hashes.
# ---------------------------------------------------------------------------
# SadTalker — produces the most natural talking-head animation.
# Find the latest version hash at https://replicate.com/cjwbw/sadtalker
SADTALKER_VERSION = (
    "3aa3dac9353cc4d6bd62a8f95957bd844003b401ca4e4a9b33baa574c549d376"
)
# Wav2Lip — lip-sync only, faster, lower compute cost. Used as Replicate-internal
# fallback if SadTalker is over capacity.
WAV2LIP_VERSION = (
    "8d65e3f4f4298520e079198b493c25adfc43c058ffec924f2aefc8010ed25eef"
)


# ---------------------------------------------------------------------------
# Presenter portrait URLs (same set as D-ID — they're public S3 photos)
# ---------------------------------------------------------------------------
PRESENTER_PHOTOS: dict[str, str] = {
    # EMD brand presenter — white male in business suit (matt.jpeg)
    "default":   "https://d-id-public-bucket.s3.amazonaws.com/matt.jpeg",
    "emd":       "https://d-id-public-bucket.s3.amazonaws.com/matt.jpeg",
    "emd_brand": "https://d-id-public-bucket.s3.amazonaws.com/matt.jpeg",
    "matt":      "https://d-id-public-bucket.s3.amazonaws.com/matt.jpeg",
    "josh":     "https://d-id-public-bucket.s3.amazonaws.com/josh.jpeg",
    "damian":   "https://d-id-public-bucket.s3.amazonaws.com/damian.jpeg",
    "obama":    "https://d-id-public-bucket.s3.amazonaws.com/obama.jpeg",
    "einstein": "https://d-id-public-bucket.s3.amazonaws.com/einstein.jpeg",
    "alice":    "https://d-id-public-bucket.s3.amazonaws.com/alice.jpg",
    "amy":      "https://d-id-public-bucket.s3.amazonaws.com/amy.jpg",
    "anita":    "https://d-id-public-bucket.s3.amazonaws.com/anita.jpg",
    "noelle":   "https://d-id-public-bucket.s3.amazonaws.com/noelle.jpeg",
    # Aliases that match D-ID's so users don't have to re-pick a presenter
    # when switching providers.
    "alex_suit": "https://d-id-public-bucket.s3.amazonaws.com/matt.jpeg",
    "william":   "https://d-id-public-bucket.s3.amazonaws.com/matt.jpeg",
    "ethan":     "https://d-id-public-bucket.s3.amazonaws.com/matt.jpeg",
    "lucas":     "https://d-id-public-bucket.s3.amazonaws.com/josh.jpeg",
    "darren":    "https://d-id-public-bucket.s3.amazonaws.com/damian.jpeg",
    "sophia":    "https://d-id-public-bucket.s3.amazonaws.com/amy.jpg",
    "diana":     "https://d-id-public-bucket.s3.amazonaws.com/anita.jpg",
    "lana":      "https://d-id-public-bucket.s3.amazonaws.com/alice.jpg",
}


REPLICATE_API = "https://api.replicate.com/v1"


def _resolve_presenter_url(presenter: str) -> str:
    """Resolve presenter name → public photo URL.

    Priority chain (highest first):
      1. EMD_PRESENTER_URL          — cross-provider EMD brand override
      2. REPLICATE_PRESENTER_URL    — Replicate-specific override
      3. DID_PRESENTER_URL          — D-ID-specific override (reused so
                                      users don't set two URLs)
      4. PRESENTER_PHOTOS[<name>]   — built-in (default = EMD brand =
                                      white male in business suit)
    """
    emd_url = (os.environ.get("EMD_PRESENTER_URL") or "").strip()
    if emd_url:
        return emd_url
    env_url = (os.environ.get("REPLICATE_PRESENTER_URL") or "").strip()
    if env_url:
        return env_url
    did_url = (os.environ.get("DID_PRESENTER_URL") or "").strip()
    if did_url:
        return did_url
    return PRESENTER_PHOTOS.get(
        (presenter or "default").lower(),
        PRESENTER_PHOTOS["default"],
    )


def _render_tts(text: str, out_path: Path) -> Path:
    """Use edge-tts (free) to render narration to MP3.

    Re-uses the same voice as D-ID (en-US-GuyNeural — male, professional).
    """
    voice = os.environ.get("AVATAR_VOICE", "en-US-GuyNeural")
    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError(
            "edge-tts is required for the Replicate provider. "
            "Run: pip install edge-tts"
        ) from exc

    async def _save() -> None:
        comm = edge_tts.Communicate(text, voice)
        await comm.save(str(out_path))

    asyncio.run(_save())
    return out_path


def _to_data_uri(path: Path, mime: str = "audio/mpeg") -> str:
    """Encode a small file as a data: URI for Replicate's input field.

    Replicate's runtime accepts data URIs up to ~5 MB. A 20s TTS mp3 is
    typically <300 KB, so this is comfortable headroom.
    """
    raw = path.read_bytes()
    if len(raw) > 5 * 1024 * 1024:
        raise ValueError(
            f"TTS file too large for data URI ({len(raw)} bytes). "
            "Configure cloud upload or shorten narration."
        )
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


class ReplicateAvatarProvider:
    """Replicate SadTalker provider."""

    name = "replicate"

    def __init__(self) -> None:
        self.token = (os.environ.get("REPLICATE_API_TOKEN") or "").strip()

    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        if not self.token:
            return False
        # Cheap pre-flight: the model versions are fixed; we don't hit the
        # network on is_available() to keep startup snappy. The actual job
        # call below will surface auth errors clearly.
        return True

    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Token {self.token}",
            "Content-Type": "application/json",
        }

    def _create_prediction(self, version: str, payload: dict[str, Any]) -> dict:
        resp = requests.post(
            f"{REPLICATE_API}/predictions",
            headers=self._headers(),
            json={"version": version, "input": payload},
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Replicate create failed [{resp.status_code}]: "
                f"{resp.text[:300]}"
            )
        return resp.json()

    def _poll(self, prediction_id: str, *, timeout_s: int = 600) -> str:
        """Poll until the prediction finishes; return the output MP4 URL."""
        url = f"{REPLICATE_API}/predictions/{prediction_id}"
        start = time.time()
        delay = 1.5
        while True:
            r = requests.get(url, headers=self._headers(), timeout=20)
            r.raise_for_status()
            data = r.json()
            status = data.get("status")
            if status == "succeeded":
                output = data.get("output")
                # SadTalker returns a single URL string; some models return list.
                if isinstance(output, list) and output:
                    return output[0]
                if isinstance(output, str):
                    return output
                raise RuntimeError(f"Unexpected output shape: {output!r}")
            if status in ("failed", "canceled"):
                err = data.get("error") or data.get("logs", "")
                raise RuntimeError(f"Replicate {status}: {str(err)[:300]}")
            if time.time() - start > timeout_s:
                raise TimeoutError(
                    f"Replicate polling timed out after {timeout_s}s "
                    f"(last status={status})"
                )
            print(f"[replicate] status={status} elapsed={int(time.time()-start)}s")
            time.sleep(delay)
            delay = min(delay * 1.3, 6.0)

    def _download(self, url: str, out_path: Path) -> None:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("wb") as fh:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        fh.write(chunk)

    # ------------------------------------------------------------------
    def generate(
        self,
        text: str,
        out_path: Path,
        presenter: str = "default",
        audio_path: Path | None = None,
    ) -> Path:
        if not self.is_available():
            raise RuntimeError("REPLICATE_API_TOKEN not set")

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # 1) Resolve TTS audio (use caller-supplied if provided)
        if audio_path and Path(audio_path).exists():
            audio_file = Path(audio_path)
            print(f"[replicate] reusing TTS: {audio_file.name}")
        else:
            audio_file = out_path.parent / "_replicate_tts.mp3"
            print(f"[replicate] rendering TTS via edge-tts -> {audio_file.name}")
            _render_tts(text, audio_file)

        # 2) Resolve presenter photo URL
        photo_url = _resolve_presenter_url(presenter)
        print(f"[replicate] presenter URL: {photo_url}")

        # 3) Encode audio as data URI (works for clips < 5 MB)
        try:
            audio_input = _to_data_uri(audio_file, mime="audio/mpeg")
        except ValueError as exc:
            raise RuntimeError(
                f"Audio too large for data URI: {exc}. "
                "Configure REPLICATE_AUDIO_HOST or shorten narration."
            )

        # 4) Submit SadTalker job
        # SadTalker input schema:
        #   source_image (URL/data-uri), driven_audio (URL/data-uri),
        #   preprocess: 'crop' | 'full' | 'extcrop' | 'extfull'
        #   still: bool   pose_style: int(0-46)   facerender: 'facevid2vid'|'pirender'
        sadtalker_input: dict[str, Any] = {
            "source_image": photo_url,
            "driven_audio": audio_input,
            "preprocess": "full",     # full body crop — keeps shoulders visible
            "still": False,           # let head move naturally
            "facerender": "facevid2vid",
            "enhancer": "gfpgan",     # face quality boost (default in model)
        }
        print(f"[replicate] submitting SadTalker job...")
        prediction = self._create_prediction(SADTALKER_VERSION, sadtalker_input)
        pred_id = prediction.get("id", "?")
        print(f"[replicate] prediction id={pred_id}")

        try:
            video_url = self._poll(pred_id)
        except RuntimeError as exc:
            # If SadTalker is overloaded/failed, attempt Wav2Lip as a
            # within-Replicate fallback (cheaper, faster, less expressive).
            print(f"[replicate] SadTalker failed: {exc}; trying Wav2Lip...")
            wav2lip_input = {
                "face": photo_url,
                "audio": audio_input,
                "pads": "0 10 0 0",
                "smooth": True,
                "fps": 25,
            }
            prediction = self._create_prediction(WAV2LIP_VERSION, wav2lip_input)
            pred_id = prediction.get("id", "?")
            print(f"[replicate] wav2lip prediction id={pred_id}")
            video_url = self._poll(pred_id)

        # 5) Download
        print(f"[replicate] downloading -> {out_path.name}")
        self._download(video_url, out_path)
        size_kb = out_path.stat().st_size // 1024
        print(f"[replicate] avatar saved -> {out_path.name} ({size_kb} KB)")
        return out_path
