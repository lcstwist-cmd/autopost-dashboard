"""content_personalizer.py — Per-user content style and topic preferences.

Each user can configure:
  - niche:   "general" | "bitcoin" | "defi" | "nft" | "altcoins" | "trading"
  - tone:    "professional" | "casual" | "hype" | "educational"
  - language: "en" | "ro" | "es" | "de" | "fr"
  - preferred_tickers: ["BTC", "ETH", "SOL", ...]
  - excluded_tickers:  ["DOGE", "SHIB", ...]  — never post about these
  - min_relevance_score: 0–100 — skip stories below this threshold

Settings are read from the user's DB settings dict.
This module provides helpers used by pipeline.py / ranker.py.

Usage:
    from src.agents.content_personalizer import Personalizer
    p = Personalizer(settings)
    stories = p.filter_stories(stories)
    prompt_extras = p.get_prompt_extras()
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
_BRAIN_PARAMS = _REPO / "analytics" / "agent_brains" / "content_personalizer" / "learned_params.json"


def _load_brain_params() -> dict:
    if _BRAIN_PARAMS.exists():
        try:
            return json.loads(_BRAIN_PARAMS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

# Default persona used when user hasn't configured anything
DEFAULT_PERSONA = {
    "niche":               "general",
    "tone":                "professional",
    "language":            "en",
    "preferred_tickers":   [],
    "excluded_tickers":    [],
    "min_relevance_score": 0,
    "max_posts_per_day":   4,
    "emoji_style":         "moderate",   # none | minimal | moderate | heavy
}

NICHE_KEYWORDS: dict[str, list[str]] = {
    "bitcoin":  ["bitcoin", "btc", "lightning", "taproot", "satoshi", "halving"],
    "defi":     ["defi", "protocol", "tvl", "yield", "liquidity", "amm", "dex",
                 "uniswap", "aave", "compound", "curve", "lending", "staking"],
    "nft":      ["nft", "opensea", "collection", "mint", "floor price", "pfp",
                 "ordinals", "blur", "jpeg"],
    "altcoins": ["altcoin", "alt season", "small cap", "mid cap", "gem",
                 "solana", "avalanche", "polygon", "bnb", "cardano"],
    "trading":  ["trade", "short", "long", "liquidation", "futures", "perp",
                 "leverage", "technical", "support", "resistance", "breakout",
                 "binance", "bybit", "okx"],
    "general":  [],   # no filter — accept all
}

TONE_PROMPT: dict[str, str] = {
    "professional": "Write in a clear, professional tone. Use precise language. No slang.",
    "casual":       "Write in a friendly, casual tone. Conversational but informative.",
    "hype":         "Write with high energy and enthusiasm. Use crypto community language. Bold claims.",
    "educational":  "Write in a clear educational tone. Explain concepts simply for beginners.",
}

EMOJI_STYLE: dict[str, str] = {
    "none":     "Use no emojis.",
    "minimal":  "Use at most 1 emoji at the start.",
    "moderate": "Use 2–3 relevant emojis throughout the post.",
    "heavy":    "Use emojis freely to make the post visually engaging.",
}

LANGUAGE_PROMPT: dict[str, str] = {
    "en": "Write in English.",
    "ro": "Write in Romanian.",
    "es": "Write in Spanish.",
    "de": "Write in German.",
    "fr": "Write in French.",
}


class Personalizer:
    def __init__(self, settings: dict[str, Any] | None = None):
        self.s = {**DEFAULT_PERSONA, **(settings or {})}
        # Normalize tickers to uppercase
        self.s["preferred_tickers"] = [t.upper() for t in self.s.get("preferred_tickers", [])]
        self.s["excluded_tickers"]  = [t.upper() for t in self.s.get("excluded_tickers", [])]

    # ------------------------------------------------------------------
    # Story filtering
    # ------------------------------------------------------------------

    def filter_stories(self, stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Filter + re-rank stories based on user preferences."""
        niche     = self.s["niche"]
        preferred = set(self.s["preferred_tickers"])
        excluded  = set(self.s["excluded_tickers"])
        min_score = self.s["min_relevance_score"]

        filtered = []
        for story in stories:
            title_lower = (story.get("title", "") + " " + story.get("summary", "")).lower()
            tickers = {t.upper() for t in (story.get("tickers") or [])}

            # Skip excluded tickers
            if excluded and tickers & excluded:
                continue

            # Skip if below minimum relevance score
            if story.get("score", 100) < min_score:
                continue

            # Niche filter
            if niche != "general":
                keywords = NICHE_KEYWORDS.get(niche, [])
                ticker_match = bool(preferred & tickers) if preferred else False
                keyword_match = any(kw in title_lower for kw in keywords)
                if not ticker_match and not keyword_match:
                    continue

            # Boost score for preferred tickers
            if preferred and tickers & preferred:
                story = {**story, "score": story.get("score", 50) + 20}

            filtered.append(story)

        # Sort by score descending
        filtered.sort(key=lambda x: x.get("score", 0), reverse=True)
        return filtered

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def get_prompt_extras(self) -> str:
        """Return extra instructions to append to the copywriter prompt.

        Brain-learned dominant_tone and trending_niche_now are used if the user
        hasn't explicitly set a preference.
        """
        parts: list[str] = []
        brain = _load_brain_params()

        # Use brain-recommended tone if user hasn't overridden
        tone = self.s.get("tone", "professional")
        if tone == "professional" and brain.get("dominant_tone"):
            tone = brain["dominant_tone"]
        parts.append(TONE_PROMPT.get(tone, TONE_PROMPT["professional"]))

        lang = self.s.get("language", "en")
        parts.append(LANGUAGE_PROMPT.get(lang, LANGUAGE_PROMPT["en"]))

        emoji = self.s.get("emoji_style", "moderate")
        parts.append(EMOJI_STYLE.get(emoji, EMOJI_STYLE["moderate"]))

        preferred = self.s.get("preferred_tickers", [])
        if preferred:
            parts.append(f"Prioritize mentions of: {', '.join('$' + t for t in preferred[:5])}.")

        niche = self.s.get("niche", "general")
        if niche == "general" and brain.get("trending_niche_now"):
            niche = brain["trending_niche_now"]
        if niche != "general":
            parts.append(f"Focus on the {niche} niche of crypto.")

        # Brain-learned trending niche hint
        live_niche = brain.get("trending_niche_now")
        if live_niche and live_niche != niche:
            parts.append(f"Note: {live_niche} content is trending right now — consider including.")

        return "\n".join(parts)

    def get_language(self) -> str:
        return self.s.get("language", "en")

    def get_max_posts_per_day(self) -> int:
        return int(self.s.get("max_posts_per_day", 4))

    def summary(self) -> dict[str, Any]:
        return {
            "niche":    self.s["niche"],
            "tone":     self.s["tone"],
            "language": self.s["language"],
            "preferred": self.s["preferred_tickers"],
            "excluded":  self.s["excluded_tickers"],
        }


