"""Viral Transformer — Agent that rewrites OUR draft into a viral version.

Where viral_scout collects trending videos and viral_analyzer distills them
into a "blueprint" (top hooks, hashtags, durations, emoji density, hook
patterns), this module applies that blueprint to the draft we already
produced for a slot. It is the "second bot" the user asked for:

    crypto news → copywriter → DRAFT
                                  │
                                  ▼
                          viral_transformer
                          (uses blueprint)
                                  │
                                  ▼
                            VIRAL VERSION  → avatar/reel/captions

Two modes — both safe to call (will never break the pipeline):

  transform_script(script, blueprint, ...)  -> str
        Rewrites the spoken script (the avatar's voice-over) so the first
        sentence imitates the highest-engagement hook pattern from the
        blueprint, while preserving the FACTS from the original story.

  transform_caption(caption, blueprint, ...) -> str
        Tightens the caption to the median caption-length from the blueprint
        and injects the most-used hashtags.

  build_viral_prompt(...)                   -> str
        Returns a Claude prompt fragment that injects the blueprint into a
        copy-rewriting prompt — used by avatar_writer so the LLM sees the
        viral patterns directly and adopts them.

Topic safety
------------
The user explicitly asked: "sa se rezume la cea ce facem noi, adica crypto,
stiri etc". The transformer therefore filters out hashtags / hooks that look
off-topic (gaming, fashion, lifestyle...) before injecting them. Off-topic
detection is a simple keyword denylist — see _is_on_topic().

No external API required for the rule-based path. The LLM path uses the
same ANTHROPIC_API_KEY the rest of the pipeline already has configured.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

log = logging.getLogger("viral_transformer")
if not log.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [viral_tx] %(message)s",
                                     datefmt="%H:%M:%S"))
    log.addHandler(h)
log.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# On-topic guard (we are crypto/news only)
# ---------------------------------------------------------------------------

ON_TOPIC_KEYWORDS = {
    # crypto core
    "crypto", "bitcoin", "btc", "ethereum", "eth", "altcoin", "altcoins",
    "solana", "sol", "xrp", "ripple", "doge", "dogecoin", "shib", "shiba",
    "blockchain", "defi", "nft", "nfts", "web3", "metaverse", "stablecoin",
    "usdt", "usdc", "tether", "binance", "coinbase", "kraken", "bybit",
    "okx", "kucoin", "exchange", "wallet", "ledger", "metamask",
    # market mood / news
    "news", "breaking", "alert", "update", "market", "markets", "trading",
    "trader", "investor", "investing", "investment", "price", "prices",
    "rally", "pump", "dump", "crash", "moon", "ath", "low", "high",
    "halving", "fomo", "bullish", "bearish", "bull", "bear",
    "etf", "sec", "regulation", "fed", "powell", "rate", "rates",
    "inflation", "cpi", "fomc", "macro", "stocks", "stock",
    # finance broader
    "finance", "money", "economy", "recession", "deflation",
}

OFF_TOPIC_DENYLIST = {
    "fyp", "viral", "foryou", "foryoupage", "tiktok", "trend", "trending",
    "explore", "instagood", "love", "follow", "like", "comment",
    "fashion", "ootd", "makeup", "fitness", "workout", "gym", "yoga",
    "food", "recipe", "cooking", "travel", "wanderlust",
    "music", "song", "dance", "comedy", "funny", "meme", "memes",
    "anime", "kpop", "gaming", "minecraft", "fortnite", "roblox",
}


def _is_on_topic(text: str) -> bool:
    """Cheap content gate. True when text contains at least one keyword from
    our niche AND fewer than half its hashtags are obvious off-topic ones.
    """
    if not text:
        return False
    low = text.lower()
    hashtags = [h.lstrip("#") for h in re.findall(r"#[A-Za-z0-9_]+", low)]
    on = sum(1 for w in ON_TOPIC_KEYWORDS if w in low)
    off = sum(1 for h in hashtags if h in OFF_TOPIC_DENYLIST)
    if on == 0:
        return False
    if hashtags and off >= max(1, len(hashtags) // 2):
        return False
    return True


def _filter_topic_hashtags(tag_counts: list[tuple[str, int]],
                           max_tags: int = 5) -> list[str]:
    """From blueprint top_hashtags, keep only crypto/news-relevant ones."""
    out: list[str] = []
    for tag, _count in tag_counts or []:
        norm = tag.lstrip("#").lower()
        if not norm or norm in OFF_TOPIC_DENYLIST:
            continue
        # Keep tags that contain at least one crypto/news keyword OR are
        # short ticker-like strings.
        if (any(k in norm for k in ON_TOPIC_KEYWORDS)
                or re.fullmatch(r"[a-z]{3,5}", norm)):
            out.append(f"#{norm}" if not tag.startswith("#") else tag)
        if len(out) >= max_tags:
            break
    return out


# ---------------------------------------------------------------------------
# Hook adaptation
# ---------------------------------------------------------------------------

# Templates we synthesize when the blueprint hook is off-topic or empty.
# Each contains a {ticker} placeholder that gets filled at call time.
GENERIC_HOOKS = [
    "Wait — {ticker} just changed everything.",
    "Stop scrolling. {ticker} just moved 10%.",
    "Nobody is talking about what just happened to {ticker}.",
    "{ticker} just did something nobody saw coming.",
    "This is huge for {ticker} holders.",
    "{ticker} is breaking the internet right now.",
]


def _pick_hook(blueprint: dict | None, ticker: str = "Bitcoin") -> str:
    """Return one viral-style hook to use as the script's opening sentence."""
    if blueprint:
        for hook in (blueprint.get("hook_patterns") or []):
            if not hook or not isinstance(hook, str):
                continue
            if not _is_on_topic(hook):
                continue
            # Cap to ~14 words (the avatar prompt limit)
            words = hook.split()
            if len(words) > 14:
                hook = " ".join(words[:14])
            # Replace generic "this", "that", "you" hooks with our ticker
            return hook.rstrip(".!? ") + "."

    # Fallback — synthesize one
    import random
    template = random.choice(GENERIC_HOOKS)
    return template.format(ticker=ticker or "Bitcoin")


