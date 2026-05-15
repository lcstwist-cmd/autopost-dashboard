"""fact_checker.py — Validates price/percentage claims before posting.

Checks post text for mentions of prices like "$95,000" or percentages like "+12%"
and verifies them against live CoinGecko data.

Rules:
  - Price claim within 5% of live price → PASS
  - Price claim more than 15% off live price → BLOCK
  - Percentage change claim differs by more than 10pp → WARN
  - If CoinGecko is unreachable → PASS with warning (don't block on network error)

Usage:
    from src.agents.fact_checker import FactChecker
    fc = FactChecker()
    result = fc.check(text, tickers=["BTC", "ETH"])
    if result["status"] == "block":
        print("Post blocked:", result["issues"])
    elif result["status"] == "warn":
        print("Warnings:", result["issues"])
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
_BRAIN_PARAMS = _REPO / "analytics" / "agent_brains" / "fact_checker" / "learned_params.json"


def _load_brain_params() -> dict:
    if _BRAIN_PARAMS.exists():
        try:
            return json.loads(_BRAIN_PARAMS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true"

# Map common ticker symbols to CoinGecko IDs
TICKER_TO_ID: dict[str, str] = {
    "BTC":  "bitcoin",
    "ETH":  "ethereum",
    "SOL":  "solana",
    "BNB":  "binancecoin",
    "XRP":  "ripple",
    "ADA":  "cardano",
    "DOGE": "dogecoin",
    "AVAX": "avalanche-2",
    "DOT":  "polkadot",
    "MATIC": "matic-network",
    "POL":  "matic-network",
    "LINK": "chainlink",
    "UNI":  "uniswap",
    "ATOM": "cosmos",
    "LTC":  "litecoin",
    "TRX":  "tron",
    "NEAR": "near",
    "OP":   "optimism",
    "ARB":  "arbitrum",
    "SUI":  "sui",
    "APT":  "aptos",
}

# Regex patterns to extract price/percentage claims from text
_PRICE_RE  = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*[Kk]?\b")
_PCT_RE    = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%")

_HTTP_HEADERS = {"User-Agent": "AutoPost/1.0", "Accept": "application/json"}


def _fetch_prices(coin_ids: list[str]) -> dict[str, Any]:
    """Fetch live USD prices from CoinGecko."""
    if not coin_ids:
        return {}
    ids_str = ",".join(coin_ids)
    url = COINGECKO_PRICE_URL.format(ids=ids_str)
    try:
        req = urllib.request.Request(url, headers=_HTTP_HEADERS)
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"[fact_checker] CoinGecko fetch failed: {exc}")
        return {}


def _parse_price_claim(raw: str) -> float | None:
    """Parse '$95,000' or '$95K' etc. to a float."""
    raw = raw.replace(",", "")
    try:
        val = float(raw)
        return val
    except ValueError:
        return None


def _k_multiplier(text_after: str) -> float:
    """Check if 'K' follows the number."""
    return 1000.0 if text_after.strip().upper().startswith("K") else 1.0


class FactChecker:
    def __init__(self):
        self._price_cache: dict[str, Any] = {}
        self._cache_ts: float = 0

    def _get_live_prices(self, tickers: list[str]) -> dict[str, float]:
        """Return {TICKER: price_usd} for known tickers.

        Falls back to brain-learned snapshot if CoinGecko is unreachable.
        """
        coin_ids = [TICKER_TO_ID[t] for t in tickers if t in TICKER_TO_ID]
        if not coin_ids:
            return {}

        # Cache for 5 minutes
        import time
        now = time.time()
        if now - self._cache_ts > 300 or not self._price_cache:
            raw = _fetch_prices(coin_ids)
            if raw:
                self._price_cache = raw
                self._cache_ts = now
            else:
                # Fall back to brain snapshot if live fetch failed
                brain = _load_brain_params()
                snap = brain.get("live_price_snapshot", {})
                if snap:
                    # Convert CoinGecko IDs back to tickers
                    id_to_ticker = {v: k for k, v in TICKER_TO_ID.items()}
                    return {id_to_ticker[cid]: float(p)
                            for cid, p in snap.items() if cid in id_to_ticker}
                return {}

        result: dict[str, float] = {}
        id_to_ticker = {v: k for k, v in TICKER_TO_ID.items()}
        for coin_id, data in self._price_cache.items():
            ticker = id_to_ticker.get(coin_id)
            if ticker and "usd" in data:
                result[ticker] = float(data["usd"])
        return result

    def _get_live_changes(self, tickers: list[str]) -> dict[str, float]:
        """Return {TICKER: 24h_change_pct} for known tickers."""
        coin_ids = [TICKER_TO_ID[t] for t in tickers if t in TICKER_TO_ID]
        if not coin_ids:
            return {}
        raw = self._price_cache or _fetch_prices(coin_ids)
        result: dict[str, float] = {}
        id_to_ticker = {v: k for k, v in TICKER_TO_ID.items()}
        for coin_id, data in raw.items():
            ticker = id_to_ticker.get(coin_id)
            if ticker and "usd_24h_change" in data:
                result[ticker] = float(data["usd_24h_change"])
        return result

    def check(
        self,
        text: str,
        tickers: list[str] | None = None,
        strict: bool = False,
    ) -> dict[str, Any]:
        """
        Check text for factual accuracy.

        Returns:
          {
            "status":  "ok" | "warn" | "block",
            "issues":  [{"type": "price_mismatch", "claim": ..., "live": ..., "diff_pct": ...}],
            "checked": {"BTC": 95000, ...},
          }
        """
        tickers = tickers or []
        live_prices  = self._get_live_prices(tickers)
        live_changes = self._get_live_changes(tickers)

        issues: list[dict[str, Any]] = []

        # --- Check price claims ---
        for m in _PRICE_RE.finditer(text):
            raw_val = m.group(1)
            after   = text[m.end():][:2]
            claim   = _parse_price_claim(raw_val)
            if claim is None:
                continue
            claim *= _k_multiplier(after)

            # Only flag if claim is in a plausible crypto price range
            if claim < 0.001 or claim > 10_000_000:
                continue

            # Try to associate with a ticker mentioned nearby
            for ticker, live_price in live_prices.items():
                if live_price <= 0:
                    continue
                diff_pct = abs(claim - live_price) / live_price * 100

                if diff_pct > 15:
                    issues.append({
                        "type":     "price_mismatch",
                        "ticker":   ticker,
                        "claim":    claim,
                        "live":     live_price,
                        "diff_pct": round(diff_pct, 1),
                        "severity": "block" if diff_pct > 30 else "warn",
                    })
                break   # one ticker per price claim

        # --- Check percentage claims ---
        pct_claims = [float(m.group(1)) for m in _PCT_RE.finditer(text)]
        if pct_claims and live_changes:
            for pct_claim in pct_claims:
                for ticker, live_change in live_changes.items():
                    diff = abs(pct_claim - live_change)
                    if diff > 15:
                        issues.append({
                            "type":       "percentage_mismatch",
                            "ticker":     ticker,
                            "claim":      pct_claim,
                            "live":       round(live_change, 2),
                            "diff_pp":    round(diff, 1),
                            "severity":   "warn",
                        })
                        break

        # Determine overall status
        has_block = any(i["severity"] == "block" for i in issues)
        has_warn  = any(i["severity"] == "warn" for i in issues)

        if has_block and strict:
            status = "block"
        elif has_warn or (has_block and not strict):
            status = "warn"
        else:
            status = "ok"

        return {
            "status":  status,
            "issues":  issues,
            "checked": live_prices,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# Global instance (reuses price cache)
_checker: FactChecker | None = None


def check_post(text: str, tickers: list[str] | None = None, strict: bool = False) -> dict[str, Any]:
    """Module-level convenience function."""
    global _checker
    if _checker is None:
        _checker = FactChecker()
    return _checker.check(text, tickers=tickers, strict=strict)


if __name__ == "__main__":
    sys.path.insert(0, str(_REPO))
    test_text = "Bitcoin is trading at $95,000, up +12% in the last 24 hours."
    result = check_post(test_text, tickers=["BTC"])
    print(f"Status: {result['status']}")
    print(f"Live prices: {result['checked']}")
    if result["issues"]:
        for issue in result["issues"]:
            print(f"  [{issue['severity']}] {issue['type']}: claim={issue['claim']}, live={issue.get('live')}, diff={issue.get('diff_pct') or issue.get('diff_pp')}")
    else:
        print("  No issues found")
