"""Viral content analyzer.

What it does:
  Scrapes trending short-form videos for a given topic across YouTube Shorts,
  TikTok, and Instagram, then distills them into a "viral blueprint" — a dict
  of patterns (hook phrases, average duration, hashtag clusters, caption
  length, emoji density) that the copywriter and hashtag generator can imitate.

Data sources & fallbacks (we try each, gracefully skip if not configured):
  1. YouTube Data API v3 — the most reliable. Set YOUTUBE_API_KEY in Settings.
  2. TikTok Discover via web scrape (no API needed, best-effort).
  3. Instagram hashtag pages via web scrape (best-effort, IG is the hardest).

The output is intentionally a single JSON-friendly dict so it can be cached
in the DB and passed straight to the copywriter.

Usage:
    from src.agents.viral_analyzer import scan_viral
    blueprint = scan_viral(
        topic="crypto",
        platforms=("youtube", "tiktok", "instagram"),
        limit_per_platform=20,
    )
    print(blueprint["hook_patterns"])      # ['You won't believe...', '3 reasons why...', ...]
    print(blueprint["top_hashtags"])       # [('#crypto', 12), ('#btc', 9), ...]
    print(blueprint["avg_duration"])       # 28.4
"""
from __future__ import annotations

import os
import re
import time
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

# ---------------------------------------------------------------------------
# Topics to scan for viral patterns
# ---------------------------------------------------------------------------

VIRAL_TOPICS = [
    # Core crypto
    "crypto news", "bitcoin", "ethereum", "crypto trading",
    # Finance & economy
    "economy news", "inflation", "federal reserve", "stock market crash",
    "investing for beginners", "financial freedom", "passive income",
    # Luxury / wealth
    "luxury lifestyle", "billionaire mindset", "wealth building",
    # Cross-topic
    "crypto millionaire", "bitcoin millionaire", "defi explained",
    "crypto vs stocks",
]

BLUEPRINT_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data"


# ---------------------------------------------------------------------------
# Common video record schema
# ---------------------------------------------------------------------------
# Each platform yields a list of dicts with this shape:
#   { "platform": "youtube"|"tiktok"|"instagram",
#     "id":        "...",
#     "title":     "...",         # full caption / video title
#     "duration":  float,         # seconds (None if unknown)
#     "views":     int,           # None if unknown
#     "likes":     int,           # None if unknown
#     "comments":  int,           # None if unknown
#     "tags":      ["#crypto", "#btc", ...],
#     "url":       "https://...",
#   }
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# YouTube Data API v3 — most reliable, gives clean structured data
# ---------------------------------------------------------------------------

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


def fetch_youtube(topic: str, limit: int = 20, api_key: str | None = None) -> list[dict]:
    """Return up to `limit` trending Shorts on `topic`.

    We search videos with `videoDuration=short` (<4 min) and order by viewCount.
    A second call to videos.list resolves stats + duration.
    """
    api_key = api_key or os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        return []

    try:
        # Step 1: collect video IDs with pageToken pagination (max 50/call)
        ids: list[str] = []
        page_token: str | None = None
        while len(ids) < limit:
            params: dict = {
                "key":               api_key,
                "q":                 topic,
                "type":              "video",
                "videoDuration":     "short",
                "order":             "viewCount",
                "maxResults":        50,
                "part":              "id,snippet",
                "relevanceLanguage": "en",
            }
            if page_token:
                params["pageToken"] = page_token
            r = requests.get(YOUTUBE_SEARCH_URL, params=params, timeout=15)
            if r.status_code != 200:
                print(f"[viral] YouTube search HTTP {r.status_code}: {r.text[:200]}")
                break
            resp_json  = r.json()
            new_ids    = [item["id"]["videoId"] for item in resp_json.get("items", [])
                          if "videoId" in item.get("id", {})]
            ids.extend(new_ids)
            page_token = resp_json.get("nextPageToken")
            if not page_token or not new_ids:
                break
        if not ids:
            return []

        # Step 2: fetch stats + contentDetails (duration) in batches of 50
        records = []
        for batch_start in range(0, len(ids), 50):
            batch = ids[batch_start:batch_start + 50]
            v = requests.get(YOUTUBE_VIDEOS_URL, params={
                "key":  api_key,
                "id":   ",".join(batch),
                "part": "snippet,statistics,contentDetails",
            }, timeout=15)
            if v.status_code != 200:
                continue
            for item in v.json().get("items", []):
                snippet = item.get("snippet", {})
                stats   = item.get("statistics", {})
                cd      = item.get("contentDetails", {})
                title   = snippet.get("title", "")
                desc    = snippet.get("description", "")
                tags    = snippet.get("tags", []) or []
                # Augment tags with hashtags found in title/description
                tags = list(set(tags) | set(_extract_hashtags(f"{title} {desc}")))
                records.append({
                    "platform":  "youtube",
                    "id":        item.get("id", ""),
                    "title":     title,
                    "duration":  _parse_iso_duration(cd.get("duration")),
                    "views":     int(stats.get("viewCount", 0) or 0),
                    "likes":     int(stats.get("likeCount", 0) or 0),
                    "comments":  int(stats.get("commentCount", 0) or 0),
                    "tags":      tags,
                    "url":       f"https://www.youtube.com/shorts/{item.get('id','')}",
                })
        # Filter to true Shorts only (<= 60s)
        records = [r for r in records if (r["duration"] or 0) <= 60]
        return records[:limit]
    except Exception as e:
        print(f"[viral] YouTube fetch failed: {e}")
        return []


def _parse_iso_duration(s: str | None) -> float | None:
    """Parse ISO 8601 duration like 'PT1M5S' -> 65.0."""
    if not s or not s.startswith("PT"):
        return None
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s)
    if not m:
        return None
    h, mi, se = (int(g) if g else 0 for g in m.groups())
    return float(h * 3600 + mi * 60 + se)


# ---------------------------------------------------------------------------
# TikTok / IG fallback datasets — per niche
# ---------------------------------------------------------------------------
# When the live RapidAPI scraper returns no data (no key, rate-limited, etc.)
# we fall back to a curated synthetic baseline so the dashboard still shows
# *something*. The pool is keyed by niche so a fitness user never sees crypto
# fallback content. Match niches.NICHE_VIRAL_TOPICS keys.