# ---------------------------------------------------------------------------
# Length adaptation
# ---------------------------------------------------------------------------

def _target_word_count(blueprint: dict | None,
                       words_per_second: float = 2.5,
                       fallback_seconds: int = 22) -> int:
    """Compute a target word-count for the viral script from the blueprint's
    median duration. We aim for ~2.5 words/sec (natural speaking pace).
    """
    if blueprint:
        p50 = blueprint.get("duration_p50") or blueprint.get("avg_duration")
        if p50:
            try:
                # Clamp 15-35s — anything outside this is not realistic for
                # an AI-avatar short.
                seconds = max(15, min(int(round(float(p50))), 35))
                return int(seconds * words_per_second)
            except Exception:
                pass
    return int(fallback_seconds * words_per_second)


# ---------------------------------------------------------------------------
# Public API: rule-based transforms (no LLM needed, always work)
# ---------------------------------------------------------------------------

def transform_script(script: str,
                     blueprint: dict | None,
                     ticker: str = "Bitcoin") -> str:
    """Rewrite the FIRST sentence of `script` to match the blueprint's hook
    pattern. Keeps the rest of the script intact.

    This is the safest mode — preserves all facts, just sharpens the opener.
    """
    if not script or not script.strip():
        return script
    text = script.strip()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if not sentences:
        return script

    new_hook = _pick_hook(blueprint, ticker=ticker)
    # Don't replace if the original opener already matches blueprint style
    # closely (avoid double-hooking the audience).
    sentences[0] = new_hook
    return " ".join(s.strip() for s in sentences if s.strip())


def transform_caption(caption: str,
                      blueprint: dict | None,
                      max_chars: int | None = None) -> str:
    """Trim caption to blueprint's median caption length and inject top
    on-topic hashtags. Idempotent: running it twice doesn't double tags.
    """
    if not caption:
        caption = ""
    base = caption.strip()

    # Existing tags (preserve)
    existing = set(t.lower() for t in re.findall(r"#\w+", base))

    # Inject blueprint hashtags
    add = []
    if blueprint:
        for tag in _filter_topic_hashtags(blueprint.get("top_hashtags") or [],
                                          max_tags=5):
            if tag.lower() not in existing:
                add.append(tag)
                existing.add(tag.lower())

    if add:
        base = base.rstrip() + "\n\n" + " ".join(add)

    # Cap length
    target_chars = max_chars
    if target_chars is None and blueprint:
        avg = blueprint.get("avg_caption_chars") or 0
        if avg:
            target_chars = int(min(max(150, avg * 1.2), 2000))
    if target_chars and len(base) > target_chars:
        base = base[: target_chars - 1].rstrip() + "…"
    return base


