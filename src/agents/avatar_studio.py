"""Avatar Studio Agent — fully local AI avatar video generator.

No D-ID, no HeyGen, no external API required.

Lip-sync engine chain (best-to-fallback):
  1. Wav2Lip      — real lip sync on a photo/video frame (needs wav2lip pkg + weights)
  2. MuseTalk     — faster lip sync (needs musetalk pkg)
  3. MediaPipe    — facial-landmark jaw animation (pip install mediapipe)
  4. Local Loop   — reference video loop with TTS audio (always available)

TTS engine chain:
  1. ElevenLabs  — API, best quality (ELEVENLABS_API_KEY)
  2. Coqui TTS   — local, good quality (pip install TTS)
  3. edge-tts    — Microsoft Edge cloud TTS, free (pip install edge-tts)
  4. pyttsx3     — system TTS, always available (no install needed)

Platform presets:
  tiktok            1080×1920  9:16  up to 3 min
  youtube_shorts    1080×1920  9:16  up to 60 s
  youtube           1920×1080  16:9  any duration
  instagram_reel    1080×1920  9:16  up to 90 s
  instagram_feed    1080×1080  1:1

Usage:
    from src.agents.avatar_studio import AvatarStudio
    studio = AvatarStudio()
    profile_id = studio.create_profile(user_id=1, name="Main", photo_path=Path(...))
    job = studio.generate_video(profile_id=profile_id, script="Hello world!",
                                 platform="tiktok", output_dir=Path("queue/1"))
    print(job["output_path"])
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

_HERE       = Path(__file__).resolve().parent
_REPO_ROOT  = _HERE.parent.parent
_AVATARS_ROOT = _REPO_ROOT / "queue" / "avatars"   # shared profile photos storage

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Platform presets
# ---------------------------------------------------------------------------

PLATFORM_PRESETS: dict[str, dict] = {
    "tiktok": {
        "width": 1080, "height": 1920, "aspect": "9:16",
        "max_seconds": 180, "fps": 30,
        "label": "TikTok",
    },
    "youtube_shorts": {
        "width": 1080, "height": 1920, "aspect": "9:16",
        "max_seconds": 60, "fps": 30,
        "label": "YouTube Shorts",
    },
    "youtube": {
        "width": 1920, "height": 1080, "aspect": "16:9",
        "max_seconds": 3600, "fps": 30,
        "label": "YouTube",
    },
    "instagram_reel": {
        "width": 1080, "height": 1920, "aspect": "9:16",
        "max_seconds": 90, "fps": 30,
        "label": "Instagram Reel",
    },
    "instagram_feed": {
        "width": 1080, "height": 1080, "aspect": "1:1",
        "max_seconds": 60, "fps": 30,
        "label": "Instagram Feed",
    },
    "facebook_reel": {
        "width": 1080, "height": 1920, "aspect": "9:16",
        "max_seconds": 90, "fps": 30,
        "label": "Facebook Reel",
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, check=check)


def _audio_duration(audio_path: Path) -> float:
    r = subprocess.run(
        [_ffmpeg(), "-i", str(audio_path), "-f", "null", "-"],
        capture_output=True, text=True, errors="replace",
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return 20.0


# ---------------------------------------------------------------------------
# TTS engines
# ---------------------------------------------------------------------------

def _tts_elevenlabs(script: str, voice_id: str, out: Path) -> bool:
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        return False
    try:
        import requests as _req
        vid = voice_id or "21m00Tcm4TlvDq8ikWAM"  # Rachel
        r = _req.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
            headers={"xi-api-key": key, "Content-Type": "application/json"},
            json={"text": script, "model_id": "eleven_multilingual_v2",
                  "voice_settings": {"stability": 0.5, "similarity_boost": 0.8}},
            timeout=60,
        )
        if r.status_code == 200:
            out.write_bytes(r.content)
            print(f"[avatar_studio] TTS via ElevenLabs -> {out.name}")
            return True
        print(f"[avatar_studio] ElevenLabs TTS error {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        print(f"[avatar_studio] ElevenLabs TTS failed: {exc}")
    return False


def _tts_coqui(script: str, out: Path) -> bool:
    try:
        from TTS.api import TTS as _CoquiTTS
        tts = _CoquiTTS(model_name="tts_models/en/ljspeech/tacotron2-DDC", progress_bar=False)
        wav_tmp = out.with_suffix(".wav")
        tts.tts_to_file(text=script, file_path=str(wav_tmp))
        # Convert wav -> mp3
        _run([_ffmpeg(), "-y", "-i", str(wav_tmp),
              "-codec:a", "libmp3lame", "-qscale:a", "2", str(out)])
        wav_tmp.unlink(missing_ok=True)
        print(f"[avatar_studio] TTS via Coqui -> {out.name}")
        return True
    except ImportError:
        return False
    except Exception as exc:
        print(f"[avatar_studio] Coqui TTS failed: {exc}")
        return False


def _tts_edge(script: str, voice: str, out: Path) -> bool:
    """Microsoft Edge TTS — free, no API key, runs async."""
    try:
        import edge_tts as _edge
        voice_name = voice or "en-US-GuyNeural"

        async def _run_async():
            communicate = _edge.Communicate(script, voice_name)
            await communicate.save(str(out))

        asyncio.run(_run_async())
        if out.exists() and out.stat().st_size > 1000:
            print(f"[avatar_studio] TTS via edge-tts ({voice_name}) -> {out.name}")
            return True
    except ImportError:
        pass
    except Exception as exc:
        print(f"[avatar_studio] edge-tts failed: {exc}")
    return False


def _tts_pyttsx3(script: str, out: Path) -> bool:
    try:
        import pyttsx3
        wav_tmp = out.with_suffix(".wav")
        engine = pyttsx3.init()
        engine.setProperty("rate", 165)
        engine.save_to_file(script, str(wav_tmp))
        engine.runAndWait()
        _run([_ffmpeg(), "-y", "-i", str(wav_tmp),
              "-codec:a", "libmp3lame", "-qscale:a", "2", str(out)])
        wav_tmp.unlink(missing_ok=True)
        if out.exists():
            print(f"[avatar_studio] TTS via pyttsx3 -> {out.name}")
            return True
    except ImportError:
        pass
    except Exception as exc:
        print(f"[avatar_studio] pyttsx3 TTS failed: {exc}")
    return False


def generate_tts(script: str, voice_id: str, out_path: Path) -> Path:
    """Generate TTS audio for `script`. Tries providers in quality order."""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if _tts_elevenlabs(script, voice_id, out_path):
        return out_path
    if _tts_edge(script, voice_id, out_path):
        return out_path
    if _tts_coqui(script, out_path):
        return out_path
    if _tts_pyttsx3(script, out_path):
        return out_path

    raise RuntimeError("All TTS engines failed. Install edge-tts: pip install edge-tts")


# ---------------------------------------------------------------------------
# Lip-sync engines
# ---------------------------------------------------------------------------

def _lipsync_wav2lip(photo_path: Path, audio_path: Path, out: Path) -> bool:
    """Wav2Lip: best quality lip sync on a static photo."""
    try:
        import wav2lip  # noqa: F401
    except ImportError:
        try:
            # Try installed via the inference script path
            import importlib.util
            w2l_path = shutil.which("wav2lip_infer") or ""
            if not w2l_path:
                return False
        except Exception:
            return False

    weights = _REPO_ROOT / "models" / "wav2lip_gan.pth"
    if not weights.exists():
        weights = _REPO_ROOT / "models" / "wav2lip.pth"
    if not weights.exists():
        print("[avatar_studio] Wav2Lip weights not found in models/. Skipping.")
        return False

    try:
        cmd = [
            sys.executable, "-m", "wav2lip.inference",
            "--checkpoint_path", str(weights),
            "--face", str(photo_path),
            "--audio", str(audio_path),
            "--outfile", str(out),
            "--nosmooth",
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=300)
        if r.returncode == 0 and out.exists():
            print(f"[avatar_studio] Wav2Lip -> {out.name}")
            return True
        print(f"[avatar_studio] Wav2Lip failed: {r.stderr[-300:].decode(errors='replace')}")
    except Exception as exc:
        print(f"[avatar_studio] Wav2Lip error: {exc}")
    return False


def _lipsync_mediapipe(photo_path: Path, audio_path: Path, out: Path) -> bool:
    """MediaPipe Face Mesh jaw animation — better than basic RMS but no real lip sync."""
    try:
        import mediapipe as mp
        import numpy as np
        from PIL import Image
    except ImportError:
        return False

    try:
        face_mesh = mp.solutions.face_mesh.FaceMesh(
            static_image_mode=True, max_num_faces=1,
            refine_landmarks=True, min_detection_confidence=0.5,
        )
        img = Image.open(photo_path).convert("RGB")
        w, h = img.size
        # Center-crop to portrait if landscape
        if w > h:
            diff = (w - h) // 2
            img = img.crop((diff, 0, diff + h, h))
            w = h
        arr = __import__("numpy").array(img)
        results = face_mesh.process(arr)

        if not results.multi_face_landmarks:
            # No face detected — fall through
            return False

        lm = results.multi_face_landmarks[0].landmark

        # Mouth landmarks: upper lip (13) and lower lip (14) in normalized coords
        upper_lip_y = lm[13].y * h
        lower_lip_y = lm[14].y * h
        chin_y      = lm[152].y * h
        forehead_y  = lm[10].y * h

        # Region heights for targeted animation
        mouth_top    = int(upper_lip_y - (lower_lip_y - upper_lip_y) * 0.5)
        mouth_bottom = int(chin_y)
        face_top     = int(forehead_y)
        face_bottom  = int(chin_y)

        face_mesh.close()

        # Now do energy-based animation with better landmarks
        from src.agents.avatar_providers.local_provider import (
            _frame_energies, _audio_duration as _dur, _get_ffmpeg,
            _HEAD_BOB_AMP, _HEAD_BOB_HZ, _BLINK_INTERVAL_S,
            _BLINK_DUR_FRAMES, _BLINK_DARKENING,
        )
        import math, random

        fps       = 25.0
        duration  = _dur(audio_path)
        n_frames  = int(duration * fps) + 1
        energies  = _frame_energies(audio_path, fps, n_frames)

        base = np.array(img.resize((w, w if w == h else int(w * 1920 / 1080)),
                                    Image.LANCZOS), dtype=np.uint8)
        fh, fw = base.shape[:2]

        # Recalculate landmark positions after resize
        scale_y = fh / h
        jaw_region_top    = int(mouth_top * scale_y)
        jaw_region_bottom = min(fh, int(mouth_bottom * scale_y))
        eye_y0            = int(face_top * scale_y)
        eye_y1            = int((upper_lip_y * 0.4 + face_top * 0.6) * scale_y)

        max_jaw_px = max(4, int((jaw_region_bottom - jaw_region_top) * 0.18))

        blink_frames: dict[int, float] = {}
        rng = random.Random(99)
        fi = 0
        while fi < n_frames:
            interval = rng.gauss(_BLINK_INTERVAL_S * fps, fps * 0.4)
            fi += max(int(fps * 1.5), int(interval))
            if fi < n_frames:
                for d in range(_BLINK_DUR_FRAMES):
                    fj = fi + d
                    if fj < n_frames:
                        pk = 1.0 - abs(d - _BLINK_DUR_FRAMES / 2) / (_BLINK_DUR_FRAMES / 2)
                        blink_frames[fj] = float(pk)

        cmd = [
            _get_ffmpeg(), "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{fw}x{fh}",
            "-r", str(fps),
            "-i", "pipe:0",
            "-i", str(audio_path),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            "-pix_fmt", "yuv420p",
            "-shortest", "-t", str(duration + 0.1),
            str(out),
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

        try:
            for i in range(n_frames):
                t      = i / fps
                energy = float(energies[i]) if i < len(energies) else 0.0
                frame  = base.copy()

                # Jaw open: shift chin region down
                jaw_px = int(energy * max_jaw_px)
                if jaw_px > 0:
                    src_h = fh - jaw_region_top - jaw_px
                    if src_h > 0:
                        result = frame.copy()
                        result[jaw_region_top + jaw_px: fh] = \
                            frame[jaw_region_top: jaw_region_top + src_h]
                        top_row = frame[jaw_region_top - 1].astype(np.float32)
                        bot_row = frame[jaw_region_top].astype(np.float32)
                        for g in range(jaw_px):
                            t2 = (g + 1) / (jaw_px + 1)
                            result[jaw_region_top + g] = (
                                top_row * (1 - t2) + bot_row * t2
                            ).astype(np.uint8)
                        frame = result

                # Head bob
                bob_px = int(_HEAD_BOB_AMP * math.sin(2 * math.pi * _HEAD_BOB_HZ * t))
                if bob_px:
                    result = frame.copy()
                    if bob_px > 0:
                        result[bob_px:] = frame[: fh - bob_px]
                        result[:bob_px] = frame[0]
                    else:
                        s = -bob_px
                        result[: fh - s] = frame[s:]
                        result[fh - s:]  = frame[-1]
                    frame = result

                # Blink
                blink_int = blink_frames.get(i, 0.0)
                if blink_int > 0 and eye_y1 > eye_y0:
                    frame[eye_y0:eye_y1] = (
                        frame[eye_y0:eye_y1]
                        * (1 - blink_int * (1 - _BLINK_DARKENING))
                    ).clip(0, 255).astype(np.uint8)

                proc.stdin.write(frame.tobytes())
        finally:
            proc.stdin.close()

        _, stderr = proc.communicate()
        if proc.returncode == 0 and out.exists():
            print(f"[avatar_studio] MediaPipe animation -> {out.name}")
            return True
        print(f"[avatar_studio] MediaPipe FFmpeg failed: "
              f"{stderr[-200:].decode(errors='replace')}")
    except Exception as exc:
        print(f"[avatar_studio] MediaPipe lipsync error: {exc}")
    return False


def _lipsync_reference_loop(audio_path: Path, out: Path) -> bool:
    """Loop the reference presenter video and mux new TTS audio — always available."""
    try:
        from src.agents.avatar_providers.local_provider import (
            _extract_reference_presenter, _loop_presenter_with_audio,
        )
        template = _extract_reference_presenter()
        if template is None:
            return False
        _loop_presenter_with_audio(template, audio_path, out)
        print(f"[avatar_studio] reference loop -> {out.name}")
        return True
    except Exception as exc:
        print(f"[avatar_studio] reference loop failed: {exc}")
    return False


def _lipsync_photo_basic(photo_path: Path, audio_path: Path, out: Path) -> bool:
    """Basic RMS jaw animation — fallback, no extra dependencies."""
    try:
        from src.agents.avatar_providers.local_provider import (
            generate_local_talking_head,
        )
        generate_local_talking_head(
            photo_url="",
            audio_path=audio_path,
            output_path=out,
        )
        print(f"[avatar_studio] basic photo animation -> {out.name}")
        return True
    except Exception as exc:
        print(f"[avatar_studio] basic photo animation failed: {exc}")
    return False


def generate_talking_head(
    photo_path: Path | None,
    audio_path: Path,
    out_path: Path,
) -> Path:
    """Generate a talking-head clip from photo + audio, best engine available."""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if photo_path and photo_path.exists():
        if _lipsync_wav2lip(photo_path, audio_path, out_path):
            return out_path
        if _lipsync_mediapipe(photo_path, audio_path, out_path):
            return out_path

    # Reference video loop (no photo needed)
    if _lipsync_reference_loop(audio_path, out_path):
        return out_path

    # Basic photo animation last resort
    if photo_path and photo_path.exists():
        if _lipsync_photo_basic(photo_path, audio_path, out_path):
            return out_path

    raise RuntimeError("All lip-sync engines failed. At minimum, ensure "
                       "imageio-ffmpeg, numpy, and Pillow are installed.")


# ---------------------------------------------------------------------------
# Platform compositor
# ---------------------------------------------------------------------------

def composite_for_platform(
    talking_head: Path,
    bg_image: Path | None,
    platform: str,
    out_path: Path,
    caption_text: str = "",
    brand_handle: str = "",
    brand_color: str = "#F59E0B",
) -> Path:
    """Overlay talking head on background, crop/pad to platform spec."""
    preset = PLATFORM_PRESETS.get(platform, PLATFORM_PRESETS["tiktok"])
    tw, th = preset["width"], preset["height"]
    ff     = _ffmpeg()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if platform in ("youtube",):
        # Landscape: talking head centered, background fills frame
        _composite_landscape(ff, talking_head, bg_image, tw, th, out_path,
                             brand_handle, brand_color)
    else:
        # Portrait 9:16 (TikTok / YouTube Shorts / Instagram Reel)
        _composite_portrait(ff, talking_head, bg_image, tw, th, out_path,
                            brand_handle, brand_color)

    return out_path


def _composite_portrait(
    ff: str, talking_head: Path, bg_image: Path | None,
    tw: int, th: int, out: Path,
    brand_handle: str, brand_color: str,
) -> None:
    """Full-frame presenter over news image, 9:16 portrait."""
    # Background chain
    if bg_image and bg_image.exists():
        bg_input  = ["-loop", "1", "-i", str(bg_image)]
        bg_chain  = (
            f"[1:v]scale={tw}:{th}:force_original_aspect_ratio=increase,"
            f"crop={tw}:{th},setsar=1"
            f"[bg]"
        )
        # Get talking-head duration
        duration = _audio_duration(talking_head)
        bg_input += ["-t", str(duration + 0.5)]
    else:
        # Animated gradient fallback
        bg_input = []
        bg_chain = (
            f"color=c=0x0F172A:size={tw}x{th}:rate=30[bg]"
        )

    # Presenter fills full frame; top 25% fades out to reveal the bg (news image)
    filter_complex = (
        f"{bg_chain};"
        f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=increase,"
        f"crop={tw}:{th},setsar=1,format=rgba,"
        f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
        f"a='255*clip(Y/({th}*0.25)\\,0\\,1)'"
        f"[presenter];"
        f"[bg][presenter]overlay=0:0[v0]"
    )

    inputs = [ff, "-y", "-i", str(talking_head)] + bg_input
    cmd = inputs + [
        "-filter_complex", filter_complex,
        "-map", "[v0]", "-map", "0:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out),
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"[avatar_studio] composite failed: "
            f"{r.stderr[-400:].decode(errors='replace')}"
        )


def _composite_landscape(
    ff: str, talking_head: Path, bg_image: Path | None,
    tw: int, th: int, out: Path,
    brand_handle: str, brand_color: str,
) -> None:
    """Talking head right-side, news image left, 16:9 landscape."""
    presenter_w = tw // 2
    presenter_h = th

    if bg_image and bg_image.exists():
        bg_input = ["-loop", "1", "-i", str(bg_image)]
        bg_chain = (
            f"[1:v]scale={tw // 2}:{th}:force_original_aspect_ratio=increase,"
            f"crop={tw // 2}:{th},setsar=1[bg]"
        )
        duration = _audio_duration(talking_head)
        bg_input += ["-t", str(duration + 0.5)]
    else:
        bg_input = []
        bg_chain = f"color=c=0x0F172A:size={tw // 2}x{th}:rate=30[bg]"

    filter_complex = (
        f"{bg_chain};"
        f"color=c=0x1E293B:size={tw}x{th}:rate=30[canvas];"
        f"[canvas][bg]overlay=0:0[left_half];"
        f"[0:v]scale={presenter_w}:{presenter_h}:force_original_aspect_ratio=increase,"
        f"crop={presenter_w}:{presenter_h},setsar=1[presenter];"
        f"[left_half][presenter]overlay={tw // 2}:0[v0]"
    )

    inputs = [ff, "-y", "-i", str(talking_head)] + bg_input
    cmd = inputs + [
        "-filter_complex", filter_complex,
        "-map", "[v0]", "-map", "0:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out),
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"[avatar_studio] landscape composite failed: "
            f"{r.stderr[-400:].decode(errors='replace')}"
        )


# ---------------------------------------------------------------------------
# Profile data model
# ---------------------------------------------------------------------------

@dataclass
class AvatarProfile:
    id:          int
    user_id:     int
    name:        str
    photo_path:  str       # relative to _AVATARS_ROOT / str(user_id)
    voice_id:    str = ""  # ElevenLabs voice id, edge-tts voice name, or ""
    voice_name:  str = ""
    settings:    dict = field(default_factory=dict)
    is_active:   bool = True
    created_at:  str  = ""
    updated_at:  str  = ""

    def full_photo_path(self) -> Path | None:
        if not self.photo_path:
            return None
        p = _AVATARS_ROOT / str(self.user_id) / self.photo_path
        return p if p.exists() else None


# ---------------------------------------------------------------------------
# Database helpers (thin wrappers around database.py connection)
# ---------------------------------------------------------------------------

def _db():
    from src.dashboard.database import _conn
    return _conn()


def _ensure_tables() -> None:
    with _db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS avatar_profiles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            name        TEXT    NOT NULL,
            photo_path  TEXT    DEFAULT '',
            voice_id    TEXT    DEFAULT '',
            voice_name  TEXT    DEFAULT '',
            settings    TEXT    DEFAULT '{}',
            is_active   INTEGER DEFAULT 1,
            created_at  TEXT    DEFAULT (datetime('now')),
            updated_at  TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS avatar_jobs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL,
            profile_id     INTEGER,
            status         TEXT    DEFAULT 'queued',
            platform       TEXT    DEFAULT 'tiktok',
            script         TEXT    DEFAULT '',
            bg_image       TEXT    DEFAULT '',
            output_path    TEXT    DEFAULT '',
            duration_s     REAL    DEFAULT 0,
            engine         TEXT    DEFAULT '',
            error          TEXT    DEFAULT '',
            progress       INTEGER DEFAULT 0,
            video_path     TEXT    DEFAULT '',
            video_filename TEXT    DEFAULT '',
            video_url      TEXT    DEFAULT '',
            file_size_mb   REAL    DEFAULT 0,
            created_at     TEXT    DEFAULT (datetime('now')),
            updated_at     TEXT    DEFAULT (datetime('now'))
        );
        """)
        # Migrate: add columns to existing tables (safe — no-op if already present)
        for col, defval in [
            ("progress",       "INTEGER DEFAULT 0"),
            ("video_path",     "TEXT DEFAULT ''"),
            ("video_filename", "TEXT DEFAULT ''"),
            ("video_url",      "TEXT DEFAULT ''"),
            ("file_size_mb",   "REAL DEFAULT 0"),
        ]:
            try:
                c.execute(f"ALTER TABLE avatar_jobs ADD COLUMN {col} {defval}")
            except Exception:
                pass