# Curated crypto viral hooks extracted from top-performing TikTok/YT content.
# Used as fallback when live scraping fails.
_CRYPTO_VIRAL_HOOKS: list[dict] = [
    {"title": "Bitcoin just did something it hasn't done in 3 years", "views": 2_800_000, "likes": 180_000, "comments": 4_200, "duration": 32.0},
    {"title": "3 altcoins that could 10x before the end of this cycle", "views": 1_950_000, "likes": 142_000, "comments": 3_100, "duration": 45.0},
    {"title": "Breaking: ETF inflows just hit an all-time record", "views": 3_400_000, "likes": 220_000, "comments": 6_800, "duration": 28.0},
    {"title": "Nobody is talking about this Bitcoin signal", "views": 2_100_000, "likes": 165_000, "comments": 5_300, "duration": 38.0},
    {"title": "This is why crypto is pumping today", "views": 1_700_000, "likes": 98_000, "comments": 2_900, "duration": 25.0},
    {"title": "You won't believe what happened to Ethereum overnight", "views": 2_500_000, "likes": 190_000, "comments": 4_700, "duration": 35.0},
    {"title": "Massive whale just moved $500M in Bitcoin", "views": 1_400_000, "likes": 87_000, "comments": 3_400, "duration": 22.0},
    {"title": "5 crypto projects the rich don't want you to know about", "views": 3_200_000, "likes": 245_000, "comments": 7_200, "duration": 52.0},
    {"title": "Crypto market just flipped bullish — here's the proof", "views": 1_850_000, "likes": 130_000, "comments": 3_600, "duration": 30.0},
    {"title": "Warning: this pattern appeared before every Bitcoin crash", "views": 2_700_000, "likes": 178_000, "comments": 5_900, "duration": 42.0},
    {"title": "The next 90 days will make or break crypto investors", "views": 2_050_000, "likes": 155_000, "comments": 4_100, "duration": 48.0},
    {"title": "Bitcoin halving aftermath explained in 60 seconds", "views": 1_300_000, "likes": 76_000, "comments": 2_200, "duration": 58.0},
    {"title": "Altcoin season is finally here — don't miss this", "views": 1_600_000, "likes": 112_000, "comments": 3_800, "duration": 36.0},
    {"title": "I analyzed 1000 crypto trades — this is what I found", "views": 2_900_000, "likes": 210_000, "comments": 6_500, "duration": 55.0},
    {"title": "BlackRock just bought more Bitcoin than you think", "views": 1_750_000, "likes": 125_000, "comments": 4_400, "duration": 27.0},
]

_CRYPTO_HASHTAGS = [
    "#crypto", "#bitcoin", "#btc", "#ethereum", "#eth", "#altcoin",
    "#cryptonews", "#web3", "#defi", "#blockchain", "#nft", "#xrp",
    "#solana", "#sol", "#binance", "#cryptotrading", "#cryptoinvestor",
    "#bullrun", "#hodl", "#altcoinseason",
]


# ── Per-niche synthetic fallback pools ────────────────────────────────────
# Each list contains generic viral-style hook templates that fit the niche.
# These ONLY load when the live RapidAPI scraper returned 0 results.

_NICHE_TIKTOK_HOOKS: dict[str, list[dict]] = {
    "fitness": [
        {"title": "I tried this workout for 30 days and the result shocked me", "views": 2_400_000, "likes": 195_000, "comments": 4_800, "duration": 32.0},
        {"title": "The only ab exercise you actually need", "views": 3_100_000, "likes": 240_000, "comments": 5_900, "duration": 24.0},
        {"title": "3 mistakes ruining your gym progress", "views": 1_850_000, "likes": 130_000, "comments": 3_600, "duration": 38.0},
        {"title": "How I lost 20 lbs without giving up carbs", "views": 2_700_000, "likes": 210_000, "comments": 6_400, "duration": 45.0},
        {"title": "POV: you finally figured out how to grow your glutes", "views": 1_600_000, "likes": 118_000, "comments": 2_900, "duration": 28.0},
        {"title": "Stop doing crunches — try this instead", "views": 2_050_000, "likes": 155_000, "comments": 4_100, "duration": 30.0},
        {"title": "My 5-minute morning routine for shredded abs", "views": 1_400_000, "likes": 92_000, "comments": 2_300, "duration": 36.0},
    ],
    "business": [
        {"title": "I made $10k in 30 days with this side hustle", "views": 2_800_000, "likes": 195_000, "comments": 5_200, "duration": 42.0},
        {"title": "3 things every entrepreneur should automate today", "views": 1_700_000, "likes": 125_000, "comments": 3_400, "duration": 35.0},
        {"title": "Why most startups fail in the first year", "views": 2_100_000, "likes": 162_000, "comments": 4_600, "duration": 48.0},
        {"title": "The one productivity trick that doubled my output", "views": 1_900_000, "likes": 142_000, "comments": 3_900, "duration": 30.0},
        {"title": "I asked 100 founders their biggest mistake — here it is", "views": 2_650_000, "likes": 198_000, "comments": 5_700, "duration": 55.0},
        {"title": "How to scale a business with $0 ad budget", "views": 1_550_000, "likes": 108_000, "comments": 2_800, "duration": 40.0},
    ],
    "food": [
        {"title": "This 10-minute recipe broke the internet", "views": 3_400_000, "likes": 265_000, "comments": 7_100, "duration": 28.0},
        {"title": "I tried Gordon Ramsay's pasta hack — it's insane", "views": 2_900_000, "likes": 220_000, "comments": 5_800, "duration": 35.0},
        {"title": "The viral cookie recipe everyone is making", "views": 2_200_000, "likes": 175_000, "comments": 4_300, "duration": 32.0},
        {"title": "5 dinner ideas that take under 15 minutes", "views": 1_800_000, "likes": 132_000, "comments": 3_500, "duration": 45.0},
        {"title": "Why your eggs never taste like restaurant eggs", "views": 1_650_000, "likes": 118_000, "comments": 2_900, "duration": 38.0},
        {"title": "POV: you learned the one trick that makes any meal go viral", "views": 2_050_000, "likes": 148_000, "comments": 4_000, "duration": 30.0},
    ],
    "fashion": [
        {"title": "This is the only outfit formula you need this year", "views": 2_300_000, "likes": 178_000, "comments": 4_500, "duration": 28.0},
        {"title": "How to style one jacket 10 different ways", "views": 2_750_000, "likes": 205_000, "comments": 5_200, "duration": 42.0},
        {"title": "The sneaker drop everyone is fighting over", "views": 1_950_000, "likes": 142_000, "comments": 3_700, "duration": 24.0},
        {"title": "5 wardrobe staples that never go out of style", "views": 1_600_000, "likes": 118_000, "comments": 2_800, "duration": 36.0},
        {"title": "I styled $20 outfits to look like $200 — here's how", "views": 2_400_000, "likes": 185_000, "comments": 4_900, "duration": 48.0},
        {"title": "Why this color combo is taking over fashion week", "views": 1_500_000, "likes": 102_000, "comments": 2_600, "duration": 30.0},
    ],
    "travel": [
        {"title": "This hidden island is the cheapest paradise on earth", "views": 2_800_000, "likes": 220_000, "comments": 5_500, "duration": 40.0},
        {"title": "5 travel hacks that will save you thousands", "views": 2_350_000, "likes": 178_000, "comments": 4_700, "duration": 35.0},
        {"title": "The best country to visit in 2026 — it's not what you think", "views": 1_950_000, "likes": 148_000, "comments": 3_800, "duration": 32.0},
        {"title": "POV: you just landed in the most underrated city in Europe", "views": 2_100_000, "likes": 165_000, "comments": 4_200, "duration": 28.0},
        {"title": "How I traveled the world on $50 a day", "views": 2_600_000, "likes": 198_000, "comments": 5_400, "duration": 50.0},
        {"title": "Stop booking hotels — do this instead", "views": 1_700_000, "likes": 125_000, "comments": 3_100, "duration": 30.0},
    ],
    "technology": [
        {"title": "This AI tool just replaced my entire workflow", "views": 3_100_000, "likes": 240_000, "comments": 6_200, "duration": 38.0},
        {"title": "5 ChatGPT prompts that will save you hours", "views": 2_500_000, "likes": 195_000, "comments": 5_100, "duration": 42.0},
        {"title": "The new iPhone feature nobody is talking about", "views": 1_900_000, "likes": 140_000, "comments": 3_600, "duration": 28.0},
        {"title": "I tried 10 AI apps so you don't have to", "views": 2_300_000, "likes": 178_000, "comments": 4_700, "duration": 55.0},
        {"title": "Why your laptop is slow — and how to fix it in 2 minutes", "views": 1_750_000, "likes": 128_000, "comments": 3_200, "duration": 35.0},
        {"title": "The AI breakthrough that changes everything", "views": 2_700_000, "likes": 205_000, "comments": 5_500, "duration": 32.0},
    ],
    "marketing": [
        {"title": "This Instagram trick grew my account by 50k in a week", "views": 2_500_000, "likes": 195_000, "comments": 5_300, "duration": 38.0},
        {"title": "The TikTok algorithm just changed — here's what to do", "views": 2_100_000, "likes": 160_000, "comments": 4_300, "duration": 32.0},
        {"title": "3 content ideas that always go viral", "views": 1_950_000, "likes": 148_000, "comments": 3_900, "duration": 28.0},
        {"title": "How I built a $1M brand with $0 ad spend", "views": 2_800_000, "likes": 215_000, "comments": 5_800, "duration": 52.0},
        {"title": "Stop posting like this if you want to grow", "views": 1_650_000, "likes": 120_000, "comments": 3_100, "duration": 30.0},
        {"title": "The hook every viral video uses (steal it)", "views": 2_400_000, "likes": 182_000, "comments": 4_700, "duration": 36.0},
    ],
    "real_estate": [
        {"title": "How I bought my first home at 23 with $5k down", "views": 2_300_000, "likes": 175_000, "comments": 4_500, "duration": 45.0},
        {"title": "This is the cheapest US city to buy a house right now", "views": 2_650_000, "likes": 198_000, "comments": 5_200, "duration": 38.0},
        {"title": "5 red flags every home buyer should know", "views": 1_750_000, "likes": 128_000, "comments": 3_300, "duration": 42.0},
        {"title": "POV: you walked into your dream home tour", "views": 2_100_000, "likes": 158_000, "comments": 4_000, "duration": 28.0},
        {"title": "The real estate hack nobody is talking about", "views": 1_950_000, "likes": 145_000, "comments": 3_700, "duration": 35.0},
        {"title": "How I made $50k flipping my first house", "views": 2_500_000, "likes": 190_000, "comments": 5_000, "duration": 50.0},
    ],
    "entertainment": [
        {"title": "This Netflix show is breaking every record", "views": 3_200_000, "likes": 245_000, "comments": 6_800, "duration": 30.0},
        {"title": "Ranking every Marvel movie from worst to best", "views": 2_700_000, "likes": 205_000, "comments": 5_700, "duration": 55.0},
        {"title": "The plot twist nobody saw coming", "views": 2_100_000, "likes": 160_000, "comments": 4_300, "duration": 28.0},
        {"title": "Why this song is taking over TikTok this week", "views": 2_500_000, "likes": 190_000, "comments": 4_900, "duration": 24.0},
        {"title": "5 underrated movies you need to watch tonight", "views": 1_850_000, "likes": 138_000, "comments": 3_500, "duration": 40.0},
        {"title": "POV: you just heard the album of the year", "views": 1_650_000, "likes": 120_000, "comments": 3_000, "duration": 30.0},
    ],
}

