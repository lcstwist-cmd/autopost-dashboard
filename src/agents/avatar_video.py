"""Avatar Video Builder — generates a talking-head reel using D-ID Clips API.

Flow:
  1. Take narration text from reel/01_script.txt
  2. Call D-ID Clips API -> avatar video (greenscreen, MP4)
  3. Download avatar MP4
  4. FFmpeg composite:
       - crypto background (image_reel_1080x1920.png) fills 9:16
       - avatar (chroma-keyed) placed in lower-center
       - captions (SRT) burned in at bottom
  5. Write reel_avatar.mp4

Presenters (greenscreen, finance/crypto look):
  MALE:
    alex_suit    v2_public_alex_black_suite_green_screen@u8RGmlrjpD
    alex_casual  v2_public_alex_biege_shirt_green_screen@sNZgzDrsOE
    william      v2_public_William_NoHands_BlackShirt_GreenScreen@1tBxWRlClh
    arran        v2_public_arran_beige_jacket_green_screen@EVXqXwvzWU
    darren       v2_public_darren@88QjSpCkcq
    ethan        v2_public_ethan@4dtv7uj_ko
    lucas        v2_public_lucas@aswtxwoss5
    jaimie       v2_public_jaimie@Isfx_UxygI
  FEMALE:
    anita        v2_public_anita@Os4oKCBIgZ
    diana        v2_public_diana@so9Pg73d6N
    lana         v2_public_lana@TtreMLgSnL
    amber        v2_public_Amber@1A_D4SXIGa
    flora        v2_public_flora@RPSCZ_knvq
    sophia       v2_public_sohpia@CtvJYUo9MA

Install: pip install moviepy imageio-ffmpeg (already in requirements)
"""
from __future__ import annotations

import argparse
import base64
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
# Presenter catalogue  (greenscreen = True -> can composite cleanly on any bg)
# ---------------------------------------------------------------------------

PRESENTERS: dict[str, dict] = {
    # --- Male ---
    "alex_suit":    {"id": "v2_public_alex_black_suite_green_screen@u8RGmlrjpD",   "gender": "M", "gs": True,  "style": "professional"},
    "alex_casual":  {"id": "v2_public_alex_biege_shirt_green_screen@sNZgzDrsOE",   "gender": "M", "gs": True,  "style": "casual"},
    "william":      {"id": "v2_public_William_NoHands_BlackShirt_GreenScreen@1tBxWRlClh", "gender": "M", "gs": True, "style": "professional"},
    "arran":        {"id": "v2_public_arran_beige_jacket_green_screen@EVXqXwvzWU", "gender": "M", "gs": True,  "style": "casual"},
    "darren":       {"id": "v2_public_darren@88QjSpCkcq",                           "gender": "M", "gs": True,  "style": "casual"},
    "ethan":        {"id": "v2_public_ethan@4dtv7uj_ko",                            "gender": "M", "gs": True,  "style": "casual"},
    "lucas":        {"id": "v2_public_lucas@aswtxwoss5",                            "gender": "M", "gs": True,  "style": "casual"},
    "jaimie":       {"id": "v2_public_jaimie@Isfx_UxygI",                          "gender": "M", "gs": True,  "style": "casual"},
    # --- Female ---
    "anita":        {"id": "v2_public_anita@Os4oKCBIgZ",                            "gender": "F", "gs": True,  "style": "professional"},
    "diana":        {"id": "v2_public_diana@so9Pg73d6N",                            "gender": "F", "gs": True,  "style": "professional"},
    "lana":         {"id": "v2_public_lana@TtreMLgSnL",                            "gender": "F", "gs": True,  "style": "casual"},
    "amber":        {"id": "v2_public_Amber@1A_D4SXIGa",                           "gender": "F", "gs": True,  "style": "casual"},
    "flora":        {"id": "v2_public_flora@RPSCZ_knvq",                           "gender": "F", "gs": True,  "style": "casual"},
    "sophia":       {"id": "v2_public_sohpia@CtvJYUo9MA",                          "gender": "F", "gs": True,  "style": "professional"},
}

DEFAULT_PRESENTER = "alex_suit"

# D-ID voices (Microsoft neural — no extra cost on trial)
VOICES = {
    "M": "en-US-GuyNeural",
    "F": "en-US-JennyNeural",
}

# ---------------------------------------------------------------------------
# D-ID auth helper
# ---------------------------------------------------------------------------

