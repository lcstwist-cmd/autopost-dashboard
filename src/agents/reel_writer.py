"""Reel Script Writer — Agent 5 of the Crypto AutoPost pipeline.

Takes the top-ranked story and produces a complete CapCut-ready package for a
30-45 second reel / short, distributable on Instagram Reels, TikTok, and YouTube
Shorts.

Input: top2.json from the Ranker.
Output directory (next to top2.json by default):
    reel/
      01_script.txt              — the spoken narration (250-350 words)
      02_scenes.json             — scene-by-scene shot list with timing
      03_captions.srt            — subtitles, second-accurate
      04_broll_prompts.txt       — stock-footage search prompts
      05_thumbnail_prompt.txt    — thumbnail brief (YT Shorts)
      06_platform_metadata.json  — per-platform title, description, hashtags
      07_capcut_instructions.md  — step-by-step CapCut walkthrough

Usage:
    python src/agents/reel_writer.py queue/2026-04-19_morning/top2.json

Note: this agent is template-driven (deterministic). It extracts facts from the
story summary, applies a 4-beat script skeleton (Hook → Context → Impact → CTA),
and fills in platform-specific metadata. For higher-quality copy, wrap this with
a call to Claude/Anthropic API — see wrap_with_llm() stub.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import timedelta
from pathlib import Path


# --- Script template -------------------------------------------------------

SCRIPT_SKELETON_BEAR = """\
[HOOK — 0:00-0:03] (on-camera, zoom-in)
"{hook}"

[CONTEXT — 0:03-0:15] (avatar + B-roll: charts, logos)
{context_lines}

[IMPACT — 0:15-0:30] (avatar + B-roll: on-chain graphics, funding rates)
{impact_lines}