_NICHE_HASHTAGS: dict[str, list[str]] = {
    "fitness": ["#fitness", "#workout", "#gym", "#fitlife", "#fittok", "#fitnesstips", "#gymmotivation", "#healthylifestyle", "#abs", "#weightloss", "#muscle", "#cardio", "#bodybuilding", "#fitfam"],
    "business": ["#business", "#entrepreneur", "#startup", "#sidehustle", "#hustle", "#businesstips", "#makemoney", "#smallbusiness", "#ceo", "#mindset", "#productivity", "#growth", "#success"],
    "food": ["#food", "#foodie", "#recipe", "#cooking", "#foodtiktok", "#easyrecipe", "#tasty", "#foodhacks", "#mealprep", "#homecooking", "#instafood", "#chef", "#yummy"],
    "fashion": ["#fashion", "#style", "#ootd", "#outfit", "#streetwear", "#fashiontiktok", "#fashionhaul", "#styletips", "#trends", "#fashiongram", "#sneakers", "#luxury", "#aesthetic"],
    "travel": ["#travel", "#wanderlust", "#travelgram", "#traveltips", "#explore", "#adventure", "#vacation", "#traveltiktok", "#destination", "#solotravel", "#beach", "#hiddengems", "#bucketlist"],
    "technology": ["#tech", "#ai", "#technology", "#chatgpt", "#aitools", "#techtips", "#gadgets", "#innovation", "#techtok", "#coding", "#machinelearning", "#smarttech", "#newtech"],
    "marketing": ["#marketing", "#digitalmarketing", "#socialmedia", "#contentcreator", "#instagramtips", "#tiktokgrowth", "#brand", "#smm", "#seo", "#marketingtips", "#contentmarketing", "#growthhacking", "#personalbranding"],
    "real_estate": ["#realestate", "#realtor", "#property", "#homebuyer", "#realestateagent", "#dreamhome", "#housetour", "#mortgage", "#realestateinvesting", "#firsttimehomebuyer", "#newhome", "#hometips"],
    "entertainment": ["#entertainment", "#movies", "#tvshows", "#netflix", "#celebrity", "#popculture", "#music", "#hollywood", "#trailer", "#newrelease", "#filmtok", "#review", "#streaming"],
}

# Generic fallback used when topic/niche doesn't match a known niche key.
_GENERIC_HASHTAGS = ["#viral", "#fyp", "#foryou", "#trending", "#tiktok", "#reels", "#shorts", "#viralvideo"]


def _niche_for_topic(topic: str, niche_hint: str | None = None) -> str:
    """Infer the niche id for a topic string.

    Order:
      1. Explicit `niche_hint` (passed from caller / dashboard).
      2. Substring match against each niche's topic list (niches.NICHE_VIRAL_TOPICS).
      3. Substring match against the crypto keyword set (legacy default).
      4. "" (unknown — caller should treat as generic).
    """
    if niche_hint:
        h = niche_hint.strip().lower()
        if h:
            return h
    t = (topic or "").strip().lower()
    try:
        from src.agents.niches import NICHE_VIRAL_TOPICS  # local import to avoid cycle
        for nid, kws in NICHE_VIRAL_TOPICS.items():
            for kw in kws:
                if kw in t or t in kw:
                    return nid
    except Exception:
        pass
    crypto_signals = ("crypto", "bitcoin", "btc", "eth", "ether", "altcoin", "defi", "web3", "blockchain", "xrp", "solana")
    if any(s in t for s in crypto_signals):
        return "crypto"
    return ""


def _fallback_hooks_for(niche: str) -> list[dict]:
    if niche == "crypto":
        return list(_CRYPTO_VIRAL_HOOKS)
    return list(_NICHE_TIKTOK_HOOKS.get(niche) or [])


def _fallback_hashtags_for(niche: str) -> list[str]:
    if niche == "crypto":
        return list(_CRYPTO_HASHTAGS)
    return list(_NICHE_HASHTAGS.get(niche) or _GENERIC_HASHTAGS)


