"""upload_presenter.py — urcă o poză locală la D-ID și salvează URL-ul.

Scop: fix definitiv pentru eroarea "Unsupported file url" — orice URL
returnat de /images endpoint e 100% acceptat de /talks pe toate planurile.

Rulează:
    python upload_presenter.py /cale/spre/portret.jpg
    python upload_presenter.py C:\\Users\\Intel\\Desktop\\poza.png

La final scrie URL-ul D-ID-hosted în `.did_presenter_cache.json` — toate
apelurile viitoare îl vor folosi automat (prioritate mai mare decât
pozele built-in).

Recomandări pentru poza ta:
  • JPG sau PNG, max 10 MB
  • față clară, frontal, lumină naturală
  • 512×512 până la 1920×1920 (orice rezoluție peste 512 merge)
  • fără ochelari de soare, fără măști, fără pixelări
  • dacă D-ID nu detectează fața, revine cu alt unghi / altă lumină
"""
from __future__ import annotations

import argparse
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", help="Calea spre poza ta (JPG/PNG)")
    ap.add_argument("--no-cache", action="store_true",
                    help="nu salva URL-ul în cache, doar afișează-l")
    args = ap.parse_args()

    img = Path(args.image)
    if not img.exists():
        print(f"✗ Poza nu există: {img}")
        return 1

    print(f"  [1/2] Verific cheia D-ID...")
    try:
        from src.agents.avatar_video import check_did_credentials
        ok, msg = check_did_credentials()
        print(f"        {'✓' if ok else '✗'} {msg}")
        if not ok:
            return 2
    except Exception as exc:
        print(f"        ✗ {exc}")
        return 2

    print(f"  [2/2] Urc poza la D-ID /images ({img.stat().st_size // 1024} KB)...")
    try:
        from src.agents.avatar_video import upload_photo_to_did, _save_cached_presenter_url
        url = upload_photo_to_did(img)
        print(f"        ✓ URL: {url}")
    except Exception as exc:
        print(f"        ✗ eroare upload: {exc}")
        print("\n  Cauze comune:")
        print("    • D-ID nu a detectat nicio față în poză → încearcă alta")
        print("    • poza e în format exotic → convertește la .jpg clasic")
        print("    • 401 → verifică DID_API_KEY în .env")
        return 3

    if not args.no_cache:
        _save_cached_presenter_url(url, source_path=str(img.resolve()))
        print(f"\n  ✓ Salvat în .did_presenter_cache.json")
        print(f"    Toate clipurile viitoare vor folosi această poză.")
    else:
        print(f"\n  (nu am salvat nimic, doar am afișat URL-ul)")
        print(f"\n  Pentru a-l folosi permanent, adaugă în .env:")
        print(f"    DID_PRESENTER_URL={url}")

    print(f"\n  Testează acum cu:")
    print(f"    python test_did.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
