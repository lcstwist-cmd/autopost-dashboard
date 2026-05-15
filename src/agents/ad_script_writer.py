"""Ad Script Writer — viral video ad generator for Instagram Reels, TikTok, YouTube Shorts.

Takes a brand brief (company, product, industry, target audience, goal) and produces
a complete production-ready ad package optimised for short-form vertical video.

Supports any industry: crypto, real_estate, fitness, ecommerce, finance, tech,
food, fashion, travel, education, healthcare, marketing.

Formulas: AIDA | PAS | BAB (auto-selected by goal if not specified).

Output directory:
    01_ad_script.txt          — full narration with timing marks
    02_hook_variants.txt      — 3 hook variants for A/B testing
    03_scenes.json            — scene-by-scene shot list
    04_captions.srt           — burnt-in subtitle file
    05_platform_metadata.json — captions + hashtags per platform
    06_image_prompt.txt       — 9:16 background image brief for Pollinations/DALL-E
    07_heygen_brief.json      — HeyGen avatar + voice + settings

Usage:
    python src/agents/ad_script_writer.py brief.json --out-dir output/ads/brand_2026-05-01
    python src/agents/ad_script_writer.py brief.json               # out-dir = next to brief
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import timedelta
from pathlib import Path

# Force UTF-8 stdout/stderr on Windows
for _s in ("stdout", "stderr"):
    _stream = getattr(sys, _s, None)
    if _stream and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Industry hashtags — 12 verticals
# ---------------------------------------------------------------------------

_INDUSTRY_HASHTAGS: dict[str, dict[str, list[str]]] = {
    "crypto": {
        "instagram": ["#bitcoin", "#crypto", "#btc", "#ethereum", "#defi", "#blockchain",
                      "#web3", "#cryptoinvesting", "#cryptonews", "#altcoins",
                      "#trading", "#investing", "#finance", "#digitalassets"],
        "tiktok":    ["#crypto", "#bitcoin", "#btc", "#fyp", "#cryptotok",
                      "#investing", "#money", "#financetok", "#defi", "#web3"],
        "youtube":   ["#Shorts", "#Bitcoin", "#Crypto", "#BTC", "#Blockchain",
                      "#Finance", "#Investing", "#Web3", "#DeFi"],
    },
    "real_estate": {
        "instagram": ["#realestate", "#property", "#investing", "#homebuying", "#realtor",
                      "#housing", "#mortgagetips", "#propertyinvesting", "#realestateinvesting",
                      "#luxuryhomes", "#firsttimehomebuyer", "#passiveincome"],
        "tiktok":    ["#realestate", "#property", "#fyp", "#homebuying", "#realtor",
                      "#realestatetok", "#investing", "#passiveincome", "#housing"],
        "youtube":   ["#Shorts", "#RealEstate", "#PropertyInvesting", "#Homebuying",
                      "#Realtor", "#PassiveIncome"],
    },
    "fitness": {
        "instagram": ["#fitness", "#gym", "#workout", "#fitlife", "#health", "#gains",
                      "#transformation", "#bodybuilding", "#fitnessmotivation", "#training",
                      "#healthylifestyle", "#weightloss", "#muscle", "#personaltrainer"],
        "tiktok":    ["#fitness", "#gym", "#fyp", "#workout", "#fitnesstok",
                      "#weightloss", "#gains", "#healthylifestyle", "#transformation"],
        "youtube":   ["#Shorts", "#Fitness", "#Gym", "#Workout", "#Transformation",
                      "#Health", "#WeightLoss"],
    },
    "ecommerce": {
        "instagram": ["#shopping", "#sale", "#deal", "#onlineshopping", "#ecommerce",
                      "#shopnow", "#productlaunch", "#dropshipping", "#entrepreneurship",
                      "#smallbusiness", "#shoplocal", "#discount", "#limitedoffer"],
        "tiktok":    ["#fyp", "#tiktokshop", "#shopping", "#sale", "#deal",
                      "#shopwithme", "#haul", "#productreview", "#smallbusiness"],
        "youtube":   ["#Shorts", "#Shopping", "#Sale", "#Deal", "#Ecommerce",
                      "#ProductReview", "#Haul"],
    },
    "finance": {
        "instagram": ["#finance", "#investing", "#money", "#wealthbuilding", "#personalfinance",
                      "#fintech", "#stocks", "#passiveincome", "#financialfreedom",
                      "#budgeting", "#savingmoney", "#wealthmindset", "#moneymanagement"],
        "tiktok":    ["#finance", "#money", "#fyp", "#financetok", "#investing",
                      "#wealthbuilding", "#personalfinance", "#moneytips", "#stocks"],
        "youtube":   ["#Shorts", "#Finance", "#Investing", "#Money", "#WealthBuilding",
                      "#PersonalFinance", "#Stocks"],
    },
    "tech": {
        "instagram": ["#tech", "#ai", "#startup", "#saas", "#software", "#innovation",
                      "#automation", "#artificialintelligence", "#machinelearning",
                      "#entrepreneur", "#businessgrowth", "#digitalmarketing", "#productivity"],
        "tiktok":    ["#tech", "#ai", "#fyp", "#techtok", "#startup", "#saas",
                      "#innovation", "#automation", "#artificialintelligence"],
        "youtube":   ["#Shorts", "#Tech", "#AI", "#Startup", "#SaaS",
                      "#Innovation", "#Automation"],
    },
    "food": {
        "instagram": ["#food", "#foodie", "#recipe", "#cooking", "#restaurant",
                      "#foodlover", "#chef", "#homemade", "#foodphotography",
                      "#yummy", "#delicious", "#mealprep", "#healthyfood", "#foodblogger"],
        "tiktok":    ["#food", "#foodtok", "#fyp", "#recipe", "#cooking",
                      "#foodie", "#mealprep", "#tasty", "#chef", "#yummy"],
        "youtube":   ["#Shorts", "#Food", "#Recipe", "#Cooking", "#Foodie",
                      "#Chef", "#MealPrep"],
    },
    "fashion": {
        "instagram": ["#fashion", "#style", "#ootd", "#streetwear", "#clothing",
                      "#luxury", "#brand", "#outfit", "#fashionista", "#model",
                      "#trendy", "#designer", "#fashionblogger", "#lookbook"],
        "tiktok":    ["#fashion", "#ootd", "#fyp", "#style", "#fashiontok",
                      "#outfit", "#streetwear", "#clothing", "#trending"],
        "youtube":   ["#Shorts", "#Fashion", "#Style", "#OOTD", "#Clothing",
                      "#Streetwear", "#Outfit"],
    },
    "travel": {
        "instagram": ["#travel", "#wanderlust", "#vacation", "#explore", "#adventure",
                      "#tourism", "#travelphotography", "#travelgram", "#holiday",
                      "#destination", "#backpacking", "#luxurytravel", "#travelblogger"],
        "tiktok":    ["#travel", "#wanderlust", "#fyp", "#traveltok", "#vacation",
                      "#explore", "#adventure", "#holiday", "#destination"],
        "youtube":   ["#Shorts", "#Travel", "#Wanderlust", "#Vacation", "#Adventure",
                      "#Explore", "#Tourism"],
    },
    "education": {
        "instagram": ["#education", "#learning", "#onlinecourse", "#skill", "#career",
                      "#studymotivation", "#knowledge", "#selfimprovement", "#growth",
                      "#course", "#certification", "#elearning", "#motivation"],
        "tiktok":    ["#education", "#fyp", "#learntok", "#learning", "#studytok",
                      "#knowledge", "#skill", "#selfimprovement", "#growth"],
        "youtube":   ["#Shorts", "#Education", "#Learning", "#Course", "#Skill",
                      "#Career", "#Knowledge"],
    },
    "healthcare": {
        "instagram": ["#health", "#wellness", "#fitness", "#mentalhealth", "#nutrition",
                      "#doctor", "#healthylifestyle", "#selfcare", "#mindfulness",
                      "#supplement", "#medical", "#therapy", "#holistic"],
        "tiktok":    ["#health", "#wellness", "#fyp", "#healthtok", "#mentalhealth",
                      "#nutrition", "#selfcare", "#healthylifestyle", "#doctor"],
        "youtube":   ["#Shorts", "#Health", "#Wellness", "#MentalHealth", "#Nutrition",
                      "#HealthyLifestyle", "#Doctor"],
    },
    "marketing": {
        "instagram": ["#marketing", "#digitalmarketing", "#socialmedia", "#branding",
                      "#contentcreator", "#growthhacking", "#seo", "#leadgeneration",
                      "#entrepreneur", "#businessgrowth", "#marketingstrategy", "#ads"],
        "tiktok":    ["#marketing", "#fyp", "#marketingtok", "#digitalmarketing",
                      "#socialmedia", "#branding", "#entrepreneur", "#business"],
        "youtube":   ["#Shorts", "#Marketing", "#DigitalMarketing", "#Branding",
                      "#SocialMedia", "#ContentCreator"],
    },
}

_FALLBACK_HASHTAGS: dict[str, list[str]] = {
    "instagram": ["#business", "#entrepreneur", "#success", "#motivation",
                  "#marketing", "#brand", "#growth", "#innovation"],
    "tiktok":    ["#fyp", "#business", "#entrepreneur", "#success", "#motivation"],
    "youtube":   ["#Shorts", "#Business", "#Entrepreneur", "#Success"],
}


# ---------------------------------------------------------------------------
# Hook templates — 6 pattern-interrupt types
# ---------------------------------------------------------------------------

def _hooks(brief: dict) -> list[str]:
    company   = brief.get("company", "This brand")
    product   = brief.get("product", "this product")
    audience  = brief.get("target_audience", "people like you")
    benefit   = brief.get("key_benefit", "change your life")
    proof     = brief.get("proof_point", "thousands of customers agree")
    industry  = brief.get("industry", "")
    price     = brief.get("price_point", "")
    goal      = brief.get("goal", "brand_awareness")

    # Audience shorthand — first noun group
    audience_short = audience.split(" who")[0].split(" that")[0].strip()
    audience_short = audience_short if audience_short else "people like you"

    hooks: list[str] = []

    # 1. Direct address + pain point
    hooks.append(
        f"If you're {audience_short} and still struggling — stop scrolling."
    )
    # 2. Question hook
    hooks.append(
        f"What if {benefit.lower()}? That's exactly what {company} does."
    )
    # 3. Shock / social proof hook
    hooks.append(
        f"{proof}. Here's why everyone is talking about {product}."
    )
    # 4. POV hook (massive on TikTok)
    hooks.append(
        f"POV: you just discovered {product} — and your life changed in 30 seconds."
    )
    # 5. Curiosity hook
    hooks.append(
        f"The {industry or 'industry'} secret no one tells {audience_short}: {product}."
    )
    # 6. Price-anchor hook (only if price given)
    if price:
        hooks.append(
            f"For {price}? This is the best investment {audience_short} can make right now."
        )
    else:
        hooks.append(
            f"This is the smartest move {audience_short} can make in {_current_year()}."
        )
    # 7. Urgency hook (for sales goal)
    if goal in ("sales", "lead_gen"):
        hooks.append(
            f"Limited time: {company} is changing the game — and most people are missing it."
        )

    return hooks[:3]  # return best 3 for A/B/C testing


def _current_year() -> int:
    import datetime
    return datetime.datetime.now().year


# ---------------------------------------------------------------------------
# Formula builders
# ---------------------------------------------------------------------------

def _formula_aida(brief: dict, hook: str) -> dict:
    """Attention → Interest → Desire → Action"""
    company  = brief.get("company", "We")
    product  = brief.get("product", "our product")
    benefit  = brief.get("key_benefit", "solves your biggest problem")
    proof    = brief.get("proof_point", "thousands of happy customers")
    cta_text = brief.get("cta", f"Try {product} today")
    audience = brief.get("target_audience", "you")
    audience_short = audience.split(" who")[0].split(" that")[0].strip()

    script = {
        "formula": "AIDA",
        "sections": [
            {
                "label": "ATTENTION",
                "timing": "0:00-0:03",
                "lines": [hook],
                "direction": "avatar_close_up — high energy, direct eye contact, micro-zoom",
            },
            {
                "label": "INTEREST",
                "timing": "0:03-0:15",
                "lines": [
                    f"{company} built {product} specifically for {audience_short}.",
                    f"The result? {benefit}.",
                    f"No complicated setup. No hidden costs. Just results.",
                ],
                "direction": "avatar_medium — calm, informative, product B-roll cut-in",
            },
            {
                "label": "DESIRE",
                "timing": "0:15-0:30",
                "lines": [
                    f"{proof}.",
                    f"Imagine waking up tomorrow with {benefit.lower()} — that's the {product} effect.",
                    f"This isn't theory. This is what happens when you make the move.",
                ],
                "direction": "avatar_medium + lifestyle B-roll — aspirational visuals, transformation shots",
            },
            {
                "label": "ACTION",
                "timing": "0:30-0:45",
                "lines": [
                    f"{cta_text}.",
                    f"Link in bio. Takes 60 seconds. Your future self will thank you.",
                ],
                "direction": "avatar_close_up — confident, direct, slight lean forward",
            },
        ],
    }
    return script


def _formula_pas(brief: dict, hook: str) -> dict:
    """Problem → Agitate → Solution"""
    company  = brief.get("company", "We")
    product  = brief.get("product", "our product")
    benefit  = brief.get("key_benefit", "solves your biggest problem")
    proof    = brief.get("proof_point", "thousands of happy customers")
    cta_text = brief.get("cta", f"Try {product} today")
    audience = brief.get("target_audience", "you")
    audience_short = audience.split(" who")[0].split(" that")[0].strip()

    # Infer pain from audience description
    pain = _infer_pain(brief)

    script = {
        "formula": "PAS",
        "sections": [
            {
                "label": "PROBLEM",
                "timing": "0:00-0:08",
                "lines": [
                    hook,
                    f"{pain}.",
                    f"And the longer you wait, the worse it gets.",
                ],
                "direction": "avatar_close_up — empathetic, serious, slight head shake",
            },
            {
                "label": "AGITATE",
                "timing": "0:08-0:20",
                "lines": [
                    f"Most {audience_short} try everything — and still hit the same wall.",
                    f"The industry doesn't want you to find a shortcut. But one exists.",
                    f"Here's the truth no one talks about: {pain.lower()} is costing you more than you think.",
                ],
                "direction": "avatar_medium — building tension, lean slightly forward",
            },
            {
                "label": "SOLUTION",
                "timing": "0:20-0:40",
                "lines": [
                    f"That's exactly why {company} created {product}.",
                    f"{benefit}. Proven by {proof}.",
                    f"{cta_text}. Stop suffering the problem. Start living the solution.",
                ],
                "direction": "avatar_medium + product B-roll — confident, upbeat energy shift",
            },
        ],
    }
    return script


def _formula_bab(brief: dict, hook: str) -> dict:
    """Before → After → Bridge"""
    company  = brief.get("company", "We")
    product  = brief.get("product", "our product")
    benefit  = brief.get("key_benefit", "everything changes")
    proof    = brief.get("proof_point", "thousands of happy customers")
    cta_text = brief.get("cta", f"Try {product} today")
    audience = brief.get("target_audience", "you")
    audience_short = audience.split(" who")[0].split(" that")[0].strip()
    pain = _infer_pain(brief)

    script = {
        "formula": "BAB",
        "sections": [
            {
                "label": "BEFORE",
                "timing": "0:00-0:10",
                "lines": [
                    hook,
                    f"Every day, {audience_short} are dealing with {pain.lower()}.",
                    f"It's exhausting. And it feels like there's no way out.",
                ],
                "direction": "avatar_close_up — relatable, soft voice, empathetic nod",
            },
            {
                "label": "AFTER",
                "timing": "0:10-0:25",
                "lines": [
                    f"But picture this: {benefit}.",
                    f"No more {pain.lower().split('.')[0]}. Just results.",
                    f"{proof}. Real people, real transformation.",
                ],
                "direction": "avatar_medium + aspirational B-roll — bright, optimistic, energy shift",
            },
            {
                "label": "BRIDGE",
                "timing": "0:25-0:45",
                "lines": [
                    f"The bridge between where you are and where you want to be is {product} by {company}.",
                    f"The system is simple. The results are proven. The time is now.",
                    f"{cta_text}. Link in bio — start your transformation today.",
                ],
                "direction": "avatar_close_up — product held/shown if possible, direct CTA, slight smile",
            },
        ],
    }
    return script


def _infer_pain(brief: dict) -> str:
    """Derive a generic pain statement from industry + audience."""
    industry = brief.get("industry", "")
    audience = brief.get("target_audience", "")
    pain_map = {
        "crypto":      "Losing money to bad trades and missing the right entries",
        "real_estate": "Being locked out of property because of high prices and bad advice",
        "fitness":     "Spending months in the gym with zero visible results",
        "ecommerce":   "Wasting hours on products that don't sell and ads that don't convert",
        "finance":     "Watching money sit idle in savings while inflation eats it away",
        "tech":        "Drowning in manual tasks when automation could do it in seconds",
        "food":        "Eating the same boring meals because cooking feels too complicated",
        "fashion":     "Spending a fortune on clothes that don't build a real personal brand",
        "travel":      "Paying full price for trips that a smarter traveller gets for half the cost",
        "education":   "Spending years on degrees that don't translate to real-world income",
        "healthcare":  "Ignoring your health until it becomes a crisis",
        "marketing":   "Running campaigns that burn budget without delivering qualified leads",
    }
    return pain_map.get(industry, f"Not getting the results you deserve from {audience}")


# ---------------------------------------------------------------------------
# Auto-formula selection
# ---------------------------------------------------------------------------

def _select_formula(brief: dict) -> str:
    goal = brief.get("formula", "auto").lower()
    if goal in ("aida", "pas", "bab"):
        return goal
    # Auto-select based on goal
    goal_map = {
        "brand_awareness": "aida",
        "sales":           "bab",
        "lead_gen":        "pas",
        "app_download":    "aida",
        "engagement":      "pas",
    }
    return goal_map.get(brief.get("goal", "brand_awareness"), "aida")


def _build_formula_script(brief: dict, hook: str) -> dict:
    formula = _select_formula(brief)
    builders = {"aida": _formula_aida, "pas": _formula_pas, "bab": _formula_bab}
    return builders[formula](brief, hook)


# ---------------------------------------------------------------------------
# Script text renderer
# ---------------------------------------------------------------------------

def render_script_text(fs: dict) -> str:
    parts: list[str] = [f"FORMULA: {fs['formula']}", ""]
    for sec in fs["sections"]:
        label   = sec["label"]
        timing  = sec.get("timing", "")
        lines   = sec.get("lines", [])
        direction = sec.get("direction", "")
        parts.append(f"[{label} — {timing}]  ({direction})")
        for ln in lines:
            parts.append(f'  "{ln}"')
        parts.append("")
    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Scenes list
# ---------------------------------------------------------------------------

def build_scenes(fs: dict, brief: dict) -> list[dict]:
    scenes: list[dict] = []
    idx = 1
    for sec in fs["sections"]:
        label  = sec["label"]
        timing = sec.get("timing", "0:00-0:45")
        try:
            start_str, end_str = timing.split("-")
            start = _ts_to_sec(start_str.strip())
            end   = _ts_to_sec(end_str.strip())
        except Exception:
            start, end = 0.0, 10.0

        lines = sec.get("lines", [])
        chunk_dur = max(1.0, (end - start) / max(1, len(lines)))
        t = start
        for ln in lines:
            overlay = ""
            if idx == 1:
                overlay = brief.get("company", "").upper()
            elif label == "ACTION" or label == "BRIDGE":
                overlay = "LINK IN BIO"
            scenes.append({
                "scene":     idx,
                "start":     round(t, 1),
                "end":       round(t + chunk_dur, 1),
                "type":      label.lower(),
                "shot":      sec.get("direction", "avatar_medium").split("—")[0].strip(),
                "line":      ln,
                "overlay":   overlay,
                "broll":     _broll_for(label, brief),
            })
            idx += 1
            t += chunk_dur
    return scenes


def _ts_to_sec(ts: str) -> float:
    """'0:15' → 15.0  |  '1:05' → 65.0"""
    parts = ts.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(parts[0])


def _broll_for(label: str, brief: dict) -> str | None:
    industry = brief.get("industry", "")
    broll_map = {
        "ATTENTION": None,
        "PROBLEM":   f"{industry} pain-point footage, frustrated person",
        "BEFORE":    f"{industry} struggle scene, dark mood",
        "INTEREST":  f"{brief.get('product', 'product')} close-up, features demo",
        "AFTER":     f"{industry} success scene, happy customer, bright lighting",
        "DESIRE":    "lifestyle aspirational footage, success moments",
        "SOLUTION":  f"{brief.get('product', 'product')} in use, results shown",
        "BRIDGE":    f"{brief.get('company', 'brand')} product on-screen",
        "ACTION":    None,
        "AGITATE":   "tension-building footage, problem escalating",
    }
    return broll_map.get(label)


# ---------------------------------------------------------------------------
# SRT generation (reuses same logic as reel_writer)
# ---------------------------------------------------------------------------

def _fmt_ts(seconds: float) -> str:
    td = timedelta(seconds=seconds)
    total = int(td.total_seconds())
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    ms = int((seconds - total) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _split_line(text: str, max_chars: int = 38) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        candidate = f"{cur} {w}".strip()
        if len(candidate) <= max_chars:
            cur = candidate
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def build_srt(scenes: list[dict]) -> str:
    parts: list[str] = []
    idx = 1
    for sc in scenes:
        line = (sc.get("line") or "").strip().strip('"')
        if not line:
            continue
        start, end = float(sc["start"]), float(sc["end"])
        dur = max(0.5, end - start)
        chunks = _split_line(line)
        weights = [len(c) for c in chunks]
        total_w = sum(weights) or 1
        t = start
        for chunk, w in zip(chunks, weights):
            seg = dur * (w / total_w)
            parts.append(
                f"{idx}\n{_fmt_ts(t)} --> {_fmt_ts(t + seg)}\n{chunk}\n"
            )
            idx += 1
            t += seg
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Platform metadata
# ---------------------------------------------------------------------------

def build_platform_metadata(brief: dict, fs: dict) -> dict:
    industry = brief.get("industry", "")
    company  = brief.get("company", "")
    product  = brief.get("product", "")
    cta_text = brief.get("cta", f"Try {product} today")
    benefit  = brief.get("key_benefit", "")
    proof    = brief.get("proof_point", "")

    base_tags = _INDUSTRY_HASHTAGS.get(industry, _FALLBACK_HASHTAGS)

    # Build caption text
    short_headline = f"{company} — {product}" if company and product else product or company
    base_desc = (
        f"{short_headline}\n\n"
        f"{benefit}.\n"
        f"{cta_text}.\n\n"
        f"Formula: {fs['formula']}"
    )

    # Add brand hashtag
    brand_tag = f"#{re.sub(r'[^a-zA-Z0-9]', '', company)}" if company else ""

    ig_tags  = (base_tags.get("instagram", []) + ([brand_tag] if brand_tag else []))[:25]
    tt_tags  = (base_tags.get("tiktok", []) + ([brand_tag] if brand_tag else []))[:12]
    yt_tags  = (base_tags.get("youtube", []) + ([brand_tag] if brand_tag else []))[:15]

    return {
        "instagram": {
            "caption": f"{short_headline}\n\n{benefit}.\n\n{cta_text}.",
            "hashtags": ig_tags,
            "full_post": f"{short_headline}\n\n{benefit}.\n\n{cta_text}.\n\n{' '.join(ig_tags)}",
        },
        "tiktok": {
            "caption": short_headline,
            "description": base_desc,
            "hashtags": tt_tags,
            "full_caption": f"{short_headline} {' '.join(tt_tags)}",
        },
        "youtube": {
            "title": f"{short_headline} #Shorts",
            "description": (
                f"{short_headline}\n\n{benefit}.\n\n{proof}.\n\n{cta_text}.\n\n"
                + " ".join(yt_tags)
            ),
            "tags": yt_tags,
        },
        "_formula": fs["formula"],
        "_industry": industry,
    }


# ---------------------------------------------------------------------------
# Image prompt (9:16 background for Pollinations / DALL-E)
# ---------------------------------------------------------------------------

def build_image_prompt(brief: dict, fs: dict) -> str:
    company  = brief.get("company", "Brand")
    product  = brief.get("product", "Product")
    industry = brief.get("industry", "business")
    benefit  = brief.get("key_benefit", "")
    formula  = fs["formula"]

    mood_map = {
        "aida": "aspirational, bright, energetic — lifestyle photography feel",
        "pas":  "dramatic contrast — dark problem side → bright solution side, split composition",
        "bab":  "transformation, before/after feel, hopeful warm palette",
    }
    mood = mood_map.get(formula, "professional, clean, modern")

    industry_visual = {
        "crypto":      "digital blockchain network, neon blue/green glow, trading charts",
        "real_estate": "luxury property exterior, golden hour lighting, clean architecture",
        "fitness":     "athletic person mid-workout, sweat, gym environment, strong lighting",
        "ecommerce":   "product flat-lay on clean background, lifestyle context, modern design",
        "finance":     "ascending graph, modern office, wealth symbols, navy and gold palette",
        "tech":        "sleek UI on screen, code aesthetic, dark background, electric blue accents",
        "food":        "delicious plated dish, steam, natural light, bokeh background",
        "fashion":     "model wearing outfit, urban environment, editorial feel, high contrast",
        "travel":      "stunning destination landscape, golden hour, wanderlust energy",
        "education":   "person studying, books + laptop, warm focused light, motivational feel",
        "healthcare":  "clean medical/wellness environment, trust-building, calm blue palette",
        "marketing":   "growth graph, laptop with analytics dashboard, professional workspace",
    }.get(industry, "professional business environment, modern, clean design")

    return (
        f"9:16 vertical ad background (1080x1920) for {company} — {product}\n"
        f"Industry: {industry}\n"
        f"Mood: {mood}\n"
        f"Visual: {industry_visual}\n"
        f"Key benefit suggestion: {benefit}\n"
        f"Style: cinematic, high-quality, no text overlays (text added in post)\n"
        f"Safe zone: leave center 60% clear for avatar and text overlay\n"
        f"Bottom third: slightly darker for subtitle readability\n"
        f"Top area: brand logo placement zone (keep clean)\n"
        f"DO NOT include any people's faces (avatar handles that separately)\n"
    )


# ---------------------------------------------------------------------------
# HeyGen brief
# ---------------------------------------------------------------------------

def build_heygen_brief(brief: dict, fs: dict) -> dict:
    formula  = fs["formula"]
    industry = brief.get("industry", "")
    goal     = brief.get("goal", "brand_awareness")
    brand_voice = brief.get("brand_voice", "professional")

    # Voice style per formula + brand voice
    voice_energy = {
        "aida": "confident and energetic — rising intonation on the DESIRE section",
        "pas":  "empathetic and building — start soft, peak urgency at SOLUTION",
        "bab":  "storytelling pace — emotional BEFORE, hopeful AFTER, decisive BRIDGE",
    }.get(formula, "professional and clear")

    # Avatar recommendation
    avatar_tone = {
        "premium":       "suit or smart casual — authoritative presenter",
        "casual":        "casual clothing — relatable, friendly vibe",
        "authoritative": "professional suit — expert, news-anchor style",
        "energetic":     "sporty or streetwear — high energy, dynamic",
        "friendly":      "casual smart — warm, approachable, smiling",
    }.get(brand_voice, "business casual — versatile, professional yet approachable")

    return {
        "recommended_avatar_style": avatar_tone,
        "voice_style":              voice_energy,
        "background_color":         "#00FF00",
        "resolution":               "1080x1920",
        "aspect_ratio":             "9:16",
        "estimated_duration_s":     45,
        "talking_speed":            "normal",
        "notes": (
            f"Industry: {industry}. Goal: {goal}. "
            f"Emphasise the hook (first 3 seconds) — this is what stops the scroll. "
            f"Cut to product B-roll during middle sections. "
            f"End with strong CTA — avatar looking directly at camera."
        ),
        "composite_instructions": (
            "Background: use 06_image_prompt.txt to generate via Pollinations.ai. "
            "Avatar: 270px width, bottom-right corner (20px margin right, 180px margin bottom for subtitles). "
            "Green screen chroma key: similarity=0.25, blend=0.08. "
            "Subtitles: burnt-in from 04_captions.srt — white bold, black outline, bottom-center."
        ),
    }


# ---------------------------------------------------------------------------
# Hook variants file
# ---------------------------------------------------------------------------

def build_hook_variants_text(hooks: list[str], brief: dict) -> str:
    formula = _select_formula(brief)
    lines   = [
        f"HOOK VARIANTS — A/B/C TEST",
        f"Formula selected: {formula.upper()}",
        f"Company: {brief.get('company', '?')}  |  Product: {brief.get('product', '?')}",
        f"Industry: {brief.get('industry', '?')}  |  Goal: {brief.get('goal', '?')}",
        "",
        "Test each hook independently — measure 3-second retention rate.",
        "Winner → use in final HeyGen generation.",
        "",
    ]
    labels = ["A (Direct address)", "B (Question)", "C (Social proof / POV)"]
    for i, (hook, label) in enumerate(zip(hooks, labels)):
        lines.append(f"VARIANT {label}:")
        lines.append(f'  "{hook}"')
        lines.append("")
    lines.append("— TIMING: 0:00-0:03  |  SHOT: avatar_close_up, micro zoom-in, no cut —")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def write_package(brief: dict, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    hooks  = _hooks(brief)
    hook_a = hooks[0]

    fs     = _build_formula_script(brief, hook_a)
    scenes = build_scenes(fs, brief)

    # 01 — ad script
    (out_dir / "01_ad_script.txt").write_text(
        render_script_text(fs), encoding="utf-8"
    )

    # 02 — hook variants
    (out_dir / "02_hook_variants.txt").write_text(
        build_hook_variants_text(hooks, brief), encoding="utf-8"
    )

    # 03 — scenes
    (out_dir / "03_scenes.json").write_text(
        json.dumps({"company": brief.get("company"), "scenes": scenes}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # 04 — captions SRT
    (out_dir / "04_captions.srt").write_text(
        build_srt(scenes), encoding="utf-8"
    )

    # 05 — platform metadata
    meta = build_platform_metadata(brief, fs)
    (out_dir / "05_platform_metadata.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # 06 — image prompt
    (out_dir / "06_image_prompt.txt").write_text(
        build_image_prompt(brief, fs), encoding="utf-8"
    )

    # 07 — HeyGen brief
    (out_dir / "07_heygen_brief.json").write_text(
        json.dumps(build_heygen_brief(brief, fs), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Save brief copy for reference
    (out_dir / "brief.json").write_text(
        json.dumps(brief, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return {
        "formula":  fs["formula"],
        "industry": brief.get("industry", ""),
        "company":  brief.get("company", ""),
        "product":  brief.get("product", ""),
        "hooks":    hooks,
        "scenes":   len(scenes),
        "out_dir":  str(out_dir),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Viral ad script writer")
    ap.add_argument("brief_json", help="path to brief.json")
    ap.add_argument("--out-dir", default=None, help="output directory")
    args = ap.parse_args(argv[1:])

    brief_path = Path(args.brief_json).resolve()
    if not brief_path.exists():
        print(f"[ad_script_writer] brief not found: {brief_path}", file=sys.stderr)
        return 1

    brief = json.loads(brief_path.read_text(encoding="utf-8-sig"))
    out_dir = (
        Path(args.out_dir).resolve()
        if args.out_dir
        else brief_path.parent / f"ad_{brief.get('company','brand').replace(' ','_').lower()}"
    )

    result = write_package(brief, out_dir)
    print(
        f"[ad_script_writer] ✓ {result['formula'].upper()} script for "
        f"{result['company']} — {result['product']} "
        f"({result['industry']}) → {result['scenes']} scenes → {out_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