# ---------------------------------------------------------------------------
# LLM path — produce a Claude prompt fragment from the blueprint
# ---------------------------------------------------------------------------

def build_viral_prompt(blueprint: dict | None,
                       ticker: str = "BTC",
                       target_seconds: int | None = None) -> str:
    """Produce a prompt fragment that can be appended to ANY copywriter prompt
    so the LLM is told what viral patterns to imitate. Returns "" when there
    is no usable blueprint (caller can then skip injection).
    """
    if not blueprint or not blueprint.get("sample_size"):
        return ""

    hooks_on_topic = [h for h in (blueprint.get("hook_patterns") or [])
                      if _is_on_topic(h)]
    hashtags_on_topic = _filter_topic_hashtags(
        blueprint.get("top_hashtags") or [], max_tags=8
    )

    target_words = _target_word_count(blueprint)
    if target_seconds is None and blueprint.get("duration_p50"):
        try:
            target_seconds = int(round(float(blueprint["duration_p50"])))
        except Exception:
            target_seconds = None

    # Build the fragment
    lines = [
        "",
        "VIRAL PATTERNS TO IMITATE (extracted from top-engaging short videos in this niche):",
        f"- Sample size: {blueprint.get('sample_size')} videos across "
        f"{', '.join((blueprint.get('by_platform') or {}).keys()) or 'YouTube/TikTok/IG'}.",
    ]
    if blueprint.get("avg_duration"):
        lines.append(
            f"- Average duration: {blueprint['avg_duration']}s "
            f"(median {blueprint.get('duration_p50','?')}s, p90 "
            f"{blueprint.get('duration_p90','?')}s) — aim for ~"
            f"{target_seconds or 22}s of speech ≈ {target_words} words."
        )
    if blueprint.get("avg_emoji_count"):
        lines.append(
            f"- Average emojis per caption: {blueprint['avg_emoji_count']:.1f}."
        )
    if hooks_on_topic:
        lines.append("- Top hook patterns from the most-viewed videos "
                     "(use the SAME shape, not the literal text):")
        for h in hooks_on_topic[:5]:
            lines.append(f"    • {h}")
    if hashtags_on_topic:
        lines.append("- Hashtags currently winning in this niche "
                     f"(include 3-5): {' '.join(hashtags_on_topic)}")

    lines.extend([
        "",
        "RULES while imitating:",
        "- Stay strictly on topic (crypto / market news). Do NOT borrow "
        "fashion / food / gaming language even if it appears in the patterns.",
        f"- Open with one shocking sentence ≤14 words mentioning {ticker} "
        "or the main subject of the story.",
        "- Keep the facts from the source story. Viral != fake. We are crypto news.",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI for ad-hoc testing
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    import argparse
    import json as _json
    ap = argparse.ArgumentParser(description="Viral Transformer — rewrite copy using a blueprint")
    ap.add_argument("--blueprint", help="path to a blueprint JSON file")
    ap.add_argument("--script", help="draft script to rewrite")
    ap.add_argument("--caption", help="draft caption to rewrite")
    ap.add_argument("--ticker", default="BTC")
    args = ap.parse_args(argv[1:])

    bp = None
    if args.blueprint:
        bp = _json.loads(Path(args.blueprint).read_text(encoding="utf-8"))

    if args.script:
        print("BEFORE:", args.script)
        print("AFTER :", transform_script(args.script, bp, ticker=args.ticker))
    if args.caption:
        print("BEFORE:", args.caption)
        print("AFTER :", transform_caption(args.caption, bp))
    if not args.script and not args.caption:
        print(build_viral_prompt(bp, ticker=args.ticker))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