def _fetch_tiktok_rapidapi(topic: str, limit: int, api_key: str) -> list[dict]:
    """Fetch real TikTok videos via RapidAPI TikTok Scraper7 with cursor pagination."""
    url = "https://tiktok-scraper7.p.rapidapi.com/feed/search"
    headers = {
        "x-rapidapi-key":  api_key,
        "x-rapidapi-host": "tiktok-scraper7.p.rapidapi.com",
    }
    records: list[dict] = []
    cursor = "0"
    seen_ids: set[str] = set()

    while len(records) < limit:
        try:
            params = {
                "keywords":     topic,
                "count":        "20",   # API max per call
                "cursor":       cursor,
                "region":       "US",
                "publish_time": "0",
                "sort_type":    "0",
            }
            r = requests.get(url, headers=headers, params=params, timeout=15)
            if r.status_code != 200:
                print(f"[viral] TikTok RapidAPI HTTP {r.status_code}: {r.text[:200]}")
                break
            data    = r.json()
            payload = data.get("data") or {}
            items   = (payload.get("videos") if isinstance(payload, dict) else None) or []
            if not isinstance(items, list) or not items:
                break

            for it in items:
                if len(records) >= limit:
                    break
                desc     = it.get("title") or it.get("desc") or ""
                stats    = it.get("play_count") or it.get("playCount") or 0
                likes    = it.get("digg_count") or it.get("diggCount") or 0
                comments = it.get("comment_count") or it.get("commentCount") or 0
                dur      = it.get("duration") or 0
                vid_id   = str(it.get("video_id") or it.get("id") or "")
                if not vid_id or vid_id in seen_ids:
                    continue
                seen_ids.add(vid_id)
                author = it.get("author") or ""
                if isinstance(author, dict):
                    author = author.get("unique_id") or author.get("uniqueId") or ""
                tags = _extract_hashtags(desc)
                records.append({
                    "platform": "tiktok",
                    "id":       vid_id,
                    "title":    desc,
                    "duration": float(dur) if dur else None,
                    "views":    int(stats),
                    "likes":    int(likes),
                    "comments": int(comments),
                    "tags":     tags,
                    "url":      f"https://www.tiktok.com/@{author}/video/{vid_id}" if vid_id else "",
                })

            # Advance cursor for next page
            next_cursor  = str(payload.get("cursor") or data.get("cursor") or "")
            has_more     = bool(payload.get("hasMore") or data.get("hasMore"))
            if not has_more or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

        except Exception as e:
            print(f"[viral] TikTok RapidAPI failed: {e}")
            break

    return records


def fetch_tiktok(topic: str, limit: int = 20, niche: str | None = None) -> list[dict]:
    """Return viral TikTok records for `topic`.

    Strategy:
      1. RapidAPI TikTok Scraper7 (real data — requires RAPIDAPI_KEY in env).
      2. Fallback: curated viral hooks from the NICHE'S pool (never cross-niche).
    """
    api_key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if api_key:
        records = _fetch_tiktok_rapidapi(topic, limit, api_key)
        if records:
            print(f"[viral] tiktok RapidAPI: {len(records)} real records")
            return records
        print("[viral] tiktok RapidAPI returned 0 — falling back to baseline")

    # Niche-scoped fallback pool. We refuse to return crypto hooks for a
    # fitness topic, so worst-case we return [] and the dashboard shows the
    # empty state for that platform — better than mis-labelled content.
    nid = _niche_for_topic(topic, niche)
    pool = _fallback_hooks_for(nid)
    if not pool:
        print(f"[viral] tiktok fallback: no pool for niche={nid!r} topic={topic!r} → 0 synthetic")
        return []

    tags_base = _fallback_hashtags_for(nid)[:8]
    records = []
    for i, hook in enumerate(pool[:limit]):
        title = hook["title"]
        tags = list(dict.fromkeys(tags_base + _extract_hashtags(title)))
        records.append({
            "platform": "tiktok",
            "id":       f"synthetic_{i}",
            "title":    title,
            "duration": hook["duration"],
            "views":    hook["views"],
            "likes":    hook["likes"],
            "comments": hook["comments"],
            "tags":     tags,
            "url":      f"https://www.tiktok.com/search/video?q={requests.utils.quote(topic)}",
        })
    print(f"[viral] tiktok fallback: {len(records)} synthetic records (niche={nid!r})")
    return records


# ---------------------------------------------------------------------------
# Instagram — IG blocks unauthenticated scraping; we skip the futile request
# and return synthetic Reels records based on known viral crypto patterns.
# ---------------------------------------------------------------------------

_IG_VIRAL_HOOKS: list[dict] = [
    {"title": "Bitcoin just crossed a key resistance level 🔥 #crypto #bitcoin #btc #cryptonews #reels", "views": 890_000, "likes": 45_000, "comments": 1_200, "duration": 30.0},
    {"title": "3 crypto coins you NEED to watch this week 👀 #altcoin #crypto #investing #web3", "views": 650_000, "likes": 38_000, "comments": 980, "duration": 42.0},
    {"title": "This is what's happening in crypto right now 📈 #cryptonews #bitcoin #ethereum #defi", "views": 720_000, "likes": 41_000, "comments": 1_450, "duration": 35.0},
    {"title": "Don't sleep on this altcoin 🚀 #altcoins #crypto #bullrun #sol #cryptotrading", "views": 540_000, "likes": 29_000, "comments": 870, "duration": 28.0},
    {"title": "Ethereum breaking out — here's what it means for your portfolio 💰 #ethereum #eth #crypto", "views": 480_000, "likes": 26_500, "comments": 760, "duration": 38.0},
    {"title": "The crypto market just flipped 🔄 #bitcoin #cryptomarket #btc #trading #bullish", "views": 610_000, "likes": 33_000, "comments": 1_100, "duration": 25.0},
    {"title": "Why smart money is buying Bitcoin RIGHT NOW 🧠 #bitcoin #institutionalbuying #crypto", "views": 750_000, "likes": 47_000, "comments": 1_680, "duration": 45.0},
    {"title": "Crypto news you can't miss today 📰 #cryptonews #bitcoin #altcoins #blockchain", "views": 420_000, "likes": 22_000, "comments": 640, "duration": 32.0},
]


# Top Instagram accounts to scrape, keyed by niche.
# We pull their recent Reels and analyze hook + hashtag patterns.
_IG_CRYPTO_ACCOUNTS = [
    "coindesk", "cointelegraph", "bitcoin", "ethereumfoundation",
    "binance", "coinbase", "cryptonews.com", "watcher.guru",
]

_IG_NICHE_ACCOUNTS: dict[str, list[str]] = {
    "crypto":        _IG_CRYPTO_ACCOUNTS,
    "fitness":       ["nike", "mensfitness", "shape", "menshealthmag", "womenshealthmag", "myproteinuk", "gymshark"],
    "business":      ["forbes", "entrepreneur", "inc_magazine", "fastcompany", "harvardbiz", "ycombinator"],
    "food":          ["foodnetwork", "bonappetitmag", "buzzfeedtasty", "seriouseats", "thekitchn", "halfbakedharvest"],
    "fashion":       ["voguemagazine", "highsnobiety", "hypebeast", "wwd", "harpersbazaarus", "elleusa"],
    "travel":        ["natgeotravel", "lonelyplanet", "cntraveler", "beautifuldestinations", "afar", "travelandleisure"],
    "technology":    ["wired", "techcrunch", "verge", "mashable", "engadget", "mkbhd"],
    "marketing":     ["hubspot", "buffer", "hootsuite", "sproutsocial", "neilpatel", "garyvee"],
    "real_estate":   ["zillow", "realtordotcom", "realestate", "redfin", "househunters", "compass"],
    "entertainment": ["variety", "hollywoodreporter", "billboard", "ew", "rollingstone", "deadline"],
}

_IG_HOST = "instagram-scraper-20251.p.rapidapi.com"


