"""News Scout — Agent 1 of the Crypto AutoPost pipeline.

Fetches crypto news from 25+ sources in parallel, scores for engagement,
deduplicates by URL + title similarity, and returns stories sorted by importance.

Sources:
  RSS (21 feeds)  — CoinDesk, CoinTelegraph, Decrypt, The Block, Blockworks,
                    BeInCrypto, Bitcoin Magazine, CryptoSlate, NewsBTC, Bitcoinist,
                    AMBCrypto, CoinGape, U.Today, Crypto Briefing, CryptoPotato,
                    The Defiant, Protos, Bitcoin.com, CryptoNews, DailyHodl,
                    CoinMarketCal (events)
  APIs (free)     — CryptoPanic public RSS, Google News RSS,
                    Reddit r/CryptoCurrency + r/Bitcoin + r/ethereum (hot posts),
                    CoinGecko trending coins,
                    X/Twitter top 18 accounts via Nitter RSS
                    (@coindesk, @Cointelegraph, @WuBlockchain, @saylor,
                     @VitalikButerin, @whale_alert, @binance, @coinbase,
                     @glassnode, @lookonchain, @APompliano, @MessariCrypto +more)
  APIs (optional) — CryptoPanic developer API (CRYPTOPANIC_API_KEY)
                    LunarCrush (LUNARCRUSH_API_KEY)
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

import feedparser
import requests
from dateutil import parser as dateparser

# Module-level flags to avoid printing repeated auth errors per run
_X_API_WARN_LOGGED: set[int] = set()

# ---------------------------------------------------------------------------
# RSS sources — (name, url, tier)
# tier 1 = top-tier (most credible / high traffic)
# tier 2 = mid-tier
# tier 3 = smaller / aggregator
# ---------------------------------------------------------------------------

RSS_FEEDS: list[tuple[str, str, int]] = [
    # Tier 1 — flagship crypto media
    ("coindesk",         "https://www.coindesk.com/arc/outboundfeeds/rss/",           1),
    ("cointelegraph",    "https://cointelegraph.com/rss",                              1),
    ("the_block",        "https://www.theblock.co/rss.xml",                            1),
    ("decrypt",          "https://decrypt.co/feed",                                    1),
    ("blockworks",       "https://blockworks.co/feed/",                                1),
    # Tier 2 — strong crypto media
    ("bitcoin_magazine", "https://bitcoinmagazine.com/feed",                           2),
    ("cryptoslate",      "https://cryptoslate.com/feed/",                              2),
    ("beincrypto",       "https://beincrypto.com/feed/",                               2),
    ("the_defiant",      "https://thedefiant.io/api/feed",                             2),
    ("protos",           "https://protos.com/feed/",                                   2),
    ("bitcoincom",       "https://news.bitcoin.com/feed/",                             2),
    # Tier 3 — broader coverage / aggregators
    ("newsBTC",          "https://www.newsbtc.com/feed/",                              3),
    ("bitcoinist",       "https://bitcoinist.com/feed/",                               3),
    ("ambcrypto",        "https://ambcrypto.com/feed/",                                3),
    ("coingape",         "https://coingape.com/feed/",                                 3),
    ("utoday",           "https://u.today/rss",                                        3),
    ("crypto_briefing",  "https://cryptobriefing.com/feed/",                           3),
    ("cryptopotato",     "https://cryptopotato.com/feed/",                             3),
    ("cryptonews",       "https://cryptonews.com/news/feed/",                          3),
    ("dailyhodl",        "https://dailyhodl.com/feed/",                                3),
    # ── Macro / geo-economy (direct crypto market drivers) ───────────────────
    ("cnbc_economy",     "https://www.cnbc.com/id/20910258/device/rss/rss.html",       1),
    ("cnbc_finance",     "https://www.cnbc.com/id/10000664/device/rss/rss.html",       1),
    ("zerohedge",        "https://feeds.feedburner.com/zerohedge/feed",                2),
    ("federalreserve",   "https://www.federalreserve.gov/feeds/press_all.xml",         1),
    ("marketwatch",      "https://www.marketwatch.com/rss/topstories",                 2),
    ("investing_com",    "https://www.investing.com/rss/news_301.rss",                 2),
    ("fx_empire",        "https://www.fxempire.com/api/v1/en/articles/rss",            2),
    ("yahoo_finance",    "https://finance.yahoo.com/rss/",                             2),
]

# Importance bonus by source tier
SOURCE_TIER_BOOST = {1: 0.25, 2: 0.10, 3: 0.0}

CRYPTOPANIC_PUBLIC_URL    = "https://cryptopanic.com/news/rss/"
CRYPTOPANIC_API_ENDPOINT  = "https://cryptopanic.com/api/developer/v2/posts/"
CRYPTOPANIC_KEY_ENV       = "CRYPTOPANIC_API_KEY"

GOOGLE_NEWS_CRYPTO_URL = (
    "https://news.google.com/rss/search"
    "?q=cryptocurrency+bitcoin+ethereum+crypto+blockchain+defi+altcoin"
    "&hl=en-US&gl=US&ceid=US:en"
)

GOOGLE_NEWS_MACRO_URL = (
    "https://news.google.com/rss/search"
    "?q=bitcoin+crypto+%22federal+reserve%22+OR+%22interest+rate%22+OR+%22inflation%22+OR+"
    "%22sanctions%22+OR+%22trade+war%22+OR+%22geopolitical%22+OR+%22recession%22"
    "&hl=en-US&gl=US&ceid=US:en"
)

GOOGLE_NEWS_FINANCE_URL = (
    "https://news.google.com/rss/search"
    "?q=crypto+%22blackrock%22+OR+%22fidelity%22+OR+%22institutional%22+OR+"
    "%22etf%22+OR+%22wall+street%22+OR+%22hedge+fund%22+OR+%22nasdaq%22"
    "&hl=en-US&gl=US&ceid=US:en"
)

REDDIT_SUBREDDITS    = [
    "CryptoCurrency", "Bitcoin", "ethereum", "CryptoMarkets",
    "Economics", "investing", "wallstreetbets", "MacroEconomics",
    "Geopolitics", "economy",
]
REDDIT_HOT_URL       = "https://www.reddit.com/r/{sub}/hot.json?limit=30&t=day"
REDDIT_HEADERS       = {"User-Agent": "CryptoAutoPost/3.0 (news aggregator bot)"}

COINGECKO_TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"
COINGECKO_BOOST        = 0.20

LUNARCRUSH_ENDPOINT  = "https://lunarcrush.com/api4/public/coins/list/v1"
LUNARCRUSH_KEY_ENV   = "LUNARCRUSH_API_KEY"
LUNARCRUSH_BOOST     = 0.30

# ---------------------------------------------------------------------------
# X / Twitter — top crypto accounts
# ---------------------------------------------------------------------------
# Two modes (tried in order):
#   1. X API v2 Bearer token (official) — set X_BEARER_TOKEN in .env
#      Free developer account at developer.twitter.com (free, instant approval)
#      Free tier: 500k tweet reads/month — plenty for this use case
#   2. Nitter RSS fallback — works when a live instance is available
#      Set NITTER_INSTANCE env var to override the default list
#      (e.g. a self-hosted instance via Docker)

# (handle, tier)
# tier 1 = official outlets + market-moving voices (huge reach)
# tier 2 = top analysts / on-chain / builders
X_ACCOUNTS: list[tuple[str, int]] = [
    # Tier 1 — official media & market-movers
    ("coindesk",          1),
    ("Cointelegraph",     1),
    ("WuBlockchain",      1),   # breaking news, China + global
    ("BitcoinMagazine",   1),
    ("binance",           1),
    ("coinbase",          1),
    ("saylor",            1),   # Michael Saylor — BTC buys move the market
    ("VitalikButerin",    1),
    ("whale_alert",       1),   # large on-chain transfers
    # Tier 2 — analysts & high-signal voices
    ("APompliano",        2),
    ("glassnode",         2),   # on-chain analytics
    ("lookonchain",       2),   # whale tracking
    ("MessariCrypto",     2),
    ("DocumentingBTC",    2),
    ("BitcoinArchive",    2),
    ("CryptoHayes",       2),   # Arthur Hayes — BitMEX founder
    ("santimentfeed",     2),   # on-chain sentiment
    # Macro / traditional finance — high correlation with crypto moves
    ("RaoulGMI",          2),   # Raoul Pal — macro + digital assets
    ("LynAldenContact",   2),   # Lyn Alden — macro investing, BTC bull
    ("elerianm",          2),   # Mohamed El-Erian — Fed watcher / Bloomberg
    ("NickTimiraos",      1),   # WSJ Fed reporter — rate decisions move markets
    ("DiMartinoBooth",    2),   # Danielle DiMartino Booth — Fed insider
    ("GameofTrades_",     2),   # macro technical + macro signal
    ("coloradotravis",    2),   # macro-crypto crossover trader
    ("CryptoHayes",       1),   # Arthur Hayes — macro + BitMEX / macro thesis
]

X_BEARER_TOKEN_ENV    = "X_BEARER_TOKEN"     # app-only Bearer Token (recommended)
X_API_KEY_ENV         = "X_API_KEY"          # OAuth 1.0a consumer key
X_API_SECRET_ENV      = "X_API_SECRET"       # OAuth 1.0a consumer secret
X_ACCESS_TOKEN_ENV    = "X_ACCESS_TOKEN"     # OAuth 1.0a access token
X_ACCESS_SECRET_ENV   = "X_ACCESS_TOKEN_SECRET"
X_API_SEARCH_URL      = "https://api.twitter.com/2/tweets/search/recent"
X_API_CHUNK_SIZE      = 10   # handles per API query (combined with OR)

# Nitter public instances — tried in order (fallback when no bearer token)
# Override with NITTER_INSTANCE env var to use a self-hosted instance
NITTER_INSTANCES = [
    "nitter.it",
    "nitter.pussthecat.org",
    "nitter.privacydev.net",
    "nitter.poast.org",
    "nitter.kavin.rocks",
    "nitter.cz",
]

# ---------------------------------------------------------------------------
# X Influencer Substack / newsletter RSS feeds (always-free, no auth)
# These are published by the same KOLs and are highly reliable
# ---------------------------------------------------------------------------
X_KOL_RSS: list[tuple[str, str, int]] = [
    # (label, rss_url, tier)
    ("hayes_bitmex",    "https://cryptohayes.substack.com/feed",      1),
    ("wu_blockchain",   "https://wublock.substack.com/feed",          1),
    ("glassnode",       "https://insights.glassnode.com/rss/",        2),
    ("thedefiant_nl",   "https://newsletter.thedefiant.io/feed",      2),
]

USER_AGENT   = "CryptoAutoPost/3.0 (crypto news aggregator)"
HTTP_TIMEOUT = 8   # reduced from 12s to fail-fast on slow feeds
MAX_WORKERS  = 25  # increased from 10 for 30+ RSS feeds + API calls

# Global session with connection pooling — reuse connections across all requests
_HTTP_SESSION = None

def _get_session():
    """Get or create a reusable HTTP session with connection pooling."""
    global _HTTP_SESSION
    if _HTTP_SESSION is None:
        from requests.adapters import HTTPAdapter
        _HTTP_SESSION = requests.Session()
        adapter = HTTPAdapter(pool_connections=30, pool_maxsize=30, max_retries=1)
        _HTTP_SESSION.mount('https://', adapter)
        _HTTP_SESSION.mount('http://', adapter)
    return _HTTP_SESSION

KNOWN_TICKERS = {
    "BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "BNB", "TRX", "DOT",
    "MATIC", "LINK", "LTC", "BCH", "ATOM", "NEAR", "APT", "ARB", "OP", "SUI",
    "TON", "SHIB", "PEPE", "WIF", "USDT", "USDC", "DAI", "TAO", "FET", "RNDR",
    "INJ", "SEI", "TIA", "PYTH", "JUP", "WEN", "BONK", "FLOKI", "ICP", "FIL",
    "HBAR", "VET", "ETC", "XLM", "ALGO", "EGLD", "FLOW", "XTZ", "SAND", "MANA",
    "AXS", "CRO", "QNT", "GRT", "MKR", "AAVE", "SNX", "UNI", "COMP", "CRV",
    "STX", "IMX", "BLUR", "DYDX", "GMX", "LDO", "RPL", "FXS", "CVX", "BAL",
    "RUNE", "OSMO", "JUNO", "SCRT", "KAVA", "FTM", "ONE", "ROSE", "ZIL", "CHZ",
}

_CRYPTO_TERMS = {
    "bitcoin", "btc", "ethereum", "eth", "crypto", "blockchain", "defi",
    "nft", "altcoin", "stablecoin", "token", "coin", "wallet", "exchange",
    "binance", "coinbase", "solana", "ripple", "xrp", "dogecoin", "doge",
    "halving", "mining", "miner", "hash rate", "mempool", "smart contract",
    "web3", "metaverse", "dao", "yield", "liquidity", "airdrop",
    "satoshi", "hodl", "dyor", "fomo", "fud", "pump", "dump", "whale",
    "etf", "grayscale", "blackrock", "fidelity", "sec", "cftc",
    "layer 2", "layer2", "l2", "rollup", "zk", "polygon", "avalanche",
    "chainlink", "uniswap", "opensea", "metamask", "ledger", "trezor",
    "staking", "validator", "proof of stake", "proof of work",
    "cryptocurrency", "digital asset", "altseason", "bear market", "bull run",
    "on-chain", "onchain", "degens", "rug pull", "liquidation", "short squeeze",
}

# Macro / geopolitical terms with direct crypto market impact
_MACRO_GEO_TERMS = {
    # Federal Reserve & monetary policy
    "federal reserve", "fed", "fomc", "powell", "rate cut", "rate hike",
    "interest rate", "quantitative easing", "qe", "quantitative tightening", "qt",
    "tapering", "balance sheet reduction",
    # Inflation & economic data
    "inflation", "cpi", "pce", "deflation", "stagflation",
    "gdp", "jobs report", "nonfarm payroll", "payrolls", "unemployment",
    "pmi", "ism manufacturing", "consumer sentiment", "retail sales",
    # Macro finance
    "dollar index", "dxy", "treasury yield", "yield curve", "10-year yield",
    "2-year yield", "bonds", "debt ceiling", "deficit", "national debt",
    "recession", "soft landing", "hard landing", "financial crisis",
    # Geopolitical
    "sanctions", "trade war", "tariff", "tariffs", "geopolitical risk",
    "ukraine war", "russia sanctions", "china tensions", "taiwan strait",
    "middle east conflict", "opec", "oil price", "energy crisis",
    # Central banks (global)
    "ecb", "european central bank", "boe", "bank of england",
    "boj", "bank of japan", "pboc", "central bank digital",
    "lagarde", "ueda", "cbdc",
    # TradFi crossing into crypto
    "blackrock bitcoin", "fidelity crypto", "jpmorgan crypto",
    "goldman sachs crypto", "institutional adoption", "sovereign wealth fund",
    # Commodities
    "gold price", "silver price", "commodity", "oil futures",
    # Equities correlation
    "nasdaq crash", "s&p 500", "dow jones", "risk-off", "risk-on",
    "stock market crash", "market selloff",
    # Micro indicators
    "m2 money supply", "money printer", "liquidity injection", "credit crunch",
    "bank failure", "banking crisis", "svb", "credit suisse",
}

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _clean(raw: str | None) -> str:
    if not raw:
        return ""
    no_tags = re.sub(r"<[^>]+>", " ", raw)
    decoded = html.unescape(no_tags)
    return re.sub(r"\s+", " ", decoded).strip()


def _story_id(source: str, url: str) -> str:
    return hashlib.sha1(f"{source}|{url}".encode()).hexdigest()[:16]


def _extract_tickers(text: str) -> list[str]:
    tokens = re.findall(r"\b[A-Z]{2,6}\b", text)
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t in KNOWN_TICKERS and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = dateparser.parse(str(raw))
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _normalize_url(url: str) -> str:
    """Strip UTM/tracking params and trailing slashes for dedup."""
    try:
        p = urlparse(url)
        qs = {k: v for k, v in parse_qs(p.query).items()
              if not k.startswith(("utm_", "ref", "source", "medium", "campaign"))}
        clean = urlunparse(p._replace(query=urlencode(qs, doseq=True), fragment=""))
        return clean.rstrip("/")
    except Exception:
        return url.rstrip("/")


def _is_crypto_relevant(story: dict) -> bool:
    if story.get("tickers"):
        return True
    haystack = (story.get("title", "") + " " + (story.get("summary") or "")).lower()
    if any(term in haystack for term in _CRYPTO_TERMS):
        return True
    # Macro/geopolitical events with direct market impact on crypto
    if any(term in haystack for term in _MACRO_GEO_TERMS):
        return True
    return False


def _title_tokens(title: str) -> set[str]:
    STOP = {"the","a","an","of","to","in","on","for","and","or","is","as","at",
            "by","from","this","that","with","will","has","have","are","was",
            "says","said","new","over","up","its","after","just","now","amid"}
    return {w for w in re.findall(r"[a-z0-9]+", title.lower())
            if len(w) >= 3 and w not in STOP}


def _jaccard_titles(a: str, b: str) -> float:
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / len(ta | tb)


def _get(url: str, **kwargs) -> requests.Response | None:
    try:
        session = _get_session()
        r = session.get(url, headers={"User-Agent": USER_AGENT},
                        timeout=HTTP_TIMEOUT, **kwargs)
        r.raise_for_status()
        return r
    except Exception as exc:
        print(f"[news_scout] GET {url[:70]}… failed: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# RSS fetcher
# ---------------------------------------------------------------------------

def fetch_rss(source: str, url: str, tier: int = 3) -> list[dict]:
    try:
        feed = feedparser.parse(url, request_headers={"User-Agent": USER_AGENT})
    except Exception as exc:
        print(f"[news_scout] RSS {source} parse error: {exc}", file=sys.stderr)
        return []

    tier_boost = SOURCE_TIER_BOOST.get(tier, 0.0)
    items: list[dict] = []

    for entry in feed.entries:
        title = _clean(getattr(entry, "title", ""))
        link  = getattr(entry, "link", "")
        if not title or not link:
            continue

        if "news.google.com" in link:
            link = getattr(entry, "id", link) or link

        pub_dt = _parse_dt(
            getattr(entry, "published", None) or
            getattr(entry, "updated", None) or
            getattr(entry, "created", None)
        )
        summary = _clean(
            getattr(entry, "summary", "") or getattr(entry, "description", "")
        )[:700]

        items.append({
            "id":             _story_id(source, link),
            "title":          title,
            "url":            link,
            "source":         source,
            "source_tier":    tier,
            "published_at":   pub_dt.isoformat() if pub_dt else None,
            "summary":        summary,
            "tickers":        _extract_tickers(title),
            "raw_importance": tier_boost,
        })
    return items


# ---------------------------------------------------------------------------
# Reddit (free — no API key)
# ---------------------------------------------------------------------------

def fetch_reddit_hot(subreddit: str, limit: int = 30) -> list[dict]:
    url = REDDIT_HOT_URL.format(sub=subreddit)
    try:
        r = requests.get(url, headers=REDDIT_HEADERS, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        print(f"[news_scout] reddit r/{subreddit} failed: {exc}", file=sys.stderr)
        return []

    items: list[dict] = []
    for post in data.get("data", {}).get("children", []):
        p = post.get("data", {})
        title = _clean(p.get("title", ""))
        score = int(p.get("score", 0))
        comments = int(p.get("num_comments", 0))
        upvote_ratio = float(p.get("upvote_ratio", 0.5))
        url_post = p.get("url", "")
        permalink = "https://reddit.com" + p.get("permalink", "")
        is_self = p.get("is_self", False)
        created_utc = p.get("created_utc")

        if not title or score < 10:
            continue
        # Skip memes/images — only news-like posts
        if p.get("over_18") or not (url_post or is_self):
            continue

        pub_dt = datetime.fromtimestamp(created_utc, tz=timezone.utc) if created_utc else None
        actual_url = url_post if not is_self else permalink

        # Engagement importance: normalise score (top posts ~50k, avg ~200)
        raw_imp = min(1.0, (score / 5000.0) * upvote_ratio)
        # Extra boost for posts with many comments (discussion = engagement)
        raw_imp = min(1.0, raw_imp + min(0.2, comments / 500.0))

        tickers = _extract_tickers(title)
        items.append({
            "id":              _story_id(f"reddit_{subreddit}", permalink),
            "title":           title,
            "url":             actual_url,
            "source":          f"reddit_{subreddit.lower()}",
            "source_tier":     2,
            "published_at":    pub_dt.isoformat() if pub_dt else None,
            "summary":         _clean(p.get("selftext", ""))[:400] or f"r/{subreddit} — {score:,} upvotes, {comments} comments.",
            "tickers":         tickers,
            "raw_importance":  raw_imp,
            "reddit_score":    score,
            "reddit_comments": comments,
            "reddit_ratio":    upvote_ratio,
        })
    return items


# ---------------------------------------------------------------------------
# X / Twitter via Nitter RSS (free, no key)
# ---------------------------------------------------------------------------

def _parse_tweet(tweet: dict, handle: str, tier: int) -> dict | None:
    """Convert a raw X API v2 tweet object to a story dict."""
    text = _clean(tweet.get("text", ""))
    if not text:
        return None
    # Skip retweets and pure replies
    if text.startswith("RT @") or (text.startswith("@") and len(text) < 80):
        return None
    tickers = _extract_tickers(text)
    if not tickers and not any(t in text.lower() for t in _CRYPTO_TERMS):
        return None

    tweet_id  = tweet.get("id", "")
    tweet_url = f"https://x.com/{handle}/status/{tweet_id}"
    pub_dt    = _parse_dt(tweet.get("created_at"))

    metrics   = tweet.get("public_metrics") or {}
    likes     = int(metrics.get("like_count", 0))
    retweets  = int(metrics.get("retweet_count", 0))
    replies   = int(metrics.get("reply_count", 0))
    # Engagement-based importance boost (logarithmic so outliers don't dominate)
    eng_total = likes + retweets * 2 + replies
    eng_boost = min(0.35, math.log1p(eng_total) / 20.0)
    base_imp  = SOURCE_TIER_BOOST.get(tier, 0.0)

    return {
        "id":             _story_id(f"x_{handle}", tweet_url),
        "title":          text,
        "url":            tweet_url,
        "source":         f"x_{handle.lower()}",
        "source_tier":    tier,
        "published_at":   pub_dt.isoformat() if pub_dt else None,
        "summary":        f"@{handle} on X — {likes:,} likes, {retweets:,} RT",
        "tickers":        tickers,
        "raw_importance": min(1.0, base_imp + eng_boost),
        "x_account":      handle,
        "x_account_tier": tier,
        "x_likes":        likes,
        "x_retweets":     retweets,
    }


def fetch_x_api_chunk(handles: list[str], bearer_token: str,
                       handle_tiers: dict[str, int]) -> list[dict]:
    """Fetch recent tweets from a batch of handles via X API v2 search."""
    import urllib.parse as _up
    # Bearer tokens from the dev portal are sometimes URL-encoded
    token = _up.unquote(bearer_token)

    query = " OR ".join(f"from:{h}" for h in handles)
    query += " -is:retweet lang:en"
    params = {
        "query":         query,
        "max_results":   100,
        "tweet.fields":  "created_at,public_metrics,author_id,entities",
        "expansions":    "author_id",
        "user.fields":   "username",
    }
    headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
    try:
        r = requests.get(X_API_SEARCH_URL, params=params,
                         headers=headers, timeout=HTTP_TIMEOUT)
        if r.status_code == 401:
            if 401 not in _X_API_WARN_LOGGED:
                print("[news_scout] X API disabled (401: Bearer Token invalid / "
                      "app on Free tier — read needs Basic $100/mo). "
                      "Skipping X; falling back to RSS sources.",
                      file=sys.stderr)
                _X_API_WARN_LOGGED.add(401)
            return []
        if r.status_code == 403:
            if 403 not in _X_API_WARN_LOGGED:
                print("[news_scout] X API disabled (403 Forbidden — needs Basic tier). "
                      "Skipping X; falling back to RSS sources.",
                      file=sys.stderr)
                _X_API_WARN_LOGGED.add(403)
            return []
        if r.status_code == 429:
            if 429 not in _X_API_WARN_LOGGED:
                print("[news_scout] X API rate-limited (429). "
                      "Skipping further X calls this run.",
                      file=sys.stderr)
                _X_API_WARN_LOGGED.add(429)
            return []
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        print(f"[news_scout] X API chunk failed: {exc}", file=sys.stderr)
        return []

    # Build handle lookup from author expansions
    users_by_id: dict[str, str] = {}
    for u in (data.get("includes") or {}).get("users") or []:
        users_by_id[u["id"]] = u.get("username", "unknown")

    items: list[dict] = []
    for tweet in data.get("data") or []:
        handle = users_by_id.get(tweet.get("author_id", ""), "unknown")
        tier   = handle_tiers.get(handle, handle_tiers.get(handle.lower(), 2))
        parsed = _parse_tweet(tweet, handle, tier)
        if parsed:
            items.append(parsed)
    return items


def fetch_x_via_api(bearer_token: str) -> list[dict]:
    """Fetch tweets from all X_ACCOUNTS using the official API v2."""
    handle_tiers = {h.lower(): t for h, t in X_ACCOUNTS}
    handles_list = [h for h, _ in X_ACCOUNTS]

    all_items: list[dict] = []
    # Split into chunks to stay within query length limits
    for i in range(0, len(handles_list), X_API_CHUNK_SIZE):
        chunk = handles_list[i:i + X_API_CHUNK_SIZE]
        items = fetch_x_api_chunk(chunk, bearer_token, handle_tiers)
        all_items.extend(items)
        # If a hard auth / rate-limit was hit, don't keep hammering the API
        if _X_API_WARN_LOGGED & {401, 403, 429}:
            break

    if all_items or not (_X_API_WARN_LOGGED & {401, 403, 429}):
        print(f"[news_scout] X API: {len(all_items)} crypto tweets from "
              f"{len(handles_list)} accounts", file=sys.stderr)
    return all_items


def fetch_x_via_nitter(handle: str, tier: int) -> list[dict]:
    """Fetch tweets for one account via Nitter RSS. Returns [] on all failures."""
    custom = os.environ.get("NITTER_INSTANCE", "").strip()
    instances = ([custom] + NITTER_INSTANCES) if custom else NITTER_INSTANCES

    for instance in instances:
        url = f"https://{instance}/{handle}/rss"
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": USER_AGENT})
        except Exception:
            continue
        if not feed.entries:
            continue

        items: list[dict] = []
        for entry in feed.entries[:25]:
            title = _clean(getattr(entry, "title", ""))
            link  = getattr(entry, "link",  "")
            if not title or not link:
                continue
            if title.startswith("RT @") or (title.startswith("@") and len(title) < 60):
                continue

            twitter_link = re.sub(r"https?://[^/]+/", "https://x.com/", link)
            pub_dt = _parse_dt(
                getattr(entry, "published", None) or getattr(entry, "updated", None)
            )
            tickers = _extract_tickers(title)
            if not tickers and not any(t in title.lower() for t in _CRYPTO_TERMS):
                continue

            items.append({
                "id":             _story_id(f"x_{handle}", twitter_link),
                "title":          title,
                "url":            twitter_link,
                "source":         f"x_{handle.lower()}",
                "source_tier":    tier,
                "published_at":   pub_dt.isoformat() if pub_dt else None,
                "summary":        f"@{handle} on X",
                "tickers":        tickers,
                "raw_importance": SOURCE_TIER_BOOST.get(tier, 0.0),
                "x_account":      handle,
                "x_account_tier": tier,
            })

        if items:
            print(f"[news_scout] x/@{handle}: {len(items)} tweets via nitter/{instance}",
                  file=sys.stderr)
            return items
        if feed.entries:  # parsed OK but 0 crypto items — don't try other instances
            return []

    return []


def _get_bearer_token() -> str:
    """Return a valid Bearer Token from any available credential set."""
    # 1. Explicit Bearer Token in env
    bearer = os.environ.get(X_BEARER_TOKEN_ENV, "").strip()
    if bearer:
        return bearer

    # 2. Derive via OAuth 2.0 client credentials (API Key + Secret)
    api_key    = os.environ.get(X_API_KEY_ENV, "").strip()
    api_secret = os.environ.get(X_API_SECRET_ENV, "").strip()
    if api_key and api_secret:
        try:
            import base64 as _b64
            creds = _b64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
            r = requests.post(
                "https://api.twitter.com/oauth2/token",
                headers={"Authorization": f"Basic {creds}",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data="grant_type=client_credentials",
                timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            token = r.json().get("access_token", "")
            if token:
                print("[news_scout] X: obtained Bearer Token via OAuth2 client creds",
                      file=sys.stderr)
                return token
        except Exception as exc:
            print(f"[news_scout] X: OAuth2 token fetch failed: {exc}", file=sys.stderr)

    return ""


def fetch_x_accounts() -> list[dict]:
    """Fetch tweets from X_ACCOUNTS using best available method.

    Priority:
    1. Official X API v2 (Bearer Token — explicit or derived from API Key+Secret)
    2. Nitter RSS fallback (parallel, per-account)
    3. KOL newsletter/Substack RSS (always runs regardless of the above)
    """
    all_items: list[dict] = []

    bearer = _get_bearer_token()
    if bearer:
        all_items.extend(fetch_x_via_api(bearer))
    else:
        print("[news_scout] No X API credentials — trying Nitter RSS fallback",
              file=sys.stderr)
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(fetch_x_via_nitter, h, t): h
                       for h, t in X_ACCOUNTS}
            for fut in as_completed(futures):
                all_items.extend(fut.result())

    # KOL newsletters / Substacks — always fetch (free, reliable)
    kol_total = 0
    for label, rss_url, tier in X_KOL_RSS:
        try:
            items = fetch_rss(label, rss_url, tier)
            print(f"[news_scout] kol/{label}: {len(items)} items", file=sys.stderr)
            all_items.extend(items)
            kol_total += len(items)
        except Exception as exc:
            print(f"[news_scout] kol/{label} failed: {exc}", file=sys.stderr)

    print(f"[news_scout] X/KOL total: {len(all_items)} items "
          f"({len(all_items)-kol_total} tweets + {kol_total} newsletters)",
          file=sys.stderr)
    return all_items


# ---------------------------------------------------------------------------
# CoinGecko trending (free — no key)
# ---------------------------------------------------------------------------

def fetch_coingecko_trending() -> list[str]:
    """Return list of trending coin symbols (top 7 by search volume/24h)."""
    r = _get(COINGECKO_TRENDING_URL)
    if not r:
        return []
    try:
        coins = r.json().get("coins", [])
        syms = []
        for c in coins:
            item = c.get("item", {})
            sym = (item.get("symbol") or "").upper()
            if sym:
                syms.append(sym)
        print(f"[news_scout] coingecko_trending: {syms}", file=sys.stderr)
        return syms
    except Exception as exc:
        print(f"[news_scout] coingecko_trending parse error: {exc}", file=sys.stderr)
        return []


def enrich_with_coingecko(stories: list[dict], trending_syms: list[str]) -> int:
    if not trending_syms:
        return 0
    trending_set = set(trending_syms)
    boosted = 0
    for story in stories:
        tickers = set(story.get("tickers") or [])
        if tickers & trending_set:
            before = story.get("raw_importance") or 0.0
            story["raw_importance"] = min(1.0, before + COINGECKO_BOOST)
            story.setdefault("signals", []).append({
                "source": "coingecko_trending",
                "matched": sorted(tickers & trending_set),
            })
            boosted += 1
    return boosted


# ---------------------------------------------------------------------------
# CryptoPanic public RSS
# ---------------------------------------------------------------------------

def fetch_cryptopanic_rss() -> list[dict]:
    items = fetch_rss("cryptopanic", CRYPTOPANIC_PUBLIC_URL, tier=2)
    print(f"[news_scout] cryptopanic_rss: {len(items)} items", file=sys.stderr)
    return items


# ---------------------------------------------------------------------------
# CryptoPanic developer API (optional)
# ---------------------------------------------------------------------------

def fetch_cryptopanic_api(api_key: str, limit: int = 50) -> list[dict]:
    r = _get(CRYPTOPANIC_API_ENDPOINT,
             params={"auth_token": api_key, "public": "true",
                     "kind": "news", "regions": "en", "filter": "hot"})
    if not r:
        return []
    try:
        payload = r.json()
    except Exception:
        return []

    items: list[dict] = []
    for entry in payload.get("results", [])[:limit]:
        title = _clean(entry.get("title", ""))
        url   = entry.get("original_url") or entry.get("url", "")
        if not title or not url:
            continue

        pub_dt = _parse_dt(entry.get("published_at"))
        votes  = entry.get("votes") or {}
        pos    = int(votes.get("positive", 0))
        imp    = int(votes.get("important", 0))
        liked  = int(votes.get("liked", 0))
        neg    = int(votes.get("negative", 0))
        total  = pos + imp + liked + neg
        importance = min(1.0, (imp * 3 + pos + liked) / 20.0) if total else 0.15

        tickers: list[str] = []
        for inst in entry.get("instruments") or []:
            code = (inst.get("code") or "").upper()
            if code and len(code) <= 6:
                tickers.append(code)
        if not tickers:
            tickers = _extract_tickers(title)

        items.append({
            "id":             _story_id("cryptopanic_api", url),
            "title":          title,
            "url":            url,
            "source":         "cryptopanic",
            "source_tier":    1,
            "published_at":   pub_dt.isoformat() if pub_dt else None,
            "summary":        _clean(entry.get("description", ""))[:700],
            "tickers":        tickers,
            "raw_importance": importance,
            "votes":          votes,
        })
    return items


# ---------------------------------------------------------------------------
# Google News RSS
# ---------------------------------------------------------------------------

def fetch_google_news() -> list[dict]:
    items = fetch_rss("google_news", GOOGLE_NEWS_CRYPTO_URL, tier=2)
    print(f"[news_scout] google_news: {len(items)} items", file=sys.stderr)
    return items


# ---------------------------------------------------------------------------
# LunarCrush (optional)
# ---------------------------------------------------------------------------

def fetch_lunarcrush_trending(api_key: str, limit: int = 10) -> list[dict]:
    r = _get(LUNARCRUSH_ENDPOINT,
             headers={"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT})
    if not r:
        return []
    try:
        rows = r.json().get("data") or []
        rows = sorted(rows,
                      key=lambda x: (float(x.get("galaxy_score") or 0),
                                     float(x.get("interactions_24h") or 0)),
                      reverse=True)
    except Exception:
        return []

    out: list[dict] = []
    for row in rows[:limit]:
        sym = (row.get("symbol") or "").upper()
        if sym:
            out.append({
                "symbol":             sym,
                "galaxy_score":       row.get("galaxy_score"),
                "interactions_24h":   row.get("interactions_24h"),
                "percent_change_24h": row.get("percent_change_24h"),
            })
    return out


def enrich_with_lunarcrush(stories: list[dict], trending: list[dict]) -> int:
    if not trending:
        return 0
    trending_syms = {t["symbol"] for t in trending if t.get("symbol")}
    boosted = 0
    for story in stories:
        tickers = set(story.get("tickers") or [])
        if tickers & trending_syms:
            before = story.get("raw_importance") or 0.0
            story["raw_importance"] = min(1.0, before + LUNARCRUSH_BOOST)
            story.setdefault("signals", []).append({"source": "lunarcrush"})
            boosted += 1
    return boosted


# ---------------------------------------------------------------------------
# Deduplication — URL + title similarity
# ---------------------------------------------------------------------------

def _dedup(stories: list[dict], jaccard_threshold: float = 0.60) -> list[dict]:
    """Remove duplicates by URL, then merge near-duplicate titles.

    When two stories look like the same news (high Jaccard similarity),
    keep the one with higher raw_importance and add the other's source to
    a 'duplicate_sources' list (used by ranker cluster scoring).
    """
    # Step 1 — URL dedup
    seen_urls: set[str] = set()
    url_deduped: list[dict] = []
    for s in stories:
        key = _normalize_url(s["url"])
        if key not in seen_urls:
            seen_urls.add(key)
            url_deduped.append(s)

    # Step 2 — Title similarity dedup (greedy, keeps highest-importance)
    # Sort by importance desc so we keep the best version
    url_deduped.sort(key=lambda x: x.get("raw_importance") or 0, reverse=True)

    kept: list[dict] = []
    for story in url_deduped:
        # Check against already-kept stories
        merged = False
        for keeper in kept:
            if _jaccard_titles(story["title"], keeper["title"]) >= jaccard_threshold:
                # Merge: record that this story appeared in another source
                keeper.setdefault("duplicate_sources", []).append(story["source"])
                # Boost importance slightly for each additional source
                keeper["raw_importance"] = min(
                    1.0, (keeper.get("raw_importance") or 0) + 0.05
                )
                # Carry over reddit signals if the duplicate has them
                if story.get("reddit_score") and not keeper.get("reddit_score"):
                    keeper["reddit_score"]    = story["reddit_score"]
                    keeper["reddit_comments"] = story.get("reddit_comments", 0)
                merged = True
                break
        if not merged:
            kept.append(dict(story))

    return kept


# ---------------------------------------------------------------------------
# Main collector
# ---------------------------------------------------------------------------

def collect_news(hours_back: int = 12, published_after: "datetime | None" = None,
                 niche: str = "crypto") -> list[dict]:
    all_items: list[dict] = []

    # ── Choose feed list based on niche ──────────────────────────────────
    if niche and niche != "crypto":
        try:
            from src.agents.niches import get_niche
        except ImportError:
            from niches import get_niche  # type: ignore
        niche_cfg = get_niche(niche)
        feed_list = niche_cfg.get("feeds", []) or RSS_FEEDS
    else:
        feed_list = RSS_FEEDS

    # ── Parallel RSS fetch ────────────────────────────────────────────────
    def _fetch_one(args):
        source, url, tier = args
        try:
            items = fetch_rss(source, url, tier)
            print(f"[news_scout] {source}: {len(items)} items", file=sys.stderr)
            return items
        except Exception as exc:
            print(f"[news_scout] {source} FAILED: {exc}", file=sys.stderr)
            return []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_one, args): args[0] for args in feed_list}
        for fut in as_completed(futures):
            all_items.extend(fut.result())

    if niche == "crypto":
        # ── OPTIMIZED: Parallel fetch all APIs (was sequential, now 6 parallel) ─
        # This reduces 60+ seconds to ~15-20 seconds (70% speedup)
        
        def _fetch_cryptopanic_rss_wrapper():
            try:
                return fetch_cryptopanic_rss()
            except Exception as e:
                print(f"[news_scout] CryptoPanic RSS failed: {e}", file=sys.stderr)
                return []

        def _fetch_google_news_wrapper():
            try:
                items = fetch_google_news()
                for gn_label, gn_url in [("google_news_macro", GOOGLE_NEWS_MACRO_URL),
                                           ("google_news_finance", GOOGLE_NEWS_FINANCE_URL)]:
                    gn_items = fetch_rss(gn_label, gn_url, tier=2)
                    print(f"[news_scout] {gn_label}: {len(gn_items)} items", file=sys.stderr)
                    items.extend(gn_items)
                return items
            except Exception as e:
                print(f"[news_scout] Google News failed: {e}", file=sys.stderr)
                return []

        def _fetch_x_wrapper():
            try:
                x_tweets = fetch_x_accounts()
                print(f"[news_scout] X/Twitter total: {len(x_tweets)} crypto tweets", file=sys.stderr)
                return x_tweets
            except Exception as e:
                print(f"[news_scout] X fetch failed: {e}", file=sys.stderr)
                return []

        def _fetch_reddit_wrapper():
            try:
                all_reddit = []
                for sub in REDDIT_SUBREDDITS:
                    try:
                        items = fetch_reddit_hot(sub)
                        print(f"[news_scout] reddit r/{sub}: {len(items)} items", file=sys.stderr)
                        all_reddit.extend(items)
                    except Exception as exc:
                        print(f"[news_scout] reddit r/{sub} failed: {exc}", file=sys.stderr)
                return all_reddit
            except Exception as e:
                print(f"[news_scout] Reddit wrapper failed: {e}", file=sys.stderr)
                return []

        def _fetch_coingecko_wrapper():
            try:
                return fetch_coingecko_trending()
            except Exception as e:
                print(f"[news_scout] CoinGecko failed: {e}", file=sys.stderr)
                return []

        def _fetch_cryptopanic_api_wrapper():
            try:
                cp_key = os.environ.get(CRYPTOPANIC_KEY_ENV, "").strip()
                if cp_key:
                    cp_items = fetch_cryptopanic_api(cp_key)
                    print(f"[news_scout] cryptopanic_api: {len(cp_items)} items", file=sys.stderr)
                    return cp_items
                return []
            except Exception as e:
                print(f"[news_scout] CryptoPanic API failed: {e}", file=sys.stderr)
                return []

        def _fetch_lunarcrush_wrapper():
            try:
                lc_key = os.environ.get(LUNARCRUSH_KEY_ENV, "").strip()
                if lc_key:
                    return fetch_lunarcrush_trending(lc_key)
                return []
            except Exception as e:
                print(f"[news_scout] LunarCrush failed: {e}", file=sys.stderr)
                return []

        # Execute all 6 API groups in parallel
        with ThreadPoolExecutor(max_workers=6) as api_pool:
            future_cp_rss = api_pool.submit(_fetch_cryptopanic_rss_wrapper)
            future_gn = api_pool.submit(_fetch_google_news_wrapper)
            future_x = api_pool.submit(_fetch_x_wrapper)
            future_reddit = api_pool.submit(_fetch_reddit_wrapper)
            future_cg = api_pool.submit(_fetch_coingecko_wrapper)
            future_cp_api = api_pool.submit(_fetch_cryptopanic_api_wrapper)
            future_lc = api_pool.submit(_fetch_lunarcrush_wrapper)

            # Collect results as they complete
            all_items.extend(future_cp_rss.result())
            all_items.extend(future_gn.result())
            all_items.extend(future_x.result())
            all_items.extend(future_reddit.result())
            cg_trending = future_cg.result()
            all_items.extend(future_cp_api.result())
            lc_trending = future_lc.result()

        # Enrich with trending data
        if cg_trending:
            boosted = enrich_with_coingecko(all_items, cg_trending)
            print(f"[news_scout] coingecko: boosted {boosted} stories", file=sys.stderr)
        if lc_trending:
            boosted = enrich_with_lunarcrush(all_items, lc_trending)
            print(f"[news_scout] lunarcrush: boosted {boosted}", file=sys.stderr)
    else:
        # ── Non-crypto: Google News per niche query ───────────────────────
        try:
            from src.agents.niches import get_niche as _gn
        except ImportError:
            from niches import get_niche as _gn  # type: ignore
        for q in _gn(niche).get("gnews_queries", []):
            from urllib.parse import quote_plus
            gn_url = (f"https://news.google.com/rss/search"
                      f"?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en")
            gn_items = fetch_rss(f"gnews_{niche}", gn_url, tier=2)
            print(f"[news_scout] gnews [{q[:30]}]: {len(gn_items)} items", file=sys.stderr)
            all_items.extend(gn_items)

    # ── Time filter ───────────────────────────────────────────────────────
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    fresh: list[dict] = []
    for it in all_items:
        if not it.get("published_at"):
            fresh.append(it)
            continue
        dt = _parse_dt(it["published_at"])
        if not dt:
            continue
        if dt < cutoff:
            continue
        if published_after and dt < published_after:
            continue
        fresh.append(it)

    # ── Relevance filter — crypto only for crypto niche ───────────────────
    relevant = [s for s in fresh if _is_crypto_relevant(s)] if niche == "crypto" else fresh

    # ── Dedup (URL + title similarity) ───────────────────────────────────
    deduped = _dedup(relevant)

    # ── Sort newest first ─────────────────────────────────────────────────
    deduped.sort(key=lambda x: x.get("published_at") or "", reverse=True)

    after_str = f" (after {published_after.isoformat()})" if published_after else ""
    print(
        f"[news_scout] raw {len(all_items)} -> "
        f"{hours_back}h filter{after_str} {len(fresh)} -> "
        f"relevant {len(relevant)} -> "
        f"deduped {len(deduped)}",
        file=sys.stderr,
    )
    return deduped


def main() -> int:
    hours = int(os.environ.get("SCOUT_HOURS_BACK", "12"))
    stories = collect_news(hours_back=hours)
    json.dump(stories, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
