"""Image Generator — Agent 4 of the Crypto AutoPost pipeline.

Generates a professional crypto-themed card image:
  1. Downloads an AI background from Pollinations.ai (free, no key)
     — 10 cinematic crypto styles: Bloomberg terminal, trading floor,
       candlestick charts, neon crypto, DeFi dashboard, etc.
  2. Composites headline + summary + branding on top with Pillow

Outputs:
  image_x_1200x675.png   — X / Twitter OG card  (16:9)
  image_tg_1080x1080.png — Telegram square       (1:1)
  image_reel_1080x1920.png — Reels/Shorts/TikTok (9:16, optional)
"""
from __future__ import annotations

import hashlib
import io
import json
import random
import re
import sys
import textwrap
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

# Force UTF-8 output on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
_BRAIN_PARAMS = _REPO_ROOT / "analytics" / "agent_brains" / "image_gen" / "learned_params.json"
_STYLE_LOG    = _REPO_ROOT / "analytics" / "agent_brains" / "image_gen" / "style_history.json"
_CACHE_DIR    = _REPO_ROOT / ".image_cache"  # Local cache for generated images

# Ensure cache dir exists
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Mood classification
# ---------------------------------------------------------------------------

BEAR_KEYWORDS = {
    "hack", "exploit", "drain", "stolen", "breach", "crash", "plunge",
    "liquidation", "liquidated", "lawsuit", "rejected", "collapse", "ban",
    "banned", "fraud", "arrest", "indicted", "scam", "rug pull", "attack",
}
BULL_KEYWORDS = {
    "record", "all-time high", "ath", "surge", "rally", "approval", "approved",
    "launch", "inflow", "partnership", "upgrade", "accumulation", "bullish",
    "breakout", "milestone", "etf", "institutional", "buy", "buys", "purchased",
}


def classify_mood(story: dict) -> str:
    title = (story.get("title") or "").lower()
    for kw in BEAR_KEYWORDS:
        if kw in title:
            return "bear"
    for kw in BULL_KEYWORDS:
        if kw in title:
            return "bull"
    return "neutral"


# ---------------------------------------------------------------------------
# Image caching — avoid re-rendering identical prompts
# ---------------------------------------------------------------------------

def _cache_key(title: str, prompt: str, width: int, height: int,
               seed: int = 0) -> str:
    """Generate unique cache key from story + prompt + dimensions + DAY + SEED.

    Including the date ordinal ensures we deliver a *different* image every
    day even when the story title repeats (e.g. recurring "Bitcoin price"
    headlines). Including `seed` makes manual re-renders bypass the cache.
    """
    today = datetime.now().toordinal()
    text = f"{title}|{prompt}|{width}|{height}|{today}|{seed}"
    return hashlib.md5(text.encode()).hexdigest()


def _get_cached_image(cache_key: str) -> "Path | None":
    """Check if cached image exists; return path or None."""
    cached = _CACHE_DIR / f"{cache_key}.png"
    if cached.exists():
        print(f"[image_gen] ✓ cache HIT: {cache_key[:8]}…", file=sys.stderr)
        return cached
    return None


