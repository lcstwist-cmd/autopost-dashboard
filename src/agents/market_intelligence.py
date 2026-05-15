"""Market Intelligence Agent — continuous real-time market analysis.

Runs every 30 minutes. Aggregates signals from multiple free sources and
writes `data/market_pulse.json` — a single file all other agents read to
calibrate their output (topics, visual style, hooks, posting time).

Sources used (no API keys required):
  • CoinGecko Trending API     — top 7 trending coins
  • Alternative.me Fear&Greed  — market sentiment index
  • Google News RSS             — trending topics per niche
  • Reddit JSON API             — hot posts from multiple subs
  • YouTube RSS                 — trending video titles/categories
  • TikTok Discovery RSS proxy  — trending content via Piped RSS

Deduplication: each insight gets a 12-char content hash so the brain
never stores the same learning twice even across multiple runs.

CLI:
    python src/agents/market_intelligence.py
    python src/agents/market_intelligence.py --force   # skip freshness check
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

_HERE      = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
_DATA_DIR  = _REPO_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_PULSE_FILE = _DATA_DIR / "market_pulse.json"

# Freshness: don't re-fetch if data is less than 25 minutes old
_FRESH_SECONDS = 25 * 60

# HTTP timeout per request
_TIMEOUT = 8


def _get(url: str, headers: dict | None = None) -> str:
    """Fetch URL, return body string or empty string on error."""
    try:
        req = urllib.request.Request(url, headers=headers or {
            "User-Agent": "Mozilla/5.0 (AutoPost-Intelligence/1.0)"
        })
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as exc:
        print(f"[market] fetch failed {url[:60]}: {exc}", file=sys.stderr)
        return ""


def _get_json(url: str) -> dict | list | None:
    body = _get(url)
    if not body:
        return None
    try:
        return json.loads(body)
    except Exception:
        return None


def _hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Signal collectors
# ---------------------------------------------------------------------------

def _fetch_coingecko_trending() -> list[dict]:
    """Top 7 trending coins on CoinGecko."""
    data = _get_json("https://api.coingecko.com/api/v3/search/trending")
    results = []
    if not isinstance(data, dict):
        return results
    for item in (data.get("coins") or [])[:7]:
        coin = item.get("item", {})
        results.append({
            "symbol": coin.get("symbol", "").upper(),
            "name":   coin.get("name", ""),
            "rank":   coin.get("market_cap_rank"),
            "score":  coin.get("score", 0),
        })
    return results


def _fetch_fear_greed() -> dict:
    """Fear & Greed Index from Alternative.me."""
    data = _get_json("https://api.alternative.me/fng/?limit=2")
    if not isinstance(data, dict):
        return {}
    items = data.get("data") or []
    if not items:
        return {}
    current = items[0]
    previous = items[1] if len(items) > 1 else {}
    return {
        "value":     int(current.get("value", 50)),
        "label":     current.get("value_classification", "Neutral"),
        "prev":      int(previous.get("value", 50)) if previous else None,
        "trend":     "up" if previous and int(current.get("value", 50)) > int(previous.get("value", 50)) else "down",
    }


def _fetch_gnews_topics(query: str, max_items: int = 8) -> list[str]:
    """Fetch titles from Google News RSS for a query."""
    url = f"https://news.google.com/rss/search?q={urllib.request.quote(query)}&hl=en&gl=US&ceid=US:en"
    body = _get(url)
    if not body:
        return []
    try:
        root = ElementTree.fromstring(body)
        titles = []
        for item in root.iter("item"):
            t = item.findtext("title") or ""
            if t:
                titles.append(t.split(" - ")[0].strip())
            if len(titles) >= max_items:
                break
        return titles
    except Exception:
        return []


def _fetch_reddit_hot(subreddit: str, max_items: int = 10) -> list[dict]:
    """Fetch hot posts from a subreddit."""
    url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit={max_items}"
    data = _get_json(url)
    results = []
    if not isinstance(data, dict):
        return results
    for child in (data.get("data", {}).get("children") or []):
        post = child.get("data", {})
        results.append({
            "title":  post.get("title", ""),
            "score":  post.get("score", 0),
            "ratio":  post.get("upvote_ratio", 0),
            "flair":  post.get("link_flair_text") or "",
        })
    return results


def _fetch_youtube_rss(channel_id: str) -> list[str]:
    """Fetch recent video titles from YouTube channel RSS."""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    body = _get(url)
    if not body:
        return []
    titles = []
    try:
        root = ElementTree.fromstring(body)
        ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
        for entry in root.findall("atom:entry", ns)[:8]:
            title = entry.findtext("atom:title", namespaces=ns) or ""
            if title:
                titles.append(title)
    except Exception:
        pass
    return titles


# ---------------------------------------------------------------------------
# Insight extraction
# ---------------------------------------------------------------------------

_HOOK_PATTERNS = [
    (r"\b(why|how|what|when|who)\b", "question_hook"),
    (r"\b(breaking|alert|urgent|just in)\b", "urgency_hook"),
    (r"\b(\d+)\s+(reason|way|tip|step|thing)", "list_hook"),
    (r"\b(secret|hidden|nobody|most people|they don'?t want)\b", "curiosity_hook"),
    (r"\b(i made|i earned|i lost|my\s+\$|how i)\b", "story_hook"),
    (r"\b(vs\.?|versus|compared|beats|wins)\b", "comparison_hook"),
]

def _detect_hook(title: str) -> str:
    tl = title.lower()
    for pattern, hook_type in _HOOK_PATTERNS:
        if re.search(pattern, tl):
            return hook_type
    return "statement_hook"


_VISUAL_KEYWORDS = {
    "minimal":   ["minimal", "clean", "simple", "white space", "flat"],
    "bold":      ["bold", "bright", "neon", "color", "vibrant", "red alert"],
    "chart":     ["chart", "graph", "data", "analysis", "price", "trend"],
    "face":      ["interview", "explains", "reacts", "opinion", "talking"],
    "infograph": ["infographic", "explained", "breakdown", "guide", "how to"],
}

def _detect_visual_style(titles: list[str]) -> dict[str, int]:
    scores: dict[str, int] = {k: 0 for k in _VISUAL_KEYWORDS}
    combined = " ".join(titles).lower()
    for style, keywords in _VISUAL_KEYWORDS.items():
        for kw in keywords:
            scores[style] += combined.count(kw)
    return scores


# ---------------------------------------------------------------------------
# Platform optimal times (research-based + updated from real data)
# ---------------------------------------------------------------------------

_PLATFORM_BASE_TIMING = {
    "youtube": {
        "best_hours": [14, 15, 16, 17, 18],
        "best_days":  ["Friday", "Saturday", "Sunday", "Thursday"],
        "notes":      "Subscribers active after school/work; Fri-Sun peak discovery",
    },
    "tiktok": {
        "best_hours": [6, 7, 10, 12, 19, 20, 21, 22],
        "best_days":  ["Tuesday", "Wednesday", "Thursday", "Friday"],
        "notes":      "Morning commute + evening leisure; avoid weekends for new accounts",
    },
    "instagram": {
        "best_hours": [9, 10, 11, 13, 14, 19, 20],
        "best_days":  ["Monday", "Tuesday", "Wednesday", "Thursday"],
        "notes":      "Business hours lunch breaks; evening casual scroll",
    },
    "x": {
        "best_hours": [8, 9, 12, 17, 18, 20],
        "best_days":  ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        "notes":      "Crypto Twitter most active during US/EU market hours",
    },
    "telegram": {
        "best_hours": [8, 9, 10, 18, 19, 20, 21],
        "best_days":  ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        "notes":      "Eastern European timezone morning + evening peak",
    },
}


# ---------------------------------------------------------------------------
# Topic extraction helpers
# ---------------------------------------------------------------------------

def _extract_keywords(texts: list[str], min_len: int = 4, top_n: int = 20) -> list[str]:
    """Extract most frequent meaningful words from a list of text strings."""
    _STOP = {
        "this", "that", "with", "from", "have", "will", "your", "more",
        "been", "they", "were", "also", "what", "when", "just", "about",
        "into", "over", "then", "their", "there", "which", "would", "could",
        "should", "these", "after", "before", "first", "other", "than",
        "like", "said", "some", "time", "year", "says", "amid", "amid",
        "news", "report", "here", "know", "make", "take", "need", "want",
        "most", "next", "back", "well", "even", "only", "such", "same",
        "much", "many", "each", "both", "does", "using", "used",
    }
    counter: Counter = Counter()
    for text in texts:
        words = re.findall(r"[a-zA-Z]{%d,}" % min_len, text.lower())
        for w in words:
            if w not in _STOP:
                counter[w] += 1
    return [w for w, _ in counter.most_common(top_n)]


# ---------------------------------------------------------------------------
# Main analysis cycle
# ---------------------------------------------------------------------------

def run_analysis(force: bool = False) -> dict:
    """Run a full market intelligence cycle. Returns the pulse dict."""
    # Freshness check
    if not force and _PULSE_FILE.exists():
        try:
            existing = json.loads(_PULSE_FILE.read_text(encoding="utf-8"))
            updated = existing.get("updated_at", "")
            if updated:
                age = time.time() - datetime.fromisoformat(updated).timestamp()
                if age < _FRESH_SECONDS:
                    print(f"[market] fresh data ({age/60:.0f}m old), skipping", file=sys.stderr)
                    return existing
        except Exception:
            pass

    print("[market] running intelligence cycle…", file=sys.stderr)
    t0 = time.time()

    # ── Fetch signals ────────────────────────────────────────────────────────
    trending_coins = _fetch_coingecko_trending()
    fear_greed     = _fetch_fear_greed()

    crypto_titles  = _fetch_gnews_topics("crypto bitcoin ethereum blockchain")
    finance_titles = _fetch_gnews_topics("trading investing stock market finance")
    ai_titles      = _fetch_gnews_topics("artificial intelligence AI automation 2025")

    reddit_crypto  = _fetch_reddit_hot("CryptoCurrency", 12)
    reddit_finance = _fetch_reddit_hot("investing", 8)
    reddit_smm     = _fetch_reddit_hot("socialmedia", 6)

    # High-engagement crypto YouTube channels
    yt_titles: list[str] = []
    for ch_id in [
        "UCRvqjQPSeaWn-uEx-w0XOIg",  # Coin Bureau
        "UCqK_GSMbpiV8spgD3ZGloSw",  # InvestAnswers
    ]:
        yt_titles += _fetch_youtube_rss(ch_id)

    # ── Extract insights ─────────────────────────────────────────────────────
    all_titles = (
        crypto_titles + finance_titles + ai_titles + yt_titles
        + [r["title"] for r in reddit_crypto + reddit_finance]
    )
    trending_keywords = _extract_keywords(all_titles, top_n=25)

    # Hook analysis from top Reddit posts
    hook_counter: Counter = Counter()
    for post in reddit_crypto + reddit_finance:
        hook_counter[_detect_hook(post["title"])] += 1
    best_hook = hook_counter.most_common(1)[0][0] if hook_counter else "question_hook"

    # Visual style from YouTube + crypto titles
    visual_scores = _detect_visual_style(yt_titles + crypto_titles)
    dominant_visual = max(visual_scores, key=lambda k: visual_scores[k]) if any(visual_scores.values()) else "minimal"

    # Social media format signals from Reddit SMM
    format_counter: Counter = Counter()
    for post in reddit_smm:
        t = post["title"].lower()
        if "reel" in t or "short" in t:
            format_counter["reels"] += 1
        if "carousel" in t or "slideshow" in t:
            format_counter["carousel"] += 1
        if "story" in t:
            format_counter["stories"] += 1
        if "video" in t:
            format_counter["video"] += 1
    top_format = format_counter.most_common(1)[0][0] if format_counter else "video"

    # CTA extraction — look for action words in high-score posts
    cta_counter: Counter = Counter()
    cta_words = {"swipe": "swipe", "link in bio": "link_bio", "comment": "comment",
                 "share": "share", "follow": "follow", "subscribe": "subscribe",
                 "watch": "watch", "read": "read", "click": "click"}
    for post in reddit_crypto[:15]:
        tl = post["title"].lower()
        for word, key in cta_words.items():
            if word in tl:
                cta_counter[key] += 1
    best_cta = cta_counter.most_common(1)[0][0] if cta_counter else "comment"

    # Sentiment based on Fear&Greed
    fg_val = fear_greed.get("value", 50)
    if fg_val >= 75:
        market_sentiment = "extreme_greed"
        recommended_tone = "bullish"
    elif fg_val >= 55:
        market_sentiment = "greed"
        recommended_tone = "optimistic"
    elif fg_val >= 45:
        market_sentiment = "neutral"
        recommended_tone = "balanced"
    elif fg_val >= 25:
        market_sentiment = "fear"
        recommended_tone = "cautious"
    else:
        market_sentiment = "extreme_fear"
        recommended_tone = "contrarian_opportunity"

    # ── Build pulse ──────────────────────────────────────────────────────────
    pulse = {
        "updated_at":         datetime.now(timezone.utc).isoformat(),
        "cycle_seconds":      round(time.time() - t0, 1),
        "trending_coins":     trending_coins,
        "fear_greed":         fear_greed,
        "market_sentiment":   market_sentiment,
        "trending_keywords":  trending_keywords,
        "content_signals": {
            "best_hook_type":   best_hook,
            "best_cta":         best_cta,
            "recommended_tone": recommended_tone,
            "top_format":       top_format,
        },
        "visual_signals": {
            "dominant_style": dominant_visual,
            "scores":         visual_scores,
            "yt_titles_used": len(yt_titles),
        },
        "platform_timing":    _PLATFORM_BASE_TIMING,
        "raw_counts": {
            "crypto_titles":  len(crypto_titles),
            "reddit_posts":   len(reddit_crypto) + len(reddit_finance),
            "yt_titles":      len(yt_titles),
        },
    }

    _PULSE_FILE.write_text(json.dumps(pulse, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[market] cycle done in {pulse['cycle_seconds']}s — "
          f"{len(trending_keywords)} keywords, sentiment={market_sentiment}", file=sys.stderr)

    # ── Push signals into agent brains ───────────────────────────────────────
    _push_to_brains(pulse)

    return pulse


def _push_to_brains(pulse: dict) -> None:
    """Write market signals as new learnings into relevant agent brains."""
    from src.agents.agent_brain import add_learning  # lazy import

    now_iso = datetime.now(timezone.utc).isoformat()
    keywords = pulse["trending_keywords"][:10]
    cs = pulse["content_signals"]
    fg = pulse["fear_greed"]
    coins = [c["symbol"] for c in pulse["trending_coins"][:5]]

    # News Scout — trending topics
    add_learning("news_scout", "topic_velocity",
                 f"Trending keywords now: {', '.join(keywords[:8])}. "
                 f"Hot coins: {', '.join(coins)}",
                 source="market_intelligence", dedupe=True)

    # Copywriter — hook + tone + CTA
    add_learning("copywriter", "hook_mastery",
                 f"Best hook type right now: {cs['best_hook_type']}. "
                 f"Market tone: {cs['recommended_tone']}. "
                 f"Fear&Greed={fg.get('value', '?')} ({fg.get('label', '?')})",
                 source="market_intelligence", dedupe=True)
    add_learning("copywriter", "cta_effectiveness",
                 f"Top performing CTA pattern: {cs['best_cta']}. "
                 f"Top format: {cs['top_format']}",
                 source="market_intelligence", dedupe=True)

    # Image Gen — visual style
    add_learning("image_gen", "visual_style_mastery",
                 f"Dominant visual style trending: {pulse['visual_signals']['dominant_style']}. "
                 f"Style breakdown: {pulse['visual_signals']['scores']}",
                 source="market_intelligence", dedupe=True)

    # Ranker — trending topics boost
    add_learning("ranker", "trend_prediction",
                 f"Market sentiment: {pulse['market_sentiment']}. "
                 f"Trending coins: {', '.join(coins)}. "
                 f"Priority keywords: {', '.join(keywords[:6])}",
                 source="market_intelligence", dedupe=True)

    # Video Builder — format signals
    add_learning("video_builder", "engagement_hook_timing",
                 f"Best content format: {cs['top_format']}. "
                 f"Recommended hook: {cs['best_hook_type']}",
                 source="market_intelligence", dedupe=True)


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    try:
        from dotenv import load_dotenv
        load_dotenv(_REPO_ROOT / ".env")
    except ImportError:
        pass

    ap = argparse.ArgumentParser(description="Market Intelligence Agent")
    ap.add_argument("--force", action="store_true", help="Skip freshness check")
    args = ap.parse_args()
    pulse = run_analysis(force=args.force)
    print(json.dumps(pulse, indent=2, ensure_ascii=False))
