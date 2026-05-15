"""setup_wizard.py — Onboarding interactiv pentru utilizatori noi.

Pași:
  1. Verifică Python + pip + dependențe (instalează ce lipsește)
  2. Te întreabă cheile obligatorii (D-ID, ANTHROPIC) + opționale
  3. Scrie .env
  4. Rulează check_health.py → arată ce merge / ce nu
  5. Face un test D-ID scurt — confirmă că totul e legat
  6. Rulează primul slot demo → deschide clipul

Rulează:
    python setup_wizard.py
    setup_wizard.bat         # variantă Windows double-click

Design: pentru utilizatori NE-TEHNICI. Fiecare pas explică ce face și de ce.
Zero jargon. Zero opțiuni obscure.
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from pathlib import Path

# Force UTF-8 output on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
_ENV_PATH = _HERE / ".env"
_ENV_EXAMPLE = _HERE / ".env.example"


# ---------------------------------------------------------------------------
# UX helpers
# ---------------------------------------------------------------------------

GREEN = "\033[92m"
RED   = "\033[91m"
YEL   = "\033[93m"
CYAN  = "\033[96m"
BOLD  = "\033[1m"
DIM   = "\033[2m"
END   = "\033[0m"

# Disable colors on Windows unless running in Terminal
if sys.platform == "win32":
    try:
        os.system("")  # enables ANSI
    except Exception:
        GREEN = RED = YEL = CYAN = BOLD = DIM = END = ""


def banner(t: str) -> None:
    print(f"\n{CYAN}{BOLD}{'═' * 68}{END}")
    print(f"{CYAN}{BOLD}  {t}{END}")
    print(f"{CYAN}{BOLD}{'═' * 68}{END}\n")


def ok(t: str)   -> None: print(f"  {GREEN}✓{END} {t}")
def bad(t: str)  -> None: print(f"  {RED}✗{END} {t}")
def warn(t: str) -> None: print(f"  {YEL}⚠{END} {t}")
def info(t: str) -> None: print(f"  {DIM}•{END} {t}")


def ask(question: str, default: str = "", secret: bool = False) -> str:
    """Prompt with default and optional secret mode (echo off)."""
    hint = f" [{default}]" if default else ""
    prompt = f"  {BOLD}?{END} {question}{hint}: "
    try:
        if secret:
            import getpass
            val = getpass.getpass(prompt).strip()
        else:
            val = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  (anulat)")
        sys.exit(1)
    return val or default


def yesno(question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    a = input(f"  {BOLD}?{END} {question} [{hint}]: ").strip().lower()
    if not a:
        return default
    return a in ("y", "yes", "da", "d")


# ---------------------------------------------------------------------------
# 1. Dependencies
# ---------------------------------------------------------------------------

def check_python() -> None:
    v = sys.version_info
    if v < (3, 10):
        bad(f"Python 3.10+ necesar. Ai {v.major}.{v.minor}.")
        print("  → descarcă-l de la https://python.org")
        sys.exit(1)
    ok(f"Python {v.major}.{v.minor}.{v.micro}")


def install_deps() -> None:
    req = _HERE / "requirements.txt"
    if not req.exists():
        warn("requirements.txt lipsește — sar peste instalare.")
        return
    info("Verific și instalez dependențe…")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(req),
         "-q", "--disable-pip-version-check"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        bad(f"pip install a eșuat:\n{result.stderr[-500:]}")
        sys.exit(2)
    ok("Dependențe Python instalate.")


# ---------------------------------------------------------------------------
# 2. .env collection
# ---------------------------------------------------------------------------

def load_existing_env() -> dict[str, str]:
    if not _ENV_PATH.exists():
        return {}
    env: dict[str, str] = {}
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def save_env(env: dict[str, str]) -> None:
    # Preserve structure of .env.example if exists
    template_lines: list[str] = []
    if _ENV_EXAMPLE.exists():
        template_lines = _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()

    written: set[str] = set()
    out_lines: list[str] = []
    for line in template_lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            k = stripped.split("=", 1)[0].strip()
            if k in env:
                out_lines.append(f"{k}={env[k]}")
                written.add(k)
                continue
        out_lines.append(line)

    # Append any keys not in the template
    extras = [k for k in env if k not in written]
    if extras:
        out_lines.append("")
        out_lines.append("# --- Adăugate de setup_wizard ---")
        for k in extras:
            out_lines.append(f"{k}={env[k]}")

    _ENV_PATH.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    ok(f".env salvat ({len(env)} variabile).")


def collect_credentials(existing: dict[str, str]) -> dict[str, str]:
    env = dict(existing)

    banner("Pas 2 — Chei API obligatorii")

    # D-ID
    print(f"  {BOLD}D-ID{END} — generează clipul cu avatar AI.")
    info("Obține cheia de la: https://studio.d-id.com → Account → API Keys")
    info("Format acceptat: 'email:secret' sau blob-ul base64 direct.")
    did = ask("DID_API_KEY", default=env.get("DID_API_KEY", ""), secret=True)
    if not did:
        bad("D-ID e obligatoriu pentru clipul cu avatar. Continuăm, dar avatar nu va merge.")
    env["DID_API_KEY"] = did

    print()

    # Anthropic
    print(f"  {BOLD}Anthropic (Claude){END} — opțional dar recomandat.")
    info("Obține de la: https://console.anthropic.com/settings/keys")
    info("Fără el, textele folosesc template-uri (funcționale dar mai generice).")
    anth = ask("ANTHROPIC_API_KEY", default=env.get("ANTHROPIC_API_KEY", ""), secret=True)
    env["ANTHROPIC_API_KEY"] = anth

    print()

    # Telegram
    if yesno("Vrei să postezi automat pe Telegram?", default=True):
        print(f"  {BOLD}Telegram{END} — creează bot cu @BotFather → /newbot")
        info("Copiază token-ul primit și ID-ul canalului/grupului.")
        tg_token = ask("TELEGRAM_BOT_TOKEN",
                       default=env.get("TELEGRAM_BOT_TOKEN", ""), secret=True)
        tg_chat = ask("TELEGRAM_CHAT_ID (ex: @canal_public sau -100123456789)",
                      default=env.get("TELEGRAM_CHAT_ID", ""))
        env["TELEGRAM_BOT_TOKEN"] = tg_token
        env["TELEGRAM_CHAT_ID"] = tg_chat

    print()

    # X / Twitter
    if yesno("Vrei să postezi automat pe X (Twitter)?", default=True):
        info("Cea mai simplă variantă: browser automation (gratuit).")
        info("Vei rula o singură dată 'python src/agents/x_login_helper.py' separat.")
        env["X_USERNAME"] = ask("X_USERNAME (username-ul tău X)",
                                default=env.get("X_USERNAME", ""))
        env["X_EMAIL"] = ask("X_EMAIL", default=env.get("X_EMAIL", ""))
        env["X_PASSWORD"] = ask("X_PASSWORD", default=env.get("X_PASSWORD", ""), secret=True)

    print()

    # Advanced (skip by default)
    if yesno("Vrei să configurezi și Instagram / TikTok / YouTube acum? (avansat)",
             default=False):
        info("Urmărește pas cu pas secțiunea 3 din REZOLVARI.md pentru fiecare.")
        info("Poți reveni oricând aici rulând `python setup_wizard.py` iar.")
        if yesno("Instagram?", default=False):
            env["IG_USER_ID"] = ask("IG_USER_ID", default=env.get("IG_USER_ID", ""))
            env["IG_ACCESS_TOKEN"] = ask("IG_ACCESS_TOKEN",
                                         default=env.get("IG_ACCESS_TOKEN", ""), secret=True)
        if yesno("TikTok?", default=False):
            env["TIKTOK_CLIENT_KEY"] = ask("TIKTOK_CLIENT_KEY",
                                           default=env.get("TIKTOK_CLIENT_KEY", ""))
            env["TIKTOK_CLIENT_SECRET"] = ask("TIKTOK_CLIENT_SECRET",
                                              default=env.get("TIKTOK_CLIENT_SECRET", ""), secret=True)
            env["TIKTOK_ACCESS_TOKEN"] = ask("TIKTOK_ACCESS_TOKEN",
                                             default=env.get("TIKTOK_ACCESS_TOKEN", ""), secret=True)
            env["TIKTOK_REFRESH_TOKEN"] = ask("TIKTOK_REFRESH_TOKEN",
                                              default=env.get("TIKTOK_REFRESH_TOKEN", ""), secret=True)
        if yesno("YouTube?", default=False):
            env["YT_CLIENT_ID"] = ask("YT_CLIENT_ID", default=env.get("YT_CLIENT_ID", ""))
            env["YT_CLIENT_SECRET"] = ask("YT_CLIENT_SECRET",
                                          default=env.get("YT_CLIENT_SECRET", ""), secret=True)
            env["YT_REFRESH_TOKEN"] = ask("YT_REFRESH_TOKEN",
                                          default=env.get("YT_REFRESH_TOKEN", ""), secret=True)

    return {k: v for k, v in env.items() if v}


# ---------------------------------------------------------------------------
# 3. Run checks
# ---------------------------------------------------------------------------

def run_health_check() -> int:
    banner("Pas 3 — Verificare finală (check_health.py)")
    script = _HERE / "check_health.py"
    if not script.exists():
        warn("check_health.py lipsește — sar peste.")
        return 0
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(_HERE),
    )
    return result.returncode


def run_did_test() -> int:
    banner("Pas 4 — Test D-ID (izolat)")
    script = _HERE / "test_did.py"
    if not script.exists():
        warn("test_did.py lipsește — sar peste.")
        return 0
    print("  Rulez un test scurt pe D-ID ca să verific că cheia merge.")
    print("  Dacă vezi erori, log-ul complet e în test_did_log.txt\n")
    result = subprocess.run(
        [sys.executable, str(script), "--text",
         "Welcome to AutoPost. This is a quick test of the avatar.",
         "--skip-download"],
        cwd=str(_HERE),
    )
    return result.returncode


def maybe_upload_presenter() -> None:
    """Oferă utilizatorului să urce o poză proprie la D-ID /images."""
    banner("Pas 5 — Poza ta pentru avatar (recomandat)")

    # Check if a cached/env URL already exists
    cache = _HERE / ".did_presenter_cache.json"
    env_url = os.environ.get("DID_PRESENTER_URL", "").strip()
    if env_url:
        ok(f"DID_PRESENTER_URL deja setat: {env_url[:60]}...")
        if not yesno("Vrei să-l schimbi cu o poză nouă?", default=False):
            return
    elif cache.exists():
        ok(f"Poză deja urcată (cache activ).")
        if not yesno("Vrei să schimbi cu altă poză?", default=False):
            return

    print(f"  D-ID poate refuza URL-uri externe (Pexels, Unsplash).")
    print(f"  Soluția sigură: urcăm poza TA direct la D-ID și primim")
    print(f"  un URL permanent, garantat compatibil.\n")
    print(f"  {BOLD}Poza recomandată:{END}")
    info("JPG sau PNG, max 10 MB")
    info("față clară, frontal, lumină bună")
    info("fără ochelari de soare, măști, filtre extreme")
    print()

    if not yesno("Vrei să urci o poză acum?", default=True):
        info("Ok, sar peste — clipurile vor folosi 'alice.jpg' din bucket-ul public D-ID.")
        info("Poți oricând să rulezi mai târziu:  python upload_presenter.py poza.jpg")
        return

    path = ask("Cale completă spre poza ta (ex: C:\\Users\\Intel\\Desktop\\poza.jpg)")
    if not path:
        warn("Cale goală — sar peste.")
        return
    img_path = Path(path.strip('"').strip("'"))
    if not img_path.exists():
        bad(f"Poza nu există: {img_path}")
        return

    try:
        from src.agents.avatar_video import upload_photo_to_did, _save_cached_presenter_url
        print(f"\n  Urc {img_path.name} ({img_path.stat().st_size // 1024} KB)...")
        url = upload_photo_to_did(img_path)
        _save_cached_presenter_url(url, source_path=str(img_path.resolve()))
        ok(f"Poză urcată. URL D-ID: {url[:70]}...")
        ok(f"Salvat în .did_presenter_cache.json — toate clipurile viitoare vor folosi poza ta.")
    except Exception as exc:
        bad(f"Upload a eșuat: {exc}")
        print("     Continuăm cu poza default (alice.jpg din bucket-ul public D-ID).")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    banner("AutoPost — Setup Wizard")
    print(f"  Acest wizard te ghidează să configurezi totul în {BOLD}5 minute{END}.")
    print(f"  Poți re-rula oricând dacă vrei să schimbi ceva.\n")

    # Pas 1
    banner("Pas 1 — Verificare mediu")
    check_python()
    install_deps()

    # Pas 2
    existing = load_existing_env()
    env = collect_credentials(existing)
    save_env(env)

    # Pas 3
    rc = run_health_check()

    # Pas 4
    did_ok = env.get("DID_API_KEY", "").strip() != ""
    if did_ok:
        # Pas 5: oferă upload poza proprie înainte de test final
        maybe_upload_presenter()

        did_rc = run_did_test()
        if did_rc == 0:
            ok("D-ID răspunde. Poți genera clipuri cu avatar.")
        else:
            warn("Testul D-ID a returnat erori. Vezi test_did_log.txt.")
    else:
        warn("D-ID nu e configurat — sar peste test.")

    # Done
    banner("Gata!")
    print(f"  Pentru prima rulare reală:\n")
    print(f"    {BOLD}python make_clip.py{END}")
    print(f"\n  Sau pornește dashboard-ul web (multi-user):\n")
    print(f"    {BOLD}python run_local.py{END}")
    print(f"\n  Documentație completă: REZOLVARI.md\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
