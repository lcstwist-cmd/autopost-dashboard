#!/usr/bin/env python3
"""Test avatar video generation."""
import io
import sys
from pathlib import Path

# Force UTF-8 output on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:
    from dotenv import load_dotenv
    load_dotenv(_HERE / ".env")
except ImportError:
    pass

from src.agents.avatar_video import generate_avatar_clip

# Use a simple test script for now
script = "Bitcoin just hit seventy eight thousand dollars. This is a major milestone. The next level to watch is eighty thousand. Follow for daily updates."
print(f"[avatar] script: {script[:80]}...")

out_path = Path("queue/2026-04-23_evening/avatar_30s.mp4")
result = generate_avatar_clip(script, out_path, presenter="alex_suit")
print(f"[avatar] saved: {result}")
print(f"[avatar] size: {result.stat().st_size / (1024*1024):.1f} MB")
