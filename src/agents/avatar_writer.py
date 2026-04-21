"""Avatar Writer — Agent 5b of the Crypto AutoPost pipeline.

Generates a complete AI avatar video package from a ranked crypto story:

  avatar/
    script_30s.txt          — 30-second spoken script (hook → facts → CTA)
    script_60s.txt          — 60-second expanded version
    teleprompter.txt        — clean teleprompter format (one sentence per line)
    heygen_instructions.md  — HeyGen avatar setup + lip-sync tips
    elevenlabs_ssml.xml     — ElevenLabs SSML with pauses, emphasis, speed
    captions.srt            — word-level SRT for auto-captioning
    platform_cuts.json      — per-platform timing guide (TikTok 15s, 30s, 60s)

Supports Claude API rewrite for higher-quality scripts when ANTHROPIC_API_KEY is set.
Falls back to a deterministic template engine otherwise.

Usage:
    python src/agents/avatar_writer.py queue/2026-04-19_morning/top2.json
    python src/agents/avatar_writer.py queue/2026-04-19_morning/top2.json --llm
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv as _ld
    _ld(_REPO_ROOT / ".env")
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_numbers(text: str) -> list[str]:
    patterns = [
        r"\$[\d,]+(?:\.\d+)?[MBTKmbtk]?(?:\s+(?:million|billion|trillion))?",
        r"\b\d+(?:\.\d+)?%",
        r"\b\d{1,3}(?:,\d{3})+\b",
    ]
    found = []
    for p in patterns:
        found.extend(re.findall(p, text, re.IGNORECASE))
    return list(dict.fromkeys(found))[:3]


def _ticker_phrase(tickers: list[str]) -> str:
    if not tickers:
        return "the market"
    if len(tickers) == 1:
        return tickers[0]
    return " and ".join(tickers[:2])


def _best_sentences(summary: str, n: int = 3) -> list[str]:
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", summary) if len(s.strip()) > 25]
    impact_words = ("million", "billion", "$", "%", "hack", "exploit", "record",
                    "surge", "crash", "launch", "approval", "drained", "attack",
                    "first", "largest", "highest", "lowest", "broke", "hits")
    scored = sorted(sents, key=lambda s: sum(1 for w in impact_words if w in s.lower()),
                    reverse=True)
    return scored[:n]


# ---------------------------------------------------------------------------
# Mood detection
# ---------------------------------------------------------------------------

def _mood(story: dict) -> str:
    title = (story.get("title") or "").lower()
    bear_kw = {"hack", "exploit", "crash", "plunge", "stolen", "breach", "collapse",
               "banned", "lawsuit", "fraud", "liquidated", "dumped", "rejected"}
    bull_kw = {"record", "ath", "surge", "rally", "approved", "launch", "inflow",
               "bullish", "breakout", "buys", "purchased", "milestone", "etf"}
    for kw in bear_kw:
        if kw in title:
            return "bear"
    for kw in bull_kw:
        if kw in title:
            return "bull"
    return "neutral"


# ---------------------------------------------------------------------------
# Hook generator — the first 3 seconds that stop the scroll
# ---------------------------------------------------------------------------

def _make_hook(story: dict, mood: str) -> str:
    title  = _clean(story.get("title", ""))
    nums   = _extract_numbers(title + " " + (story.get("summary") or ""))
    ticker = _ticker_phrase(story.get("tickers") or [])
    num    = nums[0] if nums else None

    if mood == "bear":
        hooks = [
            f"Wait — {ticker} just got hit. This is serious.",
            f"Breaking: someone just lost {num or 'millions'} in crypto. Here's what happened.",
            f"This is why {ticker} dropped today — and it's bigger than you think.",
            f"The crypto market just flashed a red warning. You need to see this.",
        ]
    elif mood == "bull":
        hooks = [
            f"{ticker} is making history right now — and most people missed it.",
            f"{'Someone just bought ' + num + ' worth of ' + ticker if num else ticker + ' is breaking records'} — here's why it matters.",
            f"This is the {ticker} news everyone's been waiting for.",
            f"If you hold {ticker} — this just changed everything.",
        ]
    else:
        hooks = [
            f"Big crypto update — {ticker} is in the headlines today.",
            f"Here's the most important crypto story you need to know right now.",
            f"This just happened in crypto — and it affects your portfolio.",
            f"{ticker} news just dropped. Let me break it down in 30 seconds.",
        ]

    # Pick hook deterministically by title hash
    idx = hash(title) % len(hooks)
    return hooks[idx]


# ---------------------------------------------------------------------------
# Script builder — 30s and 60s versions
# ---------------------------------------------------------------------------

def _build_script(story: dict, target_seconds: int = 30) -> dict:
    title   = _clean(story.get("title", ""))
    summary = _clean(story.get("summary") or "")
    ticker  = _ticker_phrase(story.get("tickers") or [])
    nums    = _extract_numbers(title + " " + summary)
    mood    = _mood(story)
    hook    = _make_hook(story, mood)
    sents   = _best_sentences(summary, n=4 if target_seconds >= 60 else 2)
    source  = (story.get("source") or "").replace("_", " ").title()

    # Context block
    if nums:
        context = f"According to {source or 'reports'}, {title.rstrip('.')}."
    else:
        context = f"{source or 'Reports'} confirm: {title.rstrip('.')}."

    # Impact block — key facts
    impact_lines = []
    for s in sents:
        if s.lower() not in context.lower():
            impact_lines.append(s)

    # Build script sections
    sections: list[tuple[str, str, str]] = []

    # HOOK (0-3s)
    sections.append(("HOOK", "0:00–0:03", hook))

    # CONTEXT (3-12s)
    sections.append(("CONTEXT", "0:03–0:12", context))

    # IMPACT — key facts (12-25s for 30s, 12-45s for 60s)
    if target_seconds >= 60 and len(impact_lines) >= 2:
        fact_block = " ".join(impact_lines[:3])
        sections.append(("FACTS", "0:12–0:40", fact_block))
    elif impact_lines:
        fact_block = impact_lines[0]
        sections.append(("FACTS", "0:12–0:22", fact_block))

    # CTA (last 5-8 seconds)
    cta_map = {
        "bear": f"Follow for more crypto alerts. And drop a comment — do you think {ticker} recovers from this?",
        "bull": f"Follow for daily crypto updates. And tell me in the comments — are you buying {ticker} right now?",
        "neutral": f"Follow for the top crypto stories every day. What do you think about {ticker}? Comment below.",
    }
    cta_timing = f"0:22–0:30" if target_seconds == 30 else f"0:50–1:00"
    sections.append(("CTA", cta_timing, cta_map[mood]))

    # Assemble full script
    lines = []
    for label, timing, text in sections:
        lines.append(f"[{label} — {timing}]")
        lines.append(text)
        lines.append("")

    full_script = "\n".join(lines).strip()

    # Plain text for teleprompter (sentences only, one per line)
    all_text = " ".join([hook, context] +
                        ([fact_block] if impact_lines else []) +
                        [cta_map[mood]])
    teleprompter_lines = []
    for sent in re.split(r"(?<=[.!?])\s+", all_text):
        sent = sent.strip()
        if sent:
            teleprompter_lines.append(sent)

    return {
        "script":      full_script,
        "teleprompter": teleprompter_lines,
        "sections":    sections,
        "mood":        mood,
        "hook":        hook,
        "cta":         cta_map[mood],
        "ticker":      ticker,
        "target_s":    target_seconds,
    }


# ---------------------------------------------------------------------------
# LLM-enhanced script (when ANTHROPIC_API_KEY is available)
# ---------------------------------------------------------------------------

def _llm_script(story: dict, target_seconds: int = 30) -> str | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        model  = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
        mood   = _mood(story)
        ticker = _ticker_phrase(story.get("tickers") or [])
        title  = _clean(story.get("title", ""))
        summary = _clean(story.get("summary") or "")[:600]
        tone_guide = {
            "bear": "urgent, alarming, serious — like a financial news anchor breaking bad news",
            "bull": "excited, energetic, optimistic — like sharing a major market win with friends",
            "neutral": "professional but engaging, conversational — like a trusted crypto analyst",
        }[mood]
        prompt = f"""You are a viral crypto content creator writing a {target_seconds}-second spoken script for an AI avatar video.

