"""x_trending.py — Fetch trending hashtags & cashtags for X posts.

Sources (in order of priority):
  1. CoinGecko /search/trending  — top 10 trending coins → $BTC / #Bitcoin
  2. trends24.in/worldwide/      — global trending hashtags (HTML scrape)
  3. Static high-reach fallback  — always-relevant crypto/finance tags

Cache: data/x_trending.json  — refreshed every 6 hours automatically.

Usage:
    python -m src.agents.x_trending          # refresh cache + print result
    from src.agents.x_trending import get_trending_tags
    tags = get_trending_tags()               # returns list of str like ['#Bitcoin', '$BTC', ...]
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE        = Path(__file__).resolve().parent
_REPO        = _HERE.parent.parent
_CACHE_FILE  = _REPO / "data" / "x_trending.json"
_CACHE_TTL   = 6 * 3600   # 6 hours

# High-reach static tags used as fallback / base layer
_STATIC_CRYPTO = [
    "#Bitcoin", "#BTC", "#Crypto", "#CryptoNews",
    "#Ethereum", "#ETH", "#Web3", "#DeFi",
    "$BTC", "$ETH", "$SOL", "$BNB",
]
_STATIC_MACRO = [
    "#Stocks", "#Finance", "#Economy", "#Investing",
    "#Fed", "#Inflation", "#Markets", "#Trading",
]

# Coin symbol → display name mapping for hashtag generation
_COIN_NAMES: dict[str, str] = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana",
    "BNB": "BNB", "XRP": "XRP", "ADA": "Cardano",
    "DOGE": "Dogecoin", "AVAX": "Avalanche", "DOT": "Polkadot",
    "MATIC": "Polygon", "LINK": "Chainlink", "LTC": "Litecoin",
    "ATOM": "Cosmos", "UNI": "Uniswap", "NEAR": "Near",
    "APT": "Aptos", "ARB": "Arbitrum", "OP": "Optimism",
    "SUI": "Sui", "INJ": "Injective", "TIA": "Celestia",
    "FET": "Fetch", "RNDR": "Render", "PEPE": "Pepe",
}


def _fetch_coingecko() -> list[str]:
    """Fetch top trending coins from CoinGecko and convert to tags."""
    try:
        import urllib.request
        url = "https://api.coingecko.com/api/v3/search/trending"
        req = urllib.request.Request(url, headers={"User-Agent": "AutoPost/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data: dict = json.loads(resp.read().decode())
        tags: list[str] = []
        for item in data.get("coins", [])[:7]:
            coin = item.get("item", {})
            symbol: str = coin.get("symbol", "").upper()
            name:   str = coin.get("name", "")
            if symbol:
                tags.append(f"${symbol}")
                display = _COIN_NAMES.get(symbol, name)
                if display:
                    tags.append(f"#{display.replace(' ', '')}")
        return tags
    except Exception as exc:
        print(f"[x_trending] CoinGecko fetch failed: {exc}")
        return []


def _fetch_trends24() -> list[str]:
    """Scrape global trending hashtags from trends24.in."""
    try:
        import urllib.request
        # Try multiple URL patterns — trends24 occasionally changes routes
        for url in ("https://trends24.in/worldwide/", "https://trends24.in/", "https://trends24.in/united-states/"):
            try:
                req_probe = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"},
                )
                with urllib.request.urlopen(req_probe, timeout=8) as r:
                    if r.status == 200:
                        break
            except Exception:
                continue
        else:
            return []
        url = url  # noqa — last working url
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        # trends24 wraps each trend in <a ...>  #Hashtag </a>
        raw = re.findall(r'<a[^>]+class="[^"]*trend-name[^"]*"[^>]*>([^<]+)</a>', html)
        if not raw:
            # fallback pattern — plain anchor text that looks like a hashtag
            raw = re.findall(r'>\s*(#\w+)\s*<', html)

        tags: list[str] = []
        for t in raw[:30]:
            t = t.strip()
            if t.startswith("#") and len(t) > 2:
                tags.append(t)
        return tags
    except Exception as exc:
        print(f"[x_trending] trends24.in fetch failed: {exc}")
        return []


def _is_relevant(tag: str) -> bool:
    """Keep only tags relevant to crypto / finance / markets."""
    low = tag.lower().lstrip("#$")
    relevant_kw = {
        "bitcoin", "btc", "crypto", "ethereum", "eth", "blockchain",
        "solana", "sol", "defi", "web3", "nft", "altcoin", "altcoins",
        "trading", "invest", "market", "stock", "finance", "economy",
        "inflation", "fed", "dollar", "gold", "silver", "oil",
        "recession", "bank", "rate", "bull", "bear", "rally",
        "xrp", "bnb", "doge", "ada", "avax", "dot", "link",
    }
    return any(kw in low for kw in relevant_kw)


def refresh_cache() -> dict[str, Any]:
    """Fetch fresh data from all sources and write to cache."""
    cg_tags    = _fetch_coingecko()
    tr_tags    = _fetch_trends24()

    # Separate cashtags ($) and hashtags (#) from CoinGecko
    cg_cash    = [t for t in cg_tags if t.startswith("$")]
    cg_hash    = [t for t in cg_tags if t.startswith("#")]

    # From trends24: keep only relevant tags
    tr_relevant = [t for t in tr_tags if _is_relevant(t)]

    # Deduplicate while preserving order, case-insensitive
    seen: set[str] = set()
    merged: list[str] = []
    for tag in (cg_cash + cg_hash + tr_relevant + _STATIC_CRYPTO + _STATIC_MACRO):
        key = tag.lower()
        if key not in seen:
            seen.add(key)
            merged.append(tag)

    payload: dict[str, Any] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "fetched_at_ts": time.time(),
        "coingecko": cg_tags,
        "trends24":  tr_relevant,
        "merged":    merged,
        # Quick-access slices
        "cashtags":  [t for t in merged if t.startswith("$")][:10],
        "hashtags":  [t for t in merged if t.startswith("#")][:20],
    }

    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[x_trending] cache refreshed — {len(cg_tags)} CoinGecko, "
          f"{len(tr_relevant)} trends24 tags saved.")
    return payload


def _load_cache() -> dict[str, Any] | None:
    if not _CACHE_FILE.exists():
        return None
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        age = time.time() - float(data.get("fetched_at_ts", 0))
        if age > _CACHE_TTL:
            return None
        return data
    except Exception:
        return None


def get_trending_tags(
    max_cashtags: int = 3,
    max_hashtags: int = 4,
    tone: str = "crypto",
) -> list[str]:
    """Return a list of trending tags ready to append to an X post.

    Automatically refreshes the cache if it's older than 6 hours.
    Returns cashtags first ($BTC), then hashtags (#Bitcoin).
    """
    data = _load_cache()
    if data is None:
        data = refresh_cache()

    cashtags = data.get("cashtags", [])[:max_cashtags]
    hashtags = data.get("hashtags", [])[:max_hashtags]

    # For macro/economy tone, skip pure-crypto hashtags and include macro ones
    if tone == "macro":
        macro_tags = [t for t in hashtags if any(
            kw in t.lower() for kw in
            ("finance", "economy", "inflation", "fed", "market", "invest", "stock", "trading")
        )]
        crypto_tags = [t for t in hashtags if t not in macro_tags]
        hashtags = (macro_tags + crypto_tags)[:max_hashtags]

    return cashtags + hashtags


if __name__ == "__main__":
    data = refresh_cache()
    print("\nTop cashtags:", data["cashtags"][:5])
    print("Top hashtags:", data["hashtags"][:8])
    print("trends24:",     data["trends24"][:5])
