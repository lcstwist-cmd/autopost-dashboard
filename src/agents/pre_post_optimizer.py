"""Pre-Post Optimizer — analizează datele zilei și alege varianta optimă de conținut.

Flow:
  1. Încarcă viral blueprint (hookuri, hashtag-uri, pattern-uri per platformă)
  2. Încarcă market pulse (sentiment, trending keywords, fear & greed, content signals)
  3. Încarcă X trending (hashtag-uri trending în timp real)
  4. Generează 4 variante de conținut cu hookuri diferite (shock / question / number / secret)
  5. Scorează fiecare variantă pe 5 dimensiuni
  6. Scrie cea mai bună variantă în slot_dir ca post_{platform}.txt
  7. Salvează raportul în optimizer_report.json

Integrat în pipeline.py între _stage_copy() și _stage_publish().
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

log = logging.getLogger("pre_post_optimizer")
if not log.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [optimizer] %(message)s",
                                     datefmt="%H:%M:%S"))
    log.addHandler(h)
log.setLevel(logging.INFO)

_DATA = _REPO / "data"
_HASHTAG_RE = re.compile(r"#[A-Za-z0-9_]+")


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def _load_market_pulse() -> dict:
    p = _DATA / "market_pulse.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def _load_x_trending() -> list[str]:
    p = _DATA / "x_trending.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        return [t.get("name", "") for t in (data.get("trends") or []) if t.get("name")]
    except Exception:
        return []


def _load_viral_blueprint(user_id: int | None = None) -> dict:
    """Load freshest viral blueprint from DB, fall back to file cache."""
    try:
        from src.dashboard.database import get_viral_blueprint, get_all_users
        uid = user_id
        if uid is None:
            admins = [u for u in get_all_users()
                      if u.get("is_admin") and u.get("is_approved")]
            uid = admins[0]["id"] if admins else None
        if uid:
            bp = get_viral_blueprint(uid, "crypto")
            if bp and bp.get("sample_size"):
                return bp
    except Exception:
        pass
    p = _DATA / "viral_blueprint.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def _load_story(slot_dir: Path) -> dict:
    top2 = slot_dir / "top2.json"
    if not top2.exists():
        return {}
    try:
        data = json.loads(top2.read_text(encoding="utf-8"))
        stories = data.get("stories") or []
        return stories[0] if stories else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Variant generator — 4 hook styles × 4 platforms
# ---------------------------------------------------------------------------

_PLATFORM_LIMITS = {
    "tiktok":    2200,
    "instagram": 2200,
    "youtube":   5000,
    "x":         280,
}

_HOOK_TEMPLATES = {
    "shock": [
        "🚨 {ticker} just did something it hasn't done in years",
        "Breaking: {title_short} — and nobody saw it coming",
        "🔴 ALERT: {ticker} move you can't ignore today",
        "This {ticker} news changes everything — read carefully",
    ],
    "question": [
        "Is {ticker} about to flip the entire market? 👀",
        "Why is everyone talking about {ticker} right now?",
        "Could this be the {ticker} breakout we've been waiting for?",
        "What just happened with {ticker}? Here's the truth ⚡",
    ],
    "number": [
        "3 things happening in {ticker} RIGHT NOW that matter 👇",
        "The #1 {ticker} story you need to read today",
        "5 words: {title_short_40}",
        "In 60 seconds: everything about {ticker} today",
    ],
    "secret": [
        "Nobody is talking about this {ticker} signal 🤫",
        "What the mainstream media won't tell you about {ticker}",
        "Smart money already knew this about {ticker}",
        "The {ticker} insight 99% of traders are missing",
    ],
}


def _pick_hook(style: str, story: dict, idx: int = 0) -> str:
    ticker = (story.get("tickers") or ["crypto"])[0]
    title = story.get("title", "Crypto Update")
    title_short = title[:50].rstrip(" ,;:")
    title_short_40 = title_short[:40]
    templates = _HOOK_TEMPLATES.get(style, _HOOK_TEMPLATES["shock"])
    tmpl = templates[idx % len(templates)]
    return tmpl.format(ticker=ticker, title_short=title_short,
                       title_short_40=title_short_40, title=title[:60])


def _extract_body(story: dict, max_sentences: int = 3) -> str:
    summary = story.get("summary") or story.get("title") or ""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", summary)
                 if len(s.strip()) > 25]
    impact_words = ("million", "billion", "$", "%", "record", "surge", "crash",
                    "hack", "launch", "approval", "ban", "etf", "sec", "halving")
    scored = sorted(sentences,
                    key=lambda s: sum(1 for w in impact_words if w in s.lower()),
                    reverse=True)
    return " ".join(scored[:max_sentences])


def _platform_tags(blueprint: dict, x_trending: list[str],
                   platform: str, limit: int = 15) -> str:
    tags: list[str] = []
    # From blueprint per-platform
    plat = (blueprint.get("by_platform") or {}).get(platform, {})
    top = (plat.get("top_hashtag") or [[]])[0]
    if isinstance(top, str) and top.startswith("#"):
        tags.append(top)
    # Global blueprint hashtags
    for tag, _ in (blueprint.get("top_hashtags") or [])[:10]:
        if tag not in tags:
            tags.append(tag)
    # X trending (filter crypto-relevant)
    crypto_kw = {"crypto", "bitcoin", "btc", "eth", "ethereum", "defi",
                 "web3", "nft", "altcoin", "xrp", "sol", "bnb", "coin"}
    for t in x_trending[:20]:
        tag = f"#{t.lstrip('#')}"
        if any(k in tag.lower() for k in crypto_kw) and tag not in tags:
            tags.append(tag)
    if platform == "tiktok":
        for t in ["#fyp", "#foryou", "#crypto", "#bitcoin"]:
            if t not in tags:
                tags.append(t)
    elif platform == "youtube":
        for t in ["#Shorts", "#crypto", "#bitcoin"]:
            if t not in tags:
                tags.insert(0, t)
    return " ".join(tags[:limit])


def generate_variants(story: dict, blueprint: dict,
                      market_pulse: dict, x_trending: list[str],
                      platform: str) -> list[dict]:
    """Return 4 content variants for `platform`, each with a different hook style."""
    sentiment = (market_pulse.get("market_sentiment") or "neutral").lower()
    content_sig = market_pulse.get("content_signals") or {}
    best_hook_type = content_sig.get("best_hook_type", "statement_hook")
    body = _extract_body(story)
    tags = _platform_tags(blueprint, x_trending, platform)

    # Map market signal → preferred hook style
    signal_to_style = {
        "statement_hook": "shock",
        "question_hook":  "question",
        "number_hook":    "number",
    }
    preferred = signal_to_style.get(best_hook_type, "shock")

    styles = ["shock", "question", "number", "secret"]
    # Put today's preferred style first
    styles = [preferred] + [s for s in styles if s != preferred]

    variants = []
    cta_map = {
        "tiktok":    "Follow for daily crypto alpha 📲",
        "instagram": "Save this 📌 | Follow @EliteMarginDesk",
        "youtube":   "🔔 Subscribe for daily crypto news",
        "x":         "👉 Follow @EliteMarginDesk for daily alpha",
    }
    cta = cta_map.get(platform, "Follow for more")

    for i, style in enumerate(styles):
        hook = _pick_hook(style, story, idx=i)
        if platform == "x":
            # X: hook + 1 sentence + 3 tags (stay ≤280)
            tickers = story.get("tickers") or []
            cash = " ".join(f"${t}" for t in tickers[:2])
            short_tags = " ".join(t for t in tags.split()[:3] if t.startswith("#"))
            short_body = body[:100].rstrip() + "…" if len(body) > 100 else body
            text = f"{hook}\n\n{short_body}\n\n{cash}\n{short_tags}\n{cta}"
        elif platform == "tiktok":
            text = f"{hook}\n\n{body[:200]}\n\n{cta}\n\n{tags}"
        elif platform == "instagram":
            bullets = "\n".join(f"• {s.strip()}"
                                for s in re.split(r"(?<=[.!?])\s+", body)[:3]
                                if len(s.strip()) > 20)
            text = f"{hook}\n\n{bullets}\n\n{cta}\n\n.\n.\n.\n{tags}"
        else:  # youtube
            text = f"{hook}\n\n{body[:400]}\n\n{cta}\n\n{tags[:120]}"

        variants.append({
            "style":    style,
            "platform": platform,
            "text":     text,
            "hook":     hook,
        })
    return variants


# ---------------------------------------------------------------------------
# Scoring engine — 5 dimensions
# ---------------------------------------------------------------------------

def score_variant(variant: dict, blueprint: dict,
                  market_pulse: dict, x_trending: list[str]) -> float:
    text = variant["text"].lower()
    score = 0.0

    # 1. Trending keyword density (0-30 pts)
    kw = [k.lower() for k in (market_pulse.get("trending_keywords") or [])]
    hits = sum(1 for k in kw if k in text)
    score += min(hits * 5, 30)

    # 2. Viral hook pattern similarity (0-25 pts)
    hooks = [h.lower() for h in (blueprint.get("hook_patterns") or [])]
    hook_text = variant["hook"].lower()
    for h in hooks[:5]:
        words = h.split()[:4]
        if sum(1 for w in words if w in hook_text) >= 2:
            score += 5

    # 3. Market sentiment alignment (0-20 pts)
    sentiment = (market_pulse.get("market_sentiment") or "neutral").lower()
    bull_words = {"surge", "pump", "bull", "ath", "breakout", "rally", "moon"}
    bear_words = {"crash", "drop", "bear", "fear", "warning", "risk", "sell"}
    if sentiment in ("bull", "greed"):
        score += len(bull_words & set(text.split())) * 4
    elif sentiment in ("fear", "bear"):
        score += len(bear_words & set(text.split())) * 4
    score = min(score + 20, score + 20)  # cap at 20 additional

    # 4. Hashtag trending score (0-15 pts)
    top_tags = {t[0].lower() for t in (blueprint.get("top_hashtags") or [])[:10]}
    text_tags = {t.lower() for t in _HASHTAG_RE.findall(variant["text"])}
    score += len(top_tags & text_tags) * 2

    xt_set = {f"#{t.lower().lstrip('#')}" for t in x_trending[:20]}
    score += len(xt_set & text_tags) * 3

    # 5. Platform content signal bonus (0-10 pts)
    cs = market_pulse.get("content_signals") or {}
    best_hook = cs.get("best_hook_type", "")
    style_map = {"statement_hook": "shock", "question_hook": "question",
                 "number_hook": "number"}
    if style_map.get(best_hook) == variant["style"]:
        score += 10

    return round(score, 2)


# ---------------------------------------------------------------------------
# Main optimizer
# ---------------------------------------------------------------------------

def optimize(slot_dir: Path, user_id: int | None = None,
             platforms: list[str] | None = None) -> dict:
    """Analyze today's data, generate variants, write best to slot_dir.

    Returns a report dict saved as optimizer_report.json.
    """
    platforms = platforms or ["tiktok", "instagram", "youtube", "x"]
    story = _load_story(slot_dir)
    if not story:
        log.warning(f"No story found in {slot_dir}")
        return {"error": "no story"}

    blueprint    = _load_viral_blueprint(user_id)
    market_pulse = _load_market_pulse()
    x_trending   = _load_x_trending()

    sentiment = market_pulse.get("market_sentiment", "neutral")
    fg_val    = (market_pulse.get("fear_greed") or {}).get("value", "?")
    kw_count  = len(market_pulse.get("trending_keywords") or [])
    bp_sample = blueprint.get("sample_size", 0)

    log.info(f"Optimizing slot={slot_dir.name} | "
             f"sentiment={sentiment} F&G={fg_val} | "
             f"blueprint_sample={bp_sample} | "
             f"trending_kw={kw_count} | x_trends={len(x_trending)}")

    report: dict[str, Any] = {
        "optimized_at":    datetime.now(timezone.utc).isoformat(),
        "slot":            slot_dir.name,
        "story_title":     story.get("title", "")[:80],
        "market_sentiment":sentiment,
        "fear_greed":      fg_val,
        "blueprint_sample":bp_sample,
        "platforms":       {},
    }

    file_map = {
        "tiktok":    "post_tiktok.txt",
        "instagram": "post_ig.txt",
        "youtube":   "post_youtube.txt",
        "x":         "post_x.txt",
    }

    for platform in platforms:
        variants = generate_variants(story, blueprint, market_pulse,
                                     x_trending, platform)
        scored = sorted(
            [{"variant": v, "score": score_variant(v, blueprint,
                                                    market_pulse, x_trending)}
             for v in variants],
            key=lambda x: x["score"],
            reverse=True,
        )
        winner = scored[0]["variant"]
        fname  = file_map.get(platform)
        if fname:
            (slot_dir / fname).write_text(winner["text"] + "\n", encoding="utf-8")
            log.info(f"  {platform:12} winner={winner['style']:8} "
                     f"score={scored[0]['score']:5.1f} "
                     f"(vs {scored[1]['score']:.1f} / {scored[2]['score']:.1f} / {scored[3]['score']:.1f})")

        report["platforms"][platform] = {
            "winner_style": winner["style"],
            "winner_score": scored[0]["score"],
            "all_scores":   {s["variant"]["style"]: s["score"] for s in scored},
            "winner_hook":  winner["hook"],
        }

    (slot_dir / "optimizer_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info(f"Optimizer done — report saved to {slot_dir / 'optimizer_report.json'}")
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    import argparse
    from dotenv import load_dotenv
    load_dotenv(_REPO / ".env")

    ap = argparse.ArgumentParser(description="Pre-Post Optimizer")
    ap.add_argument("slot_dir", help="Path to queue/<slot> directory")
    ap.add_argument("--platforms", default="tiktok,instagram,youtube,x",
                    help="Comma-separated platforms (default: all 4)")
    ap.add_argument("--user-id", type=int, default=None)
    args = ap.parse_args(argv[1:])

    slot = Path(args.slot_dir)
    if not slot.exists():
        print(f"Error: {slot} does not exist", file=sys.stderr)
        return 1

    plats = [p.strip() for p in args.platforms.split(",") if p.strip()]
    report = optimize(slot, user_id=args.user_id, platforms=plats)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
