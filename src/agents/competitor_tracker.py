"""competitor_tracker.py — Agent 37: Competitor Monitoring.

Scans competitor accounts on X, Instagram, TikTok, YouTube and RSS feeds.
Collects recent posts + engagement metrics, computes insights, updates
the agent brain so the pipeline can adapt content strategy.

Supported platforms:
  x         — X/Twitter handles, fetched via RapidAPI twitter-api45
  instagram — Instagram handles, fetched via RapidAPI instagram-scraper-20251
  tiktok    — TikTok handles, fetched via RapidAPI tiktok-scraper7
  youtube   — YouTube channels (RSS via youtube.com/feeds/videos.xml)
  website   — Any site with an RSS/Atom feed (auto-detected or explicit URL)

Usage:
    python competitor_tracker.py               # scan all users, all competitors
    python competitor_tracker.py --user 1      # scan single user
    python competitor_tracker.py --force       # ignore last-scan cooldown
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_REPO / ".env")
except ImportError:
    pass

_SCAN_COOLDOWN_HOURS = 6   # don't re-scan same account within this window
_POSTS_PER_SCAN     = 15  # posts to fetch per account per scan

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(url: str, headers: dict | None = None, timeout: int = 12) -> bytes | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": "AutoPost/1.0 (+https://elitemargindesk.io)",
        **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def _get_json(url: str, headers: dict | None = None) -> dict | None:
    raw = _get(url, headers=headers)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# X / Twitter — via RapidAPI twitter-api45
# ---------------------------------------------------------------------------

def _fetch_x_account_posts(handle: str, **_kwargs) -> list[dict]:
    """Fetch recent tweets via RapidAPI twitter-api45 /timeline.php."""
    key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if not key:
        return []
    screen = handle.lstrip("@")
    url = f"https://twitter-api45.p.rapidapi.com/timeline.php?screenname={urllib.parse.quote(screen)}"
    headers = {
        "x-rapidapi-key":  key,
        "x-rapidapi-host": "twitter-api45.p.rapidapi.com",
    }
    data = _get_json(url, headers=headers)
    if not data:
        return []
    tweets = data.get("timeline") or data.get("data") or []
    posts: list[dict] = []
    for t in tweets[:_POSTS_PER_SCAN]:
        tid     = t.get("tweet_id") or t.get("id_str") or t.get("id", "")
        text    = t.get("text") or t.get("full_text") or ""
        created = t.get("created_at") or t.get("date") or ""
        likes   = int(t.get("favorites") or t.get("favorite_count") or t.get("likes") or 0)
        rts     = int(t.get("retweets")  or t.get("retweet_count") or 0)
        views   = int(t.get("views") or t.get("view_count") or 0)
        posts.append({
            "post_id":   str(tid),
            "content":   text,
            "url":       f"https://x.com/{screen}/status/{tid}" if tid else "",
            "likes":     likes,
            "shares":    rts,
            "views":     views,
            "posted_at": created,
        })
    return posts


# ---------------------------------------------------------------------------
# Instagram — via RapidAPI instagram-scraper-20251
# ---------------------------------------------------------------------------

def _fetch_instagram_account_posts(handle: str) -> list[dict]:
    """Fetch recent posts via RapidAPI instagram-scraper-20251."""
    key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if not key:
        return []
    username = handle.lstrip("@")
    url = (f"https://instagram-scraper-20251.p.rapidapi.com/userposts/"
           f"?username_or_id={urllib.parse.quote(username)}")
    headers = {
        "x-rapidapi-key":  key,
        "x-rapidapi-host": "instagram-scraper-20251.p.rapidapi.com",
    }
    data = _get_json(url, headers=headers)
    if not data:
        return []
    items = data.get("data") or data.get("items") or data.get("posts") or []
    posts: list[dict] = []
    for item in items[:_POSTS_PER_SCAN]:
        caption_node = item.get("caption") or {}
        caption = (caption_node.get("text") if isinstance(caption_node, dict)
                   else str(caption_node or ""))
        code = item.get("code") or item.get("shortcode") or ""
        media_url = f"https://www.instagram.com/reel/{code}/" if code else ""
        likes  = int(item.get("like_count") or item.get("likes") or 0)
        views  = int(item.get("view_count") or item.get("video_view_count") or 0)
        taken  = item.get("taken_at") or item.get("taken_at_timestamp") or ""
        if taken and str(taken).isdigit():
            try:
                taken = datetime.fromtimestamp(int(taken), tz=timezone.utc).isoformat()
            except Exception:
                taken = str(taken)
        posts.append({
            "post_id":   str(item.get("id") or code),
            "content":   caption[:500],
            "url":       media_url,
            "likes":     likes,
            "shares":    0,
            "views":     views,
            "posted_at": taken,
        })
    return posts


# ---------------------------------------------------------------------------
# TikTok — via RapidAPI tiktok-scraper7
# ---------------------------------------------------------------------------

def _fetch_tiktok_account_posts(handle: str) -> list[dict]:
    """Fetch recent videos via RapidAPI tiktok-scraper7."""
    key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if not key:
        return []
    unique_id = handle.lstrip("@")
    url = (f"https://tiktok-scraper7.p.rapidapi.com/user/posts"
           f"?unique_id={urllib.parse.quote(unique_id)}&count={_POSTS_PER_SCAN}")
    headers = {
        "x-rapidapi-key":  key,
        "x-rapidapi-host": "tiktok-scraper7.p.rapidapi.com",
    }
    data = _get_json(url, headers=headers)
    if not data or data.get("code") != 0:
        return []
    items = (data.get("data") or {}).get("videos") or []
    posts: list[dict] = []
    for item in items[:_POSTS_PER_SCAN]:
        desc    = item.get("title") or item.get("desc") or ""
        vid_id  = item.get("video_id") or item.get("aweme_id") or ""
        likes   = int(item.get("digg_count") or item.get("likes") or 0)
        views   = int(item.get("play_count") or item.get("views") or 0)
        shares  = int(item.get("share_count") or item.get("shares") or 0)
        ctime   = item.get("create_time") or item.get("createTime") or ""
        if ctime and str(ctime).isdigit():
            try:
                ctime = datetime.fromtimestamp(int(ctime), tz=timezone.utc).isoformat()
            except Exception:
                ctime = str(ctime)
        posts.append({
            "post_id":   str(vid_id),
            "content":   desc[:500],
            "url":       f"https://www.tiktok.com/@{unique_id}/video/{vid_id}" if vid_id else "",
            "likes":     likes,
            "shares":    shares,
            "views":     views,
            "posted_at": ctime,
        })
    return posts


# ---------------------------------------------------------------------------
# RSS / Atom feed parser
# ---------------------------------------------------------------------------

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
}

def _parse_rss(raw: bytes) -> list[dict]:
    """Parse RSS or Atom feed bytes → list of post dicts."""
    posts = []
    try:
        root = ET.fromstring(raw.decode("utf-8", errors="replace"))
    except Exception:
        return []

    # Atom format
    tag = root.tag.lower()
    if "feed" in tag:
        for entry in root.findall("atom:entry", _NS) or root.findall("{http://www.w3.org/2005/Atom}entry"):
            title_el = entry.find("{http://www.w3.org/2005/Atom}title") or entry.find("title")
            link_el  = entry.find("{http://www.w3.org/2005/Atom}link") or entry.find("link")
            pub_el   = (entry.find("{http://www.w3.org/2005/Atom}published")
                        or entry.find("{http://www.w3.org/2005/Atom}updated")
                        or entry.find("published") or entry.find("updated"))
            url = (link_el.get("href", "") if link_el is not None else "")
            posts.append({
                "post_id":   url or (title_el.text or "")[:60] if title_el is not None else "",
                "content":   title_el.text if title_el is not None else "",
                "url":       url,
                "likes":     0, "shares": 0, "views": 0,
                "posted_at": pub_el.text if pub_el is not None else "",
            })
        return posts[:_POSTS_PER_SCAN]

    # RSS 2.0 format
    channel = root.find("channel")
    items = (channel.findall("item") if channel is not None else root.findall(".//item"))
    for item in items:
        title   = item.findtext("title", "")
        link    = item.findtext("link", "")
        pub     = item.findtext("pubDate", "") or item.findtext("dc:date", "") or ""
        posts.append({
            "post_id":   link or title[:60],
            "content":   title,
            "url":       link,
            "likes":     0, "shares": 0, "views": 0,
            "posted_at": pub,
        })
    return posts[:_POSTS_PER_SCAN]


def _detect_rss(website_url: str) -> str | None:
    """Try common RSS paths to find a feed URL."""
    candidates = [
        website_url.rstrip("/") + "/feed",
        website_url.rstrip("/") + "/rss",
        website_url.rstrip("/") + "/feed.xml",
        website_url.rstrip("/") + "/atom.xml",
        website_url.rstrip("/") + "/rss.xml",
        website_url.rstrip("/") + "/index.xml",
    ]
    # Also try to detect from <link rel="alternate" type="application/rss+xml">
    html = _get(website_url)
    if html:
        m = re.search(
            rb'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*href=["\']([^"\']+)["\']',
            html, re.I,
        )
        if m:
            href = m.group(1).decode("utf-8", errors="replace")
            if href.startswith("http"):
                return href
            return urllib.parse.urljoin(website_url, href)
    for url in candidates:
        raw = _get(url)
        if raw and (b"<rss" in raw[:500] or b"<feed" in raw[:500] or b"<channel" in raw[:500]):
            return url
    return None


def _fetch_rss_posts(rss_url: str) -> list[dict]:
    raw = _get(rss_url)
    if not raw:
        return []
    return _parse_rss(raw)


# ---------------------------------------------------------------------------
# YouTube — Data API v3 (primary) + RSS fallback
# ---------------------------------------------------------------------------

def _yt_resolve_channel_id(handle: str, api_key: str) -> str | None:
    """Resolve @handle or username to a YouTube channel ID via Data API v3."""
    clean = handle.lstrip("@")
    # Direct channel ID
    if clean.startswith("UC"):
        return clean
    # Try forHandle (new @handle system)
    url = (f"https://www.googleapis.com/youtube/v3/channels"
           f"?part=id&forHandle={urllib.parse.quote('@' + clean)}&key={api_key}")
    data = _get_json(url)
    if data:
        items = data.get("items") or []
        if items:
            return items[0]["id"]
    # Fallback: forUsername (legacy)
    url2 = (f"https://www.googleapis.com/youtube/v3/channels"
            f"?part=id&forUsername={urllib.parse.quote(clean)}&key={api_key}")
    data2 = _get_json(url2)
    if data2:
        items2 = data2.get("items") or []
        if items2:
            return items2[0]["id"]
    return None


def _yt_fetch_via_api(channel_id: str, api_key: str, max_results: int = 15) -> list[dict]:
    """Fetch recent videos for a channel using YouTube Data API v3."""
    search_url = (
        f"https://www.googleapis.com/youtube/v3/search"
        f"?part=snippet&channelId={channel_id}"
        f"&maxResults={max_results}&order=date&type=video&key={api_key}"
    )
    data = _get_json(search_url)
    if not data:
        return []
    items = data.get("items") or []
    if not items:
        return []

    # Batch stats fetch
    video_ids = ",".join(
        it["id"]["videoId"] for it in items if (it.get("id") or {}).get("videoId")
    )
    stats: dict[str, dict] = {}
    if video_ids:
        stats_url = (
            f"https://www.googleapis.com/youtube/v3/videos"
            f"?part=statistics,contentDetails&id={video_ids}&key={api_key}"
        )
        stats_data = _get_json(stats_url)
        for v in (stats_data or {}).get("items") or []:
            stats[v["id"]] = v.get("statistics") or {}

    posts: list[dict] = []
    for it in items:
        vid_id = (it.get("id") or {}).get("videoId", "")
        if not vid_id:
            continue
        snip = it.get("snippet") or {}
        st   = stats.get(vid_id, {})
        posts.append({
            "post_id":   vid_id,
            "content":   snip.get("title", ""),
            "url":       f"https://www.youtube.com/watch?v={vid_id}",
            "likes":     int(st.get("likeCount") or 0),
            "shares":    0,
            "views":     int(st.get("viewCount") or 0),
            "posted_at": snip.get("publishedAt", ""),
        })
    return posts


def _fetch_youtube_posts(handle: str) -> list[dict]:
    """Fetch recent videos — YouTube Data API v3 first, RSS fallback."""
    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if api_key:
        channel_id = _yt_resolve_channel_id(handle, api_key)
        if channel_id:
            posts = _yt_fetch_via_api(channel_id, api_key, max_results=_POSTS_PER_SCAN)
            if posts:
                return posts

    # RSS fallback
    clean = handle.lstrip("@")
    candidates = []
    if clean.startswith("UC"):
        candidates.append(f"https://www.youtube.com/feeds/videos.xml?channel_id={clean}")
    candidates.append(f"https://www.youtube.com/feeds/videos.xml?user={clean}")
    for url in candidates:
        raw = _get(url)
        if raw and b"<feed" in raw[:200]:
            return _parse_rss(raw)
    return []


# ---------------------------------------------------------------------------
# Insights computation
# ---------------------------------------------------------------------------

def _extract_hashtags(text: str) -> list[str]:
    return [t.lower() for t in re.findall(r"#\w+", text)]


def _compute_insights(posts: list[dict]) -> dict:
    if not posts:
        return {}
    hashtag_counter: Counter = Counter()
    word_counter: Counter = Counter()
    total_likes = total_shares = total_views = 0
    post_dates: list[datetime] = []
    stop = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "or",
            "is", "are", "was", "this", "that", "it", "at", "by", "from"}

    for p in posts:
        text = (p.get("content") or "")
        hashtag_counter.update(_extract_hashtags(text))
        words = [w.lower() for w in re.findall(r"\b[a-zA-Z]{4,}\b", text)
                 if w.lower() not in stop]
        word_counter.update(words)
        total_likes  += int(p.get("likes",  0))
        total_shares += int(p.get("shares", 0))
        total_views  += int(p.get("views",  0))
        raw_dt = p.get("posted_at", "")
        if raw_dt:
            try:
                from dateutil import parser as dp
                dt = dp.parse(raw_dt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                post_dates.append(dt.astimezone(timezone.utc))
            except Exception:
                pass

    n = len(posts)
    # Posting frequency: posts per week
    freq = 0.0
    if len(post_dates) >= 2:
        post_dates.sort()
        span_days = (post_dates[-1] - post_dates[0]).total_seconds() / 86400
        if span_days > 0:
            freq = round(n / (span_days / 7), 1)

    # Best day of week
    day_counter: Counter = Counter()
    for dt in post_dates:
        day_counter[dt.strftime("%A")] += 1
    best_day = day_counter.most_common(1)[0][0] if day_counter else ""

    return {
        "total_posts_analyzed": n,
        "avg_likes":   round(total_likes  / n, 1),
        "avg_shares":  round(total_shares / n, 1),
        "avg_views":   round(total_views  / n, 1),
        "posts_per_week": freq,
        "best_day":    best_day,
        "top_hashtags": [h for h, _ in hashtag_counter.most_common(10)],
        "top_words":    [w for w, _ in word_counter.most_common(15)],
    }


# ---------------------------------------------------------------------------
# Core scan logic
# ---------------------------------------------------------------------------

def scan_competitor(comp: dict) -> dict:
    """Fetch latest posts for one competitor dict (from DB). Returns summary."""
    platform = comp.get("platform", "x")
    handle   = comp.get("handle", "")
    rss_url  = comp.get("rss_url", "")
    comp_id  = comp["id"]

    posts: list[dict] = []
    profile: dict = {}
    error: str = ""

    if platform == "x":
        if not os.environ.get("RAPIDAPI_KEY", "").strip():
            error = "RAPIDAPI_KEY not configured"
        else:
            posts = _fetch_x_account_posts(handle)
            if not posts:
                error = f"No posts returned for @{handle} on X"

    elif platform == "instagram":
        if not os.environ.get("RAPIDAPI_KEY", "").strip():
            error = "RAPIDAPI_KEY not configured"
        else:
            posts = _fetch_instagram_account_posts(handle)
            if not posts:
                error = f"No posts returned for @{handle} on Instagram"

    elif platform == "tiktok":
        if not os.environ.get("RAPIDAPI_KEY", "").strip():
            error = "RAPIDAPI_KEY not configured"
        else:
            posts = _fetch_tiktok_account_posts(handle)
            if not posts:
                error = f"No posts returned for @{handle} on TikTok"

    elif platform == "youtube":
        posts = _fetch_youtube_posts(handle)
        if not posts:
            error = f"No posts returned for {handle} on YouTube"

    else:  # website / rss
        feed_url = rss_url or _detect_rss(handle if handle.startswith("http") else f"https://{handle}")
        if feed_url:
            posts = _fetch_rss_posts(feed_url)
            if feed_url != rss_url:
                try:
                    with __import__("sqlite3").connect(str(
                        __import__("src.dashboard.database", fromlist=["DB_PATH"]).DB_PATH
                    )) as _c:
                        _c.execute(
                            "UPDATE competitor_accounts SET rss_url=? WHERE id=?",
                            (feed_url, comp_id),
                        )
                except Exception:
                    pass
        else:
            error = f"Could not detect RSS feed for {handle}"

    if posts:
        try:
            from src.dashboard.database import save_competitor_posts
            save_competitor_posts(comp_id, posts)
        except Exception as e:
            error = f"DB save failed: {e}"

    insights = _compute_insights(posts)
    return {
        "comp_id": comp_id,
        "platform": platform,
        "handle": handle,
        "posts_fetched": len(posts),
        "insights": insights,
        "profile": profile,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Per-user scan
# ---------------------------------------------------------------------------

def scan_all_for_user(user_id: int, force: bool = False) -> list[dict]:
    """Scan all active competitors for one user. Returns list of results."""
    try:
        from src.dashboard.database import get_competitors
        comps = get_competitors(user_id)
    except Exception as e:
        return [{"error": str(e)}]

    results = []
    now = datetime.now(timezone.utc)
    for comp in comps:
        if not force and comp.get("last_scanned_at"):
            try:
                last = datetime.fromisoformat(comp["last_scanned_at"].replace("Z", "+00:00"))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                age_h = (now - last).total_seconds() / 3600
                if age_h < _SCAN_COOLDOWN_HOURS:
                    results.append({"comp_id": comp["id"], "skipped": True,
                                    "reason": f"scanned {age_h:.1f}h ago"})
                    continue
            except Exception:
                pass
        result = scan_competitor(comp)
        results.append(result)
        time.sleep(1)  # rate-limit courtesy pause
    return results


def run_all_users(force: bool = False) -> dict:
    """Scan all competitors for all active users. Called from scheduler."""
    try:
        from src.dashboard.database import get_all_users, get_all_competitors
        users = [u for u in get_all_users() if u.get("is_approved")]
        all_comps = get_all_competitors()
    except Exception as e:
        return {"error": str(e), "total_scanned": 0}

    total = 0
    errors = 0
    comps_by_user: dict[int, list] = {}
    for c in all_comps:
        comps_by_user.setdefault(c["user_id"], []).append(c)

    for user in users:
        uid = user["id"]
        comps = comps_by_user.get(uid, [])
        if not comps:
            continue
        try:
            results = scan_all_for_user(uid, force=force)
            scanned = [r for r in results if not r.get("skipped") and not r.get("error")]
            total  += len(scanned)
            errors += sum(1 for r in results if r.get("error"))
        except Exception:
            errors += 1

    _update_brain(total, errors)
    return {"total_scanned": total, "errors": errors}


# ---------------------------------------------------------------------------
# Agent brain update
# ---------------------------------------------------------------------------

def _update_brain(total_scanned: int, errors: int) -> None:
    try:
        from src.agents.agent_brain import AgentBrain
        brain = AgentBrain("competitor_tracker")
        brain._data["total_runs"] = brain._data.get("total_runs", 0) + 1
        brain._data["last_active"] = datetime.now(timezone.utc).isoformat()
        brain._data["status"] = "ok" if errors == 0 else "warn"

        if total_scanned > 0:
            brain.add_xp(total_scanned * 20, "data_collection")
            brain.record_learning(
                f"Scanned {total_scanned} competitor account(s) — {errors} error(s)",
                skill="data_collection", xp=total_scanned * 10,
            )
        brain.save()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Build insights report for a user (dashboard display)
# ---------------------------------------------------------------------------

def get_user_report(user_id: int) -> list[dict]:
    """Return full competitor data for dashboard rendering."""
    try:
        from src.dashboard.database import get_competitors, get_competitor_posts
        comps = get_competitors(user_id)
    except Exception:
        return []

    report = []
    for comp in comps:
        posts = []
        try:
            from src.dashboard.database import get_competitor_posts
            posts = get_competitor_posts(comp["id"], limit=10)
        except Exception:
            pass
        insights = _compute_insights(posts)
        report.append({
            "account": comp,
            "posts": posts,
            "insights": insights,
        })
    return report


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Competitor Tracker")
    ap.add_argument("--user",  type=int, help="Scan only this user ID")
    ap.add_argument("--force", action="store_true", help="Ignore cooldown")
    args = ap.parse_args(argv[1:])

    if args.user:
        results = scan_all_for_user(args.user, force=args.force)
        for r in results:
            print(json.dumps(r, indent=2, ensure_ascii=False))
    else:
        summary = run_all_users(force=args.force)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
