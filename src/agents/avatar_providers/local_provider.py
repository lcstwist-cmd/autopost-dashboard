"""Local Avatar Provider — zero API keys, zero external services.

Generates a talking-head video from a static face photo + TTS audio
using only packages already in requirements.txt:
  • Pillow   — image loading / manipulation
  • numpy    — fast array ops for per-frame animation
  • FFmpeg   — frame encoding + audio mux (imageio-ffmpeg)

Algorithm per frame (25 fps, 512×512 px):
  1. Jaw open/close  — lower face shifts down 0–9 px proportional to
     audio RMS energy (EMA-smoothed to avoid flicker).
  2. Head bob        — whole frame shifts ±2 px via a slow sin wave.
  3. Blink           — random eye-region darkening every ~4 s, 100 ms.

Output is a 512×512 H.264 MP4 that drops straight into the circular
bubble overlay in _composite_ffmpeg() — it is cropped + masked there.

Speed: ~500 frames on CPU takes ~0.3 s (numpy only) + ~2 s FFmpeg encode.
"""
from __future__ import annotations

import math
import os
import random
import re
import subprocess
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FPS          = 25
_FRAME_SIZE   = 512          # square output — composite crops to circle
_MAX_JAW_PX   = 9            # max jaw-drop pixels at peak energy
_EMA_ALPHA    = 0.35         # EMA smoothing (lower = smoother / slower)
_HEAD_BOB_AMP = 2            # ±2 px vertical head bob
_HEAD_BOB_HZ  = 0.28         # oscillations per second
_BLINK_INTERVAL_S = 3.8      # average seconds between blinks
_BLINK_DUR_FRAMES = 3        # frames an eye-blink lasts (~120 ms at 25 fps)
_BLINK_DARKENING  = 0.55     # multiply top-eye-strip by this amount during blink

_PHOTO_CACHE_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / ".presenter_photo_cache"
)

# Reference presenter video — user drops this on Desktop.
# We extract a square crop (face+upper body) and cache it as a silent clip,
# then loop it with TTS audio for every slot.  No API needed.
_REFERENCE_VIDEO = (
    Path.home() / "Desktop"
    / "The SEC just approved 91 crypto ETFs. Everything changes now. #crypto #sol #xrp #ltc #btc.mp4"
)
_PRESENTER_TEMPLATE = _PHOTO_CACHE_DIR / "presenter_clip_template.mp4"

# Portrait crop of the presenter region inside the 720×1080 reference video.
# (x=500, y=645, w=220, h=385) — from top of head to just above logo/hashtags.
# Scaled to 380×650 in the extraction step.
_REF_CROP = (500, 645, 220, 385)


# ---------------------------------------------------------------------------
# Photo helpers
# ---------------------------------------------------------------------------

def _get_ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


# Free royalty-free headshot portraits with neutral (light-gray) backgrounds.
# Tried in order before generating the cartoon fallback.
# The light background is keyed out in the FFmpeg composite (colorkey=0xC8C8C8).
_FREE_PHOTO_URLS = [
    "https://randomuser.me/api/portraits/men/7.jpg",   # dark face — safe for any mask
    "https://randomuser.me/api/portraits/men/80.jpg",
    "https://randomuser.me/api/portraits/men/75.jpg",
]


