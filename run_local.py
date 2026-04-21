"""
AutoPost Local Launcher
-----------------------
Porneste serverul FastAPI + tunel ngrok si afiseaza URL-ul public.

Utilizare:
    python run_local.py

Prima rulare:
    1. Instaleaza ngrok: https://ngrok.com/download (extrage ngrok.exe in acest folder sau in PATH)
    2. Creeaza cont gratuit la ngrok.com
    3. Ruleaza o data: ngrok config add-authtoken <token_tau>
    4. Ruleaza: python run_local.py
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT  = Path(__file__).resolve().parent
PORT  = int(os.environ.get("PORT", 8000))
NGROK = "ngrok"   # assumes ngrok.exe is in PATH or in this folder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print(msg: str, color: str = "") -> None:
    codes = {"green": "\033[92m", "red": "\033[91m", "yellow": "\033[93m",
             "cyan": "\033[96m", "bold": "\033[1m", "": ""}
    end = "\033[0m" if color else ""
    print(f"  {codes[color]}{msg}{end}")


def _check_port_free(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _kill_port(port: int) -> None:
    """Kill any process using the given port (Windows)."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                pid = line.strip().split()[-1]
                subprocess.run(["taskkill", "/PID", pid, "/F"],
                               capture_output=True, timeout=5)
    except Exception:
        pass


def _get_ngrok_url(timeout: int = 20) -> str | None:
    """Poll ngrok local API (port 4040) until a public HTTPS tunnel appears."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1)
        try:
            with urllib.request.urlopen(
                "http://localhost:4040/api/tunnels", timeout=2
            ) as resp:
                data = json.loads(resp.read())
                for t in data.get("tunnels", []):
                    if t.get("proto") == "https":
                        return t["public_url"]
        except Exception:
            pass
    return None


def _find_ngrok() -> str | None:
    """Return ngrok executable path or None if not found."""
    # Check local folder first
    for name in ("ngrok.exe", "ngrok"):
        local = ROOT / name
        if local.exists():
            return str(local)
    # Check PATH
    import shutil
    return shutil.which("ngrok")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    os.system("cls" if sys.platform == "win32" else "clear")
    print()
    _print("╔══════════════════════════════════════════════╗", "cyan")
    _print("║         AutoPost Dashboard — Local           ║", "cyan")
    _print("╚══════════════════════════════════════════════╝", "cyan")
    print()

    # ── Check Python deps ────────────────────────────────────────────────────
    _print("Verificare dependente...", "yellow")
    try:
        import fastapi, uvicorn, jinja2  # noqa: F401
        _print("✅ Dependente OK", "green")
    except ImportError as e:
        _print(f"❌ Lipsesc dependente: {e}", "red")
        _print("   Ruleaza: pip install -r requirements.txt", "yellow")
        sys.exit(1)

    # ── Load .env ────────────────────────────────────────────────────────────
    env_file = ROOT / ".env"
    if env_file.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file)
            _print("✅ .env incarcat", "green")
        except ImportError:
            pass
    else:
        _print("⚠  .env nu exista — credentialele se pun in Settings", "yellow")

    # ── Seed database if first run ───────────────────────────────────────────
    db_script = ROOT / "seed_db.py"
    db_file   = ROOT / "autopost.db"
    if not db_file.exists() and db_script.exists():
        _print("Creare baza de date...", "yellow")
        subprocess.run([sys.executable, str(db_script)], cwd=str(ROOT))

    # ── Free port if busy ────────────────────────────────────────────────────
    if not _check_port_free(PORT):
        _print(f"⚠  Portul {PORT} este ocupat — eliberez...", "yellow")
        _kill_port(PORT)
        time.sleep(1)

    # ── Start uvicorn ────────────────────────────────────────────────────────
    _print(f"Pornesc serverul pe portul {PORT}...", "yellow")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "src.dashboard.app:app",
         "--host", "0.0.0.0",
         "--port", str(PORT),
         "--app-dir", str(ROOT)],
        cwd=str(ROOT),
        stdout=open(ROOT / "server.log", "w"),
        stderr=subprocess.STDOUT,
    )
    time.sleep(2)

    if server.poll() is not None:
        _print("❌ Serverul a esuat la pornire. Verifica server.log", "red")
        sys.exit(1)

    _print(f"✅ Server pornit: http://localhost:{PORT}", "green")

    # ── Start ngrok ──────────────────────────────────────────────────────────
    ngrok_path = _find_ngrok()
    ngrok_proc = None
    public_url = None

    if ngrok_path:
        _print("Pornesc ngrok tunnel...", "yellow")
        ngrok_proc = subprocess.Popen(
            [ngrok_path, "http", str(PORT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        public_url = _get_ngrok_url(timeout=20)
    else:
        _print("⚠  ngrok nu a fost gasit.", "yellow")
        _print("   Descarca de la: https://ngrok.com/download", "yellow")
        _print("   Pune ngrok.exe in acelasi folder cu run_local.py", "yellow")
        _print("   Sau ruleaza: ngrok config add-authtoken <tokenul_tau>", "yellow")

    # ── Show result ──────────────────────────────────────────────────────────
    print()
    _print("══════════════════════════════════════════════", "cyan")
    if public_url:
        _print(f"🌐  URL PUBLIC (ngrok):  {public_url}", "bold")
        _print(f"🏠  URL local:           http://localhost:{PORT}", "green")
    else:
        _print(f"🏠  URL local:  http://localhost:{PORT}", "green")
        _print("    (ngrok indisponibil — doar acces local)", "yellow")
    _print("══════════════════════════════════════════════", "cyan")
    print()
    _print("Login cu:  lcstwist@gmail.com  /  parola din seed_db.py", "yellow")
    print()
    _print("Apasa Ctrl+C pentru a opri.", "yellow")
    print()

    # Open browser
    webbrowser.open(f"http://localhost:{PORT}")

    # ── Wait ─────────────────────────────────────────────────────────────────
    try:
        server.wait()
    except KeyboardInterrupt:
        print()
        _print("Oprire...", "yellow")
        server.terminate()
        if ngrok_proc:
            ngrok_proc.terminate()
        _print("Gata. La revedere!", "green")


if __name__ == "__main__":
    main()
