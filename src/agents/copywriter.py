"""Copywriter — Agent 3 of the Crypto AutoPost pipeline.

Template-driven (no API, no cost). Extracts facts from the ranked story and fills
pre-built skeletons for each platform.

Produces:
  * post_x.txt         — variant A factual hook, ≤280 weighted chars
  * post_x_b.txt       — variant B question hook, ≤280 weighted chars
  * post_telegram.md   — short punchy post with bold headline + 2 bullets
  * image_prompt.txt   — brief for the image generator

Usage:
  python src/agents/copywriter.py queue/2026-04-19_morning/top2.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


# --- Character weighting (Twitter/X) ---------------------------------------

def x_weighted_len(text: str) -> int:
    """X counts emoji/CJK as 2, most other chars as 1."""
    count = 0
    for ch in text:
        cp = ord(ch)
        if (cp >= 0x1F000 or
            0x2E80 <= cp <= 0x9FFF or
            0xA000 <= cp <= 0xD7FF or
            0xF900 <= cp <= 0xFAFF):
            count += 2
        else:
            count += 1
    return count


# --- Story helpers ---------------------------------------------------------

def _tone(story: dict) -> str:
    title = story.get("title", "").lower()
    bear = ("hack", "exploit", "drain", "stolen", "crash", "plunge",
            "liquidat", "rejected", "halt", "freeze", "breach", "collapse")
    bull = ("record", "all-time high", "ath", "surge", "rally", "approval",
            "approved", "launch", "inflow", "accumulation", "bullish")
    if any(k in title for k in bear):
        return "bear"
    if any(k in title for k in bull):
        return "bull"
    return "neutral"


def _emoji(tone: str) -> str:
    return {"bear": "🚨", "bull": "📈", "neutral": "📰"}.get(tone, "📰")


def _extract_numbers(text: str) -> list[str]:
    patterns = [
        r"\$[\d,]+(?:\.\d+)?[MBKmb]?",
        r"\b\d+(?:,\d{3})+\b",
        r"\b\d+(?:\.\d+)?%",
        r"\b\d+(?:\.\d+)?[MBKmb]\b",
    ]
    seen: set[str] = set()
    result: list[str] = []
    for pat in patterns:
        for m in re.findall(pat, text):
            if m not in seen:
                seen.add(m)
                result.append(m)
    return result


def _ticker(story: dict) -> str:
    tickers = story.get("tickers") or []
    return tickers[0] if tickers else ""


_TONE_HASHTAGS = {
    "bear": [
        "#CryptoCrash", "#BearMarket", "#CryptoAlert", "#RiskOff",
        "#CryptoNews", "#DYOR", "#CryptoTrading",
    ],
    "bull": [
        "#BullMarket", "#CryptoRally", "#ToTheMoon", "#CryptoBull",
        "#CryptoNews", "#DYOR", "#CryptoTrading",
    ],
    "neutral": [
        "#CryptoUpdate", "#MarketWatch", "#OnChain",
        "#CryptoNews", "#DYOR", "#CryptoTrading",
    ],
}

# Credible sources that boost authority when named in the hook
_AUTHORITY_SOURCES = {
    "chainalysis", "coindesk", "bloomberg", "reuters", "cointelegraph",
    "the block", "theblock", "decrypt", "binance", "coinbase", "kraken",
    "messari", "glassnode", "nansen", "dune", "defilama", "defillama",
    "federal reserve", "sec", "cftc", "bis", "imf", "blackrock", "fidelity",
    "grayscale", "microstrategy", "ark invest", "jpmorgan", "goldman",
}


def _source_prefix(source: str, title: str) -> str:
    """Return 'Source: ' prefix if source is authoritative AND not already in title."""
    # If title already starts with "SourceName: ...", don't duplicate
    if re.match(r"^[A-Z][a-zA-Z\s]{2,25}:\s", title):
        return ""
    s = source.lower().strip()
    for auth in _AUTHORITY_SOURCES:
        if auth in s:
            name = source.strip().title()[:20]
            return f"{name}: "
    return ""


_KEYWORD_HASHTAGS: dict[str, str] = {
    "bitcoin":     "#Bitcoin",
    "btc":         "#Bitcoin",
    "ethereum":    "#Ethereum",
    "eth":         "#Ethereum",
    "solana":      "#Solana",
    "sol":         "#Solana",
    "xrp":         "#XRP",
    "ripple":      "#Ripple",
    "defi":        "#DeFi",
    "nft":         "#NFT",
    "etf":         "#ETF",
    "sec":         "#SEC",
    "halving":     "#BitcoinHalving",
    "mining":      "#CryptoMining",
    "stablecoin":  "#Stablecoins",
    "layer":       "#Layer2",
    "web3":        "#Web3",
    "altcoin":     "#Altcoins",
    "blockchain":  "#Blockchain",
    "whale":       "#CryptoWhales",
    "hack":        "#CryptoSecurity",
    "exploit":     "#CryptoSecurity",
}


def _viral_top_hashtags(blueprint: dict | None, limit: int = 5) -> list[str]:
    """Pull trending hashtags from the viral blueprint, properly cased.

    Blueprint stores them lowercased (e.g. '#crypto'); we re-case the leading
    word to TitleCase to match the rest of our output.
    """
    if not blueprint:
        return []
    out: list[str] = []
    for tag, _count in (blueprint.get("top_hashtags") or [])[:limit]:
        if not tag.startswith("#"):
            continue
        # '#crypto' -> '#Crypto', '#btcalert' -> '#BTCAlert' (don't try to be too smart)
        body = tag[1:]
        # Crude tickers
        if body.isupper() or len(body) <= 4:
            out.append(f"#{body.upper()}")
        else:
            out.append(f"#{body[:1].upper()}{body[1:]}")
    return out


def _hashtags(story: dict, tone: str = "neutral", min_tags: int = 10,
              viral_blueprint: dict | None = None) -> str:
    """Generate cashtags ($) + hashtags (#) ordered by discoverability.

    Order matters because `ensure_x_budget` trims from the right when the
    280-char limit kicks in. So we put the highest-ROI tags first:
      1. Cashtags ($BTC, $ETH) — highest CTR on X for crypto
      2. Ticker hashtags (#BTC, #Bitcoin) — strong but redundant with cashtags
      3. Trending tags from viral_blueprint — ride existing discovery curves
      4. Keyword-based tags inferred from title/summary
      5. Tone tags (bear/bull/neutral pool)
      6. Universal fallback to reach min_tags

    De-dup is case-insensitive but we preserve the first-seen casing.
    """
    seen_lower: set[str] = set()
    tags: list[str] = []

    def _add(tag: str) -> None:
        key = tag.lower()
        if key not in seen_lower:
            seen_lower.add(key)
            tags.append(tag)

    # 1) Cashtags first — most clickable on X for crypto/finance.
    raw_tickers = [t.strip().upper() for t in (story.get("tickers") or []) if t and t.strip()]
    for t in raw_tickers[:3]:
        _add(f"${t}")

    # 2) Ticker hashtags (twin to cashtags) + full-name hashtag if known.
    name_map = {
        "BTC": "#Bitcoin", "ETH": "#Ethereum", "SOL": "#Solana",
        "XRP": "#XRP", "BNB": "#BNB", "ADA": "#Cardano",
        "DOGE": "#Dogecoin", "AVAX": "#Avalanche", "DOT": "#Polkadot",
        "MATIC": "#Polygon", "LINK": "#Chainlink", "LTC": "#Litecoin",
        "ARB": "#Arbitrum", "OP": "#Optimism", "ATOM": "#Cosmos",
        "TON": "#Toncoin", "TRX": "#Tron", "SHIB": "#ShibaInu",
    }
    for t in raw_tickers[:4]:
        _add(f"#{t}")
        if t in name_map:
            _add(name_map[t])

    # 3) Trending tags from viral blueprint — early so they survive the trim.
    for vt in _viral_top_hashtags(viral_blueprint, limit=4):
        _add(vt)

    # 4) Keyword-based hashtags from title + summary
    haystack = (story.get("title", "") + " " + (story.get("summary") or "")).lower()
    for kw, tag in _KEYWORD_HASHTAGS.items():
        if kw in haystack:
            _add(tag)

    # 5) Tone-based pool
    for tag in _TONE_HASHTAGS.get(tone, _TONE_HASHTAGS["neutral"]):
        _add(tag)

    # 6) Universal fallback
    universal = [
        "#Crypto", "#CryptoMarket", "#CryptoInvesting", "#DigitalAssets",
        "#Web3", "#Blockchain", "#Altcoins", "#CryptoTrader",
        "#CommunityAlert", "#EliteMarginDesk",
    ]
    for tag in universal:
        if len(tags) >= min_tags:
            break
        _add(tag)

    return " ".join(tags)


def _clean_summary(text: str) -> str:
    """Remove common RSS/WordPress artifacts from summary text."""
    if not text:
        return ""
    # Remove "The post ... appeared first on ..." WordPress footer
    text = re.sub(r"The post .{0,120} appeared first on .+$", "", text, flags=re.DOTALL)
    # Remove "[…]" read-more truncation markers
    text = re.sub(r"\[…\]|\[\.{3}\]|\s*\.\.\.\s*$", "", text)
    # Remove "Read more" / "Continue reading" suffixes
    text = re.sub(r"\s*(Read more|Continue reading|Read More|Full story).*$", "", text)
    return text.strip()


def _first_sentence(text: str) -> str:
    text = _clean_summary(text)
    if not text:
        return ""
    m = re.split(r"(?<=[.!?])\s+", text.strip())
    return m[0] if m else text[:120]


# --- Template generator ----------------------------------------------------

def generate_template(story: dict, viral_blueprint: dict | None = None,
                      niche: str = "crypto") -> dict:
    title = story.get("title", "")
    summary = _clean_summary(story.get("summary") or "")
    source = story.get("source", "")
    tone = _tone(story)
    emoji = _emoji(tone)
    ticker = _ticker(story)
    # For non-crypto niches use niche-specific hashtags; for crypto use existing logic
    if niche and niche != "crypto":
        try:
            from src.agents.niches import niche_hashtags, niche_tone
        except ImportError:
            from niches import niche_hashtags, niche_tone  # type: ignore
        tone = niche_tone(story, niche)
        tags = niche_hashtags(niche, tone, limit=12)
        tags_x = niche_hashtags(niche, tone, limit=4)  # tighter for X 280-char limit
        cashtag_line = ""
        hashtag_line = tags
    else:
        all_tags_str = _hashtags(story, tone=tone, min_tags=10, viral_blueprint=viral_blueprint)
        all_tags_list = all_tags_str.split()
        cashtag_line = " ".join(t for t in all_tags_list if t.startswith("$"))
        hashtag_line = " ".join(t for t in all_tags_list if t.startswith("#"))
        # X performs best with 3-5 tags total — pick the highest-ROI ones:
        # all cashtags (max 3) + top 3 hashtags. Telegram keeps the full set.
        cash_x = [t for t in all_tags_list if t.startswith("$")][:3]
        hash_x = [t for t in all_tags_list if t.startswith("#")][:3]
        tags_x_parts: list[str] = []
        if cash_x:
            tags_x_parts.append(" ".join(cash_x))
        if hash_x:
            tags_x_parts.append(" ".join(hash_x))
        tags_x = "\n".join(tags_x_parts)
    tags = (cashtag_line + "\n" + hashtag_line).strip() if cashtag_line else hashtag_line
    numbers = _extract_numbers(f"{title} {summary}")
    big_num = next((n for n in numbers if "$" in n or "M" in n.upper()), None)
    first_sent = _first_sentence(summary)
    src_prefix = _source_prefix(source, title)

    # Pick up to 3 best sentences for the X body
    all_sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", summary) if len(s.strip()) > 30]
    impact_words = ("million", "billion", "$", "%", "hack", "exploit", "record",
                    "surge", "crash", "launch", "approval", "drained", "attack",
                    "halving", "etf", "sec", "ban", "upgrade", "listing")
    scored_sents = sorted(
        all_sents,
        key=lambda s: sum(1 for w in impact_words if w in s.lower()),
        reverse=True,
    )
    # Take top 3 unique sentences, cap each at 240 chars
    body_sents: list[str] = []
    for s in scored_sents:
        if s not in body_sents:
            body_sents.append(s if len(s) <= 240 else s[:239].rstrip() + "…")
        if len(body_sents) == 3:
            break
    summary_block = "\n\n".join(body_sents)

    # --- post_x_a: news summary — lead + 2 key facts + tags + CTA ---
    if niche and niche != "crypto":
        try:
            from src.agents.niches import get_niche as _gn3
        except ImportError:
            from niches import get_niche as _gn3  # type: ignore
        niche_label = _gn3(niche).get("label", niche.title()).split("&")[0].strip()
    else:
        niche_label = "Crypto"

    if big_num:
        lead_a = f"{emoji} {src_prefix}{big_num} — {ticker or niche_label}"
    else:
        max_title = 80 - len(src_prefix)
        short_title = title if len(title) <= max_title else title[:max_title - 1].rstrip(" ,;:") + "…"
        lead_a = f"{emoji} {src_prefix}{short_title}"

    # Build a mini-summary: up to 2 key sentences, no forced cliffhanger.
    # ensure_x_budget() will trim to 280 chars if needed.
    if body_sents:
        two = [s.rstrip(".!?") for s in body_sents[:2]]
        body_a = ". ".join(two) + "."
        if len(body_a) > 210:
            body_a = body_a[:209].rstrip(" ,;:") + "…"
    else:
        body_a = ""

    if niche and niche != "crypto":
        try:
            from src.agents.niches import get_niche as _gn
        except ImportError:
            from niches import get_niche as _gn  # type: ignore
        cta_x = _gn(niche).get("cta_x", "👉 Follow for daily updates")
    else:
        cta_x = "👉 @EliteMarginDesk"
    parts_a = [lead_a]
    if body_a:
        parts_a.append(body_a)
    if tags_x:
        parts_a.append(tags_x)
    parts_a.append(cta_x)
    post_x_a = "\n\n".join(parts_a)

    # --- post_x_b: high-tension question hook, key facts, hashtags, CTA ---
    subj = src_prefix.rstrip(": ") if src_prefix else (ticker or "Crypto")
    if tone == "bear":
        question = f"🚨 {subj} alert — is this the move you've been waiting for?"
    elif tone == "bull":
        question = f"🔥 {subj} pumping — are you positioned?"
    else:
        question = f"⚡ {subj} update — don't miss this:"
    # 1 sentence + tags + CTA — always cliffhanger
    best_sent_b = body_sents[0] if body_sents else title
    if best_sent_b:
        best_sent_b = best_sent_b if len(best_sent_b) <= 140 else best_sent_b[:139].rstrip()
        if not best_sent_b.endswith("…"):
            best_sent_b = best_sent_b.rstrip(".!?") + "…"
    parts_b = [question, best_sent_b]
    if tags_x:
        parts_b.append(tags_x)
    parts_b.append(cta_x)
    post_x_b = "\n\n".join(parts_b)

    # --- post_telegram: bold title + 2 key paragraphs + strong CTA footer ---
    tg_title = title if len(title) <= 90 else title[:89].rstrip(" ,;:") + "…"
    all_sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", summary) if len(s.strip()) > 30]
    impact_words = ("million", "billion", "$", "%", "hack", "exploit", "record",
                    "surge", "crash", "launch", "approval", "drained", "attack")
    scored_sents = sorted(
        all_sentences,
        key=lambda s: sum(1 for w in impact_words if w in s.lower()),
        reverse=True,
    )
    paras: list[str] = []
    for sent in scored_sents:
        if sent not in paras:
            paras.append(sent)
        if len(paras) == 2:
            break
    trimmed = []
    for p in paras:
        trimmed.append(p if len(p) <= 220 else p[:219].rstrip() + "…")
    para_block = "\n\n".join(trimmed)
    if niche and niche != "crypto":
        try:
            from src.agents.niches import get_niche as _gn2
        except ImportError:
            from niches import get_niche as _gn2  # type: ignore
        footer = _gn2(niche).get("cta_tg", "\n\n🔔 Follow for daily updates")
    else:
        footer = (
            "\n\n🔔 Follow the channel for daily crypto updates"
            "\n🌐 https://www.elitemargindesk.io?ref=7UBN23I0"
            "\n💬 @CryptohEMD"
        )
    post_tg = f"**{tg_title}**\n\n{para_block}{footer}" if para_block else f"**{tg_title}**{footer}"

    # --- image_prompt ---
    mood = tone
    pill1 = ticker or "CRYPTO"
    pill2 = f"{big_num}" if big_num else ("BREAKING" if tone == "bear" else "UPDATE")
    pill3 = source.upper()[:12] if source else ""
    subhead = first_sent if len(first_sent) <= 120 else first_sent[:119].rstrip() + "..."
    image_prompt = (
        f"HEADLINE: {tg_title}\n"
        f"SUBHEAD: {subhead}\n"
        f"MOOD: {mood}\n"
        f"PILL1: {pill1}\n"
        f"PILL2: {pill2}\n"
        + (f"PILL3: {pill3}\n" if pill3 else "")
        + f"TICKER: {ticker}\n"
    )

    # -----------------------------------------------------------------------
    # Platform-specific captions (TikTok / Instagram Reels / YouTube Shorts)
    # Each platform has a different hook style, hashtag density and CTA.
    # We pull per-platform top hashtags from the viral blueprint when available.
    # -----------------------------------------------------------------------

    def _plat_tags(platform: str, extra: int = 0) -> str:
        """Return hashtag string optimised for `platform`."""
        base = []
        # Per-platform top hashtag from blueprint
        if viral_blueprint:
            plat_data = (viral_blueprint.get("by_platform") or {}).get(platform, {})
            top_tag = (plat_data.get("top_hashtag") or [[]])[0]
            if top_tag and isinstance(top_tag, str) and top_tag.startswith("#"):
                base.append(top_tag)
            # Global top hashtags from blueprint (up to 8)
            for tag, _ in (viral_blueprint.get("top_hashtags") or [])[:8]:
                if tag not in base:
                    base.append(tag)
        # Fill with story-derived tags
        story_tags = [t for t in (hashtag_line or "").split() if t.startswith("#")]
        for t in story_tags:
            if t not in base:
                base.append(t)
        limit = 15 + extra
        return " ".join(base[:limit])

    def _best_hook_for_platform(platform: str) -> str:
        """Pick a viral hook pattern that fits this platform's style."""
        hooks = (viral_blueprint or {}).get("hook_patterns") or []
        plat_styles = {
            "tiktok":    ["pov", "pay attention", "this is why", "nobody", "just", "breaking"],
            "instagram": ["crossed", "pumping", "surged", "don't sleep", "this is what"],
            "youtube":   ["how", "why", "what is", "explained", "lost", "millionaire", "works"],
        }
        keywords = plat_styles.get(platform, [])
        for h in hooks:
            h_low = h.lower()
            if any(k in h_low for k in keywords):
                return h
        return hooks[0] if hooks else ""

    # -- TikTok caption -------------------------------------------------
    # Short, punchy, high urgency. Hook in first line. 12-15 hashtags.
    tt_hook_pattern = _best_hook_for_platform("tiktok")
    if tone == "bear":
        tt_hook = f"🚨 {title[:60].rstrip()} — watch out"
    elif tone == "bull":
        tt_hook = f"🔥 {ticker or 'Crypto'} is MOVING — here's why"
    else:
        tt_hook = f"⚡ {title[:60].rstrip()}"
    if tt_hook_pattern:
        # Shape our hook after the viral pattern (same structure, our facts)
        if "?" in tt_hook_pattern:
            tt_hook = f"Is {ticker or 'crypto'} about to change everything? 👀"
        elif tt_hook_pattern.lower().startswith("pov"):
            tt_hook = f"POV: you just found out about {ticker or 'crypto'} 📈"
        elif any(w in tt_hook_pattern.lower() for w in ["nobody", "secret", "warning"]):
            tt_hook = f"Nobody is talking about this {ticker or 'crypto'} move 🤫"
        elif any(c.isdigit() for c in tt_hook_pattern[:5]):
            tt_hook = f"3 things happening in {ticker or 'crypto'} RIGHT NOW 👇"
    tt_fact = body_sents[0][:120].rstrip() if body_sents else first_sent[:120]
    tt_cta = "Follow for daily crypto alpha 📲"
    tt_tags = _plat_tags("tiktok", extra=3)
    post_tiktok = f"{tt_hook}\n\n{tt_fact}\n\n{tt_cta}\n\n{tt_tags}"

    # -- Instagram Reels caption ----------------------------------------
    # Storytelling tone, save-worthy, 20+ hashtags, strong CTA.
    ig_hook_pattern = _best_hook_for_platform("instagram")
    if tone == "bear":
        ig_hook = f"🚨 {title[:70].rstrip()}"
    elif tone == "bull":
        ig_hook = f"📈 {title[:70].rstrip()}"
    else:
        ig_hook = f"💡 {title[:70].rstrip()}"
    if ig_hook_pattern and len(ig_hook_pattern) > 15:
        ig_hook = f"{emoji} {ig_hook_pattern[:80]}"
    ig_body_lines = []
    for s in body_sents[:3]:
        ig_body_lines.append(f"• {s[:180].rstrip()}")
    ig_body = "\n".join(ig_body_lines) if ig_body_lines else first_sent
    ig_cta = (
        "Save this for later 📌\n"
        "Follow @EliteMarginDesk for daily crypto intel 👇\n"
        "🌐 elitemargindesk.io"
    )
    ig_tags = _plat_tags("instagram", extra=5)
    post_ig = f"{ig_hook}\n\n{ig_body}\n\n{ig_cta}\n\n.\n.\n.\n{ig_tags}"

    # -- YouTube Shorts description -------------------------------------
    # Educational first line, 2-3 facts, subscribe CTA, few hashtags.
    yt_hook_pattern = _best_hook_for_platform("youtube")
    if yt_hook_pattern and len(yt_hook_pattern) > 10:
        yt_hook = yt_hook_pattern[:90]
    else:
        yt_hook = title[:90].rstrip()
    yt_body = " ".join(body_sents[:2])[:300].rstrip() if body_sents else summary[:300]
    yt_cta = "🔔 Subscribe for daily crypto news & analysis"
    yt_tags = " ".join(
        t for t in (["#Shorts", "#crypto", "#bitcoin"] +
                    [t for t, _ in (viral_blueprint or {}).get("top_hashtags") or []][:5])
        if t.startswith("#")
    )[:120]
    post_youtube = f"{yt_hook}\n\n{yt_body}\n\n{yt_cta}\n\n{yt_tags}"

    return {
        "post_x_a":      post_x_a,
        "post_x_b":      post_x_b,
        "post_telegram": post_tg,
        "image_prompt":  image_prompt,
        "post_tiktok":   post_tiktok,
        "post_ig":       post_ig,
        "post_youtube":  post_youtube,
    }


# --- Safety & post-processing ----------------------------------------------

def ensure_x_budget(post_x: str, hard_limit: int = 280) -> str:
    """Fit post into X's 280 weighted-char limit without looking amputated.

    Strategy (priority order, drop the least-important piece first):
      1. If it already fits — return unchanged.
      2. Drop hashtags one-by-one from the end until it fits.
      3. Truncate the last body paragraph (word-boundary + ellipsis).
      4. If still over, drop paragraphs from the end one by one.
      5. As last resort, hard-truncate the hook.
    The hook (first line) is preserved as long as possible.
    """
    if x_weighted_len(post_x) <= hard_limit:
        return post_x

    blocks = post_x.split("\n\n")  # lead / body / tags
    # Isolate trailing hashtag block if present.
    tags_block = ""
    if blocks and blocks[-1].strip().startswith("#"):
        tags_block = blocks.pop().strip()

    # 2) Trim hashtags one at a time from the right.
    tag_list = tags_block.split() if tags_block else []
    while tag_list:
        rebuilt = "\n\n".join(blocks + [" ".join(tag_list)])
        if x_weighted_len(rebuilt) <= hard_limit:
            return rebuilt
        tag_list.pop()

    # 3) If still over with lead+body only, try shrinking the last body paragraph
    #    before dropping it wholesale (keeps a sentence fragment instead of losing it).
    def _rebuild(bs: list[str]) -> str:
        return "\n\n".join(bs)

    while len(blocks) >= 2 and x_weighted_len(_rebuild(blocks)) > hard_limit:
        tail = blocks[-1]
        # Trim tail word-by-word from the end until it fits, leaving an ellipsis.
        budget = hard_limit - x_weighted_len(_rebuild(blocks[:-1])) - 4  # for "\n\n…"
        if budget < 30:
            # Not worth keeping a tiny scrap — drop the whole paragraph.
            blocks.pop()
            continue
        words = tail.split()
        while words and x_weighted_len(" ".join(words)) > budget:
            words.pop()
        if words:
            blocks[-1] = " ".join(words).rstrip(" ,;:-") + "…"
            break  # paragraph fits now
        blocks.pop()

    rebuilt = _rebuild(blocks)
    if x_weighted_len(rebuilt) <= hard_limit:
        return rebuilt

    # 5) Hard-truncate the hook as last resort.
    lead = blocks[0] if blocks else post_x
    while lead and x_weighted_len(lead) > hard_limit - 1:
        lead = lead[:-1].rstrip(" ,;:-")
    return (lead.rstrip() + "…") if lead else post_x[:hard_limit - 1] + "…"


def ensure_disclaimer_tg(post_tg: str) -> str:
    """Append a disclaimer if none detected."""
    haystack = post_tg.lower()
    if "not financial advice" in haystack or "dyor" in haystack:
        return post_tg
    return post_tg.rstrip() + "\n\n⚠️ Not financial advice. DYOR."


# --- Orchestrator ----------------------------------------------------------

def _load_viral_blueprint_for_story(story: dict) -> dict | None:
    """If a viral blueprint exists for the current admin user, load it.

    Looks for the topic best matching the story (ticker keyword, else 'crypto').
    Silently returns None on any failure — the copywriter must keep working
    when no blueprint is cached.
    """
    try:
        if os.environ.get("DISABLE_VIRAL_BLUEPRINT") == "1":
            return None
        from src.dashboard.database import get_all_users, get_viral_blueprint
        admins = [u for u in get_all_users() if u.get("is_admin") and u.get("is_approved")]
        if not admins:
            return None
        user_id = admins[0]["id"]
        # Pick a topic to query — first ticker, else generic 'crypto'
        topic = "crypto"
        tickers = story.get("tickers") or []
        if tickers:
            topic = tickers[0].lower()
        # Try ticker, fall back to 'crypto'
        bp = get_viral_blueprint(user_id, topic)
        if bp is None and topic != "crypto":
            bp = get_viral_blueprint(user_id, "crypto")
        return bp
    except Exception:
        return None


def write_outputs(story: dict, out_dir: Path, model: str = "",
                  niche: str = "crypto") -> dict:
    blueprint = _load_viral_blueprint_for_story(story)
    if blueprint and blueprint.get("sample_size"):
        print(f"[copywriter] using viral blueprint: "
              f"sample={blueprint['sample_size']} "
              f"hashtags={len(blueprint.get('top_hashtags') or [])}")
    data = generate_template(story, viral_blueprint=blueprint, niche=niche)

    post_x_a = ensure_x_budget(data["post_x_a"].strip())
    post_x_b = ensure_x_budget(data["post_x_b"].strip())
    post_tg = ensure_disclaimer_tg(data["post_telegram"].strip())
    image_prompt = data["image_prompt"].strip()

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "post_x.txt").write_text(post_x_a + "\n", encoding="utf-8")
    (out_dir / "post_x_b.txt").write_text(post_x_b + "\n", encoding="utf-8")
    (out_dir / "post_tg.txt").write_text(post_tg + "\n", encoding="utf-8")
    (out_dir / "image_prompt.txt").write_text(image_prompt + "\n", encoding="utf-8")
    (out_dir / "post_tiktok.txt").write_text(data.get("post_tiktok", "") + "\n", encoding="utf-8")
    (out_dir / "post_ig.txt").write_text(data.get("post_ig", "") + "\n", encoding="utf-8")
    (out_dir / "post_youtube.txt").write_text(data.get("post_youtube", "") + "\n", encoding="utf-8")

    stats = {
        "post_x_chars":         x_weighted_len(post_x_a),
        "post_x_b_chars":       x_weighted_len(post_x_b),
        "post_telegram_chars":  len(post_tg),
        "image_prompt_chars":   len(image_prompt),
        "post_tiktok_chars":    len(data.get("post_tiktok", "")),
        "post_ig_chars":        len(data.get("post_ig", "")),
        "post_youtube_chars":   len(data.get("post_youtube", "")),
    }
    print(f"[copywriter] wrote post_x.txt ({stats['post_x_chars']} wchars) "
          f"tg={stats['post_telegram_chars']}c "
          f"tiktok={stats['post_tiktok_chars']}c "
          f"ig={stats['post_ig_chars']}c "
          f"yt={stats['post_youtube_chars']}c")
    return stats


def main(argv: list[str]) -> int:
    import argparse, json as _json
    ap = argparse.ArgumentParser()
    ap.add_argument("slot_dir", help="Path to a queue/<slot> directory")
    ap.add_argument("--model", default="")
    ap.add_argument("--niche", default="crypto")
    args = ap.parse_args(argv)
    slot = Path(args.slot_dir)
    top2 = slot / "top2.json"
    if not top2.exists():
        print(f"[copywriter] top2.json not found in {slot}", file=sys.stderr)
        return 2
    data = _json.loads(top2.read_text(encoding="utf-8"))
    stories = data.get("stories") or []
    if not stories:
        print(f"[copywriter] no stories in {top2}", file=sys.stderr)
        return 2
    write_outputs(stories[0], slot, model=args.model, niche=args.niche)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