def _ig_accounts_for(niche: str) -> list[str]:
    return list(_IG_NICHE_ACCOUNTS.get(niche) or [])


def _fetch_instagram_rapidapi(topic: str, limit: int, api_key: str,
                              accounts: list[str] | None = None) -> list[dict]:
    """Fetch real Instagram Reels via RapidAPI Instagram Scraper 2025.

    Scrapes /userposts/ from the niche's top accounts and filters for video
    (Reels). Fields confirmed from live response: play_count, like_count,
    comment_count, video_duration, caption.text, code (shortcode),
    media_type=2 for video.
    """
    headers = {
        "Content-Type":    "application/json",
        "x-rapidapi-key":  api_key,
        "x-rapidapi-host": _IG_HOST,
    }
    records: list[dict] = []
    accounts_tried = 0
    accounts = accounts if accounts is not None else _IG_CRYPTO_ACCOUNTS

    for account in accounts:
        if len(records) >= limit:
            break
        try:
            r = requests.get(
                f"https://{_IG_HOST}/userposts/",
                headers=headers,
                params={"username_or_id": account},
                timeout=12,
            )
            if r.status_code == 403:
                print(f"[viral] Instagram RapidAPI 403 — not subscribed to {_IG_HOST}")
                return []
            if r.status_code != 200:
                continue
            accounts_tried += 1
            items = r.json().get("data", {}).get("items") or []
            for it in items:
                if not isinstance(it, dict):
                    continue
                # Only Reels/video (media_type 2)
                if it.get("media_type") != 2:
                    continue
                caption_obj = it.get("caption") or {}
                caption = (caption_obj.get("text", "") if isinstance(caption_obj, dict)
                           else str(caption_obj))
                shortcode = it.get("code") or str(it.get("id") or "")
                duration  = it.get("video_duration") or None
                records.append({
                    "platform": "instagram",
                    "id":       str(it.get("id") or shortcode),
                    "title":    caption,
                    "duration": float(duration) if duration else None,
                    "views":    int(it.get("play_count") or it.get("view_count") or 0),
                    "likes":    int(it.get("like_count") or 0),
                    "comments": int(it.get("comment_count") or 0),
                    "tags":     _extract_hashtags(caption),
                    "url":      f"https://www.instagram.com/p/{shortcode}/",
                })
                if len(records) >= limit:
                    break
        except Exception as e:
            print(f"[viral] Instagram account={account} failed: {e}")
            continue

    print(f"[viral] instagram RapidAPI: {len(records)} reels from {accounts_tried} accounts")
    return records


def fetch_instagram(topic: str, limit: int = 20, niche: str | None = None) -> list[dict]:
    """Return Instagram Reels records for `topic`.

    Strategy:
      1. RapidAPI Instagram Scraper 2025 (real data) — scrapes the niche's top
         creator accounts (subscribe free on RapidAPI → 'Instagram Scraper 2025').
      2. Fallback: curated viral Reels hooks from the niche's pool.
    """
    nid = _niche_for_topic(topic, niche)
    accounts = _ig_accounts_for(nid) if nid else []

    api_key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if api_key and accounts:
        records = _fetch_instagram_rapidapi(topic, limit, api_key, accounts=accounts)
        if records:
            print(f"[viral] instagram RapidAPI: {len(records)} real records (niche={nid!r})")
            return records

    # Niche-scoped synthetic fallback. We reuse the per-niche TikTok hook pool
    # and append the niche's hashtag list to the caption so the analyzer can
    # extract them (Reels normally inline hashtags in the caption).
    if nid == "crypto":
        pool = list(_IG_VIRAL_HOOKS)
    else:
        pool = _fallback_hooks_for(nid)
    if not pool:
        print(f"[viral] instagram fallback: no pool for niche={nid!r} topic={topic!r} → 0 synthetic")
        return []

    tags_text = " " + " ".join(_fallback_hashtags_for(nid)[:6])
    records = []
    for i, hook in enumerate(pool[:limit]):
        title = hook["title"]
        # If the hook doesn't already carry niche hashtags inline (crypto pool
        # has them, generic per-niche pool doesn't), append them so the
        # analyzer picks them up as Reels hashtags.
        caption = title if "#" in title else (title + tags_text)
        records.append({
            "platform": "instagram",
            "id":       f"ig_synthetic_{i}",
            "title":    caption,
            "duration": hook["duration"],
            "views":    hook["views"],
            "likes":    hook["likes"],
            "comments": hook["comments"],
            "tags":     _extract_hashtags(caption),
            "url":      f"https://www.instagram.com/explore/tags/{requests.utils.quote(topic.lstrip('#'))}/",
        })
    print(f"[viral] instagram fallback: {len(records)} synthetic records (niche={nid!r})")
    return records


# ---------------------------------------------------------------------------
# X (Twitter) — twitter-api45.p.rapidapi.com
# ---------------------------------------------------------------------------

# Top accounts on X per niche — used for timeline scraping (quality signal pass).
_X_CRYPTO_ACCOUNTS = [
    "coindesk", "cointelegraph", "bitcoinmagazine", "Binance",
    "WatcherGuru", "CryptoRover", "saylor", "VitalikButerin",
]

_X_NICHE_ACCOUNTS: dict[str, list[str]] = {
    "crypto":        _X_CRYPTO_ACCOUNTS,
    "fitness":       ["mensfitness", "MensHealthMag", "shape_magazine", "Runnersworld", "MyProtein", "Gymshark"],
    "business":      ["Forbes", "Entrepreneur", "Inc", "FastCompany", "HarvardBiz", "ycombinator"],
    "food":          ["FoodNetwork", "bonappetit", "BuzzFeedTasty", "seriouseats", "Eater", "thekitchn"],
    "fashion":       ["voguemagazine", "highsnobiety", "HYPEBEAST", "wwd", "harpersbazaarus", "ELLEmagazine"],
    "travel":        ["NatGeoTravel", "lonelyplanet", "CNTraveler", "afarmedia", "TravelLeisure"],
    "technology":    ["WIRED", "TechCrunch", "verge", "mashable", "engadget", "MKBHD", "OpenAI"],
    "marketing":     ["HubSpot", "buffer", "hootsuite", "sproutsocial", "neilpatel", "garyvee"],
    "real_estate":   ["zillow", "realtordotcom", "realestate", "Redfin", "compass"],
    "entertainment": ["Variety", "THR", "billboard", "EW", "RollingStone", "DEADLINE"],
}

_X_HOST = "twitter-api45.p.rapidapi.com"


def _x_accounts_for(niche: str) -> list[str]:
    return list(_X_NICHE_ACCOUNTS.get(niche) or [])