def _did_headers() -> dict:
    api_key  = os.environ.get("DID_API_KEY", "").strip()
    email    = os.environ.get("DID_API_EMAIL", "").strip()
    if not api_key or not email:
        raise RuntimeError(
            "D-ID credentials missing. Set DID_API_KEY and DID_API_EMAIL in .env"
        )
    b64_email = base64.b64encode(email.encode()).decode()
    return {
        "Authorization": f"Basic {b64_email}:{api_key}",
        "Content-Type": "application/json",
    }

# ---------------------------------------------------------------------------
# D-ID Clips API
# ---------------------------------------------------------------------------

def _create_clip(text: str, presenter_key: str) -> str:
    """Submit a D-ID clip job; return clip_id."""
    p = PRESENTERS[presenter_key]
    voice = VOICES.get(p["gender"], VOICES["M"])
    payload = {
        "presenter_id": p["id"],
        "script": {
            "type": "text",
            "input": text[:1500],           # D-ID text limit ~1500 chars
            "provider": {"type": "microsoft", "voice_id": voice},
        },
        "config": {"result_format": "mp4"},
    }
    r = requests.post("https://api.d-id.com/clips", headers=_did_headers(), json=payload, timeout=30)
    if r.status_code == 429:
        raise RuntimeError("D-ID rate limit hit — wait 1-2 minutes and retry")
    r.raise_for_status()
    return r.json()["id"]


def _poll_clip(clip_id: str, timeout: int = 600) -> str:
    """Poll until done; return result_url."""
    deadline = time.time() + timeout
    busy_wait = 20   # seconds between retries when queue is busy
    normal_wait = 6
    busy_count = 0
    while time.time() < deadline:
        try:
            r = requests.get(f"https://api.d-id.com/clips/{clip_id}", headers=_did_headers(), timeout=20)
            r.raise_for_status()
            d = r.json()
        except Exception as exc:
            print(f"[avatar_video] poll error ({exc}), retrying...")
            time.sleep(normal_wait)
            continue

        status = d.get("status")
        if status == "done":
            url = d.get("result_url", "")
            if not url:
                raise RuntimeError("D-ID returned done but no result_url")
            return url
        if status in ("error", "rejected"):
            err = d.get("error", {})
            details = err.get("details", "")
            if "already in progress" in details:
                busy_count += 1
                wait = min(busy_wait * busy_count, 60)
                print(f"[avatar_video] D-ID queue busy (attempt {busy_count}), waiting {wait}s...")
                time.sleep(wait)
                continue
            raise RuntimeError(f"D-ID clip failed: {err.get('description')} — {details}")
        time.sleep(normal_wait)
    raise TimeoutError(f"D-ID clip {clip_id} timed out after {timeout}s")


def _download_clip(url: str, out_path: Path) -> None:
    r = requests.get(url, timeout=120, stream=True)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(65536):
            f.write(chunk)


def generate_avatar_clip(text: str, out_path: Path, presenter: str = DEFAULT_PRESENTER) -> Path:
    """Full D-ID flow: text -> avatar MP4 (greenscreen)."""
    print(f"[avatar_video] submitting D-ID clip ({presenter})...")
    clip_id = _create_clip(text, presenter)
    print(f"[avatar_video] clip_id={clip_id} — polling...")
    result_url = _poll_clip(clip_id)
    print(f"[avatar_video] downloading avatar clip...")
    _download_clip(result_url, out_path)
    print(f"[avatar_video] avatar clip saved -> {out_path.name}")
    return out_path

# ---------------------------------------------------------------------------
# Composite: avatar (greenscreen) + crypto background + captions
# ---------------------------------------------------------------------------

def _build_ffmpeg_cmd(
    bg_path: Path,
    avatar_path: Path,
    out_path: Path,
) -> list[str]:
    """
    Layout (9:16 = 1080x1920):
      ┌─────────────────────┐  y=0
      │   [Background]      │  200px top visible
      │  ╔═══════════════╗  │
      │  ║   Avatar AI   ║  │  756×1021 px, centered (162px sides)
      │  ╚═══════════════╝  │  bottom at y=1221
      │   [Background]      │  699px for captions + lower background
      └─────────────────────┘  y=1920

    D-ID clips are 1920×1080 landscape (person centered).
    We portrait-crop the center 800px, scale to 756px wide,
    composite centered at y=200 so background is visible around the avatar.
    """
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    bg_w, bg_h = 1080, 1920

    # Portrait crop: center 800px of the 1920px-wide D-ID clip
    # captures the full presenter body with minimal empty space on sides
    crop_w = 800
    crop_h = 1080
    crop_x = (1920 - crop_w) // 2   # = 560

    # Scale cropped portrait to 756px wide (70% of 1080) — prominent but
    # leaves 162px of background visible on each side
    av_w = 756
    av_h = int(av_w * crop_h / crop_w)   # = 1021

    # Center horizontally, 200px from top
    av_x = (bg_w - av_w) // 2            # = 162
    av_y = 200

    # Tuned chroma key — removes D-ID green cleanly without edge fringing
    chroma_color = "0x00ff00"
    chroma_sim   = "0.40"    # similarity: how close to green triggers removal
    chroma_blend = "0.15"    # blend: edge softness / anti-aliasing

    filter_complex = (
        # Background: scale to full 9:16
        f"[0:v]scale={bg_w}:{bg_h}[bg];"
        # Avatar: portrait crop → scale → YUVA → chromakey → despill
        f"[1:v]"
        f"crop={crop_w}:{crop_h}:{crop_x}:0,"
        f"scale={av_w}:{av_h},"
        f"format=yuva420p,"
        f"chromakey={chroma_color}:{chroma_sim}:{chroma_blend},"
        f"despill=type=green[av];"
        # Composite: avatar centered on background
        f"[bg][av]overlay={av_x}:{av_y}[out]"
    )

    return [
        ffmpeg, "-y",
        "-loop", "1", "-i", str(bg_path),
        "-i", str(avatar_path),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-map", "1:a",
        "-shortest",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ]