[CTA — 0:30-0:40] (on-camera, close-up)
"{cta}"
"""

SCRIPT_SKELETON_BULL = SCRIPT_SKELETON_BEAR  # same shape, different tone

# --- Helpers ---------------------------------------------------------------

def _extract_numbers(text: str) -> list[str]:
    """Pull dollar amounts / percentages / big numbers from a blob of text."""
    patterns = [
        r"\$[\d,]+(?:\.\d+)?[MBK]?",    # $292M, $1.2B
        r"\b\d+(?:,\d{3})+\b",          # 116,500
        r"\b\d+(?:\.\d+)?%",            # 3.42%
        r"\b\d+(?:\.\d+)?[MBK]\b",      # 663.9M
    ]
    found: list[str] = []
    for pat in patterns:
        found.extend(re.findall(pat, text))
    # preserve order, dedupe
    seen: set[str] = set()
    uniq: list[str] = []
    for f in found:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def _tone_of(story: dict) -> str:
    """bear | bull | neutral — used to pick phrasing."""
    title = story["title"].lower()
    bear_kw = ("hack", "exploit", "drain", "stolen", "crash", "plunge",
               "liquidation", "rejected", "halt", "freeze")
    bull_kw = ("record", "all-time high", "ath", "surge", "rally", "approval",
               "approved", "launch", "inflow", "accumulation")
    if any(k in title for k in bear_kw):
        return "bear"
    if any(k in title for k in bull_kw):
        return "bull"
    return "neutral"


def _primary_ticker(story: dict) -> str:
    ticks = story.get("tickers") or []
    return ticks[0] if ticks else ""


# --- Script generation -----------------------------------------------------

def build_script(story: dict) -> dict:
    """Return { 'script_text': str, 'scenes': [...] } for the chosen story."""
    tone = _tone_of(story)
    ticker = _primary_ticker(story)
    numbers = _extract_numbers(
        " ".join([story.get("title", ""), story.get("summary", "")])
    )
    headline = story["title"]
    summary = story.get("summary") or ""

    # --- HOOK (0-3s) ------------------------------------------------------
    if tone == "bear":
        big_num = next((n for n in numbers if "$" in n or "M" in n.upper()), None)
        hook = (
            f"{ticker or 'Crypto'} just had its biggest hit of 2026."
            if not big_num
            else f"{big_num} just got drained from crypto."
        )
    elif tone == "bull":
        big_num = next((n for n in numbers if "$" in n), None)
        hook = (
            f"Wall Street is voting {ticker or 'crypto'}."
            if not big_num
            else f"{big_num} just moved — here's what it means."
        )
    else:
        hook = f"Here's the biggest crypto story of the day."

    # --- CONTEXT (3-15s) — 2-3 key facts from summary ---------------------
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", summary) if s.strip()]
    context_src = sentences[:2] if sentences else [headline]
    context_lines = "\n".join(f'"{s}"' for s in context_src)

    # --- IMPACT (15-30s) — tone-dependent -----------------------------------
    if tone == "bear":
        impact = (
            '"Check your exposure. If you hold positions collateralized by '
            'assets in this exploit, pause and reassess."\n'
            '"Protocols often take 24 to 48 hours to resolve contagion — '
            'do not force trades during that window."'
        )
    elif tone == "bull":
        impact = (
            '"When institutions move like this, the floor under price shifts — '
            'it is structural, not speculative."\n'
            '"Watch funding rates and ETF flows over the next 48 hours to see '
            'if this momentum holds."'
        )
    else:
        impact = (
            '"This matters because it moves the narrative — watch how the market '
            'reacts over the next 24 hours."'
        )

    # --- CTA (30-40s) ----------------------------------------------------
    cta = (
        "Not financial advice. Follow for the next crypto update at seven PM — "
        "same time, same coin, sharper takes."
    )

    skeleton = SCRIPT_SKELETON_BEAR if tone == "bear" else SCRIPT_SKELETON_BULL
    script_text = skeleton.format(
        hook=hook,
        context_lines=context_lines,
        impact_lines=impact,
        cta=cta,
    )

    # --- Scenes list ------------------------------------------------------
    scenes = [
        {
            "scene": 1, "start": 0.0, "end": 3.0,
            "type": "hook", "shot": "avatar_close_up",
            "line": hook,
            "overlay": numbers[0] if numbers else (ticker or "BREAKING"),
            "broll": None,
        },
        {
            "scene": 2, "start": 3.0, "end": 9.5,
            "type": "context", "shot": "avatar_medium + broll_overlay",
            "line": context_src[0] if context_src else headline,
            "overlay": ticker,
            "broll": "chart_zoom + protocol_logos",
        },
        {
            "scene": 3, "start": 9.5, "end": 15.0,
            "type": "context", "shot": "avatar_medium + broll_overlay",
            "line": context_src[1] if len(context_src) > 1 else "",
            "overlay": numbers[1] if len(numbers) > 1 else "",
            "broll": "onchain_transaction_graph",
        },
        {
            "scene": 4, "start": 15.0, "end": 22.5,
            "type": "impact", "shot": "avatar_medium + broll_split",
            "line": impact.split("\n")[0].strip('"'),
            "overlay": "",
            "broll": "funding_rates_chart",
        },
        {
            "scene": 5, "start": 22.5, "end": 30.0,
            "type": "impact", "shot": "avatar_medium + broll_split",
            "line": impact.split("\n")[-1].strip('"'),
            "overlay": "",
            "broll": "protocol_frozen_badges",
        },
        {
            "scene": 6, "start": 30.0, "end": 40.0,
            "type": "cta", "shot": "avatar_close_up",
            "line": cta,
            "overlay": "FOLLOW",
            "broll": None,
        },
    ]
    return {"script_text": script_text, "scenes": scenes, "tone": tone}


# --- SRT generation --------------------------------------------------------

def _fmt_ts(seconds: float) -> str:
    td = timedelta(seconds=seconds)
    total = int(td.total_seconds())
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    ms = int((seconds - total) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _split_line(text: str, max_chars: int = 38) -> list[str]:
    """Break a caption into chunks under max_chars, on word boundaries."""
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
    """Turn scenes[].line into a valid SRT — each scene may become 1-2 cues."""
    srt_parts: list[str] = []
    cue_index = 1
    for sc in scenes:
        line = (sc.get("line") or "").strip()
        if not line:
            continue
        start, end = float(sc["start"]), float(sc["end"])
        duration = max(0.5, end - start)
        chunks = _split_line(line, max_chars=38)
        # rough: longer chunks get proportionally more time
        weights = [len(c) for c in chunks]
        total_w = sum(weights) or 1
        t = start
        for chunk, w in zip(chunks, weights):
            seg = duration * (w / total_w)
            srt_parts.append(
                f"{cue_index}\n"
                f"{_fmt_ts(t)} --> {_fmt_ts(t + seg)}\n"
                f"{chunk}\n"
            )
            cue_index += 1
            t += seg
    return "\n".join(srt_parts)


# --- B-roll prompts --------------------------------------------------------

def build_broll_prompts(story: dict, scenes: list[dict]) -> str:
    ticker = _primary_ticker(story)
    prompts: list[str] = []
    prompts.append("# B-roll prompts — use these in Pexels, Storyblocks, or CapCut stock library")
    prompts.append("")
    prompts.append(f"1. Close-up of a trading chart for {ticker or 'Bitcoin'}, candlesticks, slow zoom-in, dark UI")
    prompts.append("2. Digital security breach graphic — red lock icons shattering, data streams")
    prompts.append("3. Ethereum logo animated, network connections pulsing")
    prompts.append("4. Wall Street at night, skyscrapers lit, establishing shot")
    prompts.append("5. Over-the-shoulder shot of a trader's desk, multiple monitors, charts glowing")
    prompts.append("6. Abstract blockchain visualization — hexagonal blocks linking, neon")
    prompts.append("")
    prompts.append("# Scene → b-roll mapping")
    for sc in scenes:
        if sc.get("broll"):
            prompts.append(f"  Scene {sc['scene']} ({sc['start']}s-{sc['end']}s): {sc['broll']}")
    return "\n".join(prompts) + "\n"


def build_thumbnail_prompt(story: dict) -> str:
    tone = _tone_of(story)
    ticker = _primary_ticker(story) or "BTC"
    title_short = story["title"][:60]
    palette = {
        "bear":    "dark red gradient, broken chain icon, warning triangle",
        "bull":    "navy + gold gradient, Bitcoin symbol glowing, upward arrow",
        "neutral": "dark blue gradient, crypto coins stacked, minimal text",
    }[tone]
    return (
        f"YouTube Short thumbnail (1080x1920, 9:16):\n"
        f"  Tone: {tone}\n"
        f"  Palette: {palette}\n"
        f"  Foreground: avatar bust (left third), shocked/confident expression\n"
        f"  Center text (huge, 160pt): \"{ticker}\"\n"
        f"  Below (48pt, accent color): \"{title_short}\"\n"
        f"  Corner badge: \"BREAKING\" or \"UPDATE\"\n"
        f"  No clutter — max 6 elements, clear focal point.\n"
    )


# --- Platform metadata -----------------------------------------------------

HASHTAGS = {
    "instagram": [
        "#bitcoin", "#crypto", "#btc", "#cryptonews", "#etf", "#bullrun",
        "#tradingview", "#blockchain", "#cryptoupdate", "#satoshi",
    ],
    "tiktok": [
        "#crypto", "#bitcoin", "#fyp", "#cryptotok", "#btc",
        "#investing", "#money", "#financetok",
    ],
    "youtube": ["#Shorts", "#Bitcoin", "#Crypto", "#BTC", "#CryptoNews"],
}


def build_platform_metadata(story: dict) -> dict:
    title = story["title"]
    ticker = _primary_ticker(story)
    tone = _tone_of(story)

    short_title = title if len(title) <= 60 else title[:59].rstrip(" ,;:") + "…"

    base_description = (
        f"{title}\n\n"
        f"Daily crypto news — 2 posts a day, morning and evening. "
        f"Follow for the next update.\n\n"
        f"Not financial advice. DYOR."
    )

    return {
        "instagram": {
            "caption": f"{short_title}\n\n{base_description}",
            "hashtags": HASHTAGS["instagram"] + ([f"#{ticker}"] if ticker else []),
        },
        "tiktok": {
            "caption": short_title,
            "description": base_description,
            "hashtags": HASHTAGS["tiktok"] + ([f"#{ticker}"] if ticker else []),
        },
        "youtube": {
            "title": short_title + " #Shorts",
            "description": base_description,
            "tags": HASHTAGS["youtube"] + ([f"#{ticker}"] if ticker else []),
        },
        "_tone": tone,
    }


# --- CapCut walkthrough ----------------------------------------------------

def build_capcut_instructions(story: dict, tone: str) -> str:
    ticker = _primary_ticker(story)
    avatar_choice = {
        "bear": "Sophie / Marcus (serious, news-anchor vibe)",
        "bull": "Emma / Daniel (confident, energetic)",
        "neutral": "any — pick the one with best audio clarity",
    }[tone]
    voice_style = {
        "bear":    "Measured, authoritative, slightly urgent",
        "bull":    "Confident, fast-paced, rising intonation",
        "neutral": "Clear, conversational, neutral pace",
    }[tone]
    return (
        f"# CapCut production steps — ~10 minutes\n\n"
        f"1. **Open CapCut** (desktop or mobile). Create a new 9:16 project (1080x1920).\n\n"
        f"2. **Paste the script** — open `01_script.txt`, copy the narration lines "
        f"(skip the stage directions in [brackets]).\n\n"
        f"3. **AI Avatar → New Avatar**\n"
        f"   - Choose avatar: {avatar_choice}\n"
        f"   - Voice: pick an English voice matching the tone ({voice_style.lower()})\n"
        f"   - Paste the script. Generate. CapCut returns a ~35-40 second avatar clip.\n\n"
        f"4. **Drag the avatar clip** onto the timeline, position 0:00.\n\n"
        f"5. **Import subtitles** — Files → Import Captions → select `03_captions.srt`.\n"
        f"   - Style: white text, black outline, 48pt, bottom-center, max 2 lines.\n\n"
        f"6. **Add B-roll overlays** per `04_broll_prompts.txt`:\n"
        f"   - Use CapCut's Stock tab or drag in files from your Storyblocks/Pexels downloads\n"
        f"   - Map them to the scenes in `02_scenes.json`\n"
        f"   - B-roll opacity: 80-90% when over avatar\n\n"
        f"7. **Overlays / text callouts** — add big text callouts at the key moments:\n"
        f"   - Scene 1 (0-3s): huge number/emoji overlay (use the 'overlay' field from scenes.json)\n"
        f"   - Scene 6 (30-40s): 'FOLLOW' callout bottom-right\n\n"
        f"8. **Audio** — add a subtle background track from CapCut's library.\n"
        f"   - Tone: {voice_style}, volume -20 dB so narration stays dominant\n\n"
        f"9. **Thumbnail** — see `05_thumbnail_prompt.txt` for YouTube Short thumb brief.\n\n"
        f"10. **Export** — 1080x1920, 30 fps, high quality.\n"
        f"    Save as `reel_final_{(story.get('id') or 'unknown')[:8]}.mp4`.\n\n"
        f"## Upload targets\n\n"
        f"- **Instagram Reels** — caption from `06_platform_metadata.json -> instagram`\n"
        f"- **TikTok** — caption + description from `... -> tiktok`\n"
        f"- **YouTube Shorts** — title + description + tags from `... -> youtube`\n\n"
        f"Ticker being featured: **{ticker or '(none)'}**. Tone: **{tone}**.\n"
    )


# --- Orchestrator ----------------------------------------------------------

def write_package(story: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    built = build_script(story)
    scenes = built["scenes"]
    tone = built["tone"]

    (out_dir / "01_script.txt").write_text(built["script_text"], encoding="utf-8")

    (out_dir / "02_scenes.json").write_text(
        json.dumps({"story_id": story["id"], "scenes": scenes}, indent=2),
        encoding="utf-8",
    )

    (out_dir / "03_captions.srt").write_text(build_srt(scenes), encoding="utf-8")

    (out_dir / "04_broll_prompts.txt").write_text(
        build_broll_prompts(story, scenes), encoding="utf-8"
    )

    (out_dir / "05_thumbnail_prompt.txt").write_text(
        build_thumbnail_prompt(story), encoding="utf-8"
    )

    (out_dir / "06_platform_metadata.json").write_text(
        json.dumps(build_platform_metadata(story), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    (out_dir / "07_capcut_instructions.md").write_text(
        build_capcut_instructions(story, tone), encoding="utf-8"
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("top2_json", help="path to top2.json produced by the ranker")
    ap.add_argument("--out-dir", default=None, help="output dir (default: <top2>/reel)")
    args = ap.parse_args(argv[1:])

    top2_path = Path(args.top2_json).resolve()
    data = json.loads(top2_path.read_text(encoding="utf-8"))
    stories = data.get("stories", [])
    if not stories:
        print("[reel_writer] no stories in top2.json", file=sys.stderr)
        return 1

    story = stories[0]
    out_dir = Path(args.out_dir).resolve() if args.out_dir else (top2_path.parent / "reel")
    write_package(story, out_dir)
    print(f"[reel_writer] wrote 7 files → {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
