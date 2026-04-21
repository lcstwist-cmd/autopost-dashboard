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

import json
import random
import re
import sys
import textwrap
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent

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
# Background styles — 10 cinematic crypto themes
# ---------------------------------------------------------------------------

# Each mood maps to a list of style variants; one is picked per render (by seed)
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


def _llm_bg_prompt(story: dict, mood: str) -> str | None:
    """Ask Claude for a custom background prompt. Returns None if API key missing."""
    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        prompt = (
            f"You are a creative director for a crypto news channel.\n"
            f"Story title: {story.get('title','')}\n"
            f"Summary: {(story.get('summary') or '')[:300]}\n"
            f"Tickers: {', '.join(story.get('tickers') or [])}\n"
            f"Mood: {mood}\n\n"
            f"Write ONE short Stable Diffusion image prompt (max 80 words) for a "
            f"cinematic, photorealistic background image that perfectly fits this story.\n"
            f"Requirements:\n"
            f"- Dark, professional crypto/finance aesthetic\n"
            f"- NO text, NO watermarks, NO UI elements, NO faces\n"
            f"- Inspired by the specific story topic\n"
            f"- End with: 8k photorealistic, no text no watermark\n"
            f"Reply with ONLY the prompt, nothing else."
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        result = msg.content[0].text.strip().strip('"').strip("'")
        if len(result) > 30:
            return result
    except Exception:
        pass
    return None


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

    # 3. Fallback: mood-based style catalogue
    styles = BG_STYLES.get(mood, BG_STYLES["neutral"])
    return styles[seed % len(styles)]


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
# Background download
# ---------------------------------------------------------------------------

def _download_background(prompt: str, width: int, height: int, seed: int):
    try:
        import requests
    except ImportError:
        raise SystemExit("requests not installed: pip install requests")
    from PIL import Image

    encoded = quote(prompt)
    url = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width={width}&height={height}&seed={seed}&nologo=true&model=flux"
    )
    print(f"[image_gen] fetching background from Pollinations.ai ...", file=sys.stderr)
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    img = Image.open(BytesIO(resp.content)).convert("RGB")
    return img.resize((width, height))


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
    bg = _download_background(prompt, width, height, seed)

    # ── Dark gradient overlay (bottom 60%) ──────────────────────────────────
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ov_draw = ImageDraw.Draw(overlay)
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
    print(f"[image_gen] saved {out_path.name} ({width}x{height}, mood={mood}, style={seed % len(BG_STYLES[mood])})",
          file=sys.stderr)


# ---------------------------------------------------------------------------
# Render all sizes for a story
# ---------------------------------------------------------------------------

SIZES = {
    "x":     (1200, 675,  "x_1200x675"),      # X/Twitter OG card
    "tg":    (1080, 1080, "tg_1080x1080"),     # Telegram square
    "reel":  (1080, 1920, "reel_1080x1920"),   # TikTok/Reels/Shorts 9:16
}


def render_all(story: dict, out_dir: Path, seed: int = 42,
               sizes: list[str] | None = None, custom_prompt: str = "") -> dict[str, Path]:
    """Render one or all size variants. Returns {size_key: path}."""
    targets = sizes or ["x", "tg"]
    result: dict[str, Path] = {}
    for key in targets:
        w, h, suffix = SIZES[key]
        out = out_dir / f"image_{suffix}.png"
        render_card(story, w, h, out, seed=seed, custom_prompt=custom_prompt)
        result[key] = out
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("top2_json")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--seed",    type=int, default=42)
    ap.add_argument("--sizes",   default="x,tg",
                    help="comma-separated: x,tg,reel  (default: x,tg)")
    args = ap.parse_args(argv[1:])

    data    = json.loads(Path(args.top2_json).read_text(encoding="utf-8"))
    stories = data.get("stories", data) if isinstance(data, dict) else data
    if not stories:
        print("[image_gen] no stories", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir).resolve() if args.out_dir else Path(args.top2_json).parent
    sizes   = [s.strip() for s in args.sizes.split(",") if s.strip()]
    render_all(stories[0], out_dir, seed=args.seed, sizes=sizes)
    print(f"[image_gen] done -> {out_dir}")
    return 0


# Compatibility shim for pipeline.py
def derive_params(story: dict, size: str) -> dict:
    return {"mood": classify_mood(story), "size": size}


def render_png(template_path: Path, params: dict, width: int, height: int,
               out_path: Path) -> None:
    render_card(params.get("_story", {}), width, height, out_path, seed=42)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