def fetch_x(topic: str, limit: int = 20, niche: str | None = None) -> list[dict]:
    """Fetch viral X (Twitter) posts for `topic`.

    Strategy:
      1. /search.php — search tweets by keyword, filter high-engagement posts.
      2. /timeline.php — top crypto account timelines for quality signal.
    Requires RAPIDAPI_KEY + subscription to 'Twitter API45' on RapidAPI.
    """
    api_key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if not api_key:
        return []

    headers = {
        "Content-Type":    "application/json",
        "x-rapidapi-key":  api_key,
        "x-rapidapi-host": _X_HOST,
    }
    records: list[dict] = []

    # --- Pass 1: multiple keyword search queries to reach `limit` ---
    # Each search call returns max 20 results; run several queries to reach limit.
    _search_queries = [
        f"{topic} -is:retweet lang:en",
        f"#{topic.replace(' ', '')} -is:retweet lang:en",
        f"{topic} price analysis -is:retweet lang:en",
        f"{topic} news update -is:retweet lang:en",
        f"{topic} market -is:retweet lang:en",
    ]
    seen_ids: set[str] = set()
    try:
        for query in _search_queries:
            if len(records) >= limit:
                break
            r = requests.get(
                f"https://{_X_HOST}/search.php",
                headers=headers,
                params={"query": query, "count": "20"},
                timeout=12,
            )
            if r.status_code == 403:
                print(f"[viral] X RapidAPI 403 — not subscribed to {_X_HOST}")
                return []
            if r.status_code == 200:
                for t in r.json().get("timeline", []):
                    if len(records) >= limit:
                        break
                    if t.get("type") != "tweet":
                        continue
                    text = t.get("text") or ""
                    if not text:
                        continue
                    tid = str(t.get("tweet_id") or "")
                    if not tid or tid in seen_ids:
                        continue
                    seen_ids.add(tid)
                    ents = t.get("entities") or {}
                    ht_list = ents.get("hashtags") or []
                    tags = [f"#{h['text'].lower()}" for h in ht_list if h.get("text")]
                    tags += _extract_hashtags(text)
                    tags = list(dict.fromkeys(tags))
                    views = int(t.get("views") or 0)
                    records.append({
                        "platform": "x",
                        "id":       tid,
                        "title":    text,
                        "duration": None,
                        "views":    views,
                        "likes":    int(t.get("favorites") or 0),
                        "comments": int(t.get("replies") or 0),
                        "retweets": int(t.get("retweets") or 0),
                        "bookmarks":int(t.get("bookmarks") or 0),
                        "tags":     tags,
                        "url":      f"https://x.com/{t.get('screen_name','i')}/status/{tid}",
                    })
    except Exception as e:
        print(f"[viral] X search failed: {e}")

    # --- Pass 2: top account timelines (quality signal) ---
    nid = _niche_for_topic(topic, niche)
    accounts = _x_accounts_for(nid) or _X_CRYPTO_ACCOUNTS
    try:
        from src.agents.niches import NICHE_VIRAL_TOPICS as _NVT  # noqa: WPS433
        niche_signals = {w for kw in _NVT.get(nid, ()) for w in kw.lower().split()}
    except Exception:
        niche_signals = set()
    if not niche_signals:
        niche_signals = {"crypto", "bitcoin", "btc", "eth"}

    if len(records) < limit:
        for account in accounts:
            if len(records) >= limit:
                break
            try:
                r = requests.get(
                    f"https://{_X_HOST}/timeline.php",
                    headers=headers,
                    params={"screenname": account},
                    timeout=10,
                )
                if r.status_code != 200:
                    continue
                data = r.json()
                items = data if isinstance(data, list) else data.get("timeline", [])
                for t in items:
                    if not isinstance(t, dict):
                        continue
                    if t.get("type") not in ("tweet", None):
                        continue
                    text = t.get("text") or ""
                    if not text or len(text) < 20:
                        continue
                    topic_words = set(topic.lower().split())
                    if not any(w in text.lower() for w in topic_words | niche_signals):
                        continue
                    ents = t.get("entities") or {}
                    ht_list = ents.get("hashtags") or []
                    tags = [f"#{h['text'].lower()}" for h in ht_list if h.get("text")]
                    tags += _extract_hashtags(text)
                    tags = list(dict.fromkeys(tags))
                    records.append({
                        "platform": "x",
                        "id":       t.get("tweet_id", ""),
                        "title":    text,
                        "duration": None,
                        "views":    int(t.get("views") or 0),
                        "likes":    int(t.get("favorites") or 0),
                        "comments": int(t.get("replies") or 0),
                        "retweets": int(t.get("retweets") or 0),
                        "bookmarks":int(t.get("bookmarks") or 0),
                        "tags":     tags,
                        "url":      f"https://x.com/{t.get('screen_name','i')}/status/{t.get('tweet_id','')}",
                    })
                    if len(records) >= limit:
                        break
            except Exception:
                continue

    print(f"[viral] x RapidAPI: {len(records)} tweets (search + timelines)")
    return records[:limit]


# ---------------------------------------------------------------------------
# Facebook — Reels / Pages via RapidAPI 'facebook-scraper3'
# ---------------------------------------------------------------------------

_FB_HOST = "facebook-scraper3.p.rapidapi.com"

_FB_NICHE_PAGES: dict[str, list[str]] = {
    "crypto":        ["CoinDesk", "cointelegraph", "Binance", "coinbase", "Bitcoin"],
    "fitness":       ["MensHealthMag", "WomensHealthMag", "MyProtein", "Gymshark", "Nike"],
    "business":      ["Forbes", "Entrepreneur", "Inc", "FastCompany", "harvardbusinessreview"],
    "food":          ["FoodNetwork", "BonAppetitMag", "buzzfeedtasty", "seriouseats", "thekitchn"],
    "fashion":       ["Vogue", "highsnobiety", "HYPEBEAST", "WWDFashion", "ELLEmagazine"],
    "travel":        ["NationalGeographic", "lonelyplanet", "CNTraveler", "afar", "TravelandLeisure"],
    "technology":    ["wired", "TechCrunch", "verge", "mashable", "engadget"],
    "marketing":     ["hubspot", "buffer", "hootsuite", "sproutsocial", "GaryVee"],
    "real_estate":   ["Zillow", "realtor.com", "Realestate", "Redfin", "Compass"],
    "entertainment": ["Variety", "HollywoodReporter", "billboard", "EW", "RollingStone"],
}


def _fb_pages_for(niche: str) -> list[str]:
    return list(_FB_NICHE_PAGES.get(niche) or [])


def _fb_parse_item(it: dict) -> dict | None:
    """Normalize a Facebook post/video JSON object to our record schema."""
    if not isinstance(it, dict):
        return None
    text = (it.get("message") or it.get("text") or it.get("description")
            or it.get("caption") or it.get("title") or it.get("name") or "")
    if not text:
        return None
    views    = int(it.get("video_view_count") or it.get("play_count")
                   or it.get("views_count") or it.get("views")
                   or it.get("members_count") or it.get("member_count") or 0)
    likes    = int(it.get("reactions_count") or it.get("reaction_count")
                   or it.get("likes_count") or it.get("likes") or 0)
    comments = int(it.get("comments_count") or it.get("comment_count")
                   or it.get("comments") or 0)
    shares   = int(it.get("shares_count") or it.get("share_count")
                   or it.get("reshare_count") or it.get("shares") or 0)
    url      = (it.get("url") or it.get("permalink") or it.get("post_url")
                or it.get("video_url") or "")
    duration = it.get("video_duration") or it.get("duration") or None
    return {
        "platform": "facebook",
        "id":       str(it.get("post_id") or it.get("video_id") or it.get("id") or url),
        "title":    text,
        "duration": float(duration) if duration else None,
        "views":    views,
        "likes":    likes,
        "comments": comments,
        "shares":   shares,
        "tags":     _extract_hashtags(text),
        "url":      url,
    }


