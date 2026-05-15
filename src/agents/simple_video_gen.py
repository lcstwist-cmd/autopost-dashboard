"""simple_video_gen.py — ffmpeg-based video generator for TikTok / YouTube Shorts.

Creates a 20-second 9:16 vertical MP4 from a still image using a Ken Burns
zoom effect and optional text caption. Requires only ffmpeg in PATH — no
Python video library (moviepy, OpenCV) needed.

Output: `<slot_dir>/reel_simple.mp4`  (1080×1920, H.264, AAC-silent or with TTS)

Usage from pipeline:
    from src.agents.simple_video_gen import build_simple_video
    out = build_simple_video(slot_dir)  # returns Path or None on failure
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

_FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
_DURATION = 20          # seconds — TikTok / Shorts sweet spot
_TARGET_W  = 1080
_TARGET_H  = 1920
_FPS       = 30
_CRF       = 23         # H.264 quality (lower = better, larger file)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_image(slot_dir: Path) -> Path | None:
    """Return the best available image for the video background."""
    for name in (
        "image_reel_1080x1920.png",   # already vertical — perfect
        "image_tg_1080x1080.png",     # square — will be padded
        "image_ig_1080x1350.png",     # 4:5 — will be padded
        "image_x_1200x675.png",       # landscape — will be padded
    ):
        p = slot_dir / name
        if p.exists():
            return p
    # Any PNG in the slot
    for p in sorted(slot_dir.glob("*.png")):
        return p
    return None


def _find_audio(slot_dir: Path) -> Path | None:
    """Return existing TTS/narration audio if present."""
    for name in ("narration.mp3", "tts.mp3", "audio.mp3",
                 "narration.wav", "tts.wav"):
        p = slot_dir / name
        if p.exists():
            return p
    return None


def _read_caption(slot_dir: Path) -> str:
    """Read the first ~120 chars of post_x.txt as caption overlay."""
    for name in ("post_x.txt", "post_telegram.md", "summary.txt"):
        p = slot_dir / name
        if p.exists():
            text = p.read_text(encoding="utf-8", errors="replace").strip()
            # Strip markdown-style headers
            lines = [l for l in text.splitlines() if l.strip() and not l.startswith("#")]
            first = " ".join(lines[:3])
            # Wrap to ~35 chars per line, max 3 lines
            wrapped = "\n".join(textwrap.wrap(first[:120], 35)[:3])
            return wrapped
    return ""


def _run(cmd: list[str], label: str) -> bool:
    """Run a subprocess command, return True on success."""
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        if result.returncode != 0:
            print(f"[simple_video] {label} failed (exit {result.returncode})")
            print(result.stderr.decode("utf-8", errors="replace")[-400:])
            return False
        return True
    except Exception as exc:
        print(f"[simple_video] {label} error: {exc}")
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_simple_video(slot_dir: Path) -> Path | None:
    """Create reel_simple.mp4 from the best available image in slot_dir.

    Steps:
      1. Pick source image (vertical preferred, square/landscape padded)
      2. Apply Ken Burns zoom-in effect (zoompan filter)
      3. Overlay caption text at bottom third
      4. Mix with existing narration.mp3 if present, else silent
      5. Output 1080×1920 H.264 @ 30fps, 20s max

    Returns the output Path on success, None on failure.
    """
    slot_dir = Path(slot_dir)
    out_path = slot_dir / "reel_simple.mp4"

    # Skip if already generated (and newer than the source image)
    img = _find_image(slot_dir)
    if img is None:
        print(f"[simple_video] no source image in {slot_dir} — skipping")
        return None

    if out_path.exists() and out_path.stat().st_mtime > img.stat().st_mtime:
        print(f"[simple_video] {out_path.name} up-to-date — skipping")
        return out_path

    audio = _find_audio(slot_dir)
    print(f"[simple_video] building: image={img.name} audio={bool(audio)}")

    # Stable filter: scale to fill 1080x1920, crop to exact size, set fps.
    # Avoid zoompan (crashes on Windows ffmpeg) and drawtext (needs fontconfig).
    vf = (
        f"scale={_TARGET_W}:{_TARGET_H}:force_original_aspect_ratio=increase,"
        f"crop={_TARGET_W}:{_TARGET_H},"
        f"fps={_FPS}"
    )

    # Build ffmpeg command
    if audio:
        cmd = [
            _FFMPEG, "-y",
            "-loop", "1", "-i", str(img),
            "-i", str(audio),
            "-t", str(_DURATION),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", str(_CRF),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest",
            "-map", "0:v:0", "-map", "1:a:0",
            str(out_path),
        ]
    else:
        cmd = [
            _FFMPEG, "-y",
            "-loop", "1", "-i", str(img),
            "-t", str(_DURATION),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", str(_CRF),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-an",
            str(out_path),
        ]

    if not _run(cmd, "ffmpeg"):
        # Fallback: absolute minimum — no scale tricks, no audio
        cmd_min = [
            _FFMPEG, "-y",
            "-loop", "1", "-i", str(img),
            "-t", str(_DURATION),
            "-vf", f"scale={_TARGET_W}:{_TARGET_H},fps={_FPS}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
            str(out_path),
        ]
        if not _run(cmd_min, "ffmpeg-minimal"):
            return None

    size_mb = out_path.stat().st_size / 1_048_576
    print(f"[simple_video] done -> {out_path.name} ({size_mb:.1f} MB, {_DURATION}s)")
    return out_path


# ---------------------------------------------------------------------------
# CLI helper
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python simple_video_gen.py <slot_dir>")
        sys.exit(1)
    result = build_simple_video(Path(sys.argv[1]))
    if result:
        print(f"OK: {result}")
    else:
        print("FAILED")
        sys.exit(1)
