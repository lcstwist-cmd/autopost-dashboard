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


def _hashtags(story: dict, tone: str = "neutral", min_tags: int = 10) -> str:
    """Generate at least min_tags hashtags relevant to the story."""
    seen: set[str] = set()
    tags: list[str] = []

    def _add(tag: str) -> None:
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)

    # Ticker hashtags first
    for t in (story.get("tickers") or [])[:4]:
        _add(f"#{t}")
        # Add full name for known majors
        name_map = {
            "BTC": "#Bitcoin", "ETH": "#Ethereum", "SOL": "#Solana",
            "XRP": "#XRP", "BNB": "#BNB", "ADA": "#Cardano",
            "DOGE": "#Dogecoin", "AVAX": "#Avalanche", "DOT": "#Polkadot",
        }
        if t in name_map:
            _add(name_map[t])

    # Keyword-based hashtags from title + summary
    haystack = (story.get("title", "") + " " + (story.get("summary") or "")).lower()
    for kw, tag in _KEYWORD_HASHTAGS.items():
        if kw in haystack:
            _add(tag)

    # Tone-based pool
    for tag in _TONE_HASHTAGS.get(tone, _TONE_HASHTAGS["neutral"]):
        _add(tag)

    # Universal fallback tags to reach min_tags
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

def generate_template(story: dict) -> dict:
    title = story.get("title", "")
    summary = _clean_summary(story.get("summary") or "")
    source = story.get("source", "")
    tone = _tone(story)
    emoji = _emoji(tone)
    ticker = _ticker(story)
    tags = _hashtags(story, tone=tone, min_tags=10)
    numbers = _extract_numbers(f"{title} {summary}")
    big_num = next((n for n in numbers if "$" in n or "M" in n.upper()), None)
    first_sent = _first_sentence(summary)

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

    # --- post_x_a: factual hook + full summary ---
    if big_num:
        lead_a = f"{emoji} {big_num} — {title}"
    else:
        lead_a = f"{emoji} {title}"
    post_x_a = f"{lead_a}\n\n{summary_block}\n\n{tags}" if summary_block else f"{lead_a}\n\n{tags}"

    # --- post_x_b: question hook + full summary ---
    if tone == "bear":
        question = f"🚨 {ticker or 'Crypto'} under pressure — what's really happening?"
    elif tone == "bull":
        question = f"📈 {ticker or 'Crypto'} is making moves — here's what you need to know:"
    else:
        question = f"📰 Breaking crypto update — {ticker or 'market'} alert:"
    post_x_b = f"{question}\n\n{title}\n\n{summary_block}\n\n{tags}" if summary_block else f"{question}\n\n{title}\n\n{tags}"

    # --- post_telegram: bold title + 2 key paragraphs + fixed footer ---
    tg_title = title if len(title) <= 80 else title[:79].rstrip(" ,;:") + "..."
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
    # Trim each paragraph to fit caption limit (total < 900 chars before footer)
    trimmed = []
    for p in paras:
        trimmed.append(p if len(p) <= 220 else p[:219].rstrip() + "...")
    para_block = "\n\n".join(trimmed)
    footer = (
        "\n\n🌐 https://www.elitemargindesk.io?ref=7UBN23I0"
        "\n💬 Support: @CryptohEMD"
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

    return {
        "post_x_a":      post_x_a,
        "post_x_b":      post_x_b,
        "post_telegram": post_tg,
        "image_prompt":  image_prompt,
    }


# --- Safety & post-processing ----------------------------------------------

def ensure_x_budget(post_x: str, hard_limit: int = 25000) -> str:
    """X Plus allows up to 25,000 characters. Trim only if somehow exceeded."""
    if len(post_x) <= hard_limit:
        return post_x
    # Hard truncation — should never be reached in practice
    return post_x[:hard_limit - 1].rstrip() + "…"


def ensure_disclaimer_tg(post_tg: str) -> str:
    """Append a disclaimer if none detected."""
    haystack = post_tg.lower()
    if "not financial advice" in haystack or "dyor" in haystack:
        return post_tg
    return post_tg.rstrip() + "\n\n⚠️ Not financial advice. DYOR."


# --- Orchestrator ----------------------------------------------------------

def write_outputs(story: dict, out_dir: Path, model: str = "") -> dict:
    data = generate_template(story)

    post_x_a = ensure_x_budget(data["post_x_a"].strip())
    post_x_b = ensure_x_budget(data["post_x_b"].strip())
    post_tg = ensure_disclaimer_tg(data["post_telegram"].strip())
    image_prompt = data["image_prompt"].strip()

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "post_x.txt").write_text(post_x_a + "\n", encoding="utf-8")
    (out_dir / "post_x_b.txt").write_text(post_x_b + "\n", encoding="utf-8")
    (out_dir / "post_telegram.md").write_text(post_tg + "\n", encoding="utf-8")
    (out_dir / "image_prompt.txt").write_text(image_prompt + "\n", encoding="utf-8")

    return {
        "post_x_chars": x_weighted_len(post_x_a),
        "post_x_b_chars": x_weighted_len(post_x_b),
        "post_telegram_chars": len(post_tg),
        "image_prompt_chars": len(image_prompt),
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("top2_json", help="path to top2.json produced by the ranker")
    ap.add_argument("--out-dir", default=None, help="output dir (default: same as top2.json)")
    args = ap.parse_args(argv[1:])

    top2_path = Path(args.top2_json).resolve()
    data = json.loads(top2_path.read_text(encoding="utf-8"))
    stories = data.get("stories", [])
    if not stories:
        print("[copywriter] no stories in top2.json", file=sys.stderr)
        return 1

    story = stories[0]
    out_dir = Path(args.out_dir).resolve() if args.out_dir else top2_path.parent
    stats = write_outputs(story, out_dir)
    print(f"[copywriter] wrote post_x.txt ({stats['post_x_chars']} chars), "
          f"post_telegram.md ({stats['post_telegram_chars']} chars) -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