def _store_cached_image(src_path: Path, cache_key: str) -> None:
    """Store rendered image in cache for reuse."""
    if not src_path.exists():
        return
    try:
        import shutil
        dst = _CACHE_DIR / f"{cache_key}.png"
        shutil.copy2(str(src_path), str(dst))
        print(f"[image_gen] cached: {cache_key[:8]}…", file=sys.stderr)
    except Exception as exc:
        print(f"[image_gen] cache store failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Background styles — 10 cinematic crypto themes
# ---------------------------------------------------------------------------

# Each mood maps to a list of style variants; daily rotation picks one per day
BG_STYLES: dict[str, list[str]] = {
    "bull": [
        # Bloomberg terminal — green data flood
        "professional Bloomberg terminal trading room, walls of screens showing green "
        "cryptocurrency price charts surging upward, bitcoin bull run, dramatic emerald "
        "and gold lighting, dark luxury office, cinematic depth of field, 8k photorealistic, "
        "no text no watermark no UI elements",

        # Trading floor — euphoria
        "busy Wall Street crypto trading floor, dozens of monitors all showing green "
        "candlestick charts, traders celebrating, warm golden ambient lighting, "
        "wide cinematic shot, photorealistic, 8k, no text no watermark",

        # Rocket launch visualisation
        "cinematic shot of Bitcoin rocket launching through dark space filled with "
        "glowing green price charts and ascending trend lines, dramatic light rays, "
        "deep space background, photorealistic concept art, 8k, no text no watermark",

        # Luxury crypto office — bull
        "ultra-modern crypto hedge fund office at night, floor-to-ceiling windows "
        "overlooking city skyline, curved monitors showing green price action, "
        "warm accent lighting, photorealistic interior, 8k, no text no watermark",

        # Institutional whale accumulation
        "prestigious private bank vault doors opening, stacks of gold bars beside "
        "glowing Bitcoin hardware wallets, dark luxury atmosphere, warm amber light, "
        "cinematic depth of field, 8k photorealistic, no text no watermark",

        # Crypto nation-state adoption
        "government building at golden hour, holographic Bitcoin symbol floating above, "
        "national flags lining grand boulevard, official adoption ceremony aesthetic, "
        "epic wide angle, 8k photorealistic, no text no watermark",

        # Minimalist editorial bull — clean and modern
        "minimalist white and gold editorial background, single large upward trending "
        "abstract arrow in bright gold, clean negative space, high-end financial magazine "
        "aesthetic, soft bokeh, 8k photorealistic, no text no watermark",

        # Nature sunrise — dawn of a bull market
        "epic sunrise over modern city financial district, golden rays breaking through "
        "skyscrapers, warm orange and yellow sky reflecting on glass towers, optimistic "
        "atmosphere, aerial cinematic view, 8k photorealistic, no text no watermark",

        # Abstract data art — bull
        "beautiful abstract visualization of financial data flowing upward, golden "
        "luminous particles forming ascending curves against dark background, "
        "data art concept, premium aesthetic, 8k render, no text no watermark",

        # Mountain peak — achievement
        "climber reaching mountain summit at sunrise, gold light flooding the peak, "
        "vast landscape below, triumph and achievement concept art, epic composition, "
        "photorealistic, 8k, no text no watermark",

        # Futuristic bridge — progress
        "futuristic illuminated bridge spanning between two gleaming skyscrapers at "
        "night, gold and white light trails, motion and progress concept, wide cinematic, "
        "8k photorealistic, no text no watermark",

        # Premium watch + crypto — wealth
        "luxury timepiece on dark marble surface beside a glowing Bitcoin coin, "
        "premium lifestyle flat lay, single dramatic spotlight, shallow depth of field, "
        "editorial photography style, 8k, no text no watermark",
    ],

    "bear": [
        # Red alert trading terminal
        "professional cryptocurrency trading terminal in dark room, multiple ultrawide "
        "monitors showing red candlestick charts collapsing, dramatic crimson warning "
        "lights, emergency alert atmosphere, cinematic tension, 8k photorealistic, "
        "no text no watermark no UI elements",

        # Stormy market crash
        "dramatic dark crypto trading floor, every screen showing red declining charts, "
        "ominous red glow, traders looking stressed, stormy atmosphere, cinematic, "
        "8k photorealistic, no text no watermark",

        # Broken market — abstract
        "broken glass reflecting red cryptocurrency price charts, dark shattered "
        "digital landscape, dramatic red and black color palette, abstract financial "
        "crisis concept art, cinematic, 8k, no text no watermark",

        # Whale sell-off
        "underwater scene with giant whale shadow, scattered bitcoins sinking, "
        "deep red water with descending price tickers glowing, dark and ominous, "
        "photorealistic concept art, 8k, no text no watermark",

        # Regulatory crackdown
        "dark courthouse with heavy storm clouds, red regulatory gavel slamming down, "
        "crypto logos crumbling, dramatic institutional pressure concept art, "
        "cinematic, 8k photorealistic, no text no watermark",

        # Geopolitical crypto crisis
        "dark war room with multiple screens showing falling Bitcoin charts, "
        "world map with conflict zones highlighted in red, crisis atmosphere, "
        "cold blue emergency lighting, cinematic tension, 8k photorealistic, no text no watermark",

        # Storm over city — bear editorial
        "dramatic storm clouds rolling over modern financial district, lightning "
        "illuminating skyscrapers, ominous dark sky, intense cinematic weather, "
        "photorealistic, 8k, no text no watermark",

        # Abstract descent — data art
        "beautiful abstract data art showing cascading red particles falling in elegant "
        "curves, dark premium background, financial decline concept, fine art aesthetic, "
        "8k render, no text no watermark",

        # Frozen market — cold aesthetic
        "abstract digital landscape frozen in ice crystals, blue and silver tones, "
        "cold sharp aesthetic representing market freeze, concept art, cinematic depth, "
        "8k photorealistic, no text no watermark",

        # Documentary — empty trading floor
        "empty dramatic trading floor after hours, screens still glowing red, "
        "abandoned workstations, haunting atmosphere, documentary-style photography, "
        "cinematic low light, 8k photorealistic, no text no watermark",
    ],

    "neutral": [
        # Classic Bloomberg terminal — blue/white
        "professional Bloomberg terminal setup, dark room, multiple monitors displaying "
        "cryptocurrency candlestick charts and market data, cool blue accent lighting, "
        "cinematic composition, 8k photorealistic, no text no watermark no UI elements",

        # DeFi dashboard glow
        "futuristic DeFi protocol dashboard glowing in dark room, multiple screens "
        "with on-chain analytics, wallet addresses, liquidity pools, purple and cyan "
        "neon lighting, cyberpunk aesthetic, 8k photorealistic, no text no watermark",

        # Neon crypto city
        "cyberpunk cityscape at night with giant holographic Bitcoin and Ethereum "
        "logos, neon-lit crypto exchange signs, rain-soaked streets reflecting "
        "colorful lights, cinematic wide shot, 8k photorealistic, no text no watermark",

        # Macro chart background
        "extreme close-up of cryptocurrency candlestick chart on ultra-high-resolution "
        "trading monitor, dark background, green and red candles with volume bars, "
        "shallow depth of field, cinematic macro photography, 8k, no text no watermark",

        # Server room / blockchain
        "dramatic server room with glowing blockchain nodes and data streams, "
        "blue and white light rays through server racks, concept of crypto infrastructure, "
        "cinematic wide shot, 8k photorealistic, no text no watermark",

        # Central bank meets crypto
        "neoclassical Federal Reserve building at dusk, giant holographic Bitcoin "
        "looming over marble facade, tension between old finance and new, "
        "dramatic blue-grey cinematic lighting, 8k photorealistic, no text no watermark",

        # Gold + crypto hybrid
        "luxury bank vault with gold bars stacked next to Bitcoin logo etched in steel, "
        "dark premium aesthetic, single spotlight, cinematic still life, "
        "8k photorealistic, no text no watermark",

        # Macro trading desk
        "executive macro trading desk at night, three ultrawide monitors showing "
        "global market charts — equities, forex, crypto — all blue tones, "
        "city view through floor-to-ceiling glass, cinematic, 8k photorealistic, no text no watermark",

        # Editorial newsroom — premium journalism
        "high-end financial newsroom at night, glowing editorial desks, reporters "
        "working under dramatic overhead lighting, journalism meets technology aesthetic, "
        "clean and authoritative, 8k photorealistic, no text no watermark",

        # Abstract geometric — data art
        "abstract three-dimensional geometric shapes representing blockchain nodes, "
        "deep space dark background with cool indigo and silver tones, mathematical "
        "precision art, premium concept render, 8k, no text no watermark",

        # Corporate rooftop — aerial city
        "aerial view of futuristic financial district at blue hour, grid of illuminated "
        "skyscrapers, cool blue and silver palette, calm and analytical atmosphere, "
        "8k photorealistic drone shot, no text no watermark",

        # Underwater world — deep finance
        "luminous abstract underwater scene with glowing crypto symbols drifting, "
        "deep ocean blue, bioluminescent particles, mysterious premium aesthetic, "
        "cinematic concept art, 8k, no text no watermark",

        # Library / archive — research
        "grand dark library with towering bookshelves, single spotlight on open book, "
        "warm amber glow in darkness, knowledge and research concept, timeless premium "
        "aesthetic, 8k photorealistic, no text no watermark",

        # Minimal gradient — ultra-clean
        "ultra-minimal dark gradient background transitioning from deep navy to "
        "charcoal black, single thin horizontal luminous line mid-frame, "
        "premium brand aesthetic, editorial still, 8k, no text no watermark",

        # Tech lab — innovation
        "futuristic research laboratory at night, holographic displays floating in air, "
        "scientists' silhouettes, cool white and blue lighting, innovation concept, "
        "wide cinematic, 8k photorealistic, no text no watermark",

        # Rain on glass — mood + depth
        "close-up of rain drops on dark glass window with blurred city lights behind, "
        "moody and atmospheric, bokeh light effects, blue and amber tones, "
        "fine art photography style, 8k, no text no watermark",
    ],
}

# Keep legacy BG_PROMPTS dict for dashboard image-regen (single default per mood)
BG_PROMPTS: dict[str, str] = {k: v[0] for k, v in BG_STYLES.items()}


# ---------------------------------------------------------------------------
# Story-type classifier → specific visual concept per story
# ---------------------------------------------------------------------------

_STORY_TYPES: list[tuple[str, set[str]]] = [
    ("etf_institutional", {"etf", "blackrock", "fidelity", "grayscale", "institutional",
                           "spot etf", "sec approval", "asset manager", "wall street",
                           "hedge fund", "pension", "investment firm", "microstrategy"}),
    ("hack_exploit",      {"hack", "exploit", "drain", "stolen", "breach", "attack",
                           "vulnerability", "compromised", "rug pull", "scam", "fraud",
                           "phishing", "theft", "heist", "stolen funds"}),
    ("regulation_legal",  {"sec", "cftc", "regulation", "regulatory", "lawsuit", "ban",
                           "banned", "arrest", "indicted", "compliance", "kyc", "aml",
                           "government", "congress", "senate", "cbdc", "central bank"}),
    ("price_milestone",   {"all-time high", "ath", "record", "100k", "new high", "breakout",
                           "crashed", "crash", "plunge", "collapse", "liquidation",
                           "liquidated", "bull run", "bear market"}),
    ("defi_protocol",     {"defi", "protocol", "liquidity", "tvl", "yield", "staking",
                           "lending", "borrowing", "amm", "dex", "uniswap", "aave",
                           "compound", "curve", "dao", "governance", "smart contract"}),
    ("exchange_news",     {"exchange", "binance", "coinbase", "kraken", "okx", "bybit",
                           "listing", "delisting", "trading pair", "spot trading",
                           "derivatives", "futures", "options", "perpetual"}),
    ("mining_energy",     {"mining", "miner", "hashrate", "difficulty", "halving",
                           "energy", "power", "electricity", "asic", "gpu mining",
                           "proof of work", "bitcoin mining"}),
    ("layer2_tech",       {"layer 2", "l2", "lightning", "optimism", "arbitrum", "polygon",
                           "rollup", "zkproof", "zk-rollup", "scaling", "ethereum upgrade",
                           "eip", "merge", "dencun", "blob"}),
    ("nft_gaming",        {"nft", "non-fungible", "metaverse", "gaming", "play-to-earn",
                           "opensea", "blur", "digital art", "collection", "mint"}),
    ("macro_economy",     {"fed", "federal reserve", "inflation", "interest rate", "cpi",
                           "recession", "gdp", "dollar", "treasury", "yield curve",
                           "macro", "economy", "financial crisis", "banking"}),
    ("geopolitical",      {"war", "sanctions", "ukraine", "russia", "china", "taiwan",
                           "middle east", "conflict", "trade war", "tariff", "tariffs",
                           "geopolitical", "military", "nato", "opec", "oil crisis",
                           "energy crisis", "embargo"}),
    ("central_bank",      {"fomc", "rate cut", "rate hike", "quantitative easing", "qt",
                           "powell", "lagarde", "ecb", "boj", "pboc",
                           "central bank digital", "cbdc", "balance sheet",
                           "interest rate decision", "tapering", "money supply"}),
    ("gold_commodities",  {"gold price", "gold record", "silver", "commodity",
                           "oil price", "crude oil", "gold all-time", "safe haven",
                           "store of value", "hard assets", "precious metals"}),
    ("luxury_wealth",     {"billionaire", "whale buys", "ultra high net worth",
                           "hedge fund", "sovereign wealth", "family office",
                           "luxury", "wealth management", "private equity",
                           "institutional investor", "asset manager"}),
    ("market_data",       {"nonfarm payroll", "jobs report", "cpi report", "pce data",
                           "pmi reading", "gdp growth", "retail sales", "ism",
                           "unemployment rate", "consumer confidence"}),
]

# Per story-type visual prompts (2 variants each: bull/positive, bear/negative)
_TYPE_PROMPTS: dict[str, dict[str, str]] = {
    "etf_institutional": {
        "bull": "prestigious Wall Street trading floor, Bloomberg terminals showing Bitcoin ETF approval headline, suited traders celebrating, golden hour light through floor-to-ceiling windows, ultra-cinematic, 8k photorealistic, no text no watermark",
        "bear": "tense institutional trading floor, Bloomberg screens showing ETF rejection headline, worried analysts, cold blue fluorescent lighting, cinematic tension, 8k photorealistic, no text no watermark",
        "neutral": "professional institutional asset management office, multiple Bloomberg terminals, city skyline at dusk, sophisticated dark interior, cool blue and gold lighting, 8k photorealistic, no text no watermark",
    },
    "hack_exploit": {
        "bull": "digital fortress glowing with cyan security shields, locked vault with Bitcoin symbol, matrix code streams, triumphant cybersecurity aesthetic, dark background with blue accents, cinematic 8k, no text no watermark",
        "bear": "dramatic dark cyber attack visualization, red warning alerts flooding multiple screens, shattered digital vault, ominous red binary rain, emergency lighting, cinematic, 8k photorealistic, no text no watermark",
        "neutral": "advanced cybersecurity operations center, dark room with blue holographic network maps, analysts monitoring threat screens, cinematic depth of field, 8k photorealistic, no text no watermark",
    },
    "regulation_legal": {
        "bull": "gleaming government building at golden hour with Bitcoin hologram overhead, handshake between suited figures, confident atmosphere, warm cinematic lighting, 8k photorealistic, no text no watermark",
        "bear": "dark courthouse with stormy sky, red regulatory stamp concept, tension and weight of law, cold gray marble, dramatic shadows, cinematic, 8k photorealistic, no text no watermark",
        "neutral": "neoclassical government building exterior at dusk, balanced scales concept, sophisticated atmosphere, cool blue and gray tones, 8k photorealistic, no text no watermark",
    },
    "price_milestone": {
        "bull": "breathtaking rocket launch through dark space, Bitcoin symbol on rocket, green price chart trajectory as launch path, golden stars, epic wide angle shot, 8k photorealistic concept art, no text no watermark",
        "bear": "dramatic market crash visualization, red candlestick charts plunging on giant screens, shattered glass reflecting falling prices, ominous dark atmosphere, cinematic, 8k photorealistic, no text no watermark",
        "neutral": "extreme close-up of cryptocurrency candlestick chart, dark background, green and red candles with volume bars, cinematic macro photography, shallow depth of field, 8k, no text no watermark",
    },
    "defi_protocol": {
        "bull": "futuristic DeFi protocol interface, glowing green liquidity pools and yield farms visualized as flowing rivers of light, cyberpunk smart contract nodes, vibrant purple and cyan neon, 8k photorealistic, no text no watermark",
        "bear": "collapsed DeFi protocol visualization, dark withered network nodes, drained liquidity shown as empty glowing vessels, ominous red glow, cinematic dystopian tech, 8k, no text no watermark",
        "neutral": "beautiful DeFi ecosystem visualization, interconnected glowing protocol nodes, dark background, flowing liquidity streams in blue and purple, holographic blockchain network, 8k photorealistic, no text no watermark",
    },
    "exchange_news": {
        "bull": "modern cryptocurrency exchange interior, massive trading screens showing green charts, confident traders at terminals, sleek tech aesthetic, warm professional lighting, 8k photorealistic, no text no watermark",
        "bear": "cryptocurrency exchange under pressure, red alert screens, stressed trading floor atmosphere, dark and tense, cinematic shadows, 8k photorealistic, no text no watermark",
        "neutral": "sleek cryptocurrency exchange server room, rows of blinking servers, blue LED lighting, professional tech infrastructure, cinematic wide shot, 8k photorealistic, no text no watermark",
    },
    "mining_energy": {
        "bull": "massive Bitcoin mining facility at night, rows of ASICs glowing orange, powerful hum of machines, epic industrial scale, orange and gold light, cinematic wide shot, 8k photorealistic, no text no watermark",
        "bear": "dark shuttered mining facility, offline rigs covered in dust, eerie empty industrial space, dim emergency lighting, cinematic, 8k photorealistic, no text no watermark",
        "neutral": "dramatic Bitcoin mining farm, long corridors of ASIC miners glowing in dark, blue and orange accent lighting, industrial cinematic composition, 8k photorealistic, no text no watermark",
    },
    "layer2_tech": {
        "bull": "futuristic Ethereum layer-2 network visualization, glowing transaction bridges connecting blockchain layers, fast-moving data streams, cyberpunk blue and purple, epic scale, 8k photorealistic, no text no watermark",
        "bear": "disrupted blockchain network, broken bridge between layer visualization, dark fragmented nodes, ominous atmosphere, cinematic, 8k photorealistic, no text no watermark",
        "neutral": "beautiful Ethereum network architecture visualization, layered blockchain as glowing geometric structure, dark background, cyan and purple nodes, cinematic depth, 8k photorealistic, no text no watermark",
    },
    "nft_gaming": {
        "bull": "vibrant digital art gallery in metaverse, glowing NFT artworks on futuristic walls, colorful and energetic, avatars walking through, cyberpunk aesthetic, 8k photorealistic, no text no watermark",
        "bear": "abandoned metaverse space, empty digital gallery, faded NFT frames, dark and desolate virtual world, cinematic melancholy, 8k photorealistic, no text no watermark",
        "neutral": "immersive digital art gallery, colorful NFT frames on dark walls, soft gallery lighting, modern virtual space, cinematic composition, 8k photorealistic, no text no watermark",
    },
    "macro_economy": {
        "bull": "Federal Reserve building at golden sunrise, green financial charts superimposed, optimistic economic dawn, warm cinematic light, 8k photorealistic, no text no watermark",
        "bear": "stormy dark sky over Wall Street, red economic charts descending, Federal Reserve building in dramatic shadows, ominous financial atmosphere, cinematic, 8k photorealistic, no text no watermark",
        "neutral": "Federal Reserve building exterior, serious economic atmosphere, cool blue dawn light, symbolic scale and weight, cinematic composition, 8k photorealistic, no text no watermark",
    },
    "geopolitical": {
        "bull": "dramatic sunset over world map with glowing Bitcoin symbol, diplomats shaking hands in grand hall, golden flags, cinematic wide shot, optimistic geopolitical shift, 8k photorealistic, no text no watermark",
        "bear": "dark storm clouds over capitol buildings worldwide, red alert geopolitical tension, tanks silhouettes and crumbling currency symbols, dramatic war-room atmosphere, cinematic, 8k photorealistic, no text no watermark",
        "neutral": "United Nations building at dusk, world flags reflecting in water, globe hologram concept, serious diplomatic atmosphere, cool blue tones, cinematic, 8k photorealistic, no text no watermark",
    },
    "central_bank": {
        "bull": "Federal Reserve marble hall flooded with golden light, chairman's podium with Bitcoin hologram above, optimistic rate-cut atmosphere, warm cinematic glow, 8k photorealistic, no text no watermark",
        "bear": "Federal Reserve press conference room, red emergency lighting, rate hike announcement tension, dark suited figures, ominous cinematic shadows, 8k photorealistic, no text no watermark",
        "neutral": "Federal Reserve building rotunda interior, marble columns, serious monetary policy atmosphere, cool institutional blue and white light, cinematic depth, 8k photorealistic, no text no watermark",
    },
    "gold_commodities": {
        "bull": "gleaming gold bars stacked in a vault with Bitcoin coins alongside, warm golden spotlight, luxury bank safe aesthetic, rich amber and gold tones, cinematic depth, 8k photorealistic, no text no watermark",
        "bear": "melting gold bars with red price charts, dark vault atmosphere, dramatic commodity selloff concept, red and black cinematic color grade, 8k photorealistic, no text no watermark",
        "neutral": "dramatic close-up of polished gold bar with Bitcoin symbol reflection, dark luxury background, metallic texture, cinematic product photography, 8k photorealistic, no text no watermark",
    },
    "luxury_wealth": {
        "bull": "ultra-luxury penthouse overlooking city at night, Bitcoin portfolio on curved ultrawide monitor, champagne glasses reflecting digital charts, billionaire lifestyle aesthetic, warm luxury lighting, 8k photorealistic, no text no watermark",
        "bear": "luxury trading floor in crisis mode, red screens, abandoned coffee cups on mahogany desk, dramatic contrast between wealth and loss, ominous cinematic, 8k photorealistic, no text no watermark",
        "neutral": "prestigious private banking office at dusk, dark wood paneling, Bloomberg terminal glowing on antique desk, city view through floor-to-ceiling windows, understated wealth aesthetic, 8k photorealistic, no text no watermark",
    },
    "market_data": {
        "bull": "economic data release celebration on trading floor, green CPI numbers on giant screens, optimistic analysts, warm golden market data aesthetic, cinematic wide shot, 8k photorealistic, no text no watermark",
        "bear": "economic data shock moment, red jobs report numbers on multiple screens, tense trading floor, dramatic red lighting, 8k photorealistic, no text no watermark",
        "neutral": "Bloomberg data terminal extreme close-up, economic indicators glowing in the dark, professional market analysis environment, cool blue light, cinematic macro shot, 8k photorealistic, no text no watermark",
    },
}


def _classify_story_type(story: dict) -> str | None:
    """Return the most specific story type, or None to fall back to mood-based styles."""
    text = " ".join([
        (story.get("title") or ""),
        (story.get("summary") or ""),
        " ".join(story.get("tickers") or []),
        (story.get("source") or ""),
    ]).lower()

    best_type = None
    best_count = 0
    for story_type, keywords in _STORY_TYPES:
        count = sum(1 for kw in keywords if kw in text)
        if count > best_count:
            best_count = count
            best_type = story_type

    return best_type if best_count >= 1 else None


def _gather_post_context(story: dict | None) -> dict:
    """Pull the actual X / Telegram post bodies (and niche) for the slot the
    story belongs to, so the image agent can collaborate with the copywriter
    output instead of working from the headline alone.

    Returns a dict with optional keys: post_x_a, post_x_b, post_tg, niche.
    Failures are silent — the function is purely best-effort enrichment.
    """
    import os
    ctx: dict = {}
    try:
        slot_dir = (story or {}).get("_slot_dir")
        if slot_dir:
            sd = Path(slot_dir)
            for fname, key in (("post_x.txt", "post_x_a"),
                                ("post_x_b.txt", "post_x_b"),
                                ("post_tg.txt", "post_tg")):
                fp = sd / fname
                if fp.exists():
                    try:
                        ctx[key] = fp.read_text(encoding="utf-8").strip()[:600]
                    except Exception:
                        pass
        ctx["niche"] = os.environ.get("NICHE", "").strip() or "crypto"
    except Exception:
        pass
    return ctx


_IMG_PROMPT_CACHE: dict[str, tuple[float, str]] = {}
_IMG_PROMPT_TTL = 86_400  # 24 h

def _llm_bg_prompt(story: dict, mood: str) -> str | None:
    """Ask Claude for a custom, virality-optimized background prompt.

    Strategy: the image agent collaborates with the copywriter (post bodies),
    the ranker (mood + tickers), and the niche config to pick a scene that
    maximizes scroll-stop and click-through. Returns None if API key missing.
    """
    import os, time as _t
    api_key = (
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
        or os.environ.get("ANTHROPIC_KEY", "").strip()
    )
    if not api_key:
        return None

    _ck = hashlib.md5(
        f"{story.get('title','')[:80]}|{mood}".encode("utf-8", errors="replace")
    ).hexdigest()
    _now = _t.time()
    if _ck in _IMG_PROMPT_CACHE:
        _ts, _val = _IMG_PROMPT_CACHE[_ck]
        if (_now - _ts) < _IMG_PROMPT_TTL:
            return _val
    post_ctx = _gather_post_context(story)
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        post_lines = []
        if post_ctx.get("post_x_a"): post_lines.append(f"X (variant A): {post_ctx['post_x_a']}")
        if post_ctx.get("post_x_b"): post_lines.append(f"X (variant B): {post_ctx['post_x_b']}")
        if post_ctx.get("post_tg"):  post_lines.append(f"Telegram: {post_ctx['post_tg']}")
        post_block = "\n".join(post_lines) or "(post text not yet generated)"
        niche = post_ctx.get("niche") or "crypto"
        prompt = (
            f"You are the IMAGE strategist for a {niche} news channel. Your "
            f"job is to design a scroll-stopping, click-magnet background "
            f"image that pairs with the EXACT post the copywriter wrote.\n\n"
            f"=== STORY ===\n"
            f"Title: {story.get('title','')}\n"
            f"Summary: {(story.get('summary') or '')[:300]}\n"
            f"Tickers: {', '.join(story.get('tickers') or [])}\n"
            f"Source: {story.get('source','')}\n"
            f"Mood (ranker output): {mood}\n\n"
            f"=== POST COPY (what the image sits next to) ===\n"
            f"{post_block}\n\n"
            f"=== YOUR TASK ===\n"
            f"Write ONE Stable Diffusion / Flux image prompt (max 90 words) "
            f"for a cinematic, photorealistic background that:\n"
            f"1. Reinforces the SPECIFIC subject of the post (not generic crypto)\n"
            f"2. Triggers an emotional reaction matching the mood "
            f"({'panic / red alert' if mood == 'bear' else 'euphoria / green surge' if mood == 'bull' else 'curiosity / blue analytical'})\n"
            f"3. Uses high contrast + a clear focal point so it stops the thumb on mobile\n"
            f"4. Leaves negative space in the lower 60% for headline overlay\n"
            f"5. NO text, NO watermarks, NO UI elements, NO logos, NO faces\n"
            f"End the prompt with: 8k photorealistic cinematic lighting, no text no watermark.\n"
            f"Reply with ONLY the prompt — no preamble, no quotes, no explanation."
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        result = msg.content[0].text.strip().strip('"').strip("'")
        if len(result) > 30:
            _IMG_PROMPT_CACHE[_ck] = (_t.time(), result)
            return result
    except Exception:
        pass
    return None


def _story_seed(story: dict | None) -> int:
    """Deterministic seed from story title so each story gets its own style."""
    if not story:
        return 0
    title = story.get("title", "") or ""
    return int(hashlib.md5(title.encode("utf-8", errors="replace")).hexdigest()[:8], 16)


def _load_avoid_indices(mood: str) -> list[int]:
    """Return style indices used in the last 7 days (brain-guided diversity)."""
    try:
        if _BRAIN_PARAMS.exists():
            data = json.loads(_BRAIN_PARAMS.read_text(encoding="utf-8"))
            return data.get(f"avoid_style_indices_{mood}", [])
    except Exception:
        pass
    return []


def _log_style_used(mood: str, idx: int) -> None:
    """Record which style index was used today for brain learning."""
    try:
        _STYLE_LOG.parent.mkdir(parents=True, exist_ok=True)
        history: dict = {}
        if _STYLE_LOG.exists():
            history = json.loads(_STYLE_LOG.read_text(encoding="utf-8"))
        today = datetime.now().strftime("%Y-%m-%d")
        key = f"{mood}_{today}"
        used: list[int] = history.get(key, [])
        if idx not in used:
            used.append(idx)
        history[key] = used
        # Prune entries older than 14 days
        cutoff = datetime.now().toordinal() - 14
        history = {k: v for k, v in history.items()
                   if datetime.strptime(k.split("_", 1)[1], "%Y-%m-%d").toordinal() >= cutoff
                   if "_" in k}
        _STYLE_LOG.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _pick_bg_prompt(mood: str, seed: int, custom_prompt: str = "", story: dict | None = None) -> str:
    if custom_prompt.strip():
        return custom_prompt.strip()

    # 1. Try Claude LLM for a fully custom prompt (best quality)
    if story:
        llm = _llm_bg_prompt(story, mood)
        if llm:
            print(f"[image_gen] LLM background prompt: {llm[:60]}...", file=sys.stderr)
            return llm

    # 2. Try story-type specific prompt (medium quality, always works)
    if story:
        story_type = _classify_story_type(story)
        if story_type and story_type in _TYPE_PROMPTS:
            variant_key = mood if mood in _TYPE_PROMPTS[story_type] else "neutral"
            prompt = _TYPE_PROMPTS[story_type][variant_key]
            print(f"[image_gen] story-type '{story_type}' -> specific prompt", file=sys.stderr)
            return prompt

    # 3. Fallback: daily-rotating style catalogue — different visual every day
    styles = BG_STYLES.get(mood, BG_STYLES["neutral"])

    # Combine date ordinal + story hash so: same story same day = same style,
    # but different days OR different stories = different style (visual diversity)
    daily_base = datetime.now().toordinal()
    story_offset = _story_seed(story) % max(1, len(styles))
    daily_seed = daily_base + story_offset

    # Brain feedback: avoid recently used indices for maximum visual variety
    avoid = set(_load_avoid_indices(mood))
    available = [i for i in range(len(styles)) if i not in avoid]
    if not available:
        available = list(range(len(styles)))  # all used → reset

    idx = available[daily_seed % len(available)]
    _log_style_used(mood, idx)
    print(f"[image_gen] fallback style #{idx}/{len(styles)} mood={mood} "
          f"(day={daily_base}, story_offset={story_offset})", file=sys.stderr)
    return styles[idx]


# ---------------------------------------------------------------------------
# Font helpers
# ---------------------------------------------------------------------------

def _load_font(size: int, bold: bool = False):
    from PIL import ImageFont
    candidates_bold = [
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/Arial_Bold.ttf",
        "C:/Windows/Fonts/calibrib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    candidates_regular = [
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/Arial.ttf",
        "C:/Windows/Fonts/calibri.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in (candidates_bold if bold else candidates_regular):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Text wrapping
# ---------------------------------------------------------------------------

def _wrap_text(text: str, font, max_width: int, draw) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# Fallback background generator (no network required)
# ---------------------------------------------------------------------------

def _make_fallback_bg(width: int, height: int, mood: str) -> "Image.Image":
    """Generate a dark gradient background with PIL when API is unavailable."""
    from PIL import Image, ImageDraw
    import math

    base_colors = {
        "bull":    [(10, 30, 15),  (5, 18, 8)],
        "bear":    [(30, 8,  8),   (18, 4,  4)],
        "neutral": [(8,  14, 30),  (4,  8,  20)],
    }
    top_c, bot_c = base_colors.get(mood, base_colors["neutral"])

    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / height
        r = int(top_c[0] + (bot_c[0] - top_c[0]) * t)
        g = int(top_c[1] + (bot_c[1] - top_c[1]) * t)
        b = int(top_c[2] + (bot_c[2] - top_c[2]) * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    # Grid lines for a "chart" aesthetic
    accent = {"bull": (39, 201, 109), "bear": (220, 50, 50), "neutral": (42, 140, 255)}
    ac = accent.get(mood, accent["neutral"])
    grid_color = (ac[0] // 4, ac[1] // 4, ac[2] // 4)
    step_x = width // 8
    step_y = height // 10
    for x in range(0, width, step_x):
        draw.line([(x, 0), (x, height)], fill=grid_color, width=1)
    for y in range(0, height, step_y):
        draw.line([(0, y), (width, y)], fill=grid_color, width=1)

    # Diagonal "chart line" for visual interest
    pts = []
    for i in range(20):
        px = int(width * i / 19)
        noise = math.sin(i * 1.3) * height * 0.08
        py = int(height * 0.55 - height * 0.15 * (i / 19) + noise)
        pts.append((px, py))
    for i in range(len(pts) - 1):
        draw.line([pts[i], pts[i + 1]], fill=ac, width=max(2, width // 300))

    return img


# ---------------------------------------------------------------------------
# Background download  (with retry + fallback)
# ---------------------------------------------------------------------------

# Premium-quality Pollinations.ai model order:
#   flux         — best general quality (SDXL-grade)
#   flux-realism — photoreal fallback for portraits / cinematic shots
#   turbo        — last-resort, faster but lower fidelity
# We also pass enhance=true (LLM prompt rewriter) on the first try for
# richer compositions; only disabled on retry to avoid mutation if the
# first call rate-limits.
_POLLINATIONS_MODELS = ["flux", "flux-realism", "turbo"]


def _download_background(prompt: str, width: int, height: int, seed: int,
                         mood: str = "neutral"):
    try:
        import requests
    except ImportError:
        raise RuntimeError("requests not installed: pip install requests")
    from PIL import Image
    import time

    encoded = quote(prompt)
    delays = [5, 15, 30]  # seconds between retries

    for attempt, model in enumerate(_POLLINATIONS_MODELS):
        # Premium quality knobs:
        #   enhance=true  → server-side LLM prompt rewriter for richer scenes
        #   nofeed=true   → don't expose this generation in their public feed
        #   private=true  → request not shared with the community gallery
        # We render at native target resolution (no upscale step needed).
        enhance_flag = "true" if attempt == 0 else "false"
        url = (
            f"https://image.pollinations.ai/prompt/{encoded}"
            f"?width={width}&height={height}&seed={seed + attempt}"
            f"&nologo=true&model={model}&enhance={enhance_flag}"
            f"&nofeed=true&private=true"
        )
        print(f"[image_gen] Pollinations.ai attempt {attempt + 1}/3 (model={model})...",
              file=sys.stderr)
        try:
            resp = requests.get(url, timeout=90)
            if resp.status_code == 429:
                wait = delays[min(attempt, len(delays) - 1)]
                print(f"[image_gen] 429 rate-limit — waiting {wait}s before retry...",
                      file=sys.stderr)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = delays[min(attempt, len(delays) - 1)]
                print(f"[image_gen] {resp.status_code} server error — waiting {wait}s...",
                      file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            img = Image.open(BytesIO(resp.content)).convert("RGB")
            return img.resize((width, height))
        except Exception as exc:
            print(f"[image_gen] attempt {attempt + 1} failed: {exc}", file=sys.stderr)
            if attempt < len(_POLLINATIONS_MODELS) - 1:
                time.sleep(delays[attempt])

    # All retries exhausted — use PIL fallback so the pipeline keeps going
    print(
        "[image_gen] ⚠ Pollinations.ai unavailable after 3 attempts — "
        "using local gradient fallback background",
        file=sys.stderr,
    )
    return _make_fallback_bg(width, height, mood)


# ---------------------------------------------------------------------------
# Accent colours per mood
# ---------------------------------------------------------------------------

ACCENT = {
    "bear":    (220, 50,  50),
    "bull":    (39,  201, 109),
    "neutral": (42,  140, 255),
}


# ---------------------------------------------------------------------------
# Main compositor
# ---------------------------------------------------------------------------

def render_card(
    story: dict,
    width: int,
    height: int,
    out_path: Path,
    seed: int = 42,
    custom_prompt: str = "",
) -> None:
    from PIL import Image, ImageDraw, ImageFilter

    mood    = classify_mood(story)
    title   = story.get("title", "No title")
    summary = story.get("summary") or ""
    source  = story.get("source", "").replace("_", " ").title()
    tickers = story.get("tickers") or []
    accent  = ACCENT.get(mood, ACCENT["neutral"])

    # Best sentence from summary for the subhead
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", summary) if len(s.strip()) > 20]
    subhead = sentences[0] if sentences else ""
    if subhead and not subhead.endswith((".", "!", "?")):
        subhead += "."
    if len(subhead) > 180:
        subhead = subhead[:179].rstrip() + "…"

    # ── Background ──────────────────────────────────────────────────────────
    prompt = _pick_bg_prompt(mood, seed, custom_prompt, story=story)
    # Premium quality modifiers — appended to every prompt to push Pollinations
    # /flux towards sharper, magazine-grade compositions. Order matters: keep
    # negatives (`no text…`) at the end so prior commas don't dilute them.
    prompt = (
        f"{prompt}, "
        f"masterpiece, ultra detailed, sharp focus, professional color grading, "
        f"hyper-realistic textures, dramatic studio lighting, depth of field, "
        f"premium magazine cover quality, 8k uhd, photorealism, "
        f"no blurry, no watermark, no signature, no text artifacts"
    )
    bg = _download_background(prompt, width, height, seed, mood=mood)

    # Detect portrait "reel" layout (9:16). When the image will be composited
    # behind an AI avatar, we need text in the UPPER half of the frame so it
    # stays visible above the presenter strip (which sits at y=1152–1692 on
    # a 1080×1920 canvas).
    is_portrait_reel = height >= int(width * 1.4)

    # ── Dark gradient overlay ───────────────────────────────────────────────
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ov_draw = ImageDraw.Draw(overlay)
    if is_portrait_reel:
        # Dark gradient in the TOP portion so text is readable over the bg.
        grad_stop = int(height * 0.55)
        for y in range(0, grad_stop):
            t = 1 - (y / grad_stop)
            alpha = int(220 * t)
            ov_draw.line([(0, y), (width, y)], fill=(0, 0, 8, alpha))
        # Faint darkening on the avatar strip area too, to prevent bg noise.
        for y in range(int(height * 0.55), height):
            ov_draw.line([(0, y), (width, y)], fill=(0, 0, 8, 70))
    else:
        # Classic landscape/square: darken the bottom 60% for title area.
        grad_top = int(height * 0.32)
        for y in range(grad_top, height):
            alpha = int(215 * (y - grad_top) / (height - grad_top))
            ov_draw.line([(0, y), (width, y)], fill=(0, 0, 8, alpha))

        # Subtle top vignette
        for y in range(0, int(height * 0.15)):
            alpha = int(90 * (1 - y / (height * 0.15)))
            ov_draw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))

    bg = Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(bg)

    # ── Accent bar — bottom edge ─────────────────────────────────────────────
    bar_h = max(5, height // 120)
    draw.rectangle([(0, height - bar_h), (width, height)], fill=accent)

    # ── Top-left: source badge ───────────────────────────────────────────────
    if source:
        src_font = _load_font(max(16, width // 62), bold=False)
        src_text = f"  {source}  "
        src_bbox = draw.textbbox((0, 0), src_text, font=src_font)
        sw = src_bbox[2] - src_bbox[0] + 16
        sh = src_bbox[3] - src_bbox[1] + 10
        draw.rounded_rectangle([20, 20, 20 + sw, 20 + sh], radius=6,
                                fill=(0, 0, 0, 160) if False else (20, 20, 28))
        draw.rounded_rectangle([20, 20, 20 + sw, 20 + sh], radius=6,
                                outline=accent, width=1)
        draw.text((28, 25), src_text.strip(), font=src_font, fill=(180, 190, 210))

    # ── Top-right: ticker pill(s) ────────────────────────────────────────────
    pill_font = _load_font(max(18, width // 52), bold=True)
    pill_x = width - 28
    for ticker in tickers[:3]:
        txt = f" {ticker} "
        pb = draw.textbbox((0, 0), txt, font=pill_font)
        pw = pb[2] - pb[0] + 18
        ph = pb[3] - pb[1] + 12
        pill_x -= pw
        draw.rounded_rectangle([pill_x, 18, pill_x + pw, 18 + ph], radius=7, fill=accent)
        draw.text((pill_x + 9, 24), ticker, font=pill_font, fill=(255, 255, 255))
        pill_x -= 10

    # ── Mood icon (small, bottom-right corner above accent bar) ──────────────
    mood_icon = {"bear": "▼ BEARISH", "bull": "▲ BULLISH", "neutral": "◆ MARKET"}[mood]
    mood_font = _load_font(max(14, width // 72))
    mb = draw.textbbox((0, 0), mood_icon, font=mood_font)
    mx = width - (mb[2] - mb[0]) - 24
    my = height - bar_h - (mb[3] - mb[1]) - 12
    draw.text((mx + 1, my + 1), mood_icon, font=mood_font, fill=(0, 0, 0))
    draw.text((mx, my), mood_icon, font=mood_font, fill=accent)

    # ── Layout constants ─────────────────────────────────────────────────────
    pad_x     = int(width * 0.055)
    max_w     = width - pad_x * 2

    title_fs  = max(34, width // 17)
    title_fnt = _load_font(title_fs, bold=True)
    title_lh  = title_fs + 10

    sub_fs    = max(20, width // 34)
    sub_fnt   = _load_font(sub_fs, bold=False)
    sub_lh    = sub_fs + 7

    brand_fs  = max(16, width // 58)
    brand_fnt = _load_font(brand_fs, bold=True)

    # ── Wrap text ────────────────────────────────────────────────────────────
    title_lines = _wrap_text(title, title_fnt, max_w, draw)
    sub_lines   = _wrap_text(subhead, sub_fnt, max_w, draw) if subhead else []

    spacing   = int(height * 0.022)
    brand_h   = brand_fs + 8
    block_h   = (len(title_lines) * title_lh
                 + (spacing + len(sub_lines) * sub_lh if sub_lines else 0)
                 + spacing + brand_h)

    if is_portrait_reel:
        # Place text in the UPPER portion of the 9:16 canvas so it stays
        # visible above the AI-avatar strip in the final composite video.
        # Leave ~9% top margin for the source badge / ticker pills.
        y = int(height * 0.11)
    else:
        bottom_margin = bar_h + int(height * 0.055)
        y = height - bottom_margin - block_h

    # ── Draw title ───────────────────────────────────────────────────────────
    for line in title_lines:
        # Shadow
        draw.text((pad_x + 3, y + 3), line, font=title_fnt, fill=(0, 0, 0))
        draw.text((pad_x + 1, y + 1), line, font=title_fnt, fill=(0, 0, 0))
        # Main text
        draw.text((pad_x, y), line, font=title_fnt, fill=(255, 255, 255))
        y += title_lh

    # ── Accent divider ───────────────────────────────────────────────────────
    if sub_lines:
        y += spacing // 2
        draw.rectangle([(pad_x, y), (pad_x + int(max_w * 0.18), y + 2)], fill=accent)
        y += spacing

    # ── Draw subhead ─────────────────────────────────────────────────────────
    for line in sub_lines:
        draw.text((pad_x + 1, y + 1), line, font=sub_fnt, fill=(0, 0, 0))
        draw.text((pad_x, y), line, font=sub_fnt, fill=(195, 205, 225))
        y += sub_lh

    # ── Brand label ──────────────────────────────────────────────────────────
    y += spacing
    brand = "EliteMarginDesk.io"
    draw.text((pad_x + 1, y + 1), brand, font=brand_fnt, fill=(0, 0, 0))
    draw.text((pad_x, y), brand, font=brand_fnt, fill=accent)

    # ── Save ─────────────────────────────────────────────────────────────────
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bg.save(str(out_path), "PNG")
    print(f"[image_gen] saved {out_path.name} ({width}x{height}, mood={mood})",
          file=sys.stderr)


# ---------------------------------------------------------------------------
# Roundup mode — 3 headlines on one background (engagement-grade design)
# ---------------------------------------------------------------------------

def render_roundup_image(stories: list, out_path: Path,
                          width: int = 1080, height: int = 1920,
                          seed: int | None = None,
                          custom_prompt: str = "") -> None:
    """Render a 9:16 background with up to 5 numbered headlines stacked.

    Layout (1080×1920):
      • Top 6%   — "TOP CRYPTO TODAY" badge with neon underline
      • 8% .. 78% — N stacked headline cards (background-blurred dark cards
                     with neon accent number circle + 3-line max wrapped title)
      • Bottom 22% — clean area for AI avatar overlay (we keep the area
                       between y=1450..1850 fully empty so the talking head
                       sits on top without covering text)

    Falls back to single-story `render_card` if `stories` has 1 item.
    """
    from PIL import Image, ImageDraw, ImageFilter

    n = max(1, min(len(stories), 5))
    if n == 1:
        render_card(stories[0], width, height, out_path, seed=seed or 42,
                     custom_prompt=custom_prompt)
        return

    # Use mood from first story for color/prompt selection
    primary = stories[0]
    mood    = classify_mood(primary)
    accent  = ACCENT.get(mood, ACCENT["neutral"])
    seed    = seed if seed is not None else _story_seed(primary)

    # Pollinations background — premium quality prompt
    prompt = _pick_bg_prompt(mood, seed, custom_prompt, story=primary)
    prompt = (
        f"{prompt}, masterpiece, ultra detailed, sharp focus, "
        f"professional color grading, hyper-realistic textures, "
        f"dramatic studio lighting, depth of field, 8k uhd, photorealism, "
        f"no blurry, no watermark, no signature, no text artifacts"
    )
    bg = _download_background(prompt, width, height, seed, mood=mood)

    # ── Heavy darken pass: roundup needs lots of text contrast ─────────────
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ov_draw = ImageDraw.Draw(overlay)
    # Darken top 78% (text area) heavily, keep bottom 22% lighter (avatar zone)
    for y in range(0, int(height * 0.78)):
        # 0.85 alpha at top fading to 0.55 at the bottom of text zone
        t = 1 - (y / (height * 0.78))
        alpha = int(220 - 120 * (1 - t))
        ov_draw.line([(0, y), (width, y)], fill=(2, 8, 6, alpha))
    # Light darkening on avatar zone for chromakey edge cleanup
    for y in range(int(height * 0.78), height):
        ov_draw.line([(0, y), (width, y)], fill=(2, 8, 6, 90))
    bg = Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(bg)

    # ── Top badge: "TOP CRYPTO TODAY" ──────────────────────────────────────
    badge_fnt   = _load_font(34, bold=True)
    badge_text  = "▲ TOP CRYPTO TODAY"
    bb          = draw.textbbox((0, 0), badge_text, font=badge_fnt)
    badge_w     = bb[2] - bb[0]
    bx          = (width - badge_w) // 2
    by          = int(height * 0.04)
    # Soft pill background
    pill_pad_x  = 22
    pill_pad_y  = 8
    draw.rounded_rectangle(
        [(bx - pill_pad_x, by - pill_pad_y),
         (bx + badge_w + pill_pad_x, by + 38 + pill_pad_y)],
        radius=22, fill=(0, 0, 0, 200), outline=accent, width=2,
    )
    draw.text((bx + 2, by + 2), badge_text, font=badge_fnt, fill=(0, 0, 0))
    draw.text((bx, by), badge_text, font=badge_fnt, fill=accent)

    # Subtle underline glow
    line_y = by + 56
    draw.rectangle([(width // 2 - 80, line_y),
                    (width // 2 + 80, line_y + 3)], fill=accent)

    # ── Headline cards ──────────────────────────────────────────────────────
    cards_top    = int(height * 0.13)
    cards_bottom = int(height * 0.76)   # leave 24% bottom for avatar
    available_h  = cards_bottom - cards_top
    card_h       = available_h // n
    card_pad     = 18

    title_fnt    = _load_font(46, bold=True)
    num_fnt      = _load_font(64, bold=True)

    for i, story in enumerate(stories[:n]):
        title = story.get("title") or story.get("headline") or ""
        # Strip source suffixes that look ugly in headlines
        title = re.sub(r"\s+[\-–—]\s+[A-Za-z0-9 .]+\.(net|com|co|io|news|tv)\b",
                        "", title)
        title = re.sub(r"\s*\([^)]+\)\s*$", "", title).strip()
        if not title:
            continue

        cx0 = int(width * 0.05)
        cx1 = width - cx0
        cy0 = cards_top + i * card_h
        cy1 = cy0 + card_h - 14    # 14px gap between cards

        # Card background — translucent dark with accent left bar
        draw.rounded_rectangle(
            [(cx0, cy0), (cx1, cy1)],
            radius=18, fill=(8, 16, 12, 220), outline=(0, 0, 0, 0),
        )
        # Left accent bar (mood-tinted)
        draw.rounded_rectangle(
            [(cx0, cy0), (cx0 + 8, cy1)],
            radius=4, fill=accent,
        )

        # Number circle (1, 2, 3 ...)
        circ_x = cx0 + 38
        circ_y = cy0 + (cy1 - cy0) // 2
        circ_r = 36
        draw.ellipse(
            [(circ_x - circ_r, circ_y - circ_r),
             (circ_x + circ_r, circ_y + circ_r)],
            fill=accent, outline=(0, 0, 0, 0),
        )
        n_text = str(i + 1)
        nb     = draw.textbbox((0, 0), n_text, font=num_fnt)
        nw     = nb[2] - nb[0]
        nh     = nb[3] - nb[1]
        draw.text((circ_x - nw // 2, circ_y - nh // 2 - 6),
                   n_text, font=num_fnt, fill=(0, 0, 0))

        # Title — wrap to fit, max 3 lines
        title_x0 = circ_x + circ_r + 24
        title_x1 = cx1 - 24
        max_tw   = title_x1 - title_x0
        lines    = _wrap_text(title, title_fnt, max_tw, draw)[:3]
        # If we truncated, append … to the last line
        if len(_wrap_text(title, title_fnt, max_tw, draw)) > 3:
            last = lines[-1].rstrip()
            while last and draw.textbbox((0, 0), last + "…", font=title_fnt)[2] > max_tw:
                last = last[:-1].rstrip()
            lines[-1] = last + "…"

        line_h = title_fnt.size + 8
        block_h = line_h * len(lines)
        ty     = cy0 + ((cy1 - cy0) - block_h) // 2

        for line in lines:
            # Drop shadow for depth
            draw.text((title_x0 + 2, ty + 3), line, font=title_fnt, fill=(0, 0, 0))
            draw.text((title_x0, ty),         line, font=title_fnt, fill=(245, 250, 255))
            ty += line_h

    # Save
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bg.save(str(out_path), "PNG")
    print(f"[image_gen] roundup saved {out_path.name} "
          f"({width}x{height}, {n} headlines, mood={mood})", file=sys.stderr)


# ---------------------------------------------------------------------------
# Render all sizes for a story
# ---------------------------------------------------------------------------

SIZES = {
    "x":       (1200, 675,  "x_1200x675"),      # X/Twitter OG card 16:9
    "tg":      (1080, 1080, "tg_1080x1080"),    # Telegram square 1:1
    "reel":    (1080, 1920, "reel_1080x1920"),  # TikTok/Reels/Shorts 9:16
    "ig_feed": (1080, 1350, "ig_1080x1350"),    # Instagram feed 4:5 (portrait)
    "yt_thumb": (1280, 720, "yt_thumb_1280x720"),  # YouTube thumbnail 16:9
}


def render_all(story: dict, out_dir: Path, seed: int = 42,
               sizes: list[str] | None = None, custom_prompt: str = "",
               niche: str = "crypto") -> dict[str, Path]:
    """Render one or all size variants in parallel.

    Each render_card() spends ~30-60s waiting on Pollinations.ai (network I/O).
    Running 3 in parallel cuts the total from ~150s to ~50s because the GIL
    releases during HTTP calls and PIL image work.

    OPTIMIZATION: Cache check before rendering — reuse identical backgrounds.

    NEW: stamps `_slot_dir` on the story dict so the LLM image strategist can
    read the actual X / Telegram post bodies the copywriter generated and
    optimize the background for that specific copy (virality / scroll-stop).
    """
    # Annotate story with the output directory — _gather_post_context() looks
    # for post_x.txt / post_tg.txt next to the image to inform the LLM prompt.
    try:
        if isinstance(story, dict) and "_slot_dir" not in story:
            story = dict(story)
            story["_slot_dir"] = str(out_dir)
    except Exception:
        pass
    # Inject niche-specific background styles before rendering
    if niche and niche != "crypto":
        try:
            from src.agents.niches import NICHE_BG_STYLES
        except ImportError:
            from niches import NICHE_BG_STYLES  # type: ignore
        if niche in NICHE_BG_STYLES:
            _orig_styles = BG_STYLES.copy()
            niche_styles = NICHE_BG_STYLES[niche]
            BG_STYLES["bull"]    = niche_styles.get("positive", BG_STYLES["bull"])
            BG_STYLES["bear"]    = niche_styles.get("negative", BG_STYLES["bear"])
            BG_STYLES["neutral"] = niche_styles.get("neutral",  BG_STYLES["neutral"])
        else:
            _orig_styles = None
    else:
        _orig_styles = None

    targets = sizes or ["x", "tg"]
    result: dict[str, Path] = {}

    if len(targets) <= 1:
        for key in targets:
            w, h, suffix = SIZES[key]
            out = out_dir / f"image_{suffix}.png"
            
            # Check cache before rendering
            title = story.get("title", "")
            cache_k = _cache_key(title, custom_prompt, w, h, seed=seed)
            cached_img = _get_cached_image(cache_k)
            if cached_img:
                try:
                    import shutil
                    shutil.copy2(str(cached_img), str(out))
                    print(f"[image_gen] copied from cache -> {out.name}", file=sys.stderr)
                    result[key] = out
                    continue
                except Exception as exc:
                    print(f"[image_gen] cache copy failed: {exc}", file=sys.stderr)

            # Cache miss: render normally
            render_card(story, w, h, out, seed=seed, custom_prompt=custom_prompt)
            _store_cached_image(out, cache_k)
            result[key] = out
        if _orig_styles:
            BG_STYLES.update(_orig_styles)
        return result

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _one(key: str) -> tuple[str, Path]:
        w, h, suffix = SIZES[key]
        out = out_dir / f"image_{suffix}.png"

        # Check cache before rendering
        title = story.get("title", "")
        cache_k = _cache_key(title, custom_prompt, w, h, seed=seed)
        cached_img = _get_cached_image(cache_k)
        if cached_img:
            try:
                import shutil
                shutil.copy2(str(cached_img), str(out))
                print(f"[image_gen] copied from cache -> {out.name}", file=sys.stderr)
                return key, out
            except Exception as exc:
                print(f"[image_gen] cache copy failed: {exc}", file=sys.stderr)

        # Cache miss: render normally
        render_card(story, w, h, out, seed=seed, custom_prompt=custom_prompt)
        _store_cached_image(out, cache_k)
        return key, out

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = [pool.submit(_one, k) for k in targets]
        for fut in as_completed(futures):
            try:
                key, out = fut.result()
                result[key] = out
            except Exception as exc:
                print(f"[image_gen] parallel render failed: {exc}", file=sys.stderr)

    if _orig_styles:
        BG_STYLES.update(_orig_styles)
    print(f"[image_gen] rendered {len(result)}/{len(targets)} images in {time.time()-t0:.1f}s",
          file=sys.stderr)
    return result


def main(argv: list[str]) -> int:
    """CLI entrypoint - read top2.json from a slot dir and render images."""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("slot_dir", help="Path to a queue/<slot> directory")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--sizes", nargs="*", default=None,
                    choices=list(SIZES.keys()))
    ap.add_argument("--prompt", default="", help="Custom background prompt")
    ap.add_argument("--niche", default="crypto")
    args = ap.parse_args(argv)

    slot = Path(args.slot_dir)
    top2 = slot / "top2.json"
    if not top2.exists():
        print(f"[image_gen] top2.json not found in {slot}", file=sys.stderr)
        return 2
    data = json.loads(top2.read_text(encoding="utf-8"))
    stories = data.get("stories") or (data if isinstance(data, list) else [])
    if not stories:
        print(f"[image_gen] no stories in {top2}", file=sys.stderr)
        return 2

    seed = args.seed if args.seed is not None else datetime.now().toordinal()
    out = render_all(stories[0], slot, seed=seed, sizes=args.sizes,
                     custom_prompt=args.prompt, niche=args.niche)
    print(f"[image_gen] wrote: {[str(p) for p in out.values()]}")
    return 0


def render_png(template_path: Path, params: dict, width: int, height: int,
               out_path: Path) -> None:
    """Backwards-compat shim: redirect templated calls to render_card.

    Older pipeline code expected an HTML-template renderer; we now produce
    everything via PIL. The template is ignored; `params` is treated as the
    story dict (with title/summary/tickers/source) and forwarded to
    render_card.
    """
    story = {
        "title":   params.get("title", ""),
        "summary": params.get("summary") or params.get("subhead", ""),
        "source":  params.get("source", ""),
        "tickers": params.get("tickers") or [],
    }
    seed = int(params.get("seed", datetime.now().toordinal()))
    render_card(story, width, height, out_path, seed=seed,
                custom_prompt=params.get("prompt", ""))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
