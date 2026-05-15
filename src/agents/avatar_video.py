"""Avatar Video Builder — D-ID /talks API (works on all paid plans).

Flow:
  1. Take narration text from reel/01_script.txt
  2. Call D-ID /talks API with a presenter photo -> animated talking-head MP4
  3. Download avatar MP4
  4. FFmpeg composite: crypto background (9:16) + presenter video (bottom strip)
  5. Burn captions via PIL
  6. Write reel_avatar.mp4

Presenter: set DID_PRESENTER_URL env var (or via Settings → Presenter Photo URL).
Or pass presenter name — mapped to a built-in portrait photo.
"""
from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path

import requests

# Force UTF-8 output on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

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
# Presenter photos — Pexels CDN (stable direct URLs, no redirects)
# ---------------------------------------------------------------------------

#
# IMPORTANT: D-ID is VERY picky about which URLs it accepts as source_url.
# We've tested extensively and here is what we learned:
#
#   ✗ Pexels CDN                          → 500 (hotlink throttling)
#   ✗ Unsplash direct                     → 500 (CORS/redirect issues)
#   ✗ create-images-results.d-id.com      → 400 "Unsupported file url"
#                                           (that's their OUTPUT path, not input)
#   ✓ d-id-public-bucket.s3.amazonaws.com → reliably accepted
#      (published in their Python SDK tutorials)
#   ✓ Any image uploaded via /images       → always accepted (D-ID-hosted)
#
# BEST PRACTICE: upload the user's own photo via /images endpoint, store
# the returned URL in DID_PRESENTER_URL env var. This is one-time setup,
# after which every /talks call uses that URL and never fails.
#
# Fallback: use D-ID's public S3 demo photos (alice.jpg et al) — these
# are from D-ID's own tutorials and have been stable for years.
#
PRESENTER_PHOTOS: dict[str, str] = {
    # D-ID public bucket (from their tutorials — battle-tested)
    # Default is the EMD BRAND PRESENTER: a professional white male in a
    # business suit. We map it to D-ID's `matt.jpeg` (well-lit headshot,
    # navy suit, neutral background — exactly the constraint set both
    # SadTalker and D-ID need for clean lip-sync output).
    # Users can override via Settings → AI Avatar → Presenter URL or env
    # vars EMD_PRESENTER_URL / DID_PRESENTER_URL / REPLICATE_PRESENTER_URL.
    "default":      "https://d-id-public-bucket.s3.amazonaws.com/matt.jpeg",
    "emd":          "https://d-id-public-bucket.s3.amazonaws.com/matt.jpeg",
    "emd_brand":    "https://d-id-public-bucket.s3.amazonaws.com/matt.jpeg",
    "alice":        "https://d-id-public-bucket.s3.amazonaws.com/alice.jpg",
    "amy":          "https://d-id-public-bucket.s3.amazonaws.com/amy.jpg",
    "anita":        "https://d-id-public-bucket.s3.amazonaws.com/anita.jpg",
    "aladino":      "https://d-id-public-bucket.s3.amazonaws.com/aladino.jpeg",
    "damian":       "https://d-id-public-bucket.s3.amazonaws.com/damian.jpeg",
    "einstein":     "https://d-id-public-bucket.s3.amazonaws.com/einstein.jpeg",
    "josh":         "https://d-id-public-bucket.s3.amazonaws.com/josh.jpeg",
    "matt":         "https://d-id-public-bucket.s3.amazonaws.com/matt.jpeg",
    "noelle":       "https://d-id-public-bucket.s3.amazonaws.com/noelle.jpeg",
    "obama":        "https://d-id-public-bucket.s3.amazonaws.com/obama.jpeg",
    # Legacy aliases so existing configs don't break — MALE names now
    # correctly map to male portraits (alex_suit was wrongly alice before).
    "alex_suit":    "https://d-id-public-bucket.s3.amazonaws.com/matt.jpeg",
    "william":      "https://d-id-public-bucket.s3.amazonaws.com/matt.jpeg",
    "arran":        "https://d-id-public-bucket.s3.amazonaws.com/josh.jpeg",
    "darren":       "https://d-id-public-bucket.s3.amazonaws.com/damian.jpeg",
    "ethan":        "https://d-id-public-bucket.s3.amazonaws.com/matt.jpeg",
    "lucas":        "https://d-id-public-bucket.s3.amazonaws.com/josh.jpeg",
    "jaimie":       "https://d-id-public-bucket.s3.amazonaws.com/damian.jpeg",
    # Female aliases — kept for opt-in
    "sophia":       "https://d-id-public-bucket.s3.amazonaws.com/amy.jpg",
    "diana":        "https://d-id-public-bucket.s3.amazonaws.com/anita.jpg",
    "lana":         "https://d-id-public-bucket.s3.amazonaws.com/alice.jpg",
    "amber":        "https://d-id-public-bucket.s3.amazonaws.com/amy.jpg",
    "flora":        "https://d-id-public-bucket.s3.amazonaws.com/anita.jpg",
}

VOICE_ID = "en-US-GuyNeural"   # Edge TTS — male voice to match default presenter

# Last-resort fallback photo used if the user's presenter URL keeps failing.
# MUST be from d-id-public-bucket.s3.amazonaws.com — D-ID rejects its own
# `create-images-results.d-id.com/...` output URLs with HTTP 400
# "Unsupported file url" (that bucket is for rendered outputs, not inputs).
DID_BUILTIN_PRESENTER = "https://d-id-public-bucket.s3.amazonaws.com/alice.jpg"

# Soft target: 20-25 seconds of speech.
# D-ID Edge TTS speaks ~2.3 words/sec:
#   - 46 words -> ~20 s
#   - 57 words -> ~25 s (our hard ceiling)
# We only trim if narration exceeds MAX_WORDS; short stories stay short.
MAX_WORDS = 57
MAX_WORDS_20S = MAX_WORDS  # legacy alias (old code imports this)


def _resolve_profile_from_env(platform: str) -> dict:
    """Look up the current user's video profile for `platform`.

    Dashboard's _uenv injects VIDEO_* env vars per request. We read those and
    hand them to config.video_profiles.get_profile which applies defaults for
    anything missing.
    """
    from src.config.video_profiles import get_profile

    settings = {}
    # Per-platform duration (only the matching one is relevant here but we
    # pass all so the code path stays uniform)
    for p in ("tiktok", "instagram", "youtube_shorts"):
        v = os.environ.get(f"VIDEO_DURATION_{p.upper()}", "").strip()
        if v:
            settings[f"video_duration_{p}"] = v
    for key in ("video_style", "video_brand_color",
                "video_brand_handle", "video_brand_cta"):
        v = os.environ.get(key.upper(), "").strip()
        if v:
            settings[key] = v
    for key in ("video_intro_enabled", "video_outro_enabled",
                "video_bgm_enabled", "video_bgm_volume"):
        v = os.environ.get(key.upper(), "").strip()
        if v:
            settings[key] = v
    try:
        return get_profile(platform, settings)
    except ValueError:
        return get_profile("tiktok", settings)


def _cap_narration(text: str, max_words: int = MAX_WORDS) -> str:
    """Trim narration to at most `max_words` words, ending on a sentence boundary."""
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    # Cut at max_words, then rewind to the last sentence terminator we find.
    candidate = " ".join(words[:max_words])
    import re as _re
    terminators = list(_re.finditer(r"[.!?]", candidate))
    if terminators:
        end = terminators[-1].end()
        return candidate[:end].strip()
    return candidate.rstrip(",;:—- ").rstrip() + "."


# ---------------------------------------------------------------------------
# D-ID auth
# ---------------------------------------------------------------------------

def _clean_did_key(raw: str) -> str:
    """Strip common paste-noise that causes false 401s."""
    s = (raw or "").strip()
    # Strip surrounding quotes (users sometimes paste from JSON/curl examples)
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    # Strip leading "Authorization:" or "Basic " if copied from curl
    for prefix in ("Authorization:", "authorization:"):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
    if s.lower().startswith("basic "):
        s = s[6:].strip()
    # Remove internal whitespace/newlines (paste from multi-line UI)
    s = "".join(s.split())
    return s


def _key_fingerprint(s: str) -> str:
    """Safe preview of a key for error messages (first 4 + last 4 chars)."""
    if not s:
        return "(empty)"
    if len(s) <= 10:
        return f"{s[0]}...{s[-1]} (len={len(s)})"
    return f"{s[:4]}...{s[-4:]} (len={len(s)})"