def composite_avatar(
    bg_path: Path,
    avatar_path: Path,
    srt_path: Path | None,
    out_path: Path,
) -> Path:
    import subprocess
    from pathlib import Path as P

    # Step 1: FFmpeg composite (bg + avatar chroma-keyed)
    tmp_no_caps = out_path.with_suffix(".tmp.mp4")
    cmd = _build_ffmpeg_cmd(bg_path, avatar_path, tmp_no_caps)
    print("[avatar_video] compositing avatar on background (FFmpeg)...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Show last 600 chars of stderr for debugging
        print(f"[avatar_video] FFmpeg stderr:\n{result.stderr[-600:]}")
        raise RuntimeError(f"FFmpeg composite failed (exit {result.returncode})")

    # Step 2: burn captions with Python/PIL (avoids Windows path escaping issues)
    if srt_path and P(srt_path).exists():
        _burn_captions_python(tmp_no_caps, srt_path, out_path)
        tmp_no_caps.unlink(missing_ok=True)
    else:
        tmp_no_caps.rename(out_path)

    size_mb = round(out_path.stat().st_size / 1_048_576, 1)
    print(f"[avatar_video] composite done -> {out_path.name} ({size_mb} MB)")
    return out_path


def _burn_captions_python(video_path: Path, srt_path: Path, out_path: Path) -> None:
    """Overlay SRT captions on video using moviepy + PIL (no FFmpeg libass needed)."""
    from moviepy import VideoFileClip, VideoClip
    from src.agents.video_builder import parse_srt, render_caption_on_frame
    import numpy as np

    cues = parse_srt(srt_path.read_text(encoding="utf-8"))
    clip  = VideoFileClip(str(video_path))
    w, h  = clip.size

    def _caption_at(t):
        for c in cues:
            if c["start"] <= t < c["end"]:
                return c["text"]
        return None

    def make_frame(t):
        frame = clip.get_frame(t)
        cap   = _caption_at(t)
        return render_caption_on_frame(frame.copy(), cap, w, h) if cap else frame

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

def build_avatar_reel(
    slot_dir: Path,
    presenter: str = DEFAULT_PRESENTER,
) -> Path:
    """Full pipeline: script -> D-ID avatar -> composite -> reel_avatar.mp4"""
    from src.agents.video_builder import extract_narration, parse_srt

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

    # Extract clean narration
    script_text = (reel_dir / "01_script.txt").read_text(encoding="utf-8")
    narration   = extract_narration(script_text)
    if not narration.strip():
        raise ValueError("Script is empty after parsing")

    # Step 1: generate avatar clip via D-ID
    generate_avatar_clip(narration, avatar_tmp, presenter=presenter)

    # Step 2: composite on crypto background
    composite_avatar(bg_path, avatar_tmp, srt_path, out_path)

    # Cleanup temp
    if avatar_tmp.exists():
        avatar_tmp.unlink()

    return out_path

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build avatar reel via D-ID + FFmpeg composite")
    ap.add_argument("slot_dir", help="path to queue slot directory")
    ap.add_argument("--presenter", default=DEFAULT_PRESENTER,
                    choices=list(PRESENTERS.keys()),
                    help=f"avatar presenter (default: {DEFAULT_PRESENTER})")
    args = ap.parse_args(argv)

    try:
        out = build_avatar_reel(Path(args.slot_dir), presenter=args.presenter)
        print(f"[avatar_video] OK: {out}")
        return 0
    except Exception as exc:
        print(f"[avatar_video] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