def get_profiles(user_id: int) -> list[AvatarProfile]:
    _ensure_tables()
    with _db() as c:
        rows = c.execute(
            "SELECT * FROM avatar_profiles WHERE user_id=? AND is_active=1 ORDER BY id DESC",
            (user_id,),
        ).fetchall()
    return [AvatarProfile(
        id=r["id"], user_id=r["user_id"], name=r["name"],
        photo_path=r["photo_path"], voice_id=r["voice_id"],
        voice_name=r["voice_name"],
        settings=json.loads(r["settings"] or "{}"),
        is_active=bool(r["is_active"]),
        created_at=r["created_at"], updated_at=r["updated_at"],
    ) for r in rows]


def get_profile(profile_id: int, user_id: int) -> AvatarProfile | None:
    _ensure_tables()
    with _db() as c:
        r = c.execute(
            "SELECT * FROM avatar_profiles WHERE id=? AND user_id=?",
            (profile_id, user_id),
        ).fetchone()
    if not r:
        return None
    return AvatarProfile(
        id=r["id"], user_id=r["user_id"], name=r["name"],
        photo_path=r["photo_path"], voice_id=r["voice_id"],
        voice_name=r["voice_name"],
        settings=json.loads(r["settings"] or "{}"),
        is_active=bool(r["is_active"]),
        created_at=r["created_at"], updated_at=r["updated_at"],
    )