def _did_auth_variants() -> list[tuple[str, str]]:
    """Return candidate Authorization header values, in priority order.

    D-ID expects:  Authorization: Basic <base64(email:secret)>
    DID_API_KEY in Settings / .env can be:
      (a) already the base64 blob  -> send as-is
      (b) raw 'email:secret'       -> base64-encode it first
      (c) something else           -> try both as last-resort fallback

    We build a list of candidates; _did_request will try them in order
    until one succeeds (falling back on 401 only).
    """
    api_key = _clean_did_key(os.environ.get("DID_API_KEY", ""))
    if not api_key:
        raise RuntimeError(
            "D-ID API key not set. Settings -> D-ID -> paste the full API key "
            "from studio.d-id.com (either raw 'email:secret' or the base64 blob)."
        )

    import base64 as _b64
    variants: list[tuple[str, str]] = []

    # Case (b): raw "email:secret" -> base64 encode
    if ":" in api_key and "@" in api_key.split(":", 1)[0]:
        encoded = _b64.b64encode(api_key.encode("utf-8")).decode("ascii")
        variants.append(("base64(email:secret)", f"Basic {encoded}"))
        # Also try the raw value as-is, in case D-ID changed behaviour
        variants.append(("raw-value", f"Basic {api_key}"))
    else:
        # Case (a)/(c): assume already-encoded; send as-is first.
        variants.append(("as-is", f"Basic {api_key}"))
        # Fallback: maybe the user gave us base64 that decodes to email:secret;
        # try decoding + re-encoding (normalizes accidental re-encoding).
        try:
            decoded = _b64.b64decode(api_key + "==", validate=False).decode("utf-8", "ignore")
            if ":" in decoded and "@" in decoded.split(":", 1)[0]:
                re_encoded = _b64.b64encode(decoded.encode("utf-8")).decode("ascii")
                if re_encoded != api_key:
                    variants.append(("renormalized-base64", f"Basic {re_encoded}"))
        except Exception:
            pass

    return variants


# Cache of which variant worked last, so we don't keep retrying the wrong one
# within the same process. Cleared whenever the env key changes.
_good_variant_cache: dict[str, str] = {}


def _did_headers() -> dict:
    """Legacy helper — returns a single header dict using the best-guess variant.

    Prefers the variant that worked last (from _good_variant_cache) if any.
    """
    api_key = _clean_did_key(os.environ.get("DID_API_KEY", ""))
    if not api_key:
        raise RuntimeError(
            "D-ID API key not set. Settings -> D-ID -> paste the full API key "
            "from studio.d-id.com."
        )
    # If we've verified a variant this process, use it directly.
    cached = _good_variant_cache.get(api_key)
    if cached:
        return {"Authorization": cached, "Content-Type": "application/json"}

    variants = _did_auth_variants()
    _label, header = variants[0]
    return {"Authorization": header, "Content-Type": "application/json"}


def _did_request(method: str, url: str, **kwargs) -> "requests.Response":
    """Call D-ID with automatic auth-variant fallback on 401.

    Use this instead of requests.<method>(...) directly, so a 401 triggers
    a second attempt with the alternate encoding (raw vs base64) before the
    caller raises a permanent error. Caches the first variant that succeeds.
    """
    api_key = _clean_did_key(os.environ.get("DID_API_KEY", ""))
    if not api_key:
        raise RuntimeError("D-ID API key not set.")

    cached = _good_variant_cache.get(api_key)
    if cached:
        headers = kwargs.pop("headers", {}) or {}
        headers = {**headers, "Authorization": cached,
                   "Content-Type": headers.get("Content-Type", "application/json")}
        return requests.request(method, url, headers=headers, **kwargs)

    variants = _did_auth_variants()
    last_response: "requests.Response | None" = None
    for label, auth in variants:
        headers = kwargs.get("headers") or {}
        merged = {**headers, "Authorization": auth,
                  "Content-Type": headers.get("Content-Type", "application/json")}
        call_kwargs = {k: v for k, v in kwargs.items() if k != "headers"}
        resp = requests.request(method, url, headers=merged, **call_kwargs)
        last_response = resp
        if resp.status_code != 401:
            _good_variant_cache[api_key] = auth
            if label != variants[0][0]:
                print(f"[avatar_video] D-ID auth variant '{label}' succeeded "
                      f"(first choice '{variants[0][0]}' returned 401)")
            return resp
        print(f"[avatar_video] D-ID auth variant '{label}' -> 401, trying next...")

    # All variants 401'd — return the last response so caller can raise
    # a descriptive error that includes the key fingerprint.
    return last_response  # type: ignore[return-value]


def check_did_credentials() -> tuple[bool, str]:
    """Pre-flight: confirm D-ID key + credits before submitting /talks.

    Returns (ok, message). Never raises — designed for health checks.
    """
    api_key = _clean_did_key(os.environ.get("DID_API_KEY", ""))
    if not api_key:
        return False, "DID_API_KEY not set in environment / Settings."

    try:
        r = _did_request("GET", "https://api.d-id.com/credits", timeout=15)
    except Exception as exc:
        return False, f"Could not reach D-ID: {exc}"

    if r.status_code == 401:
        return False, (
            f"D-ID 401 — all auth variants rejected. Key fingerprint: "
            f"{_key_fingerprint(api_key)}. Open studio.d-id.com -> Account -> "
            f"API Keys, click the copy icon next to your key (raw "
            f"'email:secret' OR the base64 blob), and paste the WHOLE "
            f"thing into Settings -> D-ID."
        )
    if r.status_code == 403:
        return False, "D-ID 403 — your key is recognised but lacks permission."
    if r.status_code != 200:
        return False, f"D-ID /credits returned HTTP {r.status_code}: {r.text[:200]}"

    try:
        data = r.json()
        remaining = data.get("remaining") or data.get("total") or 0
    except Exception:
        return True, "D-ID auth OK (credits info not parseable)"

    try:
        remaining_n = int(float(remaining))
    except Exception:
        remaining_n = -1

    if remaining_n == 0:
        return False, "D-ID credits exhausted — top up at studio.d-id.com/billing"
    return True, f"D-ID OK — {remaining} credits remaining"


# ---------------------------------------------------------------------------
# /images — upload user's own photo to D-ID (returns a D-ID-hosted URL)
#
# This is the NUCLEAR OPTION: any URL returned by this endpoint is 100%
# guaranteed to be accepted by /talks. Use it when you have a local
# photo and want a stable, long-lived presenter_url. Store the result in
# .env as DID_PRESENTER_URL; the pipeline will prefer it automatically.
# ---------------------------------------------------------------------------

def upload_photo_to_did(image_path: Path) -> str:
    """Upload a local JPG/PNG to D-ID /images. Return the D-ID-hosted URL.

    Raises on any non-200 response with a detailed message.
    """
    image_path = Path(image_path).resolve()
    if not image_path.exists():
        raise FileNotFoundError(f"Photo not found: {image_path}")
    if image_path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
        raise ValueError(f"Unsupported format {image_path.suffix} — use JPG or PNG")
    size_mb = image_path.stat().st_size / (1024 * 1024)
    if size_mb > 10:
        raise ValueError(f"Photo too large ({size_mb:.1f} MB) — max 10 MB")

    # /images uses multipart, so we can't send Content-Type: application/json.
    # Build a fresh headers dict with just Authorization, and fall back across
    # auth variants on 401 just like _did_request does.
    api_key = _clean_did_key(os.environ.get("DID_API_KEY", ""))
    cached = _good_variant_cache.get(api_key)
    candidates = [("cached", cached)] if cached else _did_auth_variants()

    mime = "image/jpeg" if image_path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    r = None
    for label, auth in candidates:
        with open(image_path, "rb") as f:
            files = {"image": (image_path.name, f, mime)}
            r = requests.post(
                "https://api.d-id.com/images",
                headers={"Authorization": auth},
                files=files,
                timeout=120,
            )
        if r.status_code != 401:
            if not cached:
                _good_variant_cache[api_key] = auth
            break
        print(f"[avatar_video] /images auth variant '{label}' -> 401, trying next...")
    if r is None:
        raise RuntimeError("No D-ID auth variants available")
    if r.status_code >= 400:
        try:
            detail = r.json()
        except Exception:
            detail = {"raw": r.text[:400]}
        raise RuntimeError(f"D-ID /images {r.status_code}: {detail}")

    data = r.json()
    url = data.get("url") or data.get("image_url")
    if not url:
        raise RuntimeError(f"D-ID /images ok but no url in response: {data}")
    return url


def _cache_path() -> Path:
    """Where we store the uploaded presenter URL between runs."""
    return _REPO_ROOT / ".did_presenter_cache.json"


def _load_cached_presenter_url() -> str:
    p = _cache_path()
    if not p.exists():
        return ""
    try:
        import json as _json
        data = _json.loads(p.read_text(encoding="utf-8"))
        return (data.get("url") or "").strip()
    except Exception:
        return ""


