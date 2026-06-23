"""Instagram Growth Analytics Agent.

Fetches real-time data from Meta Graph API for an Instagram Business/Creator
account and caches it locally.  Provides:
  - Account overview (followers, impressions, reach, profile views)
  - Per-post metrics (likes, comments, saves, shares, video plays, reach)
  - Audience demographics (countries, cities, age/gender)
  - Daily growth snapshots for trend charts
  - AI-powered growth recommendations (Claude)

Requirements in .env:
    IG_USER_ID=<instagram business account id>
    IG_ACCESS_TOKEN=<long-lived page access token>
    ANTHROPIC_API_KEY=<for AI recommendations>

Cache location: data/ig_analytics/ (refreshed at most once per hour).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent.parent
_CACHE_DIR = _ROOT / "data" / "ig_analytics"
_GRAPH = "https://graph.facebook.com/v22.0"

CACHE_TTL = 3600  # 1 hour


def _ig_env() -> tuple[str, str]:
    """Return (ig_user_id, access_token) or raise if missing."""
    uid = os.environ.get("IG_USER_ID", "").strip()
    tok = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if not uid or not tok:
        raise RuntimeError(
            "IG_USER_ID and IG_ACCESS_TOKEN must be set in .env  "
            "(run: python src/agents/instagram_oauth_setup.py)"
        )
    return uid, tok


def _get(path: str, params: dict | None = None, token: str = "") -> dict:
    import requests
    p = {"access_token": token, **(params or {})}
    r = requests.get(f"{_GRAPH}/{path}", params=p, timeout=30)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Graph API error: {data['error'].get('message', data)}")
    return data


def _cache_path(key: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"{key}.json"


def _cache_read(key: str, ttl: int = CACHE_TTL) -> dict | None:
    p = _cache_path(key)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if time.time() - d.get("_cached_at", 0) < ttl:
            return d
    except Exception:
        pass
    return None


def _cache_write(key: str, data: dict) -> None:
    data["_cached_at"] = time.time()
    _cache_path(key).write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def fetch_account_overview(force: bool = False) -> dict[str, Any]:
    """Fetch account-level stats: followers, media count, impressions, reach."""
    cached = None if force else _cache_read("overview")
    if cached:
        return cached

    uid, tok = _ig_env()

    # Basic profile
    profile = _get(uid, {
        "fields": "id,username,name,biography,followers_count,media_count,"
                  "profile_picture_url,website"
    }, token=tok)

    # 30-day insights
    # NOTE: impressions + profile_views + website_clicks deprecated Jan 2025 (v21+)
    # Use: views (replaced impressions), reach, accounts_engaged, follower_count
    since = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
    until = int(datetime.now(timezone.utc).timestamp())

    def _insight(metrics: str, period: str = "day") -> list[dict]:
        try:
            r = _get(f"{uid}/insights", {
                "metric": metrics,
                "period": period,
                "since":  since,
                "until":  until,
            }, token=tok)
            return r.get("data", [])
        except Exception:
            return []

    views_data        = _insight("views")          # replaces impressions (deprecated)
    reach_data        = _insight("reach")
    engaged_data      = _insight("accounts_engaged")
    follower_data     = _insight("follower_count")

    def _sum_values(items: list[dict]) -> int:
        total = 0
        for item in items:
            for v in item.get("values", []):
                total += v.get("value", 0)
        return total

    def _daily_series(items: list[dict]) -> list[dict]:
        for item in items:
            return [
                {"date": v["end_time"][:10], "value": v["value"]}
                for v in item.get("values", [])
            ]
        return []

    result = {
        "username":          profile.get("username", ""),
        "name":              profile.get("name", ""),
        "bio":               profile.get("biography", ""),
        "followers":         profile.get("followers_count", 0),
        "media_count":       profile.get("media_count", 0),
        "profile_pic":       profile.get("profile_picture_url", ""),
        "website":           profile.get("website", ""),
        # v22+ metrics (impressions/profile_views/website_clicks deprecated)
        "views_30d":          _sum_values(views_data),
        "reach_30d":          _sum_values(reach_data),
        "accounts_engaged_30d": _sum_values(engaged_data),
        "follower_growth_30d":  _sum_values(follower_data),
        "views_series":       _daily_series(views_data),
        "reach_series":       _daily_series(reach_data),
        # Legacy aliases for templates (mapped from new metrics)
        "impressions_30d":    _sum_values(views_data),    # views replaces impressions
        "profile_views_30d":  _sum_values(engaged_data),  # best available replacement
        "website_clicks_30d": 0,                           # no replacement in v22
        "impressions_series": _daily_series(views_data),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    # Calculate engagement rate from posts (avg likes+comments / followers * 100)
    try:
        posts = fetch_recent_posts(force=force)
        if posts and result["followers"] > 0:
            total_eng = sum(
                (p.get("like_count", 0) + p.get("comments_count", 0))
                for p in posts[:12]
            )
            result["engagement_rate"] = round(
                (total_eng / max(len(posts[:12]), 1)) / result["followers"] * 100, 2
            )
        else:
            result["engagement_rate"] = 0.0
    except Exception:
        result["engagement_rate"] = 0.0

    _cache_write("overview", result)
    return result


def fetch_recent_posts(limit: int = 20, force: bool = False) -> list[dict[str, Any]]:
    """Fetch recent media with per-post insights."""
    cached = None if force else _cache_read("posts")
    if cached:
        return cached.get("posts", [])

    uid, tok = _ig_env()

    media_resp = _get(f"{uid}/media", {
        "fields": "id,caption,media_type,media_url,thumbnail_url,permalink,"
                  "timestamp,like_count,comments_count",
        "limit":  limit,
    }, token=tok)

    posts: list[dict] = []
    for m in media_resp.get("data", []):
        post: dict = {
            "id":             m.get("id"),
            "caption":        (m.get("caption") or "")[:200],
            "media_type":     m.get("media_type", "IMAGE"),
            "thumbnail":      m.get("thumbnail_url") or m.get("media_url", ""),
            "permalink":      m.get("permalink", ""),
            "timestamp":      m.get("timestamp", ""),
            "date":           m.get("timestamp", "")[:10],
            "like_count":     m.get("like_count", 0),
            "comments_count": m.get("comments_count", 0),
        }

        # Per-post insights — v22+ API (impressions + video_views deprecated)
        # Use: views (replaces both), reach, saved, total_interactions
        try:
            metrics = "views,reach,saved,total_interactions"
            if m.get("media_type") in ("REEL",):
                metrics += ",ig_reels_avg_watch_time"
            ins = _get(f"{m['id']}/insights", {"metric": metrics}, token=tok)
            for item in ins.get("data", []):
                name = item.get("name", "")
                val  = item.get("values", [{}])[0].get("value", 0) if item.get("values") else item.get("value", 0)
                post[name] = val
        except Exception:
            pass

        post.setdefault("views", 0)
        post.setdefault("reach", 0)
        post.setdefault("saved", 0)
        post.setdefault("total_interactions", 0)
        post.setdefault("ig_reels_avg_watch_time", 0)
        # Legacy alias so templates can use either name
        post["impressions"] = post["views"]
        post["video_views"] = post["views"]

        # Engagement rate per post
        if post.get("reach", 0) > 0:
            eng = post["like_count"] + post["comments_count"] + post.get("saved", 0)
            post["engagement_rate"] = round(eng / post["reach"] * 100, 2)
        else:
            post["engagement_rate"] = 0.0

        posts.append(post)

    posts.sort(key=lambda p: p.get("impressions", 0), reverse=True)
    _cache_write("posts", {"posts": posts})
    return posts


def fetch_audience_demographics(force: bool = False) -> dict[str, Any]:
    """Fetch follower demographics: countries, cities, age/gender."""
    cached = None if force else _cache_read("audience")
    if cached:
        return cached

    uid, tok = _ig_env()

    def _lifetime(metric: str) -> dict:
        try:
            r = _get(f"{uid}/insights", {
                "metric": metric,
                "period": "lifetime",
            }, token=tok)
            for item in r.get("data", []):
                return item.get("values", [{}])[0].get("value", {})
        except Exception:
            pass
        return {}

    countries  = _lifetime("audience_country")
    cities     = _lifetime("audience_city")
    gender_age = _lifetime("audience_gender_age")

    # Sort and limit
    top_countries = sorted(countries.items(), key=lambda x: x[1], reverse=True)[:15]
    top_cities    = sorted(cities.items(), key=lambda x: x[1], reverse=True)[:10]

    # Parse gender_age: keys like "M.25-34", "F.18-24"
    male_total = female_total = 0
    age_groups: dict[str, int] = {}
    for key, val in gender_age.items():
        parts = key.split(".")
        if len(parts) == 2:
            gender, age = parts
            if gender == "M":
                male_total += val
            elif gender == "F":
                female_total += val
            age_groups[age] = age_groups.get(age, 0) + val

    top_ages = sorted(age_groups.items(), key=lambda x: x[1], reverse=True)

    result = {
        "countries":     [{"code": c, "count": n} for c, n in top_countries],
        "cities":        [{"city": c, "count": n} for c, n in top_cities],
        "gender":        {"male": male_total, "female": female_total},
        "age_groups":    [{"range": a, "count": n} for a, n in top_ages],
        "fetched_at":    datetime.now(timezone.utc).isoformat(),
    }

    _cache_write("audience", result)
    return result


def fetch_growth_history() -> list[dict[str, Any]]:
    """
    Return stored daily follower snapshots (max 90 days).
    Records are appended once per day by record_daily_snapshot().
    """
    p = _CACHE_DIR / "growth_history.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def record_daily_snapshot() -> None:
    """Call once per day (via scheduler) to persist follower count."""
    try:
        uid, tok = _ig_env()
        profile = _get(uid, {"fields": "followers_count"}, token=tok)
        count = profile.get("followers_count", 0)
    except Exception:
        return

    history = fetch_growth_history()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Update today's entry if it exists, else append
    for entry in history:
        if entry.get("date") == today:
            entry["followers"] = count
            break
    else:
        history.append({"date": today, "followers": count})

    # Keep last 90 days
    history = sorted(history, key=lambda x: x["date"])[-90:]
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (_CACHE_DIR / "growth_history.json").write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# AI Growth Recommendations
# ---------------------------------------------------------------------------

def generate_recommendations(overview: dict, posts: list[dict],
                              audience: dict) -> list[dict[str, Any]]:
    """Ask Claude for actionable Instagram growth tips based on current data."""
    cached = _cache_read("recommendations", ttl=21600)  # 6h TTL
    if cached:
        return cached.get("tips", [])

    try:
        import anthropic
        client = anthropic.Anthropic()
    except Exception:
        return _fallback_recommendations(overview)

    top_posts = posts[:5]
    post_summary = "\n".join(
        f"- {p['media_type']} | {p['like_count']} likes | {p['comments_count']} comments "
        f"| {p.get('saved', 0)} saves | {p.get('impressions', 0)} impressions "
        f"| ER: {p['engagement_rate']}% | caption: {p['caption'][:80]}"
        for p in top_posts
    )

    top_countries = ", ".join(
        f"{c['code']} ({c['count']})" for c in (audience.get("countries") or [])[:5]
    )

    prompt = f"""You are an Instagram growth expert for a finance/crypto education brand called EliteMarginDesk.