def get_personalizer_for_user(user_id: int) -> Personalizer:
    """Load user settings from DB and return a Personalizer."""
    try:
        if str(_REPO) not in sys.path:
            sys.path.insert(0, str(_REPO))
        from src.dashboard.database import get_user_settings
        settings = get_user_settings(user_id)
        return Personalizer(settings)
    except Exception:
        return Personalizer()


if __name__ == "__main__":
    sys.path.insert(0, str(_REPO))
    p = Personalizer({
        "niche": "defi",
        "tone": "hype",
        "language": "ro",
        "preferred_tickers": ["ETH", "SOL"],
        "emoji_style": "heavy",
    })
    print("Persona:", json.dumps(p.summary(), indent=2))
    print("\nPrompt extras:")
    print(p.get_prompt_extras())

    test_stories = [
        {"title": "Uniswap v4 launches on mainnet", "score": 80, "tickers": ["UNI", "ETH"]},
        {"title": "Bitcoin halving analysis", "score": 90, "tickers": ["BTC"]},
        {"title": "Solana DeFi TVL hits $10B", "score": 75, "tickers": ["SOL"]},
    ]
    filtered = p.filter_stories(test_stories)
    print(f"\nFiltered {len(test_stories)} -> {len(filtered)} stories for niche=defi")
    for s in filtered:
        print(f"  [{s['score']}] {s['title']}")
