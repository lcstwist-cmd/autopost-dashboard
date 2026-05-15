"""Watchdog — keeps scheduler.py alive indefinitely. Only one instance allowed."""
from __future__ import annotations
import os, subprocess, sys, time, logging
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_PID_FILE = _ROOT / "watchdog.pid"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(message)s",
    handlers=[
        logging.FileHandler(_ROOT / "watchdog.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("watchdog")


def _acquire_lock() -> bool:
    if _PID_FILE.exists():
        try:
            pid = int(_PID_FILE.read_text().strip())
            if pid != os.getpid():
                # check if that PID is still alive
                try:
                    os.kill(pid, 0)
                    log.warning(f"Another watchdog already running (PID {pid}). Exiting.")
                    return False
                except OSError:
                    pass  # stale PID — overwrite
        except Exception:
            pass
    _PID_FILE.write_text(str(os.getpid()))
    return True


def run():
    if not _acquire_lock():
        sys.exit(0)
    try:
        restart_delay = 15
        while True:
            log.info("Starting scheduler...")
            try:
                proc = subprocess.Popen(
                    [sys.executable, str(_ROOT / "src" / "agents" / "scheduler.py")],
                    cwd=str(_ROOT),
                )
                exit_code = proc.wait()
                log.warning(f"Scheduler exited with code {exit_code} -- restarting in {restart_delay}s")
            except Exception as exc:
                log.error(f"Failed to start scheduler: {exc}")
            time.sleep(restart_delay)
    finally:
        _PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    run()