Analyze the following account data and give 6 specific, actionable growth recommendations.

Account: @{overview.get('username', 'EliteMarginDesk')}
Followers: {overview.get('followers', 0):,}
Engagement rate: {overview.get('engagement_rate', 0)}%
30-day impressions: {overview.get('impressions_30d', 0):,}
30-day reach: {overview.get('reach_30d', 0):,}
Profile views (30d): {overview.get('profile_views_30d', 0):,}
Top audience countries: {top_countries or 'N/A'}

Top 5 posts by impressions:
{post_summary or 'No post data available'}

Give exactly 6 recommendations. For each:
- title: short action title (max 6 words)
- description: 2-sentence specific advice
- priority: "high", "medium", or "low"
- category: one of "content", "posting_time", "engagement", "hashtags", "reels", "collaboration"

Respond ONLY with valid JSON array, no markdown:
[{{"title":"...", "description":"...", "priority":"...", "category":"..."}}]"""

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        tips = json.loads(raw)
        _cache_write("recommendations", {"tips": tips})
        return tips
    except Exception:
        return _fallback_recommendations(overview)


def _fallback_recommendations(overview: dict) -> list[dict]:
    eng = overview.get("engagement_rate", 0)
    tips = [
        {
            "title": "Postează Reels zilnic",
            "description": (
                "Algoritmul Instagram favorizează puternic Reels față de imagini statice. "
                "Postează minimum 1 Reel pe zi pentru a maximiza reach-ul organic."
            ),
            "priority": "high",
            "category": "reels",
        },
        {
            "title": "Folosește 3-5 hashtag-uri relevante",
            "description": (
                "Hashtag-urile de nișă (ex: #trading, #cryptoeducation) aduc audiențe calificate. "
                "Evită hashtag-urile prea generice cu milioane de postări."
            ),
            "priority": "high",
            "category": "hashtags",
        },
        {
            "title": "Răspunde la comentarii în primele 60 min",
            "description": (
                "Interacțiunile rapide semnalează algoritm că postarea e valoroasă. "
                "Setează-ți notificări și răspunde la fiecare comentariu în prima oră."
            ),
            "priority": "high" if eng < 3 else "medium",
            "category": "engagement",
        },
        {
            "title": "Postează între 18:00 - 21:00",
            "description": (
                "Audiența de trading/crypto e activă seara, după program. "
                "Testează ora 19:00 și 20:00 pentru engagement maxim."
            ),
            "priority": "medium",
            "category": "posting_time",
        },
        {
            "title": "Carusele educaționale săptămânal",
            "description": (
                "Caruselele sunt salvate mai des decât orice alt format — algoritmul le promovează. "
                "Creează tutoriale vizuale cu 5-8 slide-uri despre concepte de trading."
            ),
            "priority": "medium",
            "category": "content",
        },
        {
            "title": "Colaborează cu 2-3 conturi similare",
            "description": (
                "Colabs și sticker-uri de menționare pe Stories cu conturi din nișa finance "
                "aduc urmăritori noi calificați fără costuri publicitare."
            ),
            "priority": "low",
            "category": "collaboration",
        },
    ]
    return tips


# ---------------------------------------------------------------------------
# Content Kit scanner
# ---------------------------------------------------------------------------

def list_content_kit_items() -> list[dict[str, Any]]:
    """Scan the EliteMarginDesk content kit folder for ready-to-post assets."""
    kit_dir = _ROOT / "data" / "content_kit"
    if not kit_dir.exists():
        # Try the upload extraction path
        kit_dir = _ROOT / "EliteMarginDesk_Kit"

    if not kit_dir.exists():
        return []

    items: list[dict] = []

    for f in sorted(kit_dir.rglob("*")):
        if f.is_dir():
            continue
        ext = f.suffix.lower()
        if ext in (".mp4", ".mov"):
            media_type = "REEL"
        elif ext in (".jpg", ".jpeg", ".png"):
            # Check if it's in a carousel folder
            if "carousel" in str(f).lower():
                media_type = "CAROUSEL_ALBUM"
            else:
                media_type = "IMAGE"
        else:
            continue

        rel = str(f.relative_to(_ROOT))
        items.append({
            "name":       f.stem.replace("_", " ").replace("-", " "),
            "file":       f.name,
            "path":       rel,
            "media_type": media_type,
            "size_kb":    round(f.stat().st_size / 1024),
        })

    # Group carousels
    carousel_slides: dict[str, list] = {}
    normal_items = []
    for item in items:
        if item["media_type"] == "CAROUSEL_ALBUM":
            folder = str(Path(item["path"]).parent)
            carousel_slides.setdefault(folder, []).append(item)
        else:
            normal_items.append(item)

    result = normal_items[:]
    for folder, slides in carousel_slides.items():
        result.append({
            "name":       Path(folder).name.replace("_", " ").title(),
            "file":       f"{len(slides)} slides",
            "path":       folder,
            "media_type": "CAROUSEL_ALBUM",
            "size_kb":    sum(s["size_kb"] for s in slides),
            "slides":     len(slides),
        })

    return result


# ---------------------------------------------------------------------------
# Full refresh
# ---------------------------------------------------------------------------

def refresh_all(force: bool = True) -> dict[str, Any]:
    """Refresh all analytics data from Meta Graph API."""
    errors: list[str] = []

    overview = {}
    try:
        overview = fetch_account_overview(force=force)
    except Exception as e:
        errors.append(f"overview: {e}")

    posts: list = []
    try:
        posts = fetch_recent_posts(force=force)
    except Exception as e:
        errors.append(f"posts: {e}")

    audience: dict = {}
    try:
        audience = fetch_audience_demographics(force=force)
    except Exception as e:
        errors.append(f"audience: {e}")

    try:
        record_daily_snapshot()
    except Exception as e:
        errors.append(f"snapshot: {e}")

    return {
        "ok":      len(errors) == 0,
        "errors":  errors,
        "overview": overview,
        "posts":   posts,
        "audience": audience,
    }