def save_profile(
    user_id: int, name: str, photo_path: str = "",
    voice_id: str = "", voice_name: str = "",
    settings: dict | None = None, profile_id: int | None = None,
) -> int:
    _ensure_tables()
    settings_json = json.dumps(settings or {})
    with _db() as c:
        if profile_id:
            c.execute(
                """UPDATE avatar_profiles SET name=?, photo_path=?, voice_id=?,
                   voice_name=?, settings=?, updated_at=datetime('now')
                   WHERE id=? AND user_id=?""",
                (name, photo_path, voice_id, voice_name, settings_json, profile_id, user_id),
            )
            return profile_id
        cur = c.execute(
            """INSERT INTO avatar_profiles (user_id, name, photo_path, voice_id,
               voice_name, settings) VALUES (?,?,?,?,?,?)""",
            (user_id, name, photo_path, voice_id, voice_name, settings_json),
        )
        return cur.lastrowid


def delete_profile(profile_id: int, user_id: int) -> bool:
    _ensure_tables()
    with _db() as c:
        c.execute(
            "UPDATE avatar_profiles SET is_active=0 WHERE id=? AND user_id=?",
            (profile_id, user_id),
        )
    return True


def get_jobs(user_id: int, limit: int = 20) -> list[dict]:
    _ensure_tables()
    with _db() as c:
        rows = c.execute(
            """SELECT j.*, p.name as profile_name FROM avatar_jobs j
               LEFT JOIN avatar_profiles p ON j.profile_id = p.id
               WHERE j.user_id=? ORDER BY j.id DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _create_job(user_id: int, profile_id: int | None, platform: str,
                script: str, bg_image: str = "") -> int:
    _ensure_tables()
    with _db() as c:
        cur = c.execute(
            """INSERT INTO avatar_jobs (user_id, profile_id, platform, script, bg_image)
               VALUES (?,?,?,?,?)""",
            (user_id, profile_id, platform, script, bg_image),
        )
        return cur.lastrowid


def _update_job(job_id: int, **kwargs) -> None:
    if not kwargs:
        return
    sets = ", ".join(f"{k}=?" for k in kwargs)
    sets += ", updated_at=datetime('now')"
    with _db() as c:
        c.execute(
            f"UPDATE avatar_jobs SET {sets} WHERE id=?",
            list(kwargs.values()) + [job_id],
        )


def get_job(job_id: int) -> dict | None:
    _ensure_tables()
    with _db() as c:
        r = c.execute("SELECT * FROM avatar_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------------------
# Main studio class
# ---------------------------------------------------------------------------

class AvatarStudio:
    """Top-level API used by the dashboard and pipeline."""

    # Which engine was actually used for the last generate_video call
    last_engine: str = ""

    def upload_photo(self, user_id: int, file_bytes: bytes,
                     original_name: str) -> str:
        """Save uploaded photo, return relative filename."""
        user_dir = _AVATARS_ROOT / str(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        ext  = Path(original_name).suffix.lower() or ".jpg"
        name = f"avatar_{uuid.uuid4().hex[:10]}{ext}"
        (user_dir / name).write_bytes(file_bytes)
        return name

    def create_profile(
        self,
        user_id: int,
        name: str,
        photo_path: str = "",
        voice_id: str = "",
        voice_name: str = "",
        settings: dict | None = None,
    ) -> int:
        return save_profile(
            user_id=user_id, name=name, photo_path=photo_path,
            voice_id=voice_id, voice_name=voice_name, settings=settings,
        )

    def update_profile(
        self,
        profile_id: int,
        user_id: int,
        **kwargs,
    ) -> bool:
        existing = get_profile(profile_id, user_id)
        if not existing:
            return False
        save_profile(
            user_id=user_id,
            name=kwargs.get("name", existing.name),
            photo_path=kwargs.get("photo_path", existing.photo_path),
            voice_id=kwargs.get("voice_id", existing.voice_id),
            voice_name=kwargs.get("voice_name", existing.voice_name),
            settings=kwargs.get("settings", existing.settings),
            profile_id=profile_id,
        )
        return True

    def generate_video(
        self,
        user_id: int,
        script: str,
        platform: str = "tiktok",
        profile_id: int | None = None,
        bg_image: Path | None = None,
        output_dir: Path | None = None,
        brand_handle: str = "",
        brand_color: str = "#F59E0B",
    ) -> dict[str, Any]:
        """Generate a full avatar video.  Returns a result dict with output_path."""
        t0 = time.time()
        preset    = PLATFORM_PRESETS.get(platform, PLATFORM_PRESETS["tiktok"])
        out_root  = output_dir or (_REPO_ROOT / "queue" / str(user_id) / "avatar_output")
        out_root.mkdir(parents=True, exist_ok=True)

        job_id = _create_job(user_id, profile_id, platform, script,
                             str(bg_image or ""))
        _update_job(job_id, status="running")

        try:
            profile = get_profile(profile_id, user_id) if profile_id else None
            voice_id = (profile.voice_id if profile else "") or ""
            photo    = profile.full_photo_path() if profile else None

            slug = platform + "_" + uuid.uuid4().hex[:8]

            # Step 1: TTS
            audio_out = out_root / f"{slug}_tts.mp3"
            generate_tts(script, voice_id, audio_out)

            # Step 2: Talking head
            head_out = out_root / f"{slug}_head.mp4"
            generate_talking_head(photo, audio_out, head_out)

            # Step 3: Platform composite
            final_out = out_root / f"{slug}_final.mp4"
            composite_for_platform(
                head_out, bg_image, platform, final_out,
                caption_text=script[:100],
                brand_handle=brand_handle,
                brand_color=brand_color,
            )

            # Cleanup intermediates
            audio_out.unlink(missing_ok=True)
            head_out.unlink(missing_ok=True)

            duration = _audio_duration(final_out)
            _update_job(job_id, status="done",
                        output_path=str(final_out), duration_s=round(duration, 1))

            elapsed = round(time.time() - t0, 1)
            size_mb = round(final_out.stat().st_size / 1_048_576, 2) if final_out.exists() else 0
            print(f"[avatar_studio] job {job_id} done in {elapsed}s "
                  f"-> {final_out.name} ({size_mb} MB)")

            return {
                "job_id":      job_id,
                "status":      "done",
                "output_path": str(final_out),
                "platform":    platform,
                "preset":      preset["label"],
                "duration_s":  round(duration, 1),
                "size_mb":     size_mb,
                "elapsed_s":   elapsed,
            }

        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            _update_job(job_id, status="failed", error=str(exc)[:500])
            print(f"[avatar_studio] job {job_id} failed: {exc}\n{tb}")
            return {
                "job_id": job_id,
                "status": "failed",
                "error":  str(exc),
            }

    def available_engines(self) -> dict[str, bool]:
        """Report which optional engines are installed."""
        engines: dict[str, bool] = {}
        try:
            import wav2lip  # noqa
            engines["wav2lip"] = True
        except ImportError:
            engines["wav2lip"] = False
        try:
            import mediapipe  # noqa
            engines["mediapipe"] = True
        except ImportError:
            engines["mediapipe"] = False
        try:
            import edge_tts  # noqa
            engines["edge_tts"] = True
        except ImportError:
            engines["edge_tts"] = False
        try:
            from TTS.api import TTS  # noqa
            engines["coqui_tts"] = True
        except ImportError:
            engines["coqui_tts"] = False
        try:
            import pyttsx3  # noqa
            engines["pyttsx3"] = True
        except ImportError:
            engines["pyttsx3"] = False
        engines["elevenlabs"] = bool(os.environ.get("ELEVENLABS_API_KEY"))
        engines["reference_video"] = bool(
            (Path.home() / "Desktop" /
             "The SEC just approved 91 crypto ETFs. Everything changes now. #crypto #sol #xrp #ltc #btc.mp4"
             ).exists()
        )
        return engines


# Module-level singleton
studio = AvatarStudio()
