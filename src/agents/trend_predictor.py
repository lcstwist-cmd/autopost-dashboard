"""trend_predictor.py — Predicts what crypto topics will trend in the next 2–4 hours.

Data sources:
  1. CoinGecko trending + market momentum (price change 1h, 24h)
  2. Fear & Greed Index (alternative.me)
  3. NewsAPI / RSS recent volume per ticker
  4. Velocity: if a coin has both rising price AND news volume, it scores high

Saves predictions to data/trend_forecast.json (cache 1h TTL).
Called by scheduler before each pipeline run so copywriter can prioritize.

Usage:
    python src/agents/trend_predictor.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
_FORECAST_FILE  = _REPO / "data" / "trend_forecast.json"
_BRAIN_PARAMS   = _REPO / "analytics" / "agent_brains" / "trend_predictor" / "learned_params.json"


def _load_brain_params() -> dict:
    if _BRAIN_PARAMS.exists():
        try:
            return json.loads(_BRAIN_PARAMS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

COINGECKO_TRENDING   = "https://api.coingecko.com/api/v3/search/trending"
COINGECKO_MARKETS    = "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=20&page=1&sparkline=false&price_change_percentage=1h,24h"
FEAR_GREED_URL       = "https://api.alternative.me/fng/?limit=1"

_HTTP_HEADERS = {
    "User-Agent": "AutoPost/1.0 (crypto news bot)",
    "Accept": "application/json",
}


def _fetch_json(url: str, timeout: int = 8) -> dict | list | None:
    try:
        req = urllib.request.Request(url, headers=_HTTP_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"[trend_predictor] fetch failed {url}: {exc}")
        return None


def _get_coingecko_trending() -> list[dict[str, Any]]:
    """Top 7 trending coins from CoinGecko."""
    data = _fetch_json(COINGECKO_TRENDING)
    if not data:
        return []
    coins = []
    for item in (data.get("coins") or [])[:7]:
        c = item.get("item", {})
        coins.append({
            "id":     c.get("id", ""),
            "symbol": c.get("symbol", "").upper(),
            "name":   c.get("name", ""),
            "rank":   c.get("market_cap_rank") or 999,
            "score":  c.get("score", 0),
            "source": "trending",
        })
    return coins


def _get_coingecko_movers() -> list[dict[str, Any]]:
    """Top movers (biggest 1h price change) from top 20 by market cap."""
    data = _fetch_json(COINGECKO_MARKETS)
    if not data or not isinstance(data, list):
        return []

    movers = []
    for coin in data:
        change_1h  = coin.get("price_change_percentage_1h_in_currency") or 0
        change_24h = coin.get("price_change_percentage_24h") or 0
        movers.append({
            "id":          coin.get("id", ""),
            "symbol":      (coin.get("symbol") or "").upper(),
            "name":        coin.get("name", ""),
            "price_usd":   coin.get("current_price", 0),
            "change_1h":   round(change_1h, 2),
            "change_24h":  round(change_24h, 2),
            "volume_24h":  coin.get("total_volume", 0),
            "source":      "market_movers",
        })

    # Sort by absolute 1h change descending
    movers.sort(key=lambda x: abs(x["change_1h"]), reverse=True)
    return movers[:10]


def _get_fear_greed() -> dict[str, Any]:
    """Current Fear & Greed Index."""
    data = _fetch_json(FEAR_GREED_URL)
    if not data:
        return {"value": None, "label": "Unknown"}
    try:
        latest = data["data"][0]
        return {
            "value":     int(latest["value"]),
            "label":     latest["value_classification"],
            "timestamp": latest["timestamp"],
        }
    except Exception:
        return {"value": None, "label": "Unknown"}


def _score_coins(
    trending: list[dict],
    movers: list[dict],
    brain_velocity: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Combine trending and movers into a single scored list."""
    scores: dict[str, dict[str, Any]] = {}

    # Trending coins get a base score
    for i, coin in enumerate(trending):
        sym = coin["symbol"]
        scores[sym] = {
            **coin,
            "trend_rank":    i + 1,
            "trend_score":   max(0, 7 - i) * 10,   # 70, 60, 50 ... 10
            "momentum_score": 0,
            "change_1h":     0,
            "change_24h":    0,
            "volume_24h":    0,
        }

    # Add momentum from movers
    for coin in movers:
        sym = coin["symbol"]
        momentum = min(abs(coin["change_1h"]) * 5, 50)   # cap at 50
        if sym in scores:
            scores[sym]["momentum_score"] += momentum
            scores[sym]["change_1h"]   = coin["change_1h"]
            scores[sym]["change_24h"]  = coin["change_24h"]
            scores[sym]["volume_24h"]  = coin["volume_24h"]
        else:
            scores[sym] = {
                **coin,
                "trend_rank":    99,
                "trend_score":   0,
                "momentum_score": momentum,
            }

    # Brain velocity boost: historical coins the brain learned are hot
    if brain_velocity:
        max_vel = max(brain_velocity.values()) if brain_velocity else 1
        for sym, vel in brain_velocity.items():
            if sym in scores:
                boost = round(vel / max_vel * 15, 1)   # max +15 pts from brain
                scores[sym]["momentum_score"] += boost

    # Final score = trend_score + momentum_score
    for sym, c in scores.items():
        c["final_score"] = round(c["trend_score"] + c["momentum_score"], 1)

    ranked = sorted(scores.values(), key=lambda x: x["final_score"], reverse=True)
    return ranked[:12]