def _fetch_facebook_rapidapi(topic: str, limit: int, api_key: str,
                             pages: list[str] | None = None) -> list[dict]:
    """Search Facebook posts/videos via RapidAPI 'facebook-scraper3'.

    Uses /search/posts and /search/videos with cursor pagination.
    """
    headers = {
        "x-rapidapi-key":  api_key,
        "x-rapidapi-host": _FB_HOST,
        "Content-Type":    "application/json",
    }
    records: list[dict] = []
    seen_ids: set[str] = set()

    # --- Pass 1: paginated search (posts then videos) ---
    for path in ("/search/posts", "/search/videos"):
        if len(records) >= limit:
            break
        cursor = None
        for _ in range(6):  # max 6 pages per endpoint
            if len(records) >= limit:
                break
            params: dict = {"query": topic}
            if cursor:
                import json as _json
                params["cursor"] = _json.dumps(cursor)
            try:
                r = requests.get(f"https://{_FB_HOST}{path}",
                                 headers=headers, params=params, timeout=15)
            except Exception as e:
                print(f"[viral] Facebook {path} request failed: {e}")
                break
            if r.status_code in (429, 403, 404):
                if r.status_code == 429:
                    print(f"[viral] Facebook RapidAPI 429 rate limit on {path}")
                elif r.status_code == 403:
                    try:
                        msg = r.json().get("message", "")
                    except Exception:
                        msg = ""
                    if "not subscribed" not in msg.lower():
                        return records  # hard block
                break
            if r.status_code != 200:
                break
            try:
                payload = r.json()
            except Exception:
                break
            items = payload.get("results") or []
            if not isinstance(items, list) or not items:
                break
            for it in items:
                if len(records) >= limit:
                    break
                rec = _fb_parse_item(it if isinstance(it, dict) else {})
                if rec and rec["id"] not in seen_ids:
                    seen_ids.add(rec["id"])
                    records.append(rec)
            cursor = payload.get("cursor")
            if not cursor:
                break
        if records:
            print(f"[viral] Facebook {path}: {len(records)} records so far")

    # --- Pass 2: niche page video timelines (facebook-scraper3 paths) ---
    if pages:
        page_paths = ["/page/videos", "/page/posts", "/page/reels", "/get_page_videos"]
        for handle in pages:
            if len(records) >= limit:
                break
            for path in page_paths:
                try:
                    r = requests.get(f"https://{_FB_HOST}{path}",
                                     headers=headers,
                                     params={"page_id": handle},
                                     timeout=12)
                except Exception:
                    continue
                if r.status_code == 403:
                    return records
                if r.status_code != 200:
                    continue
                try:
                    payload = r.json()
                except Exception:
                    continue
                items = (payload.get("videos") or payload.get("posts")
                         or payload.get("data") or payload.get("results") or [])
                if isinstance(items, dict):
                    items = items.get("videos") or list(items.values())
                added = 0
                for it in items:
                    rec = _fb_parse_item(it if isinstance(it, dict) else {})
                    if rec:
                        records.append(rec)
                        added += 1
                    if len(records) >= limit:
                        break
                if added:
                    break
    if records:
        print(f"[viral] Facebook page-scrape: {len(records)} records")
    return records


def fetch_facebook(topic: str, limit: int = 20, niche: str | None = None) -> list[dict]:
    """Return viral Facebook records for `topic` (search + Reels).

    Strategy:
      1. RapidAPI Facebook Scraper3 (real data — same RAPIDAPI_KEY,
         subscribe at rapidapi.com → 'Facebook Scraper3').
      2. Niche-scoped synthetic fallback (same hooks as TikTok, retagged).
    """
    api_key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if api_key:
        records = _fetch_facebook_rapidapi(topic, limit, api_key, pages=_fb_pages_for(_niche_for_topic(topic, niche)))
        if records:
            print(f"[viral] facebook RapidAPI: {len(records)} real records")
            return records
        print("[viral] facebook RapidAPI returned 0 — falling back to baseline")

    nid = _niche_for_topic(topic, niche)
    pool = _fallback_hooks_for(nid)
    if not pool:
        print(f"[viral] facebook fallback: no pool for niche={nid!r} topic={topic!r} → 0 synthetic")
        return []

    tags_base = _fallback_hashtags_for(nid)[:6]
    records = []
    for i, hook in enumerate(pool[:limit]):
        title = hook["title"]
        tags = list(dict.fromkeys(tags_base + _extract_hashtags(title)))
        records.append({
            "platform": "facebook",
            "id":       f"fb_synthetic_{i}",
            "title":    title,
            "duration": hook["duration"],
            "views":    int((hook.get("views") or 0) * 0.45),
            "likes":    int((hook.get("likes") or 0) * 0.55),
            "comments": int((hook.get("comments") or 0) * 0.6),
            "shares":   int((hook.get("likes") or 0) * 0.12),
            "tags":     tags,
            "url":      f"https://www.facebook.com/search/top/?q={requests.utils.quote(topic)}",
        })
    print(f"[viral] facebook fallback: {len(records)} synthetic records (niche={nid!r})")
    return records


# ---------------------------------------------------------------------------
# Pattern extraction
# ---------------------------------------------------------------------------

_HASHTAG_RE = re.compile(r"#[A-Za-z0-9_]+")
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001F9FF\U00002600-\U000027BF\U0001FA70-\U0001FAFF]"
)


def _extract_hashtags(text: str) -> list[str]:
    return [h.lower() for h in _HASHTAG_RE.findall(text or "")]


# Classify hook type for pattern learning
_HOOK_PATTERNS = {
    "number":   re.compile(r"^\d|^\$|^#\d"),
    "question": re.compile(r"\?"),
    "statement_shock": re.compile(
        r"\b(just|breaking|alert|warning|nobody|secret|never|always|"
        r"finally|massive|insane|crazy|unbelievable|shocking)\b", re.I
    ),
    "list":     re.compile(r"\b(\d+)\s+(reason|way|thing|step|fact|sign)", re.I),
    "pov":      re.compile(r"\bpov\b", re.I),
}


def _classify_hook(text: str) -> str:
    for hook_type, pattern in _HOOK_PATTERNS.items():
        if pattern.search(text[:80]):
            return hook_type
    return "other"


def _extract_hooks(text: str) -> list[str]:
    """Pull the first sentence (or first 8-10 words) — the 'hook'."""
    if not text:
        return []
    text = text.strip().replace("\n", " ")
    m = re.split(r"[.!?]", text, maxsplit=1)
    first = m[0].strip() if m else text
    words = first.split()
    if not words:
        return []
    return [" ".join(words[:10])]


def _engagement_score(rec: dict) -> float:
    views = rec.get("views") or 0
    likes = rec.get("likes") or 0
    comm  = rec.get("comments") or 0
    if views <= 0:
        return 0.0
    like_ratio = likes / max(views, 1)
    comm_ratio = comm  / max(views, 1)
    return float(views * (1 + like_ratio + 2 * comm_ratio))