def _cache_photo(url: str) -> Path:
    """Download + cache presenter photo. Falls back to free stock then generated."""
    import hashlib
    _PHOTO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fname   = "presenter_" + hashlib.md5(url.encode()).hexdigest()[:10] + ".jpg"
    cached  = _PHOTO_CACHE_DIR / fname
    if not cached.exists():
        downloaded = False
        print(f"[local_avatar] downloading presenter photo...")
        try:
            import requests as _req
            r = _req.get(url, timeout=30, headers={
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"),
                "Referer": "https://www.d-id.com/",
            })
            r.raise_for_status()
            cached.write_bytes(r.content)
            print(f"[local_avatar] cached -> {cached.name}")
            downloaded = True
        except Exception as exc:
            print(f"[local_avatar] primary photo failed ({exc}), trying free sources...")

        if not downloaded:
            for free_url in _FREE_PHOTO_URLS:
                try:
                    import requests as _req
                    r = _req.get(free_url, timeout=20)
                    r.raise_for_status()
                    cached.write_bytes(r.content)
                    print(f"[local_avatar] using free portrait: {free_url}")
                    downloaded = True
                    break
                except Exception as exc2:
                    print(f"[local_avatar] free source failed ({free_url}): {exc2}")

        if not downloaded:
            print(f"[local_avatar] all downloads failed, using generated avatar")
            fallback = _PHOTO_CACHE_DIR / "presenter_fallback_emd.jpg"
            if not fallback.exists():
                _generate_fallback_photo(fallback)
            cached = fallback
    return cached


def _generate_fallback_photo(out_path: Path) -> Path:
    """Generate a professional-looking avatar placeholder with PIL.

    Creates a navy-suited silhouette on a neutral background —
    styled after the EMD Brand presenter. Good enough for 460px bubble.
    """
    from PIL import Image, ImageDraw

    size = 512
    # Pure white background — keyed out by FFmpeg colorkey=0xFFFFFF:similarity=0.18
    # so the presenter floats over the news image with no visible background box.
    bg_color = (255, 255, 255)
    img  = Image.new("RGB", (size, size), bg_color)
    draw = ImageDraw.Draw(img)

    cx, cy = size // 2, size // 2

    # Head (skin tone circle)
    head_r = 88
    draw.ellipse(
        (cx - head_r, cy - 200, cx + head_r, cy - 200 + 2 * head_r),
        fill=(220, 185, 155),
    )

    # Neck
    draw.rectangle(
        (cx - 22, cy - 114, cx + 22, cy - 60),
        fill=(220, 185, 155),
    )

    # Suit body (dark navy — distinct from gray bg, won't be keyed out)
    draw.ellipse(
        (cx - 160, cy - 30, cx + 160, cy + 340),
        fill=(28, 40, 70),
    )
    # White shirt + tie
    draw.polygon(
        [(cx - 22, cy - 55), (cx + 22, cy - 55), (cx + 14, cy + 80),
         (cx, cy + 160), (cx - 14, cy + 80)],
        fill=(240, 240, 240),
    )
    draw.polygon(
        [(cx - 5, cy - 40), (cx + 5, cy - 40),
         (cx + 3, cy + 100), (cx, cy + 130), (cx - 3, cy + 100)],
        fill=(180, 30, 30),
    )

    # Hair (short dark)
    draw.ellipse(
        (cx - head_r, cy - 200, cx + head_r, cy - 200 + head_r),
        fill=(60, 40, 20),
    )

    # Eyes
    for ex in (cx - 30, cx + 30):
        draw.ellipse((ex - 10, cy - 148, ex + 10, cy - 128), fill=(255, 255, 255))
        draw.ellipse((ex - 6, cy - 144, ex + 6, cy - 132), fill=(60, 40, 20))

    img.save(out_path, "JPEG", quality=92)
    print(f"[local_avatar] generated fallback avatar -> {out_path.name}")
    return out_path


