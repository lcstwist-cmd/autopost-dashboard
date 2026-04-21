"""Video Builder — Agent 6 of the Crypto AutoPost pipeline.

Takes pipeline outputs and produces a ready-to-post 9:16 MP4 reel.

Inputs (from slot_dir):
  image_reel_1080x1920.png   — crypto-themed background (from image_gen)
  reel/01_script.txt         — narration script (from reel_writer)
  reel/03_captions.srt       — timed captions (from reel_writer)

Output:
  reel_final.mp4             — 1080x1920, 30fps, H.264, ready for TikTok/Reels/Shorts

TTS priority:
  1. ElevenLabs (ELEVENLABS_API_KEY in .env)  — best quality
  2. edge-tts   (Microsoft neural, free)       — very good, no API key
  3. RuntimeError if both fail

Install:
  pip install moviepy imageio-ffmpeg edge-tts

Usage:
  python src/agents/video_builder.py queue/2026-04-21_morning
  python src/agents/video_builder.py queue/2026-04-21_morning --elevenlabs
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

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
# SRT parser
# ---------------------------------------------------------------------------

def _ts_to_sec(ts: str) -> float:
    h, m, s_ms = ts.split(":")
    s, ms = s_ms.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_srt(srt_text: str) -> list[dict]:
    cues: list[dict] = []
    for block in re.split(r"\n\n+", srt_text.strip()):
        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue
        m = re.match(r"(\d+:\d+:\d+,\d+)\s+-->\s+(\d+:\d+:\d+,\d+)", lines[1] if len(lines) > 1 else "")
        if not m:
            continue
        text = " ".join(lines[2:]).strip()
        if text:
            cues.append({"start": _ts_to_sec(m.group(1)), "end": _ts_to_sec(m.group(2)), "text": text})
    return cues


# ---------------------------------------------------------------------------
# Script -> plain narration text
# ---------------------------------------------------------------------------

def extract_narration(script_text: str) -> str:
    lines = []
    for line in script_text.split("\n"):
        line = line.strip()
        if not line or line.startswith("[") or line.startswith("#"):
            continue
        line = re.sub(r"\[.*?\]", "", line).strip().strip('"').strip()
        if line:
            lines.append(line)
    text = " ".join(lines)
    # Remove characters TTS engines can't speak (arrows, box-drawing, etc.)
    text = re.sub(r"[^\x00-\x7F\u00C0-\u024F]", " ", text)  # keep Latin + basic Latin
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------

async def _edge_tts_save(text: str, out_path: Path, voice: str = "en-US-GuyNeural") -> None:
    import edge_tts
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(out_path))


def _tts_elevenlabs(text: str, out_path: Path, api_key: str) -> bool:
    import requests
    voice_id = "21m00Tcm4TlvDq8ikWAM"  # Rachel — clear, professional
    resp = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        json={
            "text": text,
            "model_id": "eleven_turbo_v2_5",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        },
        timeout=40,
    )
    if resp.status_code == 200:
        out_path.write_bytes(resp.content)
        return True
    print(f"[video_builder] ElevenLabs error {resp.status_code}: {resp.text[:120]}")
    return False


def generate_tts(text: str, out_dir: Path, use_elevenlabs: bool = False) -> Path:
    audio_path = out_dir / "narration.mp3"

    if use_elevenlabs:
        api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        if api_key:
            try:
                if _tts_elevenlabs(text, audio_path, api_key):
                    print(f"[video_builder] TTS: ElevenLabs -> {audio_path.name}")
                    return audio_path
            except Exception as exc:
                print(f"[video_builder] ElevenLabs failed ({exc}), trying edge-tts")

    # edge-tts (Microsoft neural, free)
    try:
        asyncio.run(_edge_tts_save(text, audio_path))
        print(f"[video_builder] TTS: edge-tts -> {audio_path.name}")
        return audio_path
    except Exception as exc:
        raise RuntimeError(
            f"edge-tts failed: {exc}. Install: pip install edge-tts"
        ) from exc


# ---------------------------------------------------------------------------
# Caption rendering (PIL — no ImageMagick required)
# ---------------------------------------------------------------------------

_FONT_PATHS = [
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/calibri.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Linux fallback
]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for p in _FONT_PATHS:
        if Path(p).exists():
            return ImageFont.truetype(p, size=size)
    return ImageFont.load_default()


def _word_wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    dummy = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(dummy)
    for word in words:
        test = " ".join(current + [word])
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] > max_width and current:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines


def render_caption_on_frame(bg_array: np.ndarray, text: str, width: int, height: int) -> np.ndarray:
    img = Image.fromarray(bg_array)
    draw = ImageDraw.Draw(img)

    font_size = max(44, width // 22)
    font = _load_font(font_size)
    line_h = font_size + 10

    lines = _word_wrap(text, font, width - 100)

    # Position: lower safe zone — works for both plain video (bg only)
    # and avatar video (avatar ends at ~y=1221 in 1920px frame)
    total_h = len(lines) * line_h
    y_start = height - 320 - total_h

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        text_w = bbox[2] - bbox[0]
        x = (width - text_w) // 2

        # Semi-transparent background pill
        pad = 12
        rect = [x - pad, y_start - pad, x + text_w + pad, y_start + font_size + pad]
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        odraw.rectangle(rect, fill=(0, 0, 0, 160))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

        # Outline
        for dx, dy in [(-2, -2), (-2, 2), (2, -2), (2, 2), (-2, 0), (2, 0), (0, -2), (0, 2)]:
            draw.text((x + dx, y_start + dy), line, fill=(0, 0, 0), font=font)
        # White text
        draw.text((x, y_start), line, fill=(255, 255, 255), font=font)
        y_start += line_h

    return np.array(img)


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------

def build_video(slot_dir: Path, use_elevenlabs: bool = False) -> Path:
    from moviepy import AudioFileClip, VideoClip

    slot_dir = Path(slot_dir).resolve()
    reel_dir = slot_dir / "reel"
    bg_path = slot_dir / "image_reel_1080x1920.png"
    script_path = reel_dir / "01_script.txt"
    srt_path = reel_dir / "03_captions.srt"
    out_path = slot_dir / "reel_final.mp4"

    # Validate inputs
    if not bg_path.exists():
        raise FileNotFoundError(
            f"Background image not found: {bg_path}\n"
            "Run the image stage first (pipeline --stop-after image)"
        )
    if not script_path.exists():
        raise FileNotFoundError(
            f"Script not found: {script_path}\n"
            "Run the reel stage first (pipeline --stop-after reel)"
        )

    # Parse inputs
    narration = extract_narration(script_path.read_text(encoding="utf-8"))
    if not narration.strip():
        raise ValueError("Script is empty after parsing. Check 01_script.txt.")

    srt_cues = parse_srt(srt_path.read_text(encoding="utf-8")) if srt_path.exists() else []
    print(f"[video_builder] narration: {len(narration.split())} words, {len(srt_cues)} caption cues")

    # TTS
    audio_path = generate_tts(narration, slot_dir, use_elevenlabs)

    # Audio duration
    audio_clip = AudioFileClip(str(audio_path))
    duration = audio_clip.duration
    print(f"[video_builder] audio duration: {duration:.1f}s")

    # Load background
    bg_img = Image.open(bg_path).convert("RGB")
    width, height = bg_img.size
    bg_array = np.array(bg_img)

    # Caption lookup
    def _caption_at(t: float) -> str | None:
        for cue in srt_cues:
            if cue["start"] <= t < cue["end"]:
                return cue["text"]
        return None

    # Frame generator
    def make_frame(t: float) -> np.ndarray:
        caption = _caption_at(t)
        if caption:
            return render_caption_on_frame(bg_array.copy(), caption, width, height)
        return bg_array

    # Build & export
    print(f"[video_builder] rendering {duration:.1f}s @ 30fps -> {out_path.name} ...")
    video_clip = VideoClip(frame_function=make_frame, duration=duration)
    video_clip = video_clip.with_audio(audio_clip)
    video_clip.write_videofile(
        str(out_path),
        fps=30,
        codec="libx264",
        audio_codec="aac",
        preset="fast",
        threads=4,
        logger=None,
    )
    audio_clip.close()
    video_clip.close()

    size_mb = out_path.stat().st_size / 1_048_576
    print(f"[video_builder] done -> {out_path}  ({size_mb:.1f} MB)")
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build a 9:16 MP4 reel from pipeline outputs")
    ap.add_argument("slot_dir", help="path to the queue slot directory")
    ap.add_argument("--elevenlabs", action="store_true",
                    help="use ElevenLabs TTS instead of edge-tts")
    args = ap.parse_args(argv)

    try:
        out = build_video(Path(args.slot_dir), use_elevenlabs=args.elevenlabs)
        print(f"[video_builder] OK: {out}")
        return 0
    except Exception as exc:
        print(f"[video_builder] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
