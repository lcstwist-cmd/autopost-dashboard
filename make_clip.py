"""make_clip.py — Fă un clip cap-coadă cu un singur script.

Rulează întregul flux:
  News Scout → Ranker → Copywriter → Image Gen → Reel/Avatar Writer
  → Video (cu TTS) → Avatar Video (D-ID)

La final, îți arată exact unde e clipul și îl deschide automat.

Utilizare:
    python make_clip.py                 # slot implicit: auto (morning/evening în funcție de oră)
    python make_clip.py --slot morning  # force morning
    python make_clip.py --hours 24      # caută știri din ultimele 24h (default: 12)
    python make_clip.py --presenter alex_suit   # presenter D-ID specific
    python make_clip.py --no-open       # nu deschide clipul la final

Ieșire garantată:
    queue/<data>_<slot>/reel_avatar.mp4   ← clipul final cu avatar AI (max 20s)
    queue/<data>_<slot>/reel_final.mp4    ← varianta cu TTS fără avatar (backup)
"""
from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 output on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _print_step(title: str, step: int, total: int) -> None:
    bar = "─" * 62
    print(f"\n{bar}")
    print(f"  [{step}/{total}] {title}")
    print(bar)


def _check_prereqs() -> list[str]:
    """Return a list of blocker messages (empty list means we can proceed)."""
    issues: list[str] = []

    # Python deps
    missing = []
    for mod in ("feedparser", "requests", "dateutil", "PIL",
                "moviepy", "imageio_ffmpeg", "dotenv"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        issues.append(
            f"Dependențe Python lipsă: {', '.join(missing)}\n"
            f"   Rezolvare: python -m pip install -r requirements.txt"
        )

    # .env
    env_path = _HERE / ".env"
    if not env_path.exists():
        issues.append(
            f".env nu există la {env_path}\n"
            f"   Rezolvare: copiază .env.example în .env și completează cheile"
        )
        return issues

    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
    except Exception:
        pass

    # D-ID key required for avatar
    did_key = os.environ.get("DID_API_KEY", "").strip()
    if not did_key:
        issues.append(
            "DID_API_KEY nu e setat în .env\n"
            "   Fără el nu pot genera avatarul AI.\n"
            "   Ia cheia de la studio.d-id.com → Account → API Keys"
        )

    return issues


def _open_file(path: Path) -> None:
    """Open a file with the default OS application (Windows/Mac/Linux)."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception as exc:
        print(f"   (Nu am putut deschide automat fișierul: {exc})")


def _default_slot() -> str:
    """morning before 14:00 local, evening after."""
    return "morning" if datetime.now().hour < 14 else "evening"


def main() -> int:
    # Ensure UTF-8 in subprocess environment (for any spawned processes)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", choices=["morning", "evening"], default=None)
    ap.add_argument("--hours", type=int, default=24,
                    help="câte ore înapoi să caute știri (default 24)")
    ap.add_argument("--presenter", default="default",
                    help="presenter D-ID (default, alex_suit, anita, sophia, ...)")
    ap.add_argument("--out-root", default="queue")
    ap.add_argument("--no-open", action="store_true",
                    help="nu deschide clipul la final")
    ap.add_argument("--skip-preflight", action="store_true")
    ap.add_argument("--slot-dir", default=None,
                    help="sari peste pipeline și doar regenerează "
                         "reel_final.mp4 + reel_avatar.mp4 într-un slot "
                         "existent (ex: queue/2026-04-23_evening)")
    args = ap.parse_args()

    slot = args.slot or _default_slot()
    print(f"\n  ╔══════════════════════════════════════════════════════════════╗")
    print(f"  ║             Crypto AutoPost — Full Pipeline Run              ║")
    print(f"  ║                                                              ║")
    print(f"  ║  Slot:      {slot:<48}     ║")
    print(f"  ║  Ore știri: {args.hours:<48}     ║")
    print(f"  ║  Presenter: {args.presenter:<48}     ║")
    print(f"  ╚══════════════════════════════════════════════════════════════╝")

    # --- Pre-flight -------------------------------------------------------
    if not args.skip_preflight:
        _print_step("Verificare dependențe și configurație", 1, 7)
        issues = _check_prereqs()
        if issues:
            print("\n  ✗ Probleme de rezolvat ÎNAINTE de rulare:\n")
            for i, msg in enumerate(issues, 1):
                print(f"   {i}. {msg}\n")
            print("  Rulează scriptul din nou după ce rezolvi problemele de mai sus.\n")
            return 1
        print("  ✓ Toate dependențele + cheile obligatorii sunt OK")

        # Optional: D-ID pre-flight against live API
        try:
            from src.agents.avatar_video import check_did_credentials
            ok, msg = check_did_credentials()
            prefix = "  ✓" if ok else "  ⚠"
            print(f"{prefix} D-ID: {msg}")
            if not ok:
                print("\n  Nu continuăm fără D-ID funcțional. Rezolvă și rulează din nou.")
                return 1
        except Exception as exc:
            print(f"  ⚠ D-ID pre-flight a eșuat: {exc}")
            # Non-fatal — let the real request give a detailed error later

    # --- Pipeline run sau mod recovery ------------------------------------
    if args.slot_dir:
        # Recovery-only mode: sari peste scout/rank/copy/image/reel
        out_dir = Path(args.slot_dir).resolve()
        if not out_dir.exists():
            print(f"\n  ✗ Slot-dir nu există: {out_dir}")
            return 2
        _print_step(f"Mod recovery — re-generez video pentru {out_dir.name}", 2, 7)
        print(f"  ✓ Folosesc slot-ul existent: {out_dir}")
    else:
        from src.agents.pipeline import run_pipeline

        out_root = Path(args.out_root).resolve()
        out_root.mkdir(parents=True, exist_ok=True)

        _print_step("Rulez pipeline: Scout → Rank → Copy → Image → Reel/Avatar writer → Video → Avatar", 2, 7)
        t0 = time.time()
        try:
            out_dir = run_pipeline(
                slot=slot,
                hours_back=args.hours,
                out_root=out_root,
                stop_after="avatar",
                model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
                template_path=Path("src/templates/news_card.html").resolve(),
                publish_live=False,
                platforms=set(),
            )
        except Exception as exc:
            print(f"\n  ✗ Pipeline a eșuat: {exc}")
            return 2

        elapsed = time.time() - t0
        print(f"\n  ✓ Pipeline terminat în {elapsed:.0f}s -> {out_dir}")

    # --- Report outputs ---------------------------------------------------
    _print_step("Verificare rezultate", 7, 7)
    out_dir = Path(out_dir)

    produced: list[tuple[str, Path, float]] = []
    targets = {
        "Știri brute":        out_dir / "raw_news.json",
        "Top 2 știri":        out_dir / "top2.json",
        "Post X (A)":         out_dir / "post_x.txt",
        "Post Telegram":      out_dir / "post_telegram.md",
        "Imagine X (card)":   out_dir / "image_x_1200x675.png",
        "Imagine TG":         out_dir / "image_tg_1080x1080.png",
        "Imagine Reel 9:16":  out_dir / "image_reel_1080x1920.png",
        "Script 20s":         out_dir / "avatar" / "script_20s.txt",
        "Clip TTS (backup)":  out_dir / "reel_final.mp4",
        "Clip cu AVATAR AI":  out_dir / "reel_avatar.mp4",
    }
    for label, p in targets.items():
        if p.exists():
            size_mb = p.stat().st_size / (1024 * 1024)
            mark = "✓" if size_mb > 0 else "⚠"
            produced.append((label, p, size_mb))
            print(f"  {mark} {label:<24} {size_mb:6.2f} MB   {p.relative_to(_HERE)}")
        else:
            print(f"  ✗ {label:<24} lipsește        {p.relative_to(_HERE)}")

    final_clip = out_dir / "reel_avatar.mp4"
    if not final_clip.exists():
        # Fallback: TTS-only version
        final_clip = out_dir / "reel_final.mp4"

    # -----------------------------------------------------------------
    # Diagnostic recovery — dacă clipurile finale lipsesc, încearcă să
    # le generezi direct ACUM (fără try/except), ca să vezi eroarea
    # adevărată în loc de „lipsește".
    # -----------------------------------------------------------------
    need_tts_video    = not (out_dir / "reel_final.mp4").exists()
    need_avatar_video = not (out_dir / "reel_avatar.mp4").exists()

    if need_tts_video or need_avatar_video:
        print("\n" + "─" * 64)
        print("  ⚠ Unul sau ambele clipuri finale lipsesc.")
        print("    Rerulez stage-urile PROBLEMATICE cu erori vizibile…")
        print("─" * 64)

        # Pre-check inputs pe care ambele le folosesc
        bg = out_dir / "image_reel_1080x1920.png"
        script_candidates = [
            out_dir / "avatar" / "script_20s.txt",
            out_dir / "avatar" / "script_30s.txt",
            out_dir / "reel"   / "01_script.txt",
        ]
        script_found = next((p for p in script_candidates if p.exists()), None)

        if not bg.exists():
            print(f"\n  ✗ Fundalul 9:16 NU există: {bg}")
            print("    Cauza: stage-ul 'image' a eșuat (Pollinations.ai offline sau blocat de firewall?).")
            print("    Rerulează doar image-ul:")
            print(f"      python -m src.agents.pipeline --slot {slot} --stop-after image --hours {args.hours}")
            return 4

        if script_found is None:
            print(f"\n  ✗ Niciun script nu există. Căutate:")
            for p in script_candidates:
                print(f"      {p}")
            print("    Cauza: stage-ul 'reel' a eșuat (probabil ANTHROPIC_API_KEY missing + eroare în template).")
            print("    Rerulează:")
            print(f"      python -m src.agents.pipeline --slot {slot} --stop-after reel --hours {args.hours}")
            return 5

        print(f"  ✓ Fundal: {bg.name} ({bg.stat().st_size/1024:.0f} KB)")
        print(f"  ✓ Script: {script_found.relative_to(out_dir)} ({script_found.stat().st_size} b)")

        # Încercarea 1: reel_final.mp4 (TTS + moviepy) ------------------
        if need_tts_video:
            print("\n  ▶ Rerun: build_video (TTS + moviepy)")
            try:
                from src.agents.video_builder import build_video
                out = build_video(out_dir)
                print(f"  ✓ reel_final.mp4 generat -> {out}")
            except Exception as exc:
                import traceback
                print(f"\n  ✗ build_video a eșuat: {exc}")
                print("\n  Traceback complet:")
                traceback.print_exc()
                print("\n  Cauze comune:")
                print("    • edge-tts nu poate ajunge la serverul Microsoft (firewall/VPN)")
                print("    • moviepy < 2.1.0 (rulează: pip install -U moviepy)")
                print("    • reel/03_captions.srt lipsește (fallback: se generează fără captions)")

        # Încercarea 2: reel_avatar.mp4 (D-ID) --------------------------
        if need_avatar_video:
            print("\n  ▶ Rerun: build_avatar_reel (D-ID /talks)")
            try:
                from src.agents.avatar_video import (
                    build_avatar_reel, check_did_credentials,
                )
                ok, msg = check_did_credentials()
                if not ok:
                    print(f"\n  ✗ D-ID pre-flight eșuat: {msg}")
                    print("    Mergi la studio.d-id.com → Account → API Keys,")
                    print("    copiază cheia 'email:secret' și pune-o în .env ca DID_API_KEY.")
                    return 6
                print(f"  ✓ D-ID: {msg}")
                out = build_avatar_reel(out_dir, presenter=args.presenter)
                print(f"  ✓ reel_avatar.mp4 generat -> {out}")
            except Exception as exc:
                import traceback
                print(f"\n  ✗ build_avatar_reel a eșuat: {exc}")
                print("\n  Traceback complet:")
                traceback.print_exc()
                print("\n  Cauze comune:")
                print("    • D-ID credite zero — top up la studio.d-id.com/billing")
                print("    • DID_API_KEY greșit format — trebuie 'email:secret' sau base64-ul")
                print("    • D-ID outage — verifică https://status.d-id.com/")
                print("    • FFmpeg lipsește — rulează: pip install -U imageio-ffmpeg")

        # Re-evaluate final clip existence after recovery
        if (out_dir / "reel_avatar.mp4").exists():
            final_clip = out_dir / "reel_avatar.mp4"
        elif (out_dir / "reel_final.mp4").exists():
            final_clip = out_dir / "reel_final.mp4"

    print("\n" + "═" * 64)
    if final_clip.exists():
        print(f"  🎬 CLIP FINAL: {final_clip}")
        print(f"      Dimensiune: {final_clip.stat().st_size / (1024*1024):.1f} MB")
        if not args.no_open:
            print(f"      Îl deschid acum...")
            _open_file(final_clip)
        else:
            print(f"      (Deschidere automată dezactivată cu --no-open)")
    else:
        print("  ✗ Nu s-a produs niciun clip. Verifică erorile de mai sus.")
        return 3
    print("═" * 64 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
