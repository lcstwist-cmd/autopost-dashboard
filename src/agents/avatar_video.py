"""Avatar Video Builder — D-ID /talks API (works on all paid plans).

Flow:
  1. Take narration text from reel/01_script.txt
  2. Call D-ID /talks API with a presenter photo -> animated talking-head MP4
  3. Download avatar MP4
  4. FFmpeg composite: crypto background (9:16) + presenter video (bottom strip)
  5. Burn captions via PIL
  6. Write reel_avatar.mp4

Presenter: set DID_PRESENTER_URL env var (or via Settings → Presenter Photo URL).
Or pass presenter name — mapped to a built-in portrait photo.
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
# Presenter photos — Pexels CDN (stable direct URLs, no redirects)
# ---------------------------------------------------------------------------

PRESENTER_PHOTOS: dict[str, str] = {
    # Males
    "default":      "https://images.pexels.com/photos/2379004/pexels-photo-2379004.jpeg?auto=compress&cs=tinysrgb&w=480",
    "alex_suit":    "https://images.pexels.com/photos/2379004/pexels-photo-2379004.jpeg?auto=compress&cs=tinysrgb&w=480",
    "william":      "https://images.pexels.com/photos/1222271/pexels-photo-1222271.jpeg?auto=compress&cs=tinysrgb&w=480",
    "arran":        "https://images.pexels.com/photos/1681010/pexels-photo-1681010.jpeg?auto=compress&cs=tinysrgb&w=480",
    "alex_casual":  "https://images.pexels.com/photos/1043474/pexels-photo-1043474.jpeg?auto=compress&cs=tinysrgb&w=480",
    "darren":       "https://images.pexels.com/photos/1484801/pexels-photo-1484801.jpeg?auto=compress&cs=tinysrgb&w=480",
    "ethan":        "https://images.pexels.com/photos/91227/pexels-photo-91227.jpeg?auto=compress&cs=tinysrgb&w=480",
    "lucas":        "https://images.pexels.com/photos/937481/pexels-photo-937481.jpeg?auto=compress&cs=tinysrgb&w=480",
    "jaimie":       "https://images.pexels.com/photos/428364/pexels-photo-428364.jpeg?auto=compress&cs=tinysrgb&w=480",
    # Females
    "anita":        "https://images.pexels.com/photos/774909/pexels-photo-774909.jpeg?auto=compress&cs=tinysrgb&w=480",
    "diana":        "https://images.pexels.com/photos/712513/pexels-photo-712513.jpeg?auto=compress&cs=tinysrgb&w=480",
    "sophia":       "https://images.pexels.com/photos/1239291/pexels-photo-1239291.jpeg?auto=compress&cs=tinysrgb&w=480",
    "lana":         "https://images.pexels.com/photos/1181690/pexels-photo-1181690.jpeg?auto=compress&cs=tinysrgb&w=480",
    "amber":        "https://images.pexels.com/photos/415829/pexels-photo-415829.jpeg?auto=compress&cs=tinysrgb&w=480",
    "flora":        "https://images.pexels.com/photos/1130626/pexels-photo-1130626.jpeg?auto=compress&cs=tinysrgb&w=480",
}

VOICE_ID = "en-US-GuyNeural"   # Microsoft neural — included in all D-ID plans


# ---------------------------------------------------------------------------
# D-ID auth
# ---------------------------------------------------------------------------

def _did_headers() -> dict:
    """D-ID Basic auth: key format is base64(email):secret — used directly."""
    api_key = os.environ.get("DID_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("D-ID API key not set — go to Settings and enter your D-ID key")
    # D-ID keys are already in Basic-auth format: base64(email):secret
    # Just pass them directly in the Authorization header
    if ":" in api_key and len(api_key) > 20:
        return {
            "Authorization": f"Basic {api_key}",
            "Content-Type": "application/json",
        }
    # Fallback: build from email + key
    import base64
    email = os.environ.get("DID_API_EMAIL", "").strip()
    if not email:
        raise RuntimeError(
            "D-ID key format unrecognized and DID_API_EMAIL not set. "
            "Enter your full key (e.g. bGNz...@gmail.com:secret) in Settings."
        )
    b64 = base64.b64encode(f"{email}:{api_key}".encode()).decode()
    return {
        "Authorization": f"Basic {b64}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Presenter URL resolution
# ---------------------------------------------------------------------------

def _presenter_url(presenter: str = "default") -> str:
    """Resolve presenter name to a portrait photo URL."""
    # User's custom URL (from Settings) takes top priority
    env_url = os.environ.get("DID_PRESENTER_URL", "").strip()
    if env_url:
        return env_url
    # Mapped presenter name
    return PRESENTER_PHOTOS.get(presenter, PRESENTER_PHOTOS["default"])


# ---------------------------------------------------------------------------
# D-ID /talks API  (works on Lite, Basic, Pro — all paid plans)
# ---------------------------------------------------------------------------

def _create_talk(text: str, presenter: str = "default") -> str:
    """Submit a D-ID /talks job; return talk_id."""
    source_url = _presenter_url(presenter)
    payload = {
        "source_url": source_url,
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
    print(f"[avatar_video] D-ID /talks — presenter={presenter}, photo={source_url[:60]}...")
    r = requests.post(
        "https://api.d-id.com/talks",
        headers=_did_headers(),
        json=payload,
        timeout=30,
    )
    if r.status_code == 429:
        raise RuntimeError("D-ID rate limit hit — wait 1-2 minutes and retry")
    if r.status_code == 403:
        raise RuntimeError(
            "D-ID 403 Forbidden — check your API key in Settings. "
            "The key must be the full value shown in D-ID Studio → API Keys "
            "(format: bGNz...@gmail.com:secret)"
        )
    if r.status_code == 402:
        raise RuntimeError(
            "D-ID 402 — insufficient credits or plan does not support /talks. "
            "Check your D-ID billing at studio.d-id.com"
        )
    if not r.ok:
        try:
            detail = r.json().get("description", r.text[:200])
        except Exception:
            detail = r.text[:200]
        raise RuntimeError(f"D-ID API error {r.status_code}: {detail}")
    data = r.json()
    talk_id = data.get("id")
    if not talk_id:
        raise RuntimeError(f"D-ID did not return a talk id. Response: {data}")
    return talk_id


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
            print(f"[avatar_video] poll error ({exc}), retrying in {wait}s...")
            time.sleep(wait)
            continue

        status = d.get("status")
        print(f"[avatar_video] talk {talk_id} status={status}")
        if status == "done":
            url = d.get("result_url", "")
            if not url:
                raise RuntimeError("D-ID returned done but no result_url")
            return url
        if status in ("error", "rejected"):
            err = d.get("error", {})
            raise RuntimeError(
                f"D-ID talk failed (status={status}): "
                f"{err.get('description', '')} — {err.get('details', '')}"
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


def generate_avatar_clip(text: str, out_path: Path, presenter: str = "default") -> Path:
    """text → D-ID talking-head MP4."""
    print(f"[avatar_video] submitting D-ID /talks job (presenter={presenter})...")
    talk_id = _create_talk(text, presenter=presenter)
    print(f"[avatar_video] talk_id={talk_id} — polling for completion...")
    result_url = _poll_talk(talk_id)
    print(f"[avatar_video] downloading result...")
    _download(result_url, out_path)
    size_kb = out_path.stat().st_size // 1024
    print(f"[avatar_video] avatar saved -> {out_path.name} ({size_kb} KB)")
    return out_path


# ---------------------------------------------------------------------------
# Composite: presenter strip at bottom + crypto background
#
# Layout (1080×1920):
#   ┌─────────────────────┐  y=0
#   │  Crypto background  │  upper 60% — news visual
#   │                     │
#   ├─────────────────────┤  y=1152
#   │  Presenter video    │  540px strip (scaled to 1080×540)
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

    # Scale avatar to fit 1080×540 (preserve aspect, letterbox if needed),
    # then overlay on a 1080×1920 background starting at y=1152.
    # This handles any D-ID output size (512×512, 1280×720, 640×480, etc.)
    filter_complex = (
        "[0:v]scale=1080:1920,setsar=1[bg];"
        "[1:v]scale=1080:540:force_original_aspect_ratio=decrease,"
        "pad=1080:540:(ow-iw)/2:(oh-ih)/2:black,setsar=1[av];"
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
        print(f"[avatar_video] FFmpeg stderr:\n{result.stderr[-1200:]}")
        raise RuntimeError(f"FFmpeg composite failed (exit {result.returncode}): {result.stderr[-300:]}")


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
    if clip.audio is not None:
        out_clip = out_clip.with_audio(clip.audio)
    out_clip.write_videofile(
        str(out_path), fps=clip.fps or 30,
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

    slot_dir   = Path(slot_dir).resolve()
    reel_dir   = slot_dir / "reel"
    bg_path    = slot_dir / "image_reel_1080x1920.png"
    srt_path   = reel_dir / "03_captions.srt"
    out_path   = slot_dir / "reel_avatar.mp4"
    avatar_tmp = slot_dir / "_avatar_raw.mp4"
    nocap_tmp  = slot_dir / "_avatar_nocap.mp4"

    if not bg_path.exists():
        raise FileNotFoundError(
            f"Background image missing: {bg_path}\n"
            "Run the pipeline first or click Generate from the slot page."
        )
    script_file = reel_dir / "01_script.txt"
    if not script_file.exists():
        raise FileNotFoundError(
            f"Reel script missing: {script_file}\n"
            "Run the pipeline first or click Generate from the slot page."
        )

    script_text = script_file.read_text(encoding="utf-8")
    narration   = extract_narration(script_text)
    if not narration.strip():
        raise ValueError("Script is empty after parsing — check reel/01_script.txt")

    print(f"[avatar_video] narration: {len(narration.split())} words")

    # Step 1: Generate D-ID talking-head clip
    generate_avatar_clip(narration, avatar_tmp, presenter=presenter)

    # Step 2: Composite background + avatar
    if srt_path.exists():
        # Composite into a temp file, then burn captions on top
        _composite_ffmpeg(bg_path, avatar_tmp, nocap_tmp)
        if nocap_tmp.exists():
            _burn_captions(nocap_tmp, srt_path, out_path)
            nocap_tmp.unlink(missing_ok=True)
        else:
            raise RuntimeError("Composite step produced no output file")
    else:
        # No captions — composite directly to final output
        _composite_ffmpeg(bg_path, avatar_tmp, out_path)

    # Cleanup temp avatar clip
    avatar_tmp.unlink(missing_ok=True)

    if not out_path.exists():
        raise RuntimeError(f"Build completed but output file not found: {out_path}")

    size_mb = out_path.stat().st_size / 1_048_576
    print(f"[avatar_video] done -> {out_path.name} ({size_mb:.1f} MB)")
    return out_path
