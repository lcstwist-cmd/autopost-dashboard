"""Per-platform video generation profiles.

Each platform has different optimal specs — this module centralizes them so
writers (avatar_writer, reel_writer), builders (avatar_video, video_builder)
and publishers all agree on the parameters.

Profile structure (see PROFILE_DEFAULTS for concrete values):
  * duration_seconds  — target clip length (hard ceiling; trimming applied)
  * aspect_ratio      — (width, height), always 9:16 for shorts-style feeds
  * fps               — 30 works everywhere; YouTube supports higher
  * narration_wpm     — words-per-minute cap used by the copywriter
  * narration_max_words — computed cap so the TTS stays under duration
  * style             — 'full_ai' | 'avatar_only' | 'reel_only'
  * intro_enabled     — prepend a title card (story title + category)
  * outro_enabled     — append a CTA card (@handle + "Follow for more")
  * bgm_enabled       — mix background music under narration
  * bgm_volume        — 0..100 (0=silent, 100=raw; recommend 10-20)
  * bitrate           — video bitrate hint for FFmpeg encoder
  * hashtags_max      — how many hashtags the caption writer appends
  * caption_max_chars — max chars for the platform's caption field

Users override most of these via Settings → Video Profile.
"""
from __future__ import annotations

from typing import Any


# Speaking rate used by the D-ID Edge TTS voice (~140 wpm = ~2.3 words/sec)
DEFAULT_WPM = 140


# Authoritative per-platform defaults — chosen for max reach on short-form feeds.
# Kept conservative: 25s for TikTok/Reels (algorithm sweet spot), 45s for Shorts.
PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    "tiktok": {
        "duration_seconds":   25,
        "aspect_ratio":       (1080, 1920),
        "fps":                30,
        "style":              "full_ai",
        "intro_enabled":      True,
        "outro_enabled":      True,
        "bgm_enabled":        True,
        "bgm_volume":         15,
        "bitrate":            "4500k",
        "hashtags_max":       4,
        "caption_max_chars":  2200,
    },
    "instagram": {
        "duration_seconds":   25,
        "aspect_ratio":       (1080, 1920),
        "fps":                30,
        "style":              "full_ai",
        "intro_enabled":      True,
        "outro_enabled":      True,
        "bgm_enabled":        True,
        "bgm_volume":         15,
        "bitrate":            "5000k",
        "hashtags_max":       8,
        "caption_max_chars":  2200,
    },
    "youtube_shorts": {
        "duration_seconds":   45,
        "aspect_ratio":       (1080, 1920),
        "fps":                30,
        "style":              "full_ai",
        "intro_enabled":      True,
        "outro_enabled":      True,
        "bgm_enabled":        True,
        "bgm_volume":         15,
        "bitrate":            "6000k",
        "hashtags_max":       3,  # YouTube penalizes hashtag spam
        "caption_max_chars":  5000,
    },
}


# Brand defaults applied to intro/outro cards (user can override in Settings).
BRAND_DEFAULTS: dict[str, str] = {
    "color":   "#F59E0B",      # amber — readable on dark bg
    "handle":  "",             # empty = no handle shown (user fills in)
    "cta":     "Follow for more",
    "font":    "Inter",
}


# Hard bounds to prevent UI sliders from producing nonsense values that
# would break D-ID (minimum narration) or overload Pollinations.
DURATION_MIN = 10
DURATION_MAX = 60
BGM_VOLUME_MIN = 0
BGM_VOLUME_MAX = 50


def get_profile(platform: str, user_settings: dict | None = None) -> dict[str, Any]:
    """Resolve the active profile for a platform, applying user overrides.

    `platform` is one of 'tiktok' | 'instagram' | 'youtube_shorts'.
    `user_settings` is the dict from database.get_user_settings() (or None
    for stock defaults). Any of these per-user keys override the defaults:

        video_duration_<platform>    (int seconds)
        video_style                  (applies to all platforms)
        video_bgm_enabled            (applies to all)
        video_bgm_volume             (applies to all)
        video_intro_enabled          (applies to all)
        video_outro_enabled          (applies to all)
        video_brand_color            (hex)
        video_brand_handle           (e.g. "@cryptonews")
        video_brand_cta              (e.g. "Follow for more")
    """
    platform = platform.lower().strip()
    if platform not in PROFILE_DEFAULTS:
        raise ValueError(f"Unknown video platform: {platform!r}")

    profile = dict(PROFILE_DEFAULTS[platform])   # shallow copy

    if user_settings:
        # Per-platform duration override
        dur_key = f"video_duration_{platform}"
        dur = user_settings.get(dur_key)
        if dur:
            try:
                dur = int(dur)
                profile["duration_seconds"] = max(
                    DURATION_MIN, min(DURATION_MAX, dur)
                )
            except (TypeError, ValueError):
                pass

        # Cross-platform overrides
        if user_settings.get("video_style"):
            profile["style"] = user_settings["video_style"]
        for key in ("intro_enabled", "outro_enabled", "bgm_enabled"):
            val = user_settings.get(f"video_{key}")
            if val is not None and val != "":
                profile[key] = _to_bool(val)
        bgm_v = user_settings.get("video_bgm_volume")
        if bgm_v is not None and bgm_v != "":
            try:
                profile["bgm_volume"] = max(
                    BGM_VOLUME_MIN, min(BGM_VOLUME_MAX, int(bgm_v))
                )
            except (TypeError, ValueError):
                pass

    # Derived: max narration words at the target speaking rate.
    # Subtract ~4s for intro+outro cards so avatar narration stays under limit.
    reserved = 0
    if profile["intro_enabled"]:
        reserved += 2
    if profile["outro_enabled"]:
        reserved += 2
    avatar_seconds = max(8, profile["duration_seconds"] - reserved)
    profile["avatar_seconds"] = avatar_seconds
    profile["narration_max_words"] = int(avatar_seconds * DEFAULT_WPM / 60)

    # Attach brand info (cross-platform)
    profile["brand"] = _resolve_brand(user_settings)

    profile["platform"] = platform
    return profile


def get_all_profiles(user_settings: dict | None = None) -> dict[str, dict[str, Any]]:
    """Return resolved profiles for every supported platform."""
    return {p: get_profile(p, user_settings) for p in PROFILE_DEFAULTS}


def _resolve_brand(user_settings: dict | None) -> dict[str, str]:
    brand = dict(BRAND_DEFAULTS)
    if user_settings:
        for key, skey in (
            ("color",  "video_brand_color"),
            ("handle", "video_brand_handle"),
            ("cta",    "video_brand_cta"),
        ):
            v = user_settings.get(skey)
            if v:
                brand[key] = str(v).strip()
    return brand


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "on", "t")


# Settings-form keys the dashboard should persist. Used by save_user_settings
# whitelist and by the Settings HTML template.
SETTINGS_KEYS = [
    "video_duration_tiktok",
    "video_duration_instagram",
    "video_duration_youtube_shorts",
    "video_style",
    "video_intro_enabled",
    "video_outro_enabled",
    "video_bgm_enabled",
    "video_bgm_volume",
    "video_brand_color",
    "video_brand_handle",
    "video_brand_cta",
]