def _prepare_base(photo_path: Path, size: int = _FRAME_SIZE) -> np.ndarray:
    """Load photo, center-crop to square, resize. Returns uint8 RGB (H,W,3)."""
    img = Image.open(photo_path).convert("RGB")
    w, h = img.size
    m = min(w, h)
    img = img.crop(((w - m) // 2, (h - m) // 2,
                    (w + m) // 2, (h + m) // 2))
    img = img.resize((size, size), Image.LANCZOS)
    return np.array(img, dtype=np.uint8)


# ---------------------------------------------------------------------------
# Audio energy analysis
# ---------------------------------------------------------------------------

def _audio_duration(audio_path: Path) -> float:
    """Return audio file duration in seconds via FFmpeg probe."""
    ffmpeg = _get_ffmpeg()
    r = subprocess.run(
        [ffmpeg, "-i", str(audio_path), "-f", "null", "-"],
        capture_output=True, text=True, errors="replace",
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return 20.0


def _frame_energies(audio_path: Path, fps: float, n_frames: int) -> np.ndarray:
    """Return per-frame normalised RMS energy, shape (n_frames,)."""
    ffmpeg = _get_ffmpeg()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = Path(f.name)
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", str(audio_path),
             "-ar", "16000", "-ac", "1", "-f", "wav", str(wav_path)],
            capture_output=True, check=True,
        )
        with wave.open(str(wav_path), "rb") as wf:
            sr        = wf.getframerate()
            n_samp    = wf.getnframes()
            raw       = wf.readframes(n_samp)
    except Exception as exc:
        print(f"[local_avatar] audio analysis failed: {exc}")
        return np.zeros(n_frames)
    finally:
        wav_path.unlink(missing_ok=True)

    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    spf     = max(1, int(sr / fps))          # samples per video frame
    raw_e   = np.zeros(n_frames)
    for i in range(n_frames):
        chunk = samples[i * spf: i * spf + spf]
        if len(chunk):
            raw_e[i] = np.sqrt(np.mean(chunk ** 2))

    # Normalise
    mx = raw_e.max()
    if mx > 0:
        raw_e /= mx

    # EMA smoothing
    smoothed = np.empty_like(raw_e)
    smoothed[0] = raw_e[0]
    for i in range(1, n_frames):
        smoothed[i] = _EMA_ALPHA * raw_e[i] + (1 - _EMA_ALPHA) * smoothed[i - 1]

    mx2 = smoothed.max()
    if mx2 > 0:
        smoothed /= mx2
    return smoothed


# ---------------------------------------------------------------------------
# Per-frame effects (pure numpy — fast)
# ---------------------------------------------------------------------------

def _jaw_open(frame: np.ndarray, jaw_px: int) -> np.ndarray:
    """Shift lower face down jaw_px pixels, blending the gap."""
    if jaw_px <= 0:
        return frame
    h, w = frame.shape[:2]
    jaw_line = int(h * 0.70)   # ~mouth level for a centered headshot
    result   = frame.copy()

    src_h = h - jaw_line - jaw_px
    if src_h > 0:
        result[jaw_line + jaw_px: h] = frame[jaw_line: jaw_line + src_h]

    # Blend gap
    top_row = frame[jaw_line - 1].astype(np.float32)
    bot_row = frame[jaw_line    ].astype(np.float32)
    for i in range(jaw_px):
        t = (i + 1) / (jaw_px + 1)
        result[jaw_line + i] = (top_row * (1 - t) + bot_row * t).astype(np.uint8)
    return result


def _head_bob(frame: np.ndarray, shift_px: int) -> np.ndarray:
    """Vertical shift of entire frame (positive = down)."""
    if shift_px == 0:
        return frame
    result = frame.copy()
    if shift_px > 0:
        result[shift_px:] = frame[: frame.shape[0] - shift_px]
        result[:shift_px] = frame[0]
    else:
        s = -shift_px
        result[: frame.shape[0] - s] = frame[s:]
        result[frame.shape[0] - s:] = frame[-1]
    return result


def _blink(frame: np.ndarray, intensity: float) -> np.ndarray:
    """Darken the upper eye strip slightly to simulate a blink."""
    if intensity <= 0:
        return frame
    h  = frame.shape[0]
    y0 = int(h * 0.28)   # eyebrow top
    y1 = int(h * 0.45)   # below eye
    result = frame.copy()
    result[y0:y1] = (frame[y0:y1] * (1 - intensity * (1 - _BLINK_DARKENING))
                     ).clip(0, 255).astype(np.uint8)
    return result


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------

def generate_local_talking_head(
    photo_url: str,
    audio_path: Path,
    output_path: Path,
    size: int = _FRAME_SIZE,
    fps: float = _FPS,
) -> Path:
    """Photo + TTS audio -> talking-head MP4.  No API calls.

    Args:
        photo_url   : HTTP URL of presenter portrait (will be cached locally).
        audio_path  : Path to TTS mp3/wav file.
        output_path : Destination mp4 path.
        size        : Output frame width/height in pixels (square).
        fps         : Frame rate.

    Returns:
        output_path on success.

    Raises:
        RuntimeError if FFmpeg encoding fails.
    """
    ffmpeg    = _get_ffmpeg()
    t_start   = time.time()
    duration  = _audio_duration(audio_path)
    n_frames  = int(duration * fps) + 1
    print(f"[local_avatar] {n_frames} frames × {size}px @ {fps}fps  "
          f"({duration:.1f}s)  no API needed")

    # --- Prepare base frame -----------------------------------------------
    photo_path = _cache_photo(photo_url)
    base       = _prepare_base(photo_path, size)

    # --- Per-frame energies -----------------------------------------------
    energies = _frame_energies(audio_path, fps, n_frames)

    # --- Pre-schedule random blinks ----------------------------------------
    blink_frames: dict[int, float] = {}   # frame_index -> intensity at peak
    rng         = random.Random(42)       # deterministic for same slot
    f = 0
    while f < n_frames:
        interval = rng.gauss(_BLINK_INTERVAL_S * fps, fps * 0.5)
        f += max(int(fps * 1.5), int(interval))
        if f < n_frames:
            for d in range(_BLINK_DUR_FRAMES):
                fi = f + d
                if fi < n_frames:
                    # triangle: rises then falls
                    pk = 1.0 - abs(d - _BLINK_DUR_FRAMES / 2) / (_BLINK_DUR_FRAMES / 2)
                    blink_frames[fi] = float(pk)

    # --- Pipe frames to FFmpeg ---------------------------------------------
    cmd = [
        ffmpeg, "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{size}x{size}",
        "-r", str(fps),
        "-i", "pipe:0",
        "-i", str(audio_path),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        "-t", str(duration + 0.1),
        str(output_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    try:
        for i in range(n_frames):
            t       = i / fps
            energy  = float(energies[i]) if i < len(energies) else 0.0

            frame = base.copy()

            # 1. Jaw animation
            jaw_px = int(energy * _MAX_JAW_PX)
            if jaw_px > 0:
                frame = _jaw_open(frame, jaw_px)

            # 2. Head bob (slow sin wave)
            bob_px = int(_HEAD_BOB_AMP
                         * math.sin(2 * math.pi * _HEAD_BOB_HZ * t))
            if bob_px:
                frame = _head_bob(frame, bob_px)

            # 3. Blink
            blink_int = blink_frames.get(i, 0.0)
            if blink_int > 0:
                frame = _blink(frame, blink_int)

            proc.stdin.write(frame.tobytes())
    finally:
        proc.stdin.close()

    _, stderr = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"FFmpeg local-avatar encode failed (exit {proc.returncode}):\n"
            + stderr[-800:].decode("utf-8", errors="replace")
        )

    elapsed = time.time() - t_start
    size_kb = output_path.stat().st_size // 1024
    print(f"[local_avatar] done -> {output_path.name} "
          f"({size_kb} KB, {elapsed:.1f}s)")
    return output_path


# ---------------------------------------------------------------------------
# Reference-presenter extraction (uses the real person from a sample video)
# ---------------------------------------------------------------------------

def _extract_reference_presenter() -> Path | None:
    """Crop + cache the presenter from the reference video on the Desktop.

    Returns the cached 380×650 silent MP4, or None if the source is missing.
    One-time operation; subsequent calls return the cached path immediately.
    """
    if _PRESENTER_TEMPLATE.exists():
        return _PRESENTER_TEMPLATE
    if not _REFERENCE_VIDEO.exists():
        return None

    print(f"[local_avatar] extracting presenter from reference video...")
    _PHOTO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ffmpeg = _get_ffmpeg()
    cx, cy, cw, ch = _REF_CROP
    # Scale crop to 380×650 portrait (ratio ≈ 1:1.71 — close to the reference)
    target_w, target_h = 380, 650
    cmd = [
        ffmpeg, "-y", "-i", str(_REFERENCE_VIDEO),
        "-vf", (
            f"crop={cw}:{ch}:{cx}:{cy},"
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1"
        ),
        "-an",                                          # strip original audio
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        str(_PRESENTER_TEMPLATE),
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        print(f"[local_avatar] presenter extraction failed: "
              f"{r.stderr[-200:].decode(errors='replace')}")
        return None
    size_kb = _PRESENTER_TEMPLATE.stat().st_size // 1024
    print(f"[local_avatar] presenter template cached -> "
          f"{_PRESENTER_TEMPLATE.name} ({size_kb} KB)")
    return _PRESENTER_TEMPLATE


def _loop_presenter_with_audio(
    template: Path,
    audio_path: Path,
    output_path: Path,
) -> Path:
    """Loop the presenter clip to match TTS duration and mux the TTS audio.

    The original audio from the reference is already stripped from `template`.
    The presenter's natural gestures + head movements are preserved; only the
    audio track changes (TTS narration for the current news story).
    """
    ffmpeg  = _get_ffmpeg()
    duration = _audio_duration(audio_path)
    cmd = [
        ffmpeg, "-y",
        "-stream_loop", "-1", "-i", str(template),  # loop presenter clip
        "-i", str(audio_path),                       # TTS narration
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-shortest", "-t", str(duration + 0.2),
        str(output_path),
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"[local_avatar] loop+mux failed: "
            f"{r.stderr[-300:].decode(errors='replace')}"
        )
    size_kb = output_path.stat().st_size // 1024
    print(f"[local_avatar] presenter loop done -> {output_path.name} ({size_kb} KB)")
    return output_path


# ---------------------------------------------------------------------------
# AvatarProvider interface
# ---------------------------------------------------------------------------

class LocalAvatarProvider:
    """Always-available local talking-head generator.

    Plugs into the provider chain as a last resort before the
    branded-no-avatar fallback — ensures EVERY slot gets an avatar.
    """

    name = "local"

    def is_available(self) -> bool:
        try:
            import numpy   # noqa: F401
            from PIL import Image  # noqa: F401
            return True
        except ImportError:
            return False

    def generate(
        self,
        text: str,
        out_path: Path,
        presenter: str = "default",
        audio_path: Path | None = None,
    ) -> Path:
        if audio_path is None or not Path(audio_path).exists():
            raise RuntimeError(
                "LocalAvatarProvider requires pre-rendered TTS audio "
                "(audio_path). Did TTS generation fail?"
            )

        # Priority 1: use the real presenter extracted from the reference video.
        # Gives natural gestures + head movements with the TTS voice overlaid.
        template = _extract_reference_presenter()
        if template is not None:
            print(f"[local_avatar] using reference presenter (real video loop)")
            return _loop_presenter_with_audio(template, audio_path, out_path)

        # Priority 2: photo animation fallback (randomuser.me portrait + jaw anim)
        print(f"[local_avatar] reference video not found, using photo animation")
        try:
            from src.agents.avatar_video import _presenter_url
            photo_url = _presenter_url(presenter)
        except Exception:
            photo_url = "https://d-id-public-bucket.s3.amazonaws.com/matt.jpeg"

        return generate_local_talking_head(photo_url, audio_path, out_path)