def _save_cached_presenter_url(url: str, source_path: str = "") -> None:
    import json as _json
    _cache_path().write_text(
        _json.dumps({"url": url, "source": source_path,
                     "ts": time.time()}, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Presenter URL resolution  (4-tier priority chain)
# ---------------------------------------------------------------------------

def _presenter_url(presenter: str = "default") -> str:
    """Resolve presenter name to a portrait photo URL.

    Priority chain (highest first):
      1. EMD_PRESENTER_URL env var         — EMD brand override (cross-provider)
      2. DID_PRESENTER_URL env var         — D-ID-specific override
      3. .did_presenter_cache.json         — previously uploaded photo
      4. PRESENTER_PHOTOS[<name>]          — built-in public demo photos
      5. PRESENTER_PHOTOS["default"]       — fallback if name unknown
                                             (= EMD brand presenter: white
                                              male in business suit)
    """
    emd_url = os.environ.get("EMD_PRESENTER_URL", "").strip()
    if emd_url:
        return emd_url
    env_url = os.environ.get("DID_PRESENTER_URL", "").strip()
    if env_url:
        return env_url
    cached = _load_cached_presenter_url()
    if cached:
        return cached
    return PRESENTER_PHOTOS.get(presenter, PRESENTER_PHOTOS["default"])


# ---------------------------------------------------------------------------
# D-ID /talks API  (works on Lite, Basic, Pro — all paid plans)
# ---------------------------------------------------------------------------

def _create_talk(text: str, presenter: str = "default",
                 source_url_override: str | None = None) -> str:
    """Submit a D-ID /talks job; return talk_id.

    Retry policy:
      - 500/502/503/504 → retry up to 2 times with 10s/20s backoff
        (transient D-ID hiccups — very common on their infra).
      - After 2 failed retries with the user's photo, fall back to
        D-ID's built-in presenter (guaranteed-compatible photo) and
        re-submit once — that rules out "image format/URL broken".
    """
    # Sanitize text: replace UTF-8 special chars that D-ID doesn't like
    text = text.replace("—", "-").replace("–", "-")
    text = text.replace(""", '"').replace(""", '"')
    text = text.replace("'", "'").replace("'", "'")
    text = text.replace("…", "...")
    # Also strip any control characters and non-ASCII
    text = text.encode('ascii', 'ignore').decode('ascii')
    text = ''.join(c for c in text if ord(c) >= 32 or c in '\n\t')
    
    source_url = source_url_override or _presenter_url(presenter)
    payload = {
        "source_url": source_url,
        "script": {
            "type": "text",
            "input": text[:1200],
            "provider": {
                # 'microsoft' (Azure Neural) is more stable than 'edge' and
                # works on Lite plan. 'edge' can return 500 due to upstream
                # rate limits. Using microsoft for production reliability.
                "type": "microsoft",
                "voice_id": os.environ.get("DID_VOICE_ID", VOICE_ID),
            },
        },
        "config": {
            "result_format": "mp4",
            "pad_audio": 0.0,
            # 'fluent: true' triggers 500 errors on some plans — omitted.
            # 'stitch: true' adds ~30% processing time — skip for 20s clips.
        },
        # Green background → FFmpeg chromakey removes it → transparent avatar bubble
        "background": {"color": "#00FF00"},
    }
    print(f"[avatar_video] D-ID /talks — presenter={presenter}")
    print(f"[avatar_video]   photo: {source_url}")
    print(f"[avatar_video]   text ({len(text.split())} words): {text[:100]}{'...' if len(text) > 100 else ''}")

    last_exc: Exception | None = None
    for attempt in range(3):  # initial + 2 retries
        try:
            r = _did_request(
                "POST",
                "https://api.d-id.com/talks",
                json=payload,
                timeout=30,
            )
        except requests.RequestException as net_exc:
            last_exc = net_exc
            if attempt < 2:
                wait_s = 10 * (attempt + 1)
                print(f"[avatar_video] network error ({net_exc}); retrying in {wait_s}s...")
                time.sleep(wait_s)
                continue
            raise RuntimeError(f"D-ID unreachable after 3 attempts: {net_exc}")

        if r.status_code == 401:
            api_key = _clean_did_key(os.environ.get("DID_API_KEY", ""))
            raise RuntimeError(
                f"D-ID 401 Unauthorized — both 'raw email:secret' and "
                f"'base64 blob' interpretations of your key were rejected.\n"
                f"  Key fingerprint: {_key_fingerprint(api_key)}\n"
                f"  Likely causes:\n"
                f"    1. The key was truncated when pasted (check length).\n"
                f"    2. You pasted the email only, not the secret (need both).\n"
                f"    3. The key was revoked/rotated at studio.d-id.com.\n"
                f"  Fix: open studio.d-id.com -> Account -> API Keys, click "
                f"the copy icon, and paste the WHOLE value (no quotes, no "
                f"'Basic ' prefix) into Settings -> D-ID."
            )
        if r.status_code == 429:
            raise RuntimeError("D-ID rate limit hit — wait 1-2 minutes and retry")
        if r.status_code == 403:
            raise RuntimeError(
                "D-ID 403 Forbidden — key is valid but not allowed to use /talks. "
                "Check your plan at studio.d-id.com (Lite plan is required minimum)."
            )
        if r.status_code == 402:
            raise RuntimeError(
                "D-ID 402 — insufficient credits. Top up at studio.d-id.com/billing"
            )
        if r.status_code == 400:
            # 400 = payload rejected. Most common causes (D-ID-specific):
            #  - source_url returns non-image or times out (Pexels rate limit!)
            #  - voice_id not recognised by selected provider
            #  - text contains emoji/special chars after our sanitizer
            try:
                detail = r.json()
            except Exception:
                detail = {"raw": r.text[:400]}
            desc = detail.get("description") or detail.get("message") or str(detail)

            # If the image is the problem and we haven't yet tried built-in,
            # retry with D-ID's own presenter — fixes ~90% of 400s caused by
            # stale/hotlinked photo URLs.
            image_related = any(k in str(desc).lower() for k in
                                ("image", "source", "url", "photo", "face"))
            if (image_related and source_url != DID_BUILTIN_PRESENTER
                    and not source_url_override):
                print(f"[avatar_video] D-ID 400 on image ({desc[:120]}) — "
                      f"retrying with D-ID built-in presenter...")
                return _create_talk(text, presenter=presenter,
                                    source_url_override=DID_BUILTIN_PRESENTER)

            raise RuntimeError(
                f"D-ID 400 Bad Request: {desc}\n"
                f"   Payload source_url: {source_url}\n"
                "   Common causes: presenter photo unreachable/invalid, "
                "voice_id wrong, text too long. Full response: " +
                str(detail)[:300]
            )
        if 500 <= r.status_code < 600:
            # On first 5xx: pre-flight auth check. If creds are bad, bail loud.
            if attempt == 0:
                ok, msg = check_did_credentials()
                if not ok:
                    raise RuntimeError(f"D-ID {r.status_code} — root cause: {msg}")
            try:
                detail = r.json().get("description", r.text[:200])
            except Exception:
                detail = r.text[:200]

            if attempt < 2:
                wait_s = 10 * (attempt + 1)
                print(f"[avatar_video] D-ID {r.status_code} (attempt {attempt+1}/3): "
                      f"{detail}; retrying in {wait_s}s...")
                time.sleep(wait_s)
                continue

            # Exhausted retries on the user's photo — try built-in presenter
            # as a last resort IF we weren't already using it.
            if source_url != DID_BUILTIN_PRESENTER and not source_url_override:
                print(f"[avatar_video] D-ID 5xx persisted; falling back to "
                      f"built-in presenter (user photo may be incompatible)...")
                return _create_talk(text, presenter=presenter,
                                    source_url_override=DID_BUILTIN_PRESENTER)
            raise RuntimeError(
                f"D-ID server error {r.status_code}: {detail}. "
                "Tried 3 times + built-in presenter fallback — all failed. "
                "This is a D-ID outage. Check https://status.d-id.com/"
            )
        if not r.ok:
            try:
                detail = r.json().get("description", r.text[:200])
            except Exception:
                detail = r.text[:200]
            raise RuntimeError(f"D-ID API error {r.status_code}: {detail}")

        # Success!
        data = r.json()
        talk_id = data.get("id")
        if not talk_id:
            raise RuntimeError(f"D-ID did not return a talk id. Response: {data}")
        return talk_id

    # Should be unreachable — loop either returns or raises.
    raise RuntimeError(f"D-ID /talks failed: {last_exc}")


def _poll_talk(talk_id: str, timeout: int = 300) -> str:
    """Poll GET /talks/{id} until done; return result_url.

    Aggressive polling schedule tuned for 20-25s clips (D-ID typically
    finishes in 15-40s):
      - First check after 3s (not 5) — sometimes it's already done.
      - Poll every 2s for the first 20s (fast-response window).
      - Then every 4s up to 60s (normal zone).
      - Then every 8s up to timeout (rare slow-render zone).
    Average total wait drops from ~70s to ~25-35s when D-ID is fast.
    """
    deadline = time.time() + timeout
    start = time.time()
    # First check quickly — D-ID is fast on short scripts.
    time.sleep(3)
    while time.time() < deadline:
        try:
            r = _did_request(
                "GET",
                f"https://api.d-id.com/talks/{talk_id}",
                timeout=20,
            )
            r.raise_for_status()
            d = r.json()
        except Exception as exc:
            print(f"[avatar_video] poll error ({exc}), retrying in 3s...")
            time.sleep(3)
            continue

        status = d.get("status")
        elapsed = int(time.time() - start)
        print(f"[avatar_video] talk {talk_id} status={status} (elapsed {elapsed}s)")
        if status == "done":
            url = d.get("result_url", "")
            if not url:
                raise RuntimeError("D-ID returned done but no result_url")
            return url
        if status in ("error", "rejected"):
            err = d.get("error", {})
            raise RuntimeError(
                f"D-ID talk failed (status={status}): "
                f"{err.get('description', '')} -- {err.get('details', '')}"
            )
        # Adaptive wait: tight for first 20s, moderate to 60s, then loose.
        if elapsed < 20:
            wait = 2
        elif elapsed < 60:
            wait = 4
        else:
            wait = 8
        time.sleep(wait)

    raise TimeoutError(f"D-ID talk {talk_id} timed out after {timeout}s")


def _download(url: str, out_path: Path) -> None:
    r = requests.get(url, timeout=120, stream=True)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(65536):
            f.write(chunk)


def generate_avatar_clip(text: str, out_path: Path, presenter: str = "default") -> Path:
    """text → D-ID talking-head MP4. Narration is hard-capped at MAX_WORDS_20S (~20s)."""
    # Hard cap — D-ID will still generate a clean clip at exactly this length.
    capped = _cap_narration(text)
    orig_words = len(text.split())
    capped_words = len(capped.split())
    est_seconds = capped_words / 2.3
    if capped_words < orig_words:
        print(f"[avatar_video] narration capped: {orig_words} -> {capped_words} words "
              f"(~{est_seconds:.1f}s at 2.3 wps)")
    else:
        print(f"[avatar_video] narration: {capped_words} words "
              f"(~{est_seconds:.1f}s at 2.3 wps)")

    print(f"[avatar_video] submitting D-ID /talks job (presenter={presenter})...")
    talk_id = _create_talk(capped, presenter=presenter)
    print(f"[avatar_video] talk_id={talk_id} — polling for completion...")
    result_url = _poll_talk(talk_id)
    print(f"[avatar_video] downloading result...")
    _download(result_url, out_path)
    size_kb = out_path.stat().st_size // 1024
    print(f"[avatar_video] avatar saved -> {out_path.name} ({size_kb} KB)")
    return out_path


# ---------------------------------------------------------------------------
# Composite: presenter strip at bottom + crypto background
#
# Layout (1080×1920):
#   ┌─────────────────────┐  y=0
#   │  Crypto background  │  upper 60% — news visual
#   │                     │
#   ├─────────────────────┤  y=1152
#   │  Presenter video    │  540px strip (scaled to 1080×540)
#   ├─────────────────────┤  y=1692
#   │  Captions area      │  228px for text
#   └─────────────────────┘  y=1920
# ---------------------------------------------------------------------------

def _hex_to_rgba(hex_color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    """'#F59E0B' / 'F59E0B' -> (245, 158, 11, 255). Falls back to white."""
    h = (hex_color or "").strip().lstrip("#")
    try:
        if len(h) == 6:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)
    except ValueError:
        pass
    return (255, 255, 255, alpha)


def _hex_to_ffcolor(hex_color: str) -> str:
    """Convert '#F59E0B' or 'F59E0B' to FFmpeg 0xF59E0B style (kept for back-compat)."""
    r, g, b, _ = _hex_to_rgba(hex_color)
    return f"0x{r:02X}{g:02X}{b:02X}"


def _render_text_card(text: str, brand_hex: str, out_path: Path,
                      width: int = 1080, height: int = 360,
                      font_size: int = 110) -> None:
    """Render a transparent PNG with a styled text card for intro/outro.

    Uses PIL so we don't depend on FFmpeg being built with --enable-libfreetype
    (many pip-installed imageio_ffmpeg binaries aren't).
    """
    from PIL import Image, ImageDraw, ImageFont
    from src.agents.video_builder import _load_font

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Auto-shrink font until text fits comfortably in 90% of width
    font = _load_font(font_size)
    while font_size > 28:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        if tw <= int(width * 0.88):
            break
        font_size -= 8
        font = _load_font(font_size)

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (width - tw) // 2 - bbox[0]
    ty = (height - th) // 2 - bbox[1]

    # Semi-transparent pill background sized to text
    pad_x, pad_y = 60, 30
    pill = (
        max(0, tx + bbox[0] - pad_x),
        max(0, ty + bbox[1] - pad_y),
        min(width,  tx + bbox[0] + tw + pad_x),
        min(height, ty + bbox[1] + th + pad_y),
    )
    draw.rounded_rectangle(pill, radius=28, fill=(0, 0, 0, 165))

    # Text in brand color, with a black outline for readability
    r, g, b, _ = _hex_to_rgba(brand_hex)
    # PIL stroke_width renders an outline around each glyph
    draw.text((tx, ty), text, font=font,
              fill=(r, g, b, 255), stroke_width=4, stroke_fill=(0, 0, 0, 255))

    img.save(out_path, "PNG")


def _render_news_panel(
    headline: str,
    brand_hex: str,
    out_path: Path,
    width: int = 1080,
    height: int = 1920,
    script_excerpt: str = "",
) -> None:
    """Render a full 1080x1920 RGBA PNG — top 38% transparent, bottom 62% cinematic news panel.

    Layout: avatar bottom-right (460px) — news text left 62% of panel width.
    Design: dark gradient, brand pill badge, large headline, bullet points from script.
    """
    from PIL import Image, ImageDraw, ImageFilter
    from src.agents.video_builder import _load_font
    import re as _re

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    panel_y = int(height * 0.38)          # gradient starts at 38%
    text_x  = 58                           # left margin
    text_right = int(width * 0.63)         # right boundary (avatar lives right of this)

    # ── Cinematic dark gradient ──
    # Transparent → near-black over the first 18% of panel, then solid dark
    panel_h = height - panel_y
    for y in range(panel_y, height):
        rel = (y - panel_y) / panel_h
        if rel < 0.18:
            alpha = int((rel / 0.18) ** 1.6 * 230)
        else:
            alpha = 230
        # Slight blue-black tint for cinematic feel
        draw.line([(0, y), (width, y)], fill=(5, 8, 18, alpha))

    r, g, b, _ = _hex_to_rgba(brand_hex)

    # ── BREAKING / LIVE pill badge ──
    badge_top = panel_y + 52
    badge_font = _load_font(26)
    badge_text = "  ● BREAKING NEWS  "
    bb = draw.textbbox((0, 0), badge_text, font=badge_font)
    bw, bh = bb[2] - bb[0] + 24, bb[3] - bb[1] + 16
    # Pill background with brand color
    draw.rounded_rectangle(
        [text_x, badge_top, text_x + bw, badge_top + bh],
        radius=bh // 2,
        fill=(r, g, b, 220),
    )
    draw.text((text_x + 12, badge_top + 8), badge_text, font=badge_font,
              fill=(0, 0, 0, 255))

    # ── Thin accent line below badge ──
    line_y = badge_top + bh + 16
    # Gradient line: bright left → transparent right
    for x in range(text_x, text_right):
        ratio = 1.0 - ((x - text_x) / (text_right - text_x)) ** 0.6
        la = int(200 * ratio)
        draw.point((x, line_y), fill=(r, g, b, la))
        draw.point((x, line_y + 1), fill=(r, g, b, la // 2))

    # ── Headline — large, white, word-wrapped to text_right ──
    headline = headline.strip()
    h_size = 64
    h_font = _load_font(h_size)

    def _wrap_text(text: str, font, max_w: int) -> list[str]:
        words = text.split()
        lines_out: list[str] = []
        buf: list[str] = []
        for w in words:
            buf.append(w)
            bb2 = draw.textbbox((0, 0), " ".join(buf), font=font)
            if (bb2[2] - bb2[0]) > max_w:
                if len(buf) > 1:
                    lines_out.append(" ".join(buf[:-1]))
                    buf = [w]
                else:
                    lines_out.append(" ".join(buf))
                    buf = []
        if buf:
            lines_out.append(" ".join(buf))
        return lines_out[:3]

    lines = _wrap_text(headline, h_font, text_right - text_x - 20)

    # Auto-shrink if any line overflows
    max_line = max(lines, key=len) if lines else ""
    while h_size > 38 and max_line:
        bb2 = draw.textbbox((0, 0), max_line, font=h_font)
        if (bb2[2] - bb2[0]) <= text_right - text_x - 20:
            break
        h_size -= 4
        h_font = _load_font(h_size)
        lines = _wrap_text(headline, h_font, text_right - text_x - 20)
        max_line = max(lines, key=len) if lines else ""

    head_top = line_y + 20
    line_h = h_size + 12
    for i, ln in enumerate(lines):
        # Subtle drop-shadow then white text
        draw.text((text_x + 2, head_top + i * line_h + 2), ln,
                  font=h_font, fill=(0, 0, 0, 120))
        draw.text((text_x, head_top + i * line_h), ln,
                  font=h_font, fill=(255, 255, 255, 255))

    # ── Short separator ──
    sep_y = head_top + len(lines) * line_h + 18
    draw.rectangle([(text_x, sep_y), (text_x + 120, sep_y + 2)],
                   fill=(r, g, b, 160))
    draw.rectangle([(text_x + 124, sep_y), (text_right - 30, sep_y + 2)],
                   fill=(255, 255, 255, 25))

    # ── Bullet points (key sentences from narration) ──
    bullets: list[str] = []
    if script_excerpt:
        sents = _re.split(r"(?<=[.!?])\s+", script_excerpt.strip())
        # Use first 2 sentences — keep short & punchy
        bullets = [s.strip() for s in sents if 12 < len(s.strip()) < 100][:2]
    if not bullets:
        bullets = ["24/7 algo trading signals", "elitemargindesk.io"]

    b_font = _load_font(34)
    b_y = sep_y + 26
    for bullet in bullets:
        if b_y + 48 < height - 140:
            # Truncate to fit
            while len(bullet) > 4:
                bb3 = draw.textbbox((0, 0), "— " + bullet, font=b_font)
                if (bb3[2] - bb3[0]) <= text_right - text_x - 20:
                    break
                bullet = bullet[:-5].rstrip() + "..."
            # Drop-shadow
            draw.text((text_x + 2, b_y + 2), "— " + bullet,
                      font=b_font, fill=(0, 0, 0, 100))
            draw.text((text_x, b_y), "— " + bullet,
                      font=b_font, fill=(195, 210, 235, 220))
            b_y += 54

    # ── Brand handle at bottom of panel (small, subtle) ──
    brand_font = _load_font(24)
    brand_text = "EliteMarginDesk.io"
    draw.text((text_x, height - 110), brand_text,
              font=brand_font, fill=(r, g, b, 130))

    img.save(out_path, "PNG")


def _find_bgm_path() -> Path | None:
    """Locate a background-music track for the mix-in.

    Priority:
      1. BGM_PATH env var (absolute path set by user)
      2. src/assets/bgm/default.mp3 bundled with the repo
      3. None — caller should skip BGM silently
    """
    env = os.environ.get("BGM_PATH", "").strip()
    if env:
        p = Path(env)
        if p.exists():
            return p
    bundled = _REPO_ROOT / "src" / "assets" / "bgm" / "default.mp3"
    if bundled.exists():
        return bundled
    return None


def _srt_time_to_ass(ts: str) -> str:
    """Convert '00:00:05,454' (SRT) -> '0:00:05.45' (ASS h:mm:ss.cc)."""
    ts = ts.strip().replace(",", ".")
    # SRT is HH:MM:SS.mmm; ASS wants H:MM:SS.cc (centiseconds, single-digit hour OK)
    try:
        h, m, rest = ts.split(":")
        s, ms = rest.split(".")
        cs = int(round(int(ms) / 10.0))
        cs = min(99, max(0, cs))
        return f"{int(h)}:{int(m):02d}:{int(s):02d}.{cs:02d}"
    except Exception:
        return "0:00:00.00"


def _srt_to_ass(srt_path: Path, out_path: Path, brand_hex: str = "#F59E0B") -> Path:
    """Convert an SRT to a 1080x1920-aware ASS file with brand styling.

    Why ASS instead of force_style on `subtitles`:
      libass scales font sizes by PlayResY. With the default PlayResY=288
      and PlayResX=384, a FontSize=18 renders ~120px tall on a 1920px
      canvas — huge, and overlaps the avatar bubble. By writing our own
      [Script Info] block with PlayResX=1080 / PlayResY=1920 we make
      FontSize a pixel-accurate value, so 44 means ~44px line height.

    Layout decisions (must NOT collide with the avatar bubble):
      Bubble occupies y = 1140 .. 1600 on the right half of the canvas.
      Captions are bottom-anchored (Alignment=2) with MarginV=80,
      so a 1-line caption sits at y ~ 1840; a 2-line one at y ~ 1788.
      Both stay clear of the bubble (>180 px below it).

    We also make captions slightly narrower (MarginL/R = 100) so wrapped
    lines never sneak up under the bubble's right side.
    """
    import re

    raw = srt_path.read_text(encoding="utf-8", errors="replace")
    # Split SRT into cues
    cues: list[tuple[str, str, str]] = []
    pat = re.compile(
        r"\d+\s*\n"
        r"(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*\n"
        r"(.+?)(?=\n\s*\n|\Z)",
        re.DOTALL,
    )
    for m in pat.finditer(raw):
        start, end, body = m.group(1), m.group(2), m.group(3).strip()
        # ASS uses \N for hard newline; normalise body
        body = body.replace("\r", "").replace("\n", "\\N")
        cues.append((_srt_time_to_ass(start), _srt_time_to_ass(end), body))

    # ASS color is &HAABBGGRR — convert brand hex (#RRGGBB) to BGR with 00 alpha
    h = brand_hex.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except Exception:
        r, g, b = 0x00, 0xFF, 0x7A  # default neon green
    # Brand color in ASS BGR format (primary text color)
    primary_color = f"&H00{b:02X}{g:02X}{r:02X}"
    # Outline: black always; BackColour: semi-transparent dark box
    # Style:
    #   FontSize=52       -> large, visible on mobile screens
    #   Bold=1
    #   BorderStyle=3     -> filled background box (pill-style readability)
    #   Outline=0         -> no border when using box style
    #   Shadow=0
    #   BackColour=&H99000000 -> 60% opaque black background box
    #   Alignment=2       -> bottom-center
    #   MarginL/R=90      -> 90px in from each edge
    #   MarginV=130       -> 130px from bottom (above avatar bubble)
    #   ScaledBorderAndShadow=yes
    style_block = (
        f"Style: Default,Arial,60,{primary_color},&H000000FF,&H00000000,&H99000000,"
        "1,0,0,0,100,100,0,0,3,0,0,2,90,90,130,1"
    )
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{style_block}\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
    )
    # Alternating neon colors per cue — futuristic karaoke effect
    # Odd  cues → neon green  #00FF7A  ASS: &H007AFF00
    # Even cues → cyan        #06D6A0  ASS: &H00A0D606
    NEON_COLORS = [r"&H007AFF00", r"&H00A0D606"]
    body_lines = []
    for i, (s, e, txt) in enumerate(cues):
        color = NEON_COLORS[i % 2]
        body_lines.append(
            r"Dialogue: 0," + s + "," + e + r",Default,,0,0,0,,{\1c" + color +
            r"\fad(100,60)\t(0,150,\fscx110\fscy110)\t(150,300,\fscx100\fscy100)}" +
            txt.upper()
        )
    out_path.write_text(header + "\n".join(body_lines) + "\n",
                        encoding="utf-8")
    return out_path


def _composite_ffmpeg(
    bg_path: Path,
    avatar_path: Path | None,
    out_path: Path,
    srt_path: Path | None = None,
    profile: dict | None = None,
    intro_text: str | None = None,
    outro_text: str | None = None,
    audio_path: Path | None = None,
    headline: str | None = None,
    script_excerpt: str | None = None,
) -> None:
    """Composite background + avatar, optionally burning subtitles in one pass.

    Passing `srt_path` here skips the slow MoviePy caption-burn step entirely
    (~30-60s saved on a 20s clip). FFmpeg's `subtitles` filter renders text
    directly onto the video during the same encode.

    When `profile` is provided we also:
      * cap the output at profile['duration_seconds']
      * draw an intro title card (first 2.5s) if profile['intro_enabled']
      * draw an outro CTA card (last 2.5s) if profile['outro_enabled']
      * mix a background-music track under narration if profile['bgm_enabled']

    Avatar layout — IMPORTANT:
      The avatar (talking head) is now overlaid as a CIRCULAR bubble at the
      bottom-right corner of the news image, NOT as a giant 540px strip that
      covered half the image. This keeps the headline + summary text from the
      image fully visible while the avatar still presents the story.

    Modes:
      * avatar_path is a Path  -> avatar bubble overlay + audio from avatar_path
      * avatar_path is None    -> "branded" fallback: image-only background,
        TTS audio comes from `audio_path` (caller must supply it) so the slot
        still has narration when D-ID is down. This is what makes the no-D-ID
        path monetization-grade.
    """
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        ffmpeg = "ffmpeg"

    # ---- Resolve profile-driven knobs (with safe fallbacks) ---------------
    profile = profile or {}
    duration = int(profile.get("duration_seconds", 25))
    brand = profile.get("brand", {}) or {}
    brand_hex = brand.get("color", "#F59E0B")
    intro_on = bool(profile.get("intro_enabled", False)) and bool(intro_text)
    outro_on = bool(profile.get("outro_enabled", False)) and bool(outro_text)
    bgm_on   = bool(profile.get("bgm_enabled", False))
    bgm_volume_pct = int(profile.get("bgm_volume", 15))
    bgm_gain = max(0.0, min(0.5, bgm_volume_pct / 100.0))

    # Card windows
    intro_end   = 2.5
    outro_start = max(0.0, duration - 2.5)

    # Render card PNGs via PIL (into sibling dir of the output clip)
    work_dir = out_path.parent
    intro_png: Path | None = None
    outro_png: Path | None = None
    news_panel_png: Path | None = None
    try:
        if intro_on:
            intro_png = work_dir / "_intro_card.png"
            _render_text_card(str(intro_text), brand_hex, intro_png,
                              width=1080, height=360, font_size=120)
        if outro_on:
            outro_png = work_dir / "_outro_card.png"
            _render_text_card(str(outro_text), brand_hex, outro_png,
                              width=1080, height=320, font_size=100)
    except Exception as e:
        print(f"[avatar_video] card render skipped: {e}")
        intro_png = None
        outro_png = None

    # News panel PNG — rendered only when avatar mode + headline provided
    has_avatar_pre = avatar_path is not None and Path(avatar_path).exists()
    if has_avatar_pre and headline:
        try:
            news_panel_png = work_dir / "_news_panel.png"
            _render_news_panel(headline, brand_hex, news_panel_png,
                               script_excerpt=script_excerpt or "")
            print(f"[avatar_video] news panel rendered: {headline[:60]}")
        except Exception as e:
            print(f"[avatar_video] news panel render skipped: {e}")
            news_panel_png = None

    # ---- FFmpeg inputs -----------------------------------------------------
    # Input 0: background image (looped)
    # Input 1: avatar video (with audio)  OR  TTS-only audio (no avatar)
    # Input 2?: intro card PNG
    # Input 3?: outro card PNG
    # Input N?: BGM file
    has_avatar = avatar_path is not None and Path(avatar_path).exists()
    cmd: list[str] = [ffmpeg, "-y",
                      "-loop", "1", "-i", str(bg_path)]
    if has_avatar:
        cmd += ["-i", str(avatar_path)]
    elif audio_path and Path(audio_path).exists():
        cmd += ["-i", str(audio_path)]
    else:
        # No audio at all — generate silent track via lavfi.
        cmd += ["-f", "lavfi", "-t", str(duration),
                "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    idx = 2
    intro_idx = outro_idx = bgm_idx = news_panel_idx = None

    # News panel input (static PNG overlay for avatar mode)
    if news_panel_png and news_panel_png.exists():
        cmd += ["-loop", "1", "-i", str(news_panel_png)]
        news_panel_idx = idx
        idx += 1

    if intro_png and intro_png.exists():
        cmd += ["-loop", "1", "-i", str(intro_png)]
        intro_idx = idx
        idx += 1
    if outro_png and outro_png.exists():
        cmd += ["-loop", "1", "-i", str(outro_png)]
        outro_idx = idx
        idx += 1

    bgm_path = _find_bgm_path() if bgm_on else None
    if bgm_path:
        cmd += ["-stream_loop", "-1", "-i", str(bgm_path)]
        bgm_idx = idx
        idx += 1

    # ---- Video filter chain ------------------------------------------------
    # Avatar mode → always static background (Ken Burns behind a static talking
    # head is distracting and makes the news panel text unreadable).
    # No-avatar mode → use profile style (Ken Burns allowed).
    if has_avatar:
        style = "simple"
    else:
        style = (profile.get("style") or "full_ai").lower()

    if style == "full_ai":
        total_s = max(1, duration)
        ow, oh = 1210, 2150
        dw = ow - 1080
        dh = oh - 1920
        bg_chain = (
            f"[0:v]scale={ow}:{oh}:force_original_aspect_ratio=increase,"
            f"crop={ow}:{oh},setsar=1,"
            f"crop="
            f"w='trunc(({ow}-{dw}*min(t,{total_s})/{total_s})/2)*2':"
            f"h='trunc(({oh}-{dh}*min(t,{total_s})/{total_s})/2)*2':"
            f"x='trunc({dw}*min(t,{total_s})/{total_s}/4)*2':"
            f"y='trunc({dh}*min(t,{total_s})/{total_s}/4)*2',"
            f"scale=1080:1920:force_original_aspect_ratio=disable,setsar=1,"
            f"fps=30[bg]"
        )
    else:
        bg_chain = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,setsar=1[bg]"
        )

    if has_avatar:
        # Layout: TV news anchor style
        #   • Background: static news image (full 1080×1920)
        #   • News panel PNG (RGBA): bottom 60% dark gradient + headline + bullets
        #   • Avatar: 460px wide, bottom-right corner (left side free for news text)
        #   • Chromakey removes green (#00FF00) → clean composite
        av_w = 460
        av_x = "W-w-30"     # 30px from right edge
        av_y = "H-h-70"     # 70px from bottom (space for captions)

        filter_complex = f"{bg_chain};"

        if news_panel_idx is not None:
            # Overlay news panel (RGBA) on background
            filter_complex += (
                f"[{news_panel_idx}:v]format=rgba[np];"
                f"[bg][np]overlay=0:0:format=auto[bg_np];"
            )
            bg_label = "bg_np"
        else:
            bg_label = "bg"

        # Avatar chromakey — IMPORTANT: similarity=0.42 was way too aggressive
        # and made entire avatars invisible whenever skin/clothing leaned warm
        # (yellow → got keyed as green-ish). 0.16 is the conservative sweet
        # spot used by professional broadcast green-screen workflows.
        # blend=0.08 keeps edges crisp without halo.
        # Env override: AVATAR_CHROMAKEY_SIM=0.20 (or "off" to skip the key).
        _ck_sim = os.environ.get("AVATAR_CHROMAKEY_SIM", "0.16").strip()
        if _ck_sim.lower() in ("off", "none", "false", "0"):
            # No chromakey — useful when HeyGen returned a non-green or
            # transparent avatar. Just resize and overlay as-is.
            filter_complex += (
                f"[1:v]scale={av_w}:-2,setsar=1,format=yuva420p[avatar_k];"
                f"[{bg_label}][avatar_k]overlay=x={av_x}:y={av_y}:format=auto[v0]"
            )
        else:
            try:
                _ck_sim_f = float(_ck_sim)
            except ValueError:
                _ck_sim_f = 0.16
            filter_complex += (
                f"[1:v]scale={av_w}:-2,setsar=1,"
                f"chromakey=color=0x00FF00:similarity={_ck_sim_f}:blend=0.08,"
                f"despill=type=green:mix=0.4:expand=0,"
                f"format=yuva420p[avatar_k];"
                f"[{bg_label}][avatar_k]overlay=x={av_x}:y={av_y}:format=auto[v0]"
            )
    else:
        # No avatar — just image background. Ken Burns + intro/outro/captions/BGM.
        filter_complex = f"{bg_chain};[bg]copy[v0]"
    current_label = "v0"

    # Intro card — PNG overlay with per-frame alpha via 'format=rgba,fade'.
    if intro_idx is not None:
        # Fade in 0..0.3s and out 2.2..2.5s.
        filter_complex += (
            f";[{intro_idx}:v]format=rgba,"
            f"fade=t=in:st=0:d=0.3:alpha=1,"
            f"fade=t=out:st={intro_end-0.3:.2f}:d=0.3:alpha=1[intro]"
        )
        # Place in upper third (y ≈ 380). 'enable' hides after intro ends.
        filter_complex += (
            f";[{current_label}][intro]overlay=0:380:"
            f"enable='between(t,0,{intro_end:.2f})'[v1]"
        )
        current_label = "v1"

    # Outro card — slide-up reveal via fade.
    if outro_idx is not None:
        filter_complex += (
            f";[{outro_idx}:v]format=rgba,"
            f"fade=t=in:st={outro_start:.2f}:d=0.3:alpha=1[outro]"
        )
        # Place slightly above center (y ≈ 500) so it's readable above captions.
        filter_complex += (
            f";[{current_label}][outro]overlay=0:500:"
            f"enable='gte(t,{outro_start:.2f})'[v2]"
        )
        current_label = "v2"

    # Burned-in subtitles. Convert SRT -> 1080x1920-aware ASS so font sizes
    # are pixel-accurate and we can keep the captions clear of the bubble.
    if srt_path and srt_path.exists():
        try:
            ass_path = srt_path.with_suffix(".ass")
            _srt_to_ass(srt_path, ass_path, brand_hex=brand_hex)
            ass_str = str(ass_path.resolve()).replace("\\", "/")
            if len(ass_str) > 2 and ass_str[1] == ":":
                ass_str = ass_str[0] + r"\:" + ass_str[2:]
            filter_complex += (
                f";[{current_label}]ass='{ass_str}'[vcap]"
            )
            current_label = "vcap"
        except Exception as exc:
            print(f"[avatar_video] caption render skipped: {exc}")

    # ---- Audio filter chain ------------------------------------------------
    # In all modes input #1 has the narration audio (avatar.mp4's audio track,
    # OR the TTS-only mp3 we passed via audio_path, OR silent fallback).
    if bgm_idx is not None:
        filter_complex += (
            f";[1:a]volume=1.0[va];"
            f"[{bgm_idx}:a]volume={bgm_gain:.3f},"
            f"afade=t=out:st={max(0, duration-1.0):.2f}:d=1.0[vb];"
            f"[va][vb]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        )
        audio_map = ["-map", "[aout]"]
    else:
        audio_map = ["-map", "1:a?"]

    # Premium encoding profile:
    #   preset=medium  →  ~3× the encode time of ultrafast but ~50% smaller files
    #                       AND visibly sharper. On a 20s clip this is ~6-10s
    #                       extra wait vs ultrafast — totally worth it for the
    #                       reduction in compression artefacts on text & faces.
    #   crf=18         →  visually lossless (default 23 is "good enough" but
    #                       softens fine details on subtitles/avatar skin).
    #   profile=high level=4.0  →  Instagram/TikTok/Shorts all accept this.
    #   tune=film      →  better psy-RD for natural footage (avatars, B-roll).
    #   audio 192k     →  bumped from 128k; voice clarity matters on phones.
    cmd += [
        "-filter_complex", filter_complex,
        "-map", f"[{current_label}]",
        *audio_map,
        "-shortest",
        "-threads", "0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-profile:v", "high", "-level", "4.0", "-tune", "film",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        "-t", str(duration),
        str(out_path),
    ]
    print(f"[avatar_video] composite: duration={duration}s intro={intro_on} "
          f"outro={outro_on} bgm={'yes' if bgm_path else 'no'}")
    import subprocess
    result = subprocess.run(cmd, capture_output=True, text=True)
    # Cleanup temp card PNGs regardless of outcome
    for p in (intro_png, outro_png):
        if p is not None:
            try:
                p.unlink()
            except Exception:
                pass
    if result.returncode != 0:
        print(f"[avatar_video] FFmpeg stderr:\n{result.stderr[-1500:]}")
        raise RuntimeError(f"FFmpeg composite failed (exit {result.returncode}): {result.stderr[-300:]}")


def _burn_captions(video_path: Path, srt_path: Path, out_path: Path) -> None:
    from moviepy import VideoFileClip, VideoClip
    from src.agents.video_builder import parse_srt, render_caption_on_frame

    cues = parse_srt(srt_path.read_text(encoding="utf-8"))
    clip = VideoFileClip(str(video_path))
    w, h = clip.size

    def make_frame(t):
        frame = clip.get_frame(t)
        for c in cues:
            if c["start"] <= t < c["end"]:
                return render_caption_on_frame(frame.copy(), c["text"], w, h)
        return frame

    out_clip = VideoClip(frame_function=make_frame, duration=clip.duration)
    if clip.audio is not None:
        out_clip = out_clip.with_audio(clip.audio)
    out_clip.write_videofile(
        str(out_path), fps=clip.fps or 30,
        codec="libx264", audio_codec="aac",
        preset="fast", logger=None,
    )
    clip.close()
    out_clip.close()


# ---------------------------------------------------------------------------
# Intro / outro text resolution
# ---------------------------------------------------------------------------

def _resolve_intro_text(slot_dir: Path, platform: str) -> str:
    """Pick a 1-3 word title card for the intro.

    Preference order:
      1. Story category if present in top2.json (e.g. "CRYPTO NEWS")
      2. First 2 words of the story title, uppercased
      3. Platform-specific fallback ("BREAKING" / "TRENDING" / "WATCH NOW")
    """
    import json as _json
    try:
        top2 = slot_dir / "top2.json"
        if top2.exists():
            data = _json.loads(top2.read_text(encoding="utf-8"))
            stories = data if isinstance(data, list) else data.get("stories") or []
            if stories:
                s = stories[0]
                cat = (s.get("category") or s.get("tag") or "").strip()
                if cat:
                    return cat.upper()[:28]
                title = (s.get("title") or s.get("headline") or "").strip()
                if title:
                    words = title.split()[:2]
                    return " ".join(w.upper() for w in words)[:28]
    except Exception as e:
        print(f"[avatar_video] intro lookup failed: {e}")
    fallbacks = {
        "tiktok":         "TRENDING",
        "instagram":      "WATCH NOW",
        "youtube_shorts": "BREAKING",
    }
    return fallbacks.get(platform, "WATCH NOW")


def _resolve_outro_text(profile: dict) -> str:
    """Outro text is brand.handle if set, otherwise brand.cta."""
    brand = (profile or {}).get("brand", {}) or {}
    handle = (brand.get("handle") or "").strip()
    if handle:
        return handle if handle.startswith("@") else f"@{handle}"
    cta = (brand.get("cta") or "Follow for more").strip()
    return cta


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def build_avatar_reel(slot_dir: Path, presenter: str = "default",
                       platform: str = "tiktok") -> Path:
    """Script → AI avatar (Replicate → D-ID → branded fallback) → reel_avatar.mp4.

    `platform` resolves the target duration/intro/outro via
    src.config.video_profiles. Env vars injected by dashboard's `_uenv`
    (VIDEO_DURATION_TIKTOK etc.) act as user overrides.

    Provider chain (configurable via AVATAR_PROVIDER env var):
      1. Replicate SadTalker/Wav2Lip — primary, cheap (~$0.01-0.05/clip)
      2. D-ID /talks                 — fallback if Replicate not configured/fails
      3. Branded reel (no avatar)    — last resort, still monetization-grade
                                       (Ken Burns + intro/outro + captions + BGM)

    The function ALWAYS returns a usable MP4 — if every provider fails it
    falls through to the branded path so the slot never goes dark.
    """
    from src.agents.video_builder import extract_narration
    from src.agents.avatar_providers import generate_with_chain

    # Resolve profile from env (set by dashboard _uenv) + platform defaults.
    profile = _resolve_profile_from_env(platform)
    max_words = profile["narration_max_words"]
    print(f"[avatar_video] platform={platform!r} target={profile['duration_seconds']}s "
          f"avatar={profile['avatar_seconds']}s max_words={max_words}")

    slot_dir   = Path(slot_dir).resolve()
    reel_dir   = slot_dir / "reel"
    avatar_dir = slot_dir / "avatar"
    bg_path    = slot_dir / "image_reel_1080x1920.png"
    # Prefer avatar-writer's caption track (timed to 20s); fall back to reel's.
    srt_path   = avatar_dir / "captions_20s.srt"
    if not srt_path.exists():
        srt_path = reel_dir / "03_captions.srt"
    out_path   = slot_dir / "reel_avatar.mp4"
    avatar_tmp = slot_dir / "_avatar_raw.mp4"
    nocap_tmp  = slot_dir / "_avatar_nocap.mp4"

    if not bg_path.exists():
        raise FileNotFoundError(
            f"Background image missing: {bg_path}\n"
            "Run the pipeline first or click Generate from the slot page."
        )

    # Preference order for narration source:
    #   1. avatar/script_20s.txt   ← written by avatar_writer (35–45 words, tuned for 20s)
    #   2. avatar/script_30s.txt   ← legacy name, still 20s content after refactor
    #   3. reel/01_script.txt      ← 250-350 words director's script (last resort, will be capped)
    script_candidates = [
        avatar_dir / "script_20s.txt",
        avatar_dir / "script_30s.txt",
        reel_dir   / "01_script.txt",
    ]
    script_file = next((p for p in script_candidates if p.exists()), None)
    if script_file is None:
        raise FileNotFoundError(
            "No narration script found. Looked for:\n  " +
            "\n  ".join(str(p) for p in script_candidates) +
            "\nRun the pipeline first or click Generate from the slot page."
        )
    print(f"[avatar_video] using script: {script_file.relative_to(slot_dir)}")

    script_text = script_file.read_text(encoding="utf-8")
    narration   = extract_narration(script_text)
    if not narration.strip():
        raise ValueError(f"Script is empty after parsing — check {script_file}")

    # Cap to the platform-specific word budget (overrides the module-wide
    # MAX_WORDS when a shorter duration was configured).
    narration = _cap_narration(narration, max_words=max_words)
    print(f"[avatar_video] narration: {len(narration.split())} words "
          f"(~{len(narration.split())/2.3:.1f}s, cap={max_words})")

    # Step 1: Pre-render TTS once. We hand it to the Replicate provider so
    # SadTalker has the audio to drive lip-sync; if Replicate is skipped
    # and D-ID runs, D-ID renders TTS internally and ignores audio_path;
    # if every provider fails we still have the mp3 for the branded path.
    tts_audio = avatar_dir / "narration.mp3"
    avatar_dir.mkdir(parents=True, exist_ok=True)
    if not tts_audio.exists():
        try:
            import asyncio as _asyncio
            from src.agents.video_builder import _edge_tts_save
            voice = os.environ.get("AVATAR_VOICE", VOICE_ID)
            print(f"[avatar_video] rendering TTS via edge-tts (voice={voice})")
            _asyncio.run(_edge_tts_save(narration, tts_audio, voice=voice))
        except Exception as exc:
            print(f"[avatar_video] TTS pre-render failed: {exc}")
            tts_audio = None  # the chain providers will handle it themselves

    # Step 2: Walk the provider chain (Replicate → D-ID). We pass a status
    # dict so the chain can populate per-provider diagnostics; we then write
    # it as `_avatar_status.json` in the slot so the dashboard can surface
    # WHY the avatar didn't render (missing token, auth fail, timeout, ...).
    chain_status: dict = {"attempts": []}
    t0 = time.time()
    chain_result = generate_with_chain(
        narration,
        avatar_tmp,
        presenter=presenter,
        audio_path=tts_audio if tts_audio and tts_audio.exists() else None,
        status=chain_status,
    )
    t_avatar = time.time() - t0
    if chain_result is not None:
        _, provider_name = chain_result
        print(f"[avatar_video] avatar via {provider_name}: {t_avatar:.1f}s")
    else:
        print(f"[avatar_video] no avatar provider succeeded ({t_avatar:.1f}s)")
        print(f"[avatar_video] chain summary: {chain_status.get('summary', '?')}")

    # Persist chain diagnostics next to the output mp4 so the dashboard
    # / user can see exactly which provider was used (and why others failed).
    try:
        import json as _json
        status_path = slot_dir / "_avatar_status.json"
        chain_status["winner"] = chain_status.get("winner")
        chain_status["used_branded_fallback"] = chain_result is None
        chain_status["total_elapsed_s"] = round(t_avatar, 2)
        chain_status["presenter"] = presenter
        chain_status["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                  time.gmtime())
        status_path.write_text(_json.dumps(chain_status, indent=2),
                               encoding="utf-8")
        print(f"[avatar_video] status -> {status_path.name}")
    except Exception as exc:
        print(f"[avatar_video] could not write _avatar_status.json: {exc}")

    # Step 3: Composite background + avatar + captions in ONE ffmpeg pass.
    # Previously we did a 2nd pass with MoviePy which rendered every frame
    # in Python (~30-60s for a 20s clip). Now the FFmpeg subtitles filter
    # burns captions during the same encode (~3-5s total).
    #
    # Resolve intro/outro text:
    #   * intro_text = story category or upper-cased first title word
    #                  (falls back to platform hashtag)
    #   * outro_text = brand.handle if set, else brand.cta ("Follow for more")
    intro_text = _resolve_intro_text(slot_dir, platform)
    outro_text = _resolve_outro_text(profile)
    srt_final = srt_path if srt_path.exists() else None

    # Extract headline + first 3 sentences for news panel overlay
    _headline: str = ""
    _excerpt: str = ""
    try:
        import json as _json2, re as _re2
        top2_f = slot_dir / "top2.json"
        if top2_f.exists():
            _d = _json2.loads(top2_f.read_text(encoding="utf-8"))
            _stories = _d if isinstance(_d, list) else _d.get("stories") or []
            if _stories:
                _s0 = _stories[0]
                _headline = (_s0.get("title") or _s0.get("headline") or "").strip()
        if not _headline:
            # Fallback: first non-empty line of script
            for ln in script_text.splitlines():
                ln = ln.strip().lstrip("#").strip()
                if ln and not ln.startswith("["):
                    _headline = ln[:120]
                    break
        # First 3 sentences of narration for bullet points
        _sents = _re2.split(r"(?<=[.!?])\s+", narration.strip())
        _excerpt = " ".join(_sents[:3])
    except Exception as _exc:
        print(f"[avatar_video] headline extract skipped: {_exc}")

    t1 = time.time()
    if chain_result is not None and avatar_tmp.exists():
        # Avatar branch — news panel + avatar composite
        _composite_ffmpeg(
            bg_path, avatar_tmp, out_path,
            srt_path=srt_final,
            profile=profile,
            intro_text=intro_text,
            outro_text=outro_text,
            headline=_headline or None,
            script_excerpt=_excerpt or None,
        )
    else:
        # Branded fallback — image background + TTS narration, no avatar.
        if not (tts_audio and tts_audio.exists()):
            raise RuntimeError(
                "All avatar providers failed AND TTS pre-render failed. "
                "Cannot build a branded fallback. Check edge-tts install."
            )
        print(f"[avatar_video] using branded fallback (no avatar, TTS+image)")
        _composite_ffmpeg(
            bg_path, None, out_path,
            srt_path=srt_final,
            profile=profile,
            intro_text=intro_text,
            outro_text=outro_text,
            audio_path=tts_audio,
        )
    t_ff = time.time() - t1
    print(f"[avatar_video] FFmpeg composite: {t_ff:.1f}s")

    # Cleanup temp avatar clip
    avatar_tmp.unlink(missing_ok=True)
    nocap_tmp.unlink(missing_ok=True)

    if not out_path.exists():
        raise RuntimeError(f"Build completed but output file not found: {out_path}")

    size_mb = out_path.stat().st_size / 1_048_576
    print(f"[avatar_video] done -> {out_path.name} ({size_mb:.1f} MB)")
    return out_path


def build_branded_reel(slot_dir: Path, platform: str = "tiktok") -> Path:
    """Build a polished reel WITHOUT any AI avatar.

    Same composition pipeline as `build_avatar_reel` (Ken Burns background,
    intro/outro cards, BGM mix, burned-in captions) but with no talking head.
    Useful when the user explicitly disables avatars or every provider is
    unavailable. Output goes to `reel_branded.mp4` so it doesn't collide
    with the avatar output.
    """
    from src.agents.video_builder import extract_narration, _edge_tts_save
    import asyncio as _asyncio

    profile = _resolve_profile_from_env(platform)
    max_words = profile["narration_max_words"]
    print(f"[avatar_video] branded build platform={platform!r} "
          f"target={profile['duration_seconds']}s max_words={max_words}")

    slot_dir   = Path(slot_dir).resolve()
    reel_dir   = slot_dir / "reel"
    avatar_dir = slot_dir / "avatar"
    bg_path    = slot_dir / "image_reel_1080x1920.png"
    srt_path   = avatar_dir / "captions_20s.srt"
    if not srt_path.exists():
        srt_path = reel_dir / "03_captions.srt"
    out_path   = slot_dir / "reel_branded.mp4"

    if not bg_path.exists():
        raise FileNotFoundError(
            f"Background image missing: {bg_path}\n"
            "Run the pipeline first or click Generate from the slot page."
        )

    script_candidates = [
        avatar_dir / "script_20s.txt",
        avatar_dir / "script_30s.txt",
        reel_dir   / "01_script.txt",
    ]
    script_file = next((p for p in script_candidates if p.exists()), None)
    if script_file is None:
        raise FileNotFoundError(
            "No narration script found. Run the pipeline first."
        )

    narration = extract_narration(script_file.read_text(encoding="utf-8"))
    if not narration.strip():
        raise ValueError(f"Script is empty after parsing — check {script_file}")
    narration = _cap_narration(narration, max_words=max_words)

    avatar_dir.mkdir(parents=True, exist_ok=True)
    tts_audio = avatar_dir / "narration.mp3"
    if not tts_audio.exists():
        voice = os.environ.get("AVATAR_VOICE", VOICE_ID)
        _asyncio.run(_edge_tts_save(narration, tts_audio, voice=voice))

    intro_text = _resolve_intro_text(slot_dir, platform)
    outro_text = _resolve_outro_text(profile)
    srt_final = srt_path if srt_path.exists() else None

    _composite_ffmpeg(
        bg_path, None, out_path,
        srt_path=srt_final,
        profile=profile,
        intro_text=intro_text,
        outro_text=outro_text,
        audio_path=tts_audio,
    )
    if not out_path.exists():
        raise RuntimeError(f"Branded build failed: {out_path}")
    size_mb = out_path.stat().st_size / 1_048_576
    print(f"[avatar_video] branded done -> {out_path.name} ({size_mb:.1f} MB)")
    return out_path