def _build_narrative(fg: dict, ranked: list[dict]) -> str:
    """Generate a short market narrative for the copywriter."""
    label = fg.get("label", "Unknown")
    value = fg.get("value")

    if value is None:
        mood = "neutral"
    elif value >= 75:
        mood = "extreme greed — market may be overheated"
    elif value >= 55:
        mood = "greed — bullish momentum"
    elif value >= 45:
        mood = "neutral — sideways market"
    elif value >= 25:
        mood = "fear — potential dip buying opportunity"
    else:
        mood = "extreme fear — high volatility expected"

    top3 = [c["symbol"] for c in ranked[:3]]
    top3_str = ", ".join(f"${s}" for s in top3) if top3 else "no data"

    parts = [f"Market sentiment: {label} ({value})" if value else "Market sentiment: unknown"]
    parts.append(f"Mood: {mood}")
    parts.append(f"Trending: {top3_str}")

    if ranked:
        biggest_mover = max(ranked, key=lambda x: abs(x.get("change_1h", 0)))
        ch = biggest_mover.get("change_1h", 0)
        if abs(ch) >= 3:
            direction = "up" if ch > 0 else "down"
            parts.append(f"${biggest_mover['symbol']} moved {abs(ch):.1f}% {direction} in 1h")

    return " | ".join(parts)


def run_prediction() -> dict[str, Any]:
    """Fetch data, score coins, save forecast. Returns forecast dict."""
    print("[trend_predictor] fetching market data...")

    brain     = _load_brain_params()
    brain_vel = brain.get("velocity_leaders", {})  # e.g. {"BTC": 32, "ETH": 20}

    trending  = _get_coingecko_trending()
    movers    = _get_coingecko_movers()
    fg        = _get_fear_greed()
    ranked    = _score_coins(trending, movers, brain_velocity=brain_vel)
    narrative = _build_narrative(fg, ranked)

    forecast = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "fear_greed":    fg,
        "narrative":     narrative,
        "top_coins":     ranked,
        "trending_raw":  trending,
        "movers_raw":    movers[:5],
    }

    (_REPO / "data").mkdir(parents=True, exist_ok=True)
    _FORECAST_FILE.write_text(
        json.dumps(forecast, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"[trend_predictor] saved -> {_FORECAST_FILE}")
    print(f"  Narrative: {narrative}")
    return forecast


def get_forecast(max_age_seconds: int = 3600) -> dict[str, Any]:
    """Return cached forecast (refresh if older than max_age_seconds)."""
    if _FORECAST_FILE.exists():
        try:
            data = json.loads(_FORECAST_FILE.read_text(encoding="utf-8"))
            ts  = datetime.fromisoformat(data.get("generated_at", "2000-01-01T00:00:00+00:00"))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age < max_age_seconds:
                return data
        except Exception:
            pass
    return run_prediction()


def get_top_tickers(n: int = 5) -> list[str]:
    """Quick helper: return top N ticker symbols from the forecast."""
    fc = get_forecast()
    return [c["symbol"] for c in fc.get("top_coins", [])[:n]]


if __name__ == "__main__":
    sys.path.insert(0, str(_REPO))
    fc = run_prediction()
    print(f"\nTop 5 coins to watch:")
    for c in fc["top_coins"][:5]:
        print(f"  ${c['symbol']:8} score={c['final_score']:5.1f}  1h={c.get('change_1h', 0):+.1f}%")
    print(f"\nFear & Greed: {fc['fear_greed']['label']} ({fc['fear_greed']['value']})")