def build_blueprint(records: list[dict], topic: str) -> dict:
    """Distill records into a viral blueprint."""
    if not records:
        return {
            "topic":         topic,
            "generated_at":  datetime.now(timezone.utc).isoformat(),
            "sample_size":   0,
            "by_platform":   {},
            "hook_patterns": [],
            "top_hashtags":  [],
            "avg_duration":  None,
            "duration_p50":  None,
            "duration_p90":  None,
            "avg_caption_chars": None,
            "avg_emoji_count":   None,
            "top_examples":  [],
        }

    durations = [r["duration"] for r in records if r.get("duration")]
    captions  = [r.get("title") or "" for r in records]
    all_tags: list[str] = []
    for r in records:
        all_tags.extend(r.get("tags") or [])
    hashtag_counts = Counter(all_tags).most_common(20)

    ranked = sorted(records, key=_engagement_score, reverse=True)
    hooks: list[str] = []
    for r in ranked[:8]:
        hooks.extend(_extract_hooks(r.get("title") or ""))
    seen: set[str] = set()
    hooks = [h for h in hooks if h.lower() not in seen and not seen.add(h.lower())][:5]

    by_platform: dict[str, dict] = {}
    for plat in ("youtube", "tiktok", "instagram", "x", "facebook"):
        plat_recs = [r for r in records if r["platform"] == plat]
        if plat_recs:
            by_platform[plat] = {
                "count":          len(plat_recs),
                "avg_views":      sum(r.get("views") or 0 for r in plat_recs) / len(plat_recs),
                "avg_duration":   sum(r["duration"] or 0 for r in plat_recs) / len(plat_recs)
                                  if any(r["duration"] for r in plat_recs) else None,
                "top_hashtag":    Counter(t for r in plat_recs for t in (r.get("tags") or []))
                                  .most_common(1),
            }

    hook_type_counter: Counter = Counter()
    for r in ranked[:15]:
        title = (r.get("title") or "").strip()
        if title:
            hook_type_counter[_classify_hook(title)] += 1

    cta_patterns = [
        "follow for", "follow me", "link in bio", "comment below", "share this",
        "like if", "save this", "tag a friend", "don't miss", "next update",
    ]
    cta_usage = Counter(
        pat for r in records
        for pat in cta_patterns
        if pat in (r.get("title") or "").lower()
    ).most_common(5)

    high_algo: list[dict] = []
    for r in ranked[:20]:
        views = r.get("views") or 0
        likes = r.get("likes") or 0
        if views > 10_000 and likes > 0:
            vpl = views / likes
            if vpl > 20:
                high_algo.append({
                    "platform": r["platform"],
                    "title": (r.get("title") or "")[:100],
                    "views_per_like": round(vpl, 0),
                    "url": r.get("url"),
                })

    blueprint = {
        "topic":         topic,
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "sample_size":   len(records),
        "by_platform":   by_platform,
        "hook_patterns": hooks,
        "hook_type_distribution": dict(hook_type_counter.most_common()),
        "top_cta_patterns": cta_usage,
        "algo_boosted_examples": high_algo[:3],
        "top_hashtags":  hashtag_counts,
        "avg_duration":  round(sum(durations) / len(durations), 1) if durations else None,
        "duration_p50":  _percentile(durations, 50),
        "duration_p90":  _percentile(durations, 90),
        "avg_caption_chars": round(sum(len(c) for c in captions) / len(captions), 1)
                             if captions else None,
        "avg_emoji_count":   round(sum(len(_EMOJI_RE.findall(c)) for c in captions) / len(captions), 2)
                             if captions else None,
        "top_examples":  [
            {"platform": r["platform"], "title": (r.get("title") or "")[:120],
             "views": r.get("views"), "likes": r.get("likes"),
             "comments": r.get("comments"), "url": r.get("url")}
            for r in ranked[:8]
        ],
    }
    return blueprint


def _percentile(values: Iterable[float], p: int) -> float | None:
    vals = sorted(v for v in values if v)
    if not vals:
        return None
    k = (len(vals) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    return round(vals[f] + (vals[c] - vals[f]) * (k - f), 1)


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def scan_viral(
    topic: str = "crypto",
    platforms: tuple[str, ...] = ("youtube", "tiktok", "instagram", "x", "facebook"),
    limit_per_platform: int = 20,
    youtube_api_key: str | None = None,
    niche: str | None = None,
) -> dict:
    """Scrape every platform in `platforms`, then build & return a blueprint."""
    t0 = time.time()
    records: list[dict] = []
    nid = _niche_for_topic(topic, niche)

    if "youtube" in platforms:
        ty = time.time()
        recs = fetch_youtube(topic, limit_per_platform, api_key=youtube_api_key)
        records.extend(recs)
        print(f"[viral] youtube: {len(recs)} records ({time.time()-ty:.1f}s)")

    if "tiktok" in platforms:
        tt = time.time()
        recs = fetch_tiktok(topic, limit_per_platform, niche=nid)
        records.extend(recs)
        print(f"[viral] tiktok: {len(recs)} records ({time.time()-tt:.1f}s)")

    if "instagram" in platforms:
        ti = time.time()
        recs = fetch_instagram(topic, limit_per_platform, niche=nid)
        records.extend(recs)
        print(f"[viral] instagram: {len(recs)} records ({time.time()-ti:.1f}s)")

    if "x" in platforms:
        tx = time.time()
        recs = fetch_x(topic, limit_per_platform, niche=nid)
        records.extend(recs)
        print(f"[viral] x: {len(recs)} records ({time.time()-tx:.1f}s)")

    if "facebook" in platforms:
        tf = time.time()
        recs = fetch_facebook(topic, limit_per_platform, niche=nid)
        records.extend(recs)
        print(f"[viral] facebook: {len(recs)} records ({time.time()-tf:.1f}s)")

    blueprint = build_blueprint(records, topic)
    blueprint["scan_duration_seconds"] = round(time.time() - t0, 1)
    blueprint["niche"] = nid
    print(f"[viral] blueprint: sample={blueprint['sample_size']} "
          f"hooks={len(blueprint['hook_patterns'])} hashtags={len(blueprint['top_hashtags'])}")
    return blueprint


def save_blueprint(blueprint: dict, path: Path | None = None) -> Path:
    out = path or (BLUEPRINT_CACHE_DIR / "viral_blueprint.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(blueprint, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def load_blueprint(path: Path | None = None) -> dict:
    p = path or (BLUEPRINT_CACHE_DIR / "viral_blueprint.json")
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def scan_multi_topics(
    topics: list[str] | None = None,
    platforms: tuple[str, ...] = ("youtube", "tiktok", "instagram", "x", "facebook"),
    limit_per_platform: int = 20,
    youtube_api_key: str | None = None,
    save: bool = True,
    niche: str | None = None,
) -> dict:
    """Scan multiple topics, merge records, build a combined blueprint."""
    topics = topics or VIRAL_TOPICS[:8]
    t0 = time.time()
    all_records: list[dict] = []
    nid = _niche_for_topic(topics[0] if topics else "", niche)

    for topic in topics:
        print(f"[viral] scanning topic: '{topic}'")
        for platform in platforms:
            if platform == "youtube":
                recs = fetch_youtube(topic, limit_per_platform, api_key=youtube_api_key)
            elif platform == "tiktok":
                recs = fetch_tiktok(topic, limit_per_platform, niche=nid)
            elif platform == "instagram":
                recs = fetch_instagram(topic, limit_per_platform, niche=nid)
            elif platform == "x":
                recs = fetch_x(topic, limit_per_platform, niche=nid)
            elif platform == "facebook":
                recs = fetch_facebook(topic, limit_per_platform, niche=nid)
            else:
                recs = []
            for r in recs:
                r["_topic"] = topic
            all_records.extend(recs)

    seen_urls: set[str] = set()
    deduped: list[dict] = []
    for r in all_records:
        u = r.get("url") or ""
        if u and u in seen_urls:
            continue
        if u:
            seen_urls.add(u)
        deduped.append(r)

    blueprint = build_blueprint(deduped, topic=", ".join(topics))
    blueprint["scan_duration_seconds"] = round(time.time() - t0, 1)
    blueprint["niche"] = nid
    blueprint["topics_scanned"] = list(topics)
    if save:
        try:
            save_blueprint(blueprint)
        except Exception as exc:
            print(f"[viral] save_blueprint failed: {exc}")
    return blueprint
