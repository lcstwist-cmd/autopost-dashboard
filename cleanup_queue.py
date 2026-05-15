"""Clean up old / manual / debug queue slots.

Usage:
    python cleanup_queue.py            # dry-run, show what would be deleted
    python cleanup_queue.py --delete   # actually delete

Rules:
  * Always keeps the latest 10 real (morning / evening) slots.
  * Always deletes "manual_*" slots older than 2 days.
  * Always deletes misplaced numeric subdirs (queue/1/, queue/2/ …).
"""
from __future__ import annotations

import argparse
import io
import re
import shutil
import sys
import time
from pathlib import Path

# Force UTF-8 output on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
QUEUE = ROOT / "queue"

SLOT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(morning|evening)$")


def _age_days(path: Path) -> float:
    return (time.time() - path.stat().st_mtime) / 86400


def collect_targets() -> list[tuple[Path, str]]:
    """Return (path, reason) tuples for directories we want to remove."""
    targets: list[tuple[Path, str]] = []
    if not QUEUE.exists():
        return targets

    # Debris: numeric dirs (queue/1, queue/2 …) — always delete
    for entry in QUEUE.iterdir():
        if entry.is_dir() and entry.name.isdigit():
            targets.append((entry, f"numeric debris dir"))

    # Manual slots older than 2 days
    for entry in QUEUE.iterdir():
        if entry.is_dir() and "manual" in entry.name:
            age = _age_days(entry)
            if age > 2:
                targets.append((entry, f"manual slot, {age:.1f}d old"))

    # Keep only the latest 10 real morning/evening slots
    real = sorted(
        [e for e in QUEUE.iterdir() if e.is_dir() and SLOT_RE.match(e.name)],
        key=lambda p: p.name,
    )
    if len(real) > 10:
        for old in real[:-10]:
            targets.append((old, "older than latest 10 real slots"))

    return targets


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--delete", action="store_true",
                    help="actually delete (default: dry-run)")
    args = ap.parse_args()

    targets = collect_targets()

    if not targets:
        print("[cleanup] nothing to clean — queue is tidy.")
        return 0

    print(f"[cleanup] {'DELETING' if args.delete else 'DRY-RUN'} "
          f"{len(targets)} director(y/ies):")
    total_bytes = 0
    for path, reason in targets:
        try:
            size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        except Exception:
            size = 0
        total_bytes += size
        print(f"  {'x' if args.delete else '-'} {path.relative_to(ROOT)}  "
              f"({size/1024/1024:.1f} MB) — {reason}")
        if args.delete:
            try:
                shutil.rmtree(path)
            except Exception as exc:
                print(f"    ERROR: {exc}", file=sys.stderr)

    print(f"[cleanup] total: {total_bytes/1024/1024:.1f} MB")
    if not args.delete:
        print("[cleanup] run with --delete to actually remove these.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