Story title: {title}
Summary: {summary}
Main ticker: {ticker}
Tone: {tone_guide}

Write a {target_seconds}-second spoken script with this EXACT structure:
1. HOOK (0-3s): One shocking/intriguing sentence that stops the scroll. NO "Hey guys" or generic intros.
2. CONTEXT (3-12s): What happened, with the most important number/fact.
3. KEY FACTS (12-{target_seconds-8}s): 1-2 most impactful details. Short sentences. Like you're explaining to a friend.
4. CTA ({target_seconds-8}s-{target_seconds}s): Ask a specific question for comments. "Comment X if..." or "Do you think Y?"

Rules:
- Total word count: {target_seconds * 2 - 5} to {target_seconds * 2 + 5} words (speaking pace ~2 words/sec)
- NO emojis in the script (they're added as captions separately)
- Natural speech — contractions OK ("it's", "you're", "don't")
- Each sentence max 15 words
- End with a direct question to drive comments

Output ONLY the script text, no labels, no timestamps."""

        msg = client.messages.create(
            model=model,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as exc:
        print(f"[avatar_writer] LLM script failed: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# ElevenLabs SSML generator
# ---------------------------------------------------------------------------

def _build_ssml(script_data: dict) -> str:
    sections = script_data["sections"]
    parts = ['<?xml version="1.0"?>', '<speak>']

    for label, timing, text in sections:
        if label == "HOOK":
            # Hook: slower, dramatic
            parts.append(f'  <prosody rate="90%" pitch="+2st">{text}</prosody>')
            parts.append('  <break time="400ms"/>')
        elif label == "CONTEXT":
            parts.append(f'  <prosody rate="100%">{text}</prosody>')
            parts.append('  <break time="300ms"/>')
        elif label == "FACTS":
            # Facts: slightly faster, punchy
            for sent in re.split(r"(?<=[.!?])\s+", text):
                if sent.strip():
                    parts.append(f'  <prosody rate="105%">{sent.strip()}</prosody>')
                    parts.append('  <break time="200ms"/>')
        elif label == "CTA":
            # CTA: warm, slower
            parts.append('  <break time="500ms"/>')
            parts.append(f'  <prosody rate="88%" pitch="-1st">{text}</prosody>')

    parts.append("</speak>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# SRT caption builder
# ---------------------------------------------------------------------------

def _build_srt(teleprompter_lines: list[str], target_seconds: int) -> str:
    if not teleprompter_lines:
        return ""
    words_per_second = 2.2
    entries = []
    t = 0.0
    idx = 1
    for line in teleprompter_lines:
        words  = line.split()
        dur    = max(1.5, len(words) / words_per_second)
        start  = timedelta(seconds=t)
        end    = timedelta(seconds=min(t + dur, target_seconds))

        def _ts(td: timedelta) -> str:
            total = int(td.total_seconds())
            ms    = int((td.total_seconds() - total) * 1000)
            h, rem = divmod(total, 3600)
            m, s   = divmod(rem, 60)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        entries.append(f"{idx}\n{_ts(start)} --> {_ts(end)}\n{line}\n")
        t += dur
        idx += 1
        if t >= target_seconds:
            break

    return "\n".join(entries)


# ---------------------------------------------------------------------------
# Platform timing guide
# ---------------------------------------------------------------------------

def _platform_cuts(script_data: dict) -> dict:
    ticker = script_data["ticker"]
    mood   = script_data["mood"]
    hook   = script_data["hook"]
    cta    = script_data["cta"]

    emoji  = {"bear": "🚨", "bull": "📈", "neutral": "📰"}[mood]

    return {
        "tiktok": {
            "recommended_length_seconds": 30,
            "algorithm_notes": [
                "First 3 seconds CRITICAL — hook must be on screen immediately (no black intro)",
                "Use trending audio from TikTok sound library — crypto/finance trending sounds",
                "Caption style: large text, high contrast, covers bottom 30% of screen",
                "Post between 7-9am or 7-9pm local time for max reach",
                "Add 3-5 hashtags only: #crypto #bitcoin + 1-2 niche tags",
                "Reply to EVERY comment in first 60 min — signals engagement to algorithm",
                "Duet/stitch bait: end with 'What do you think?' to encourage reactions",
                "Use trending transitions at the 3s and 15s mark",
            ],
            "hashtags": [f"#{ticker.lower()}", "#crypto", "#cryptonews",
                         "#cryptocurrency", f"#{mood}market"],
            "caption": f"{emoji} {hook[:80]} Follow for daily crypto drops.",
            "cta_overlay": cta,
            "optimal_post_time": "07:30 or 19:30 local",
        },
        "instagram_reels": {
            "recommended_length_seconds": 30,
            "algorithm_notes": [
                "Cover frame = first frame — make it visually striking (close-up face or big text)",
                "Caption: 150-300 chars with line breaks for readability",
                "Use 5-10 hashtags: mix of large (1M+) and niche (<100K)",
                "Share to Stories immediately after posting — cross-promotes",
                "Go Live within 24h of a viral Reel — Instagram boosts your next post",
                "Carousel post with data screenshots the next day = more saves",
                "Saves and shares matter MORE than likes for Reels algorithm",
                "Subtitles increase watch-through rate by 40% — always add captions",
            ],
            "hashtags": [f"#{ticker.lower()}", "#crypto", "#cryptonews",
                         "#investing", "#finance", "#cryptotrading",
                         "#blockchain", "#bitcoin", f"#{mood}signal"],
            "caption": (f"{emoji} {hook[:120]}\n\n"
                        f"Follow @EliteMarginDesk for daily crypto analysis 🔔\n\n"
                        f"#crypto #{ticker.lower()} #cryptonews #investing #bitcoin"),
            "cta_overlay": cta,
            "optimal_post_time": "09:00 or 18:00 local",
        },
        "youtube_shorts": {
            "recommended_length_seconds": 59,
            "algorithm_notes": [
                "Title: under 60 chars, include ticker symbol and a number/stat",
                "Thumbnail: first frame — use bold text overlay (Shorts auto-thumbnails from frame 1)",
                "Description: first 100 chars shown — put main keyword + link",
                "End screen: 'Subscribe for daily crypto alerts' — pump subscribe CTA",
                "YouTube Shorts gets 50B daily views — consistency matters more than virality",
                "Upload daily — algorithm rewards daily uploaders with wider distribution",
                "Use chapters if > 60s: 0:00 Hook, 0:10 Story, 0:45 Analysis, 0:55 CTA",
                "Audience retention > 70% = YouTube pushes the Short widely",
                "Reply to comments with pinned comment to boost engagement score",
            ],
            "title": f"{emoji} {ticker} Alert: {hook[:45]}",
            "description": (
                f"{hook}\n\n"
                f"📊 Daily crypto analysis | EliteMarginDesk.io\n"
                f"🔔 Subscribe for real-time alerts\n\n"
                f"#{ticker.lower()} #crypto #shorts #cryptonews #bitcoin"
            ),
            "hashtags": [f"#{ticker.lower()}", "#crypto", "#shorts",
                         "#cryptonews", "#bitcoin", "#investing"],
            "cta_overlay": cta,
            "optimal_post_time": "15:00-17:00 UTC (global peak)",
        },
    }


# ---------------------------------------------------------------------------
# HeyGen / D-ID instructions
# ---------------------------------------------------------------------------

def _heygen_instructions(script_data: dict, script_30s: str, script_60s: str) -> str:
    mood   = script_data["mood"]
    ticker = script_data["ticker"]
    mood_style = {
        "bear": "serious, slightly worried expression — like a financial anchor breaking bad news",
        "bull": "energetic, confident, slightly excited — like sharing great news with a friend",
        "neutral": "professional, calm, trustworthy — crypto analyst presenting data",
    }[mood]
    avatar_style = {
        "bear": "Professional suit, dark background with red accents, formal studio",
        "bull": "Smart casual, modern office background, green or gold accent lighting",
        "neutral": "Business casual, clean modern background with blue/white tones",
    }[mood]

    return f"""# HeyGen Avatar Video Setup
## Story: {script_data.get('hook','')[:80]}

---

## STEP 1 — Choose Your Avatar
- Style: {mood_style}
- Recommended HeyGen avatars: "Aria" or "Ethan" (professional, news-anchor style)
- Alternative: upload your own custom avatar for brand consistency
- Avatar outfit: {avatar_style}

## STEP 2 — Script Input
Paste the 30-second script below into HeyGen's script field:

```
{script_30s}
```

For the 60-second version (YouTube Shorts):
```
{script_60s}
```

## STEP 3 — Voice Settings
- Voice: English (US), neutral/professional tone
- Speed: 0.95x (slightly slower = clearer, more authoritative)
- Pitch: default or -1 semitone for more gravitas
- For bear news: choose a slightly deeper voice variant
- For bull news: slightly faster speed (1.05x), higher energy

## STEP 4 — Background & B-roll
Use the generated image (image_reel_1080x1920.png) as background, OR:
- Bear mood: dark trading floor, red charts (Pexels: "stock market crash")
- Bull mood: green charts, bull symbol (Pexels: "bull market crypto")
- Neutral: Bloomberg-style terminal (Pexels: "trading desk")

## STEP 5 — Captions
- Enable auto-captions in HeyGen
- Font: Bold, high contrast (white text + black stroke)
- Position: bottom 25% of screen
- Max 6 words per caption line
- Import captions.srt for manual timing control

## STEP 6 — Export Settings
- Resolution: 1080x1920 (9:16 vertical)
- FPS: 30
- Quality: High (for all platforms)
- Export separate: 30s version + 60s version

## STEP 7 — Post-Production (CapCut/Premiere)
1. Import HeyGen export
2. Add: animated price chart overlay (bottom 20%)
3. Add: ticker pill animation (top right, accent colour)
4. Add: trending audio from TikTok/Instagram library (lower volume ~15%)
5. Add: text pop-ups on key numbers/facts
6. Export: H.264, 1080x1920, 60fps for TikTok/Reels

---

## D-ID Alternative Setup
1. Upload avatar photo → create talking avatar
2. Paste script_30s.txt as input
3. Use "natural" voice with pauses from elevenlabs_ssml.xml
4. Same background/export settings as above

## ElevenLabs Voice-Only (for voice-over use)
1. Import elevenlabs_ssml.xml into ElevenLabs SSML editor
2. Voice: "Adam" (authoritative) or "Rachel" (professional female)
3. Stability: 0.65, Similarity: 0.80
4. Export as MP3 → import into CapCut as voice track
"""


# ---------------------------------------------------------------------------
# Main package writer
# ---------------------------------------------------------------------------

def write_package(story: dict, out_dir: Path, use_llm: bool = False) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    # Build template scripts
    data_30 = _build_script(story, target_seconds=30)
    data_60 = _build_script(story, target_seconds=60)

    script_30 = data_30["script"]
    script_60 = data_60["script"]

    # LLM enhancement if requested + key available
    if use_llm:
        llm_30 = _llm_script(story, 30)
        llm_60 = _llm_script(story, 60)
        if llm_30:
            script_30 = llm_30
            print("[avatar_writer] using LLM-enhanced 30s script", file=sys.stderr)
        if llm_60:
            script_60 = llm_60
            print("[avatar_writer] using LLM-enhanced 60s script", file=sys.stderr)

    # Teleprompter (clean, sentence-per-line)
    tele_lines = data_30["teleprompter"]
    teleprompter = "\n".join(f"{i+1:02d}. {line}" for i, line in enumerate(tele_lines))

    # ElevenLabs SSML
    ssml = _build_ssml(data_30)

    # SRT captions
    srt_30 = _build_srt(tele_lines, 30)
    srt_60 = _build_srt(data_60["teleprompter"], 60)

    # Platform cuts
    cuts = _platform_cuts(data_30)

    # HeyGen instructions
    heygen = _heygen_instructions(data_30, script_30, script_60)

    # Write all files
    files = {
        "script_30s.txt":         script_30,
        "script_60s.txt":         script_60,
        "teleprompter.txt":       teleprompter,
        "heygen_instructions.md": heygen,
        "elevenlabs_ssml.xml":    ssml,
        "captions_30s.srt":       srt_30,
        "captions_60s.srt":       srt_60,
        "platform_cuts.json":     json.dumps(cuts, indent=2, ensure_ascii=False),
    }
    for fname, content in files.items():
        (out_dir / fname).write_text(content + "\n", encoding="utf-8")

    print(f"[avatar_writer] wrote {len(files)} files -> {out_dir}", file=sys.stderr)
    return {"files": list(files.keys()), "mood": data_30["mood"],
            "hook": data_30["hook"], "out_dir": str(out_dir)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("top2_json")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--llm", action="store_true",
                    help="enhance script with Claude API (requires ANTHROPIC_API_KEY)")
    args = ap.parse_args(argv[1:])

    data    = json.loads(Path(args.top2_json).read_text(encoding="utf-8"))
    stories = data.get("stories", data) if isinstance(data, dict) else data
    if not stories:
        print("[avatar_writer] no stories", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir).resolve() if args.out_dir else Path(args.top2_json).parent / "avatar"
    result  = write_package(stories[0], out_dir, use_llm=args.llm)
    print(f"[avatar_writer] done -> {result['out_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
