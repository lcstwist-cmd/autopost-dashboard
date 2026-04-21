"""Avatar Video Builder — D-ID /talks API (works on all paid plans).

Flow:
  1. Take narration text from reel/01_script.txt
  2. Call D-ID /talks API with a presenter photo -> animated talking-head MP4
  3. Download avatar MP4
  4. FFmpeg composite: crypto background (9:16) + presenter video (bottom strip)
  5. Burn captions via PIL
  6. Write reel_avatar.mp4

Presenter photo: set DID_PRESENTER_URL env var (or via Settings → Presenter Photo URL).
Default: professional male presenter stock photo (royalty-free).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import requests

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Default presenter photo (royalty-free, neutral background works best)
# Override with DID_PRESENTER_URL env var or user Settings
# ---------------------------------------------------------------------------

DEFAULT_PRESENTER_URL = (
    "https://upload.wikimedia.org/wikipedia/commons/thumb/1/14/"
    "Gatto_europeo4.jpg/320px-Gatto_europeo4.jpg"
    # ^ placeholder — set DID_PRESENTER_URL in Settings to a real portrait photo
)

# A reliable neutral presenter photo (professional male, neutral background):
FALLBACK_PRESENTER_URL = (
    "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d"
    "?w=400&q=80&fit=crop&crop=faces"
)

VOICE_ID = "en-US-GuyNeural"   # Microsoft neural — included in all D-ID plans


# ---------------------------------------------------------------------------
# D-ID auth
# ---------------------------------------------------------------------------

def _did_headers() -> dict:
    """D-ID Basic auth: Authorization: Basic base64(email):secret"""
    import base64
    api_key = os.environ.get("DID_API_KEY", "").strip()
    email   = os.environ.get("DID_API_EMAIL", "").strip()
    if not api_key:
        raise RuntimeError("D-ID credentials missing — set DID_API_KEY in Settings")
    # If user stored the full key (already base64:secret format), use directly
    if ":" in api_key and len(api_key) > 30:
        return {
            "Authorization": f"Basic {api_key}",
            "Content-Type": "application/json",
        }
    # Otherwise build from email + key
    if not email:
        raise RuntimeError("DID_API_EMAIL missing — set it in Settings")
    b64 = base64.b64encode(f"{email}:{api_key}".encode()).decode()
    return {
        "Authorization": f"Basic {b64}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# D-ID /talks API  (works on Lite, Basic, Pro — all paid plans)
# ---------------------------------------------------------------------------

def _presenter_url() -> str:
    url = os.environ.get("DID_PRESENTER_URL", "").strip()
    return url if url else FALLBACK_PRESENTER_URL


def _create_talk(text: str) -> str:
    """Submit a D-ID /talks job; return talk_id."""
    payload = {
        "source_url": _presenter_url(),
        "script": {
            "type": "text",
            "input": text[:1200],
            "provider": {
                "type": "microsoft",
                "voice_id": VOICE_ID,
            },
        },
        "config": {
            "result_format": "mp4",
            "fluent": True,
            "pad_audio": 0.0,
        },
    }
    r = requests.post(
        "https://api.d-id.com/talks",
        headers=_did_headers(),
        json=payload,
        timeout=30,
    )
    if r.status_code == 429:
        raise RuntimeError("D-ID rate limit — wait 1-2 min and retry")
    if r.status_code == 403:
        raise RuntimeError(
            "D-ID 403 Forbidden — check your API key in Settings. "
            "Make sure you copied the full key (e.g. bGNz...@gmail.com:secret)"
        )
    r.raise_for_status()
    return r.json()["id"]


def _poll_talk(talk_id: str, timeout: int = 300) -> str:
    """Poll GET /talks/{id} until done; return result_url."""
    deadline = time.time() + timeout
    wait = 5
    while time.time() < deadline:
        try:
            r = requests.get(
                f"https://api.d-id.com/talks/{talk_id}",
                headers=_did_headers(),
                timeout=20,
            )
            r.raise_for_status()
            d = r.json()
        except Exception as exc:
            print(f"[avatar_video] poll error ({exc}), retrying...")
            time.sleep(wait)
            continue

        status = d.get("status")
        if status == "done":
            url = d.get("result_url", "")
            if not url:
                raise RuntimeError("D-ID returned done but no result_url")
            return url
        if status in ("error", "rejected"):
            err = d.get("error", {})
            raise RuntimeError(
                f"D-ID talk failed: {err.get('description', '')} — {err.get('details', '')}"
            )
        time.sleep(wait)
        wait = min(wait + 2, 15)

    raise TimeoutError(f"D-ID talk {talk_id} timed out after {timeout}s")


def _download(url: str, out_path: Path) -> None:
    r = requests.get(url, timeout=120, stream=True)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(65536):
            f.write(chunk)


def generate_avatar_clip(text: str, out_path: Path) -> Path:
    """text → D-ID talking-head MP4."""
    print(f"[avatar_video] submitting D-ID /talks job...")
    talk_id = _create_talk(text)
    print(f"[avatar_video] talk_id={talk_id} — polling...")
    result_url = _poll_talk(talk_id)
    print(f"[avatar_video] downloading...")
    _download(result_url, out_path)
    print(f"[avatar_video] avatar saved -> {out_path.name}")
    return out_path


# ---------------------------------------------------------------------------
# Composite: presenter strip at bottom + crypto background
#
# Layout (1080×1920):
#   ┌─────────────────────┐  y=0
#   │  Crypto background  │  upper 60% — news visual
#   │                     │
#   ├─────────────────────┤  y=1152
#   │  Presenter video    │  540px strip (scaled 1080×540 from 16:9 talk)
#   ├─────────────────────┤  y=1692
#   │  Captions area      │  228px for text
#   └─────────────────────┘  y=1920
# ---------------------------------------------------------------------------

def _composite_ffmpeg(bg_path: Path, avatar_path: Path, out_path: Path) -> None:
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        ffmpeg = "ffmpeg"

    # Scale D-ID output (usually 512×512 or 1280×720) to 1080 wide, cropped to 540 tall
    # Then overlay at y=1152 on the 1080×1920 background
    filter_complex = (
        "[0:v]scale=1080:1920,setsar=1[bg];"
        "[1:v]scale=1080:-1,crop=1080:540:0:ih/2-270[av];"
        "[bg][av]overlay=0:1152[out]"
    )
    cmd = [
        ffmpeg, "-y",
        "-loop", "1", "-i", str(bg_path),
        "-i", str(avatar_path),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-map", "1:a?",
        "-shortest",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-t", "60",
        str(out_path),
    ]
    import subprocess
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[avatar_video] FFmpeg stderr:\n{result.stderr[-800:]}")
        raise RuntimeError(f"FFmpeg composite failed (exit {result.returncode})")


def _burn_captions(video_path: Path, srt_path: Path, out_path: Path) -> None:
    from moviepy import VideoFileClip, VideoClip
    from src.agents.video_builder import parse_srt, render_caption_on_frame

    cues = parse_srt(srt_path.read_text(encoding="utf-8"))
    clip = VideoFileClip(str(video_path))
    w, h = clip.size

    def make_frame(t):
        frame = clip.get_frame(t)
        for c in cues:
            if c["start"] <= t < c["end"]:
                return render_caption_on_frame(frame.copy(), c["text"], w, h)
        return frame

    out_clip = VideoClip(frame_function=make_frame, duration=clip.duration)
    out_clip = out_clip.with_audio(clip.audio)
    out_clip.write_videofile(
        str(out_path), fps=clip.fps,
        codec="libx264", audio_codec="aac",
        preset="fast", logger=None,
    )
    clip.close()
    out_clip.close()


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def build_avatar_reel(slot_dir: Path, presenter: str = "default") -> Path:
    """Script → D-ID avatar → composite → reel_avatar.mp4"""
    from src.agents.video_builder import extract_narration

    slot_dir  = Path(slot_dir).resolve()
    reel_dir  = slot_dir / "reel"
    bg_path   = slot_dir / "image_reel_1080x1920.png"
    srt_path  = reel_dir / "03_captions.srt"
    out_path  = slot_dir / "reel_avatar.mp4"
    avatar_tmp = slot_dir / "_avatar_raw.mp4"

    if not bg_path.exists():
        raise FileNotFoundError(f"Background image missing: {bg_path}")
    if not (reel_dir / "01_script.txt").exists():
        raise FileNotFoundError(f"Script missing: {reel_dir / '01_script.txt'}")

    script_text = (reel_dir / "01_script.txt").read_text(encoding="utf-8")
    narration   = extract_narration(script_text)
    if not narration.strip():
        raise ValueError("Script is empty after parsing")

    generate_avatar_clip(narration, avatar_tmp)
    _composite_ffmpeg(bg_path, avatar_tmp, out_path if not (srt_path and srt_path.exists()) else slot_dir / "_avatar_nocap.mp4")

    if srt_path and srt_path.exists():
        nocap = slot_dir / "_avatar_nocap.mp4"
        if nocap.exists():
            _burn_captions(nocap, srt_path, out_path)
            nocap.unlink(missing_ok=True)

    if avatar_tmp.exists():
        avatar_tmp.unlink()

    return out_path
