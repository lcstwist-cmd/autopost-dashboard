"""Avatar Writer — Agent 5b of the Crypto AutoPost pipeline.

Generates a complete AI avatar video package from a ranked crypto story.
Scripts are tight 20-second pieces (35–45 words) tuned for D-ID + short-form
social algorithms (TikTok/Reels/Shorts).

  avatar/
    script_20s.txt          — 20-second spoken script (hook → context → impact → CTA)
    teleprompter.txt        — clean teleprompter format (one sentence per line)
    heygen_instructions.md  — HeyGen/D-ID avatar setup + lip-sync tips
    elevenlabs_ssml.xml     — ElevenLabs SSML with pauses, emphasis, speed
    captions_20s.srt        — SRT captions timed for 20s output
    platform_cuts.json      — per-platform algorithm notes + captions/hashtags

  Legacy filenames (script_30s.txt / script_60s.txt) are also written as
  symlinks-via-copy for backwards compatibility with older pipeline code that
  still references them.

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

# Force UTF-8 on stdout/stderr so log lines with unicode (e.g. "≈", "→")
# don't crash on Windows cp1252 terminals.
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

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
    """Extract most impactful sentences from summary for video script."""
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", summary) if len(s.strip()) > 20]
    
    # High-impact keywords (financial/technical impact)
    impact_words = (
        "million", "billion", "trillion", "$", "%", "gained", "lost",
        "hack", "exploit", "breach", "record", "ath", "surge", "crash", 
        "plunge", "launch", "approval", "drained", "attack", "stolen",
        "first", "largest", "highest", "lowest", "broke", "hits", "pump",
        "dump", "liquidat", "fund", "invest", "raise", "acquire", "partner",
        "regulatory", "sec", "cftc", "compliance", "ban", "restrict"
    )
    
    def score_sent(s: str) -> tuple[int, int]:
        """Score by impact keywords (primary) and % mentions (secondary)."""
        impact_count = sum(1 for w in impact_words if w in s.lower())
        pct_count = len(re.findall(r"\d+(?:\.\d+)?%", s))
        return (impact_count, pct_count)
    
    scored = sorted(sents, key=score_sent, reverse=True)
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
            f"Something just happened with {ticker}... and you need to hear this.",
            f"Red alert. {ticker or 'Crypto'} is flashing warning signs right now.",
            f"The market just sent a brutal signal — here's what's going on.",
            f"Okay, stop scrolling. {ticker} just did something alarming.",
        ]
    elif mood == "bull":
        hooks = [
            f"{ticker} is making history — and most people missed it.",
            f"Okay... {ticker} just did something incredible. Let me explain.",
            f"If you hold {ticker} — stop what you're doing. This is huge.",
            f"Everybody's talking about {ticker} right now, and for good reason.",
            f"{'Someone just moved ' + num + ' into ' + ticker if num else ticker + ' just broke a major level'} — here's why it matters.",
        ]
    else:
        hooks = [
            f"Quick — something important just happened with {ticker}.",
            f"Here's what's moving the crypto market right now.",
            f"This just dropped... and you need to know about it.",
            f"{ticker} is in the headlines — let me break it down fast.",
            f"Not gonna lie — this {ticker} news caught me off guard.",
        ]

    idx = hash(title) % len(hooks)
    return hooks[idx]


# ---------------------------------------------------------------------------
# Roundup script — multiple headlines in one clip
# ---------------------------------------------------------------------------

def _build_roundup_script(stories: list, target_seconds: int = 45) -> dict:
    """Build a professional news-anchor roundup script covering N stories.

    Target 45 seconds for 3 stories — enough time for context + impact per story.

    Layout:
        HOOK        ~8%  — punchy opener that stops the scroll
        STORY 1..N  ~82% — each story: headline + one impact sentence
        CTA         ~10% — professional sign-off with follow prompt
    """
    target_seconds = max(15, min(int(target_seconds or 45), 90))
    n = max(1, min(len(stories), 5))

    wps = 2.2   # words per second at natural TTS pace
    hook_s  = max(2.5, target_seconds * 0.08)
    cta_s   = max(3.0, target_seconds * 0.10)
    story_s = max(5.0, (target_seconds - hook_s - cta_s) / n)
    # Allow more words per story for 45s clips so each story gets real context
    story_words = max(10, int(story_s * wps))

    def _ts(t: float) -> str:
        m = int(t // 60); s = int(t % 60)
        return f"{m}:{s:02d}"

    def _trim(text: str, max_w: int) -> str:
        """Trim to max_w words, ending on a clean word boundary with proper period."""
        text = (text or "").strip()
        if not text:
            return ""
        words = text.split()[:max_w]
        filler = {"and", "or", "but", "of", "for", "to", "in", "on", "at", "by",
                  "with", "the", "a", "an", "is", "are", "was", "were", "as",
                  "via", "from", "into", "onto", "—", "–", "-", "&", "that"}
        while words and words[-1].lower().rstrip(".,!?;:") in filler:
            words.pop()
        if not words:
            return ""
        result = " ".join(words).rstrip(" .,!?;:—–-")
        # Ensure it ends with a period for clean TTS delivery
        if not result.endswith((".", "!", "?")):
            result += "."
        return result

    def _clean_title(title: str) -> str:
        """Strip source suffixes and parentheticals that read awkwardly aloud."""
        title = re.sub(r"\s+[\-–—]\s+[A-Za-z0-9 .]+\.(net|com|co|io|news|tv)\b",
                       "", title)
        title = re.sub(r"\s*\([^)]+\)\s*$", "", title)
        # Remove quote marks that sound odd in TTS
        title = title.replace('"', '').replace('"', '').replace('"', '')
        return title.strip()

    def _impact_phrase(story: dict) -> str:
        """Extract a short impact phrase from the story summary."""
        summary = _clean(story.get("summary") or "")
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", summary)
                 if len(s.strip()) > 15]
        nums = _extract_numbers(summary)
        if nums and sents:
            # Find the sentence with a number — most impactful
            for s in sents:
                if any(num in s for num in nums):
                    return _trim(s, 12)
        return _trim(sents[0], 12) if sents else ""

    sections: list[tuple[str, str, str]] = []
    t = 0.0

    # HOOK — professional news-anchor opener, varies by mood
    primary_mood = _mood(stories[0]) if stories else "neutral"
    hook_variants = {
        "bull": [
            "Big moves in crypto today — here are the three stories you need to know.",
            "The market is moving fast. Three stories, right now.",
            "Crypto is on fire today — let's break down the top three stories.",
        ],
        "bear": [
            "Major developments in crypto — three critical stories breaking right now.",
            "Crypto markets are under pressure. Here's what you need to know.",
            "Three breaking crypto stories you cannot ignore today.",
        ],
        "neutral": [
            "Here are the three most important crypto stories of the day.",
            "Your daily crypto briefing — three stories, straight to the point.",
            "Top three crypto stories right now — let's get into it.",
        ],
    }
    variants = hook_variants.get(primary_mood, hook_variants["neutral"])
    hook_line = variants[hash(stories[0].get("title", "")) % len(variants)]
    sections.append(("HOOK", f"{_ts(t)}-{_ts(t + hook_s)}", hook_line))
    teleprompter_lines = [hook_line]
    t += hook_s

    # Per-story segments — headline + impact
    ordinals = ["First", "Second", "Third", "Fourth", "Fifth"]
    for i, story in enumerate(stories[:n]):
        title = _clean_title(_clean(story.get("title", "") or story.get("headline", "")))
        if not title:
            continue
        headline = _trim(title, story_words // 2)
        impact   = _impact_phrase(story)

        # Build the sentence: "First — Bitcoin just crossed $80K, a new all-time high."
        ordinal = ordinals[i]
        if impact and impact.rstrip(".") not in headline.rstrip("."):
            # Two sentences: "First — Headline. Impact sentence."
            full = f"{ordinal} — {headline} {impact}"
        else:
            full = f"{ordinal} — {headline}"

        # Natural spoken punctuation: em-dash creates a dramatic pause in TTS
        sections.append((f"STORY{i + 1}", f"{_ts(t)}-{_ts(t + story_s)}", full))
        teleprompter_lines.append(full)
        t += story_s

    # CTA — warm, professional sign-off
    cta_line = "Follow us for daily crypto briefings — straight, fast, and reliable."
    sections.append(("CTA", f"{_ts(t)}-{_ts(target_seconds)}", cta_line))
    teleprompter_lines.append(cta_line)

    full_script = "\n".join(
        f"[{lab} — {tim}]\n{txt}\n"
        for lab, tim, txt in sections
    ).strip()

    return {
        "script":       full_script,
        "teleprompter": teleprompter_lines,
        "sections":     sections,
        "mood":         primary_mood,
        "hook":         hook_line,
        "cta":          cta_line,
        "ticker":       "",
        "target_s":     target_seconds,
        "n_stories":    n,
        "headlines":    [_clean_title(_clean(s.get("title") or s.get("headline") or ""))
                         for s in stories[:n]],
    }


# ---------------------------------------------------------------------------
# Script builder — 30s and 60s versions
# ---------------------------------------------------------------------------

def _build_script(story: dict, target_seconds: int = 20) -> dict:
    """Build a tight script of `target_seconds` length.

    Pacing: ~2.2 words/sec (D-ID/HeyGen TTS natural rate).
        20s ≈ 44 words   25s ≈ 55 words   30s ≈ 66 words   45s ≈ 99 words

    Section split (proportional to total duration):
        HOOK        10% of clip
        CONTEXT     15%
        KEY_FACT    45%
        CTA         30%
    """
    title   = _clean(story.get("title", ""))
    summary = _clean(story.get("summary") or "")
    ticker  = _ticker_phrase(story.get("tickers") or [])
    nums    = _extract_numbers(title + " " + summary)
    mood    = _mood(story)
    hook    = _make_hook(story, mood)
    # Pull two distinct sentences when target_seconds >= 25 to fill KEY_FACT
    # without repeating the title (this was the CONTEXT == KEY_FACT bug).
    n_sents = 2 if target_seconds >= 25 else 1
    sents   = _best_sentences(summary, n=n_sents)

    # ---- Compute per-section durations + word budgets --------------------
    target_seconds = max(10, min(int(target_seconds or 20), 60))
    wps = 2.2
    hook_s    = max(2.0, target_seconds * 0.10)
    context_s = max(2.5, target_seconds * 0.15)
    cta_s     = max(4.0, target_seconds * 0.30)
    fact_s    = max(4.0, target_seconds - hook_s - context_s - cta_s)

    hook_words    = max(8,  int(hook_s    * wps))
    context_words = max(8,  int(context_s * wps))
    fact_words    = max(10, int(fact_s    * wps))

    def _ts(t: float) -> str:
        m = int(t // 60); s = int(t % 60)
        return f"{m}:{s:02d}"

    # Helper: truncate to max N words, terminate with a period, and never
    # cut mid-phrase ("…Ahead of.", "…right now — and.").
    _trailing_filler = {
        "and", "or", "but", "of", "for", "to", "in", "on", "at", "by",
        "with", "the", "a", "an", "is", "are", "was", "were", "as",
        "via", "from", "into", "onto",
    }
    def _words_max(text: str, n: int) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        words = text.split()[:n]
        # Strip trailing connectors / dashes / single-letter junk
        while words and (words[-1].lower().rstrip(".,!?;:") in _trailing_filler
                          or words[-1] in {"—", "–", "-", "&"}):
            words.pop()
        if not words:
            return ""
        return " ".join(words).rstrip(" .,!?;:—–-") + "."

    # HOOK — attention-grabbing
    hook_short = _words_max(hook, hook_words)

    # CONTEXT — what happened (always built from the title, never the summary)
    context = _words_max(title, context_words)

    # KEY_FACT — must be DIFFERENT from CONTEXT.
    # Priority order:
    #   1) summary sentence #2 (if available and >= 6 words distinct from title)
    #   2) summary sentence #1 (if it doesn't just repeat the title)
    #   3) ticker + first numeric figure ("Bitcoin moved 12% in 24h")
    #   4) generic mood-aware fallback
    def _too_similar(a: str, b: str) -> bool:
        a_words = set(a.lower().split())
        b_words = set(b.lower().split())
        if not a_words or not b_words:
            return False
        overlap = len(a_words & b_words) / max(len(a_words), 1)
        return overlap > 0.75

    fact_pool: list[str] = []
    for s in sents:
        s = (s or "").strip()
        if not s or _too_similar(s, title):
            continue
        if any(_too_similar(s, prev) for prev in fact_pool):
            continue
        fact_pool.append(s)

    if fact_pool:
        impact_line = _words_max(fact_pool[0], fact_words)
    elif ticker and nums:
        first_num = nums[0]
        impact_line = f"{ticker} moved {first_num} — the market's reacting fast."
    else:
        impact_line = {
            "bull":    "The market's responding — momentum is building fast.",
            "bear":    "Pressure is mounting — watch your positions carefully.",
            "neutral": "Watch this one closely — it could move fast.",
        }[mood]
        impact_line = _words_max(impact_line, fact_words)

    # Build script sections — timings computed from duration budget
    sections: list[tuple[str, str, str]] = []
    t = 0.0
    sections.append(("HOOK",     f"{_ts(t)}–{_ts(t+hook_s)}",     hook_short));    t += hook_s
    sections.append(("CONTEXT",  f"{_ts(t)}–{_ts(t+context_s)}",  context));       t += context_s
    sections.append(("KEY_FACT", f"{_ts(t)}–{_ts(t+fact_s)}",     impact_line));   t += fact_s

    cta_map = {
        "bear":    "Are you still holding? Drop your take in the comments.",
        "bull":    "Are you buying this? Tell me below — and follow for daily updates.",
        "neutral": "What's your read on this? Follow for daily crypto updates.",
    }
    sections.append(("CTA",      f"{_ts(t)}–{_ts(target_seconds)}", _words_max(cta_map[mood], max(8, int(cta_s * wps)))))

    # Assemble full script
    lines = []
    for label, timing, text in sections:
        lines.append(f"[{label} — {timing}]")
        lines.append(text)
        lines.append("")

    full_script = "\n".join(lines).strip()

    # Plain text for teleprompter (sentences only, one per line)
    all_text = " ".join([hook_short, context] +
                        ([impact_line] if impact_line else []) +
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
        "hook":        hook_short,
        "cta":         cta_map[mood],
        "ticker":      ticker,
        "target_s":    target_seconds,
    }


# ---------------------------------------------------------------------------
# LLM-enhanced script (when ANTHROPIC_API_KEY is available)
# ---------------------------------------------------------------------------

def _load_user_blueprint(story: dict) -> dict | None:
    """Best-effort: pull the freshest viral blueprint for THIS story's topic.

    Tries the story's first ticker as topic, then "crypto news", then "crypto".
    Returns None when nothing is cached or the DB is unreachable — caller
    must handle this gracefully (the avatar_writer never fails on missing BP).
    """
    try:
        from src.dashboard.database import get_all_users, get_viral_blueprint
        admins = [u for u in get_all_users()
                  if u.get("is_admin") and u.get("is_approved")]
        if not admins:
            return None
        uid = admins[0]["id"]
        candidates: list[str] = []
        if story.get("tickers"):
            candidates.extend(t.lower() for t in story["tickers"][:2])
        candidates.extend(["crypto news", "crypto"])
        for t in candidates:
            bp = get_viral_blueprint(uid, t)
            if bp and bp.get("sample_size"):
                return bp
    except Exception:
        return None
    return None


def _llm_script(story: dict, target_seconds: int = 20) -> str | None:
    """Generate a 20-second (35–45 word) spoken script via Claude.

    Output is plain prose, 4 sentences, ready to paste into D-ID/HeyGen.
    Structure: HOOK → CONTEXT → IMPACT → CTA.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        model  = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
        mood   = _mood(story)
        ticker = _ticker_phrase(story.get("tickers") or [])
        title  = _clean(story.get("title", ""))
        summary = _clean(story.get("summary") or "")[:800]

        # Pull viral blueprint (if any) and turn it into a prompt fragment
        # the model can imitate. Falls back to "" gracefully when missing.
        viral_block = ""
        try:
            from src.agents.viral_transformer import build_viral_prompt
            bp = _load_user_blueprint(story)
            viral_block = build_viral_prompt(bp, ticker=ticker or "BTC",
                                             target_seconds=target_seconds)
        except Exception as exc:
            print(f"[avatar_writer] viral blueprint inject skipped: {exc}",
                  file=sys.stderr)

        tone_guide = {
            "bear": "urgent, alarming, serious — like a financial news anchor breaking bad news",
            "bull": "excited, energetic, optimistic — like sharing a major market win with friends",
            "neutral": "professional but engaging, conversational — like a trusted crypto analyst",
        }[mood]

        prompt = f"""You are a viral crypto content creator writing a 20-to-25-SECOND spoken script for an AI avatar video (TikTok / Instagram Reels / YouTube Shorts).

STORY:
- Title: {title}
- Summary: {summary}
- Main ticker: {ticker}
- Market mood: {mood.upper()}
- Tone: {tone_guide}

HARD RULES — violating any of these breaks the pipeline:
- Total length: 45-55 words (scales to ~20-24 seconds at normal pace).
  Count your words. Under 45 = too short. Over 55 = too long.
- Exactly 4 sentences, one per section below.
- No labels, no headers, no timestamps, no markdown, no emojis.
- Output is ONE plain paragraph of 4 sentences separated by spaces.

SECTION STRUCTURE (4 sentences total):
1. HOOK (up to 14 words) — one shocking sentence that stops the scroll.
   Bear example: "Wait — {ticker} just got hacked. This is critical."
   Bull example: "{ticker} just hit a new all-time high."
   Neutral example: "Breaking — {ticker} just announced something huge."

2. CONTEXT (up to 14 words) — the core fact. Include a number if available
   (price, %, volume, $ amount).

3. IMPACT (up to 16 words) — why this matters to the viewer's portfolio or
   the market. One concrete consequence. No hedging, no "could potentially".

4. CTA (up to 10 words) — one direct question that invites a comment.
   Examples: "What's your next move?" / "Are you buying or selling?" /
   "Is this the bottom?"

STYLE — CRITICAL for human-sounding TTS:
- Natural spoken English. Use contractions: it's, they're, don't, can't, that's, here's.
- NO "Hey guys", NO "welcome back", NO "let's dive in".
- NO "in this video I'll explain" — just GO.
- Sound like a trusted analyst texting a friend, NOT a corporate press release.

PUNCTUATION FOR NATURAL TTS RHYTHM:
- Comma = short breath: "Bitcoin, once again, is in the spotlight."
- Em-dash = dramatic pause: "And the price — it just jumped 12%."
- Ellipsis = suspense / build-up: "Wait... this is bigger than it looks."
- Question mark = natural inflection rise: "Is this the bottom? I don't think so."
- Keep sentences SHORT — max 12 words each. Short = punchy = human.
- Avoid: "however", "therefore", "furthermore", "it is worth noting" — stiff and robotic.
- Use: "here's the thing", "look", "honestly", "watch this", "think about it" — natural.

GOOD EXAMPLES (4-sentence structure):
Bear: "Wait — {ticker} just dropped hard. The price fell {num or '8%'} in under two hours. That's a major liquidation event — traders are panic-selling right now. Are you still in? Drop it below."
Bull: "Okay, {ticker} just hit a new level — and it happened fast. {num or 'A big fund'} just moved in, and volume is exploding. This is the breakout people have been waiting for. Are you buying this? Tell me."

OUTPUT ONLY the 4 sentences, joined by single spaces. Nothing else."""

        if viral_block:
            prompt = prompt + "\n" + viral_block

        msg = client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        # Strip any stray labels the model might include (HOOK:, CONTEXT: etc.)
        text = re.sub(r"(?im)^\s*(HOOK|CONTEXT|IMPACT|CTA|KEY\s*FACT)\s*[:\-—]\s*",
                      "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text
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
        elif label in ("FACTS", "KEY_FACT", "IMPACT"):
            # Facts/impact: slightly faster, punchy
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
            "recommended_length_seconds": 20,
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
            "recommended_length_seconds": 20,
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
            "recommended_length_seconds": 20,
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

def _heygen_instructions(script_data: dict, script_20s: str) -> str:
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

    word_count = len(script_20s.split())
    est_seconds = round(word_count / 2.3, 1)

    return f"""# HeyGen / D-ID Avatar Video Setup — 20 SECONDS
## Story: {script_data.get('hook','')[:80]}
## Script length: {word_count} words ≈ {est_seconds}s of speech

---

## STEP 1 — Choose Your Avatar
- Style: {mood_style}
- Recommended HeyGen avatars: "Aria" or "Ethan" (professional, news-anchor style)
- Recommended D-ID presenter: "Noelle_f" (built-in, always compatible)
- Alternative: upload your own custom avatar for brand consistency
- Avatar outfit: {avatar_style}

## STEP 2 — Script Input
Paste the 20-second script below into HeyGen/D-ID's script field:

```
{script_20s}
```

This is THE ONLY script — no 30s/60s variants. Short-form platforms
(TikTok / Reels / Shorts) reward 15–22s clips; going longer kills
watch-through rate and caps reach.

## STEP 3 — Voice Settings
- Voice: English (US), neutral/professional tone
- Speed: 1.0x (the script is already trimmed — don't slow it down)
- Pitch: default or -1 semitone for more gravitas
- For bear news: choose a slightly deeper voice variant
- For bull news: slightly faster speed (1.05x), higher energy

## STEP 4 — Background & B-roll
Use the generated image (image_reel_1080x1920.png) as background, OR:
- Bear mood: dark trading floor, red charts (Pexels: "stock market crash")
- Bull mood: green charts, bull symbol (Pexels: "bull market crypto")
- Neutral: Bloomberg-style terminal (Pexels: "trading desk")

## STEP 5 — Captions
- Enable auto-captions in HeyGen/D-ID
- Font: Bold, high contrast (white text + black stroke)
- Position: bottom 25% of screen
- Max 6 words per caption line
- Import captions_20s.srt for manual timing control

## STEP 6 — Export Settings
- Resolution: 1080x1920 (9:16 vertical)
- FPS: 30
- Quality: High (for all platforms)
- Hard cap: 20 seconds

## STEP 7 — Post-Production (CapCut/Premiere, optional)
1. Import HeyGen/D-ID export
2. Add: animated price chart overlay (bottom 20%)
3. Add: ticker pill animation (top right, accent colour)
4. Add: trending audio from TikTok/Instagram library (lower volume ~15%)
5. Add: text pop-ups on key numbers/facts
6. Export: H.264, 1080x1920, 60fps

---

## D-ID API Setup (automated)
The pipeline calls D-ID /talks automatically — no manual step needed.
If you want to regenerate by hand:
1. Go to studio.d-id.com → Create Video → Text-to-Video
2. Paste `script_20s.txt` as the input
3. Pick a presenter (or upload a photo)
4. Select "Edge TTS" voice (free on Lite plan)
5. Export

## ElevenLabs Voice-Only (for voice-over use)
1. Import elevenlabs_ssml.xml into ElevenLabs SSML editor
2. Voice: "Adam" (authoritative) or "Rachel" (professional female)
3. Stability: 0.65, Similarity: 0.80
4. Export as MP3 → import into CapCut as voice track
"""


# ---------------------------------------------------------------------------
# Main package writer
# ---------------------------------------------------------------------------

def _cap_words_20s(text: str, max_words: int = 57) -> str:
    """Truncate LLM output to at most 57 words (~25s) on a sentence boundary."""
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    candidate = " ".join(words[:max_words])
    terminators = list(re.finditer(r"[.!?]", candidate))
    if terminators:
        return candidate[: terminators[-1].end()].strip()
    return candidate.rstrip(",;:—- ").rstrip() + "."


def write_package(story, out_dir: Path, use_llm: bool = False,
                   target_seconds: int | None = None) -> dict:
    """Write the full avatar package (script, SRT, teleprompter, …) for a story.

    `story` can now be either:
      • dict      → single-story format (legacy behaviour)
      • list[dict]→ multi-story roundup ("Three crypto stories you can't miss…")
                     Each story produces one numbered bullet in the script.

    target_seconds:
        Length of the spoken clip. When None we honour, in order:
          1) caller-provided value
          2) os.environ['AVATAR_TARGET_SECONDS']
          3) os.environ['VIDEO_DURATION_TIKTOK'] (most common platform)
          4) 20-second default
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve duration (per-call → env → default).
    if target_seconds is None:
        for key in ("AVATAR_TARGET_SECONDS",
                    "VIDEO_DURATION_TIKTOK",
                    "VIDEO_DURATION_INSTAGRAM",
                    "VIDEO_DURATION_YOUTUBE_SHORTS"):
            v = (os.environ.get(key, "") or "").strip()
            if v:
                try:
                    target_seconds = int(float(v))
                    break
                except (TypeError, ValueError):
                    continue
    target_seconds = max(10, min(int(target_seconds or 20), 60))

    # Multi-story roundup support: if caller passed a list, switch to roundup.
    is_roundup = isinstance(story, list) and len(story) > 1
    if is_roundup:
        roundup_data = _build_roundup_script(story, target_seconds=target_seconds)
        # Roundup never uses LLM (the structure is too tight). Write minimal
        # SRT + teleprompter + headline list for the image renderer.
        script_20 = roundup_data["script"]
        tele_lines = roundup_data["teleprompter"]
        teleprompter = "\n".join(f"{i+1:02d}. {ln}"
                                  for i, ln in enumerate(tele_lines))
        srt_20 = _build_srt(tele_lines, target_seconds)
        files = {
            "script_20s.txt":         script_20,
            "script_30s.txt":         script_20,   # legacy alias
            "script_60s.txt":         script_20,   # legacy alias
            "teleprompter.txt":       teleprompter,
            "captions_20s.srt":       srt_20,
            "captions_30s.srt":       srt_20,
            "captions_60s.srt":       srt_20,
            "headlines.json":         json.dumps(roundup_data["headlines"],
                                                  indent=2, ensure_ascii=False),
        }
        (out_dir / ".target_seconds").write_text(str(target_seconds),
                                                   encoding="utf-8")
        for fname, content in files.items():
            (out_dir / fname).write_text(content + "\n", encoding="utf-8")
        print(f"[avatar_writer] roundup mode: {roundup_data['n_stories']} "
              f"stories -> {target_seconds}s clip", file=sys.stderr)
        return {"files": list(files.keys()),
                "mood": "neutral",
                "hook": roundup_data["hook"],
                "out_dir": str(out_dir),
                "headlines": roundup_data["headlines"]}

    # Legacy single-story path: unwrap if a 1-element list was passed
    if isinstance(story, list):
        story = story[0] if story else {}

    # Word cap scales with duration (~2.2 wps natural TTS pace + 25% headroom).
    max_words_for_dur = int(target_seconds * 2.7)

    # Build template script for the requested duration.
    data_20 = _build_script(story, target_seconds=target_seconds)
    script_20 = data_20["script"]

    # LLM enhancement when ANTHROPIC_API_KEY is set.
    # LLM output is plain prose (no labels), so the word cap is safe to apply.
    llm_used = False
    if use_llm:
        llm_20 = _llm_script(story, target_seconds)
        if llm_20:
            script_20 = _cap_words_20s(llm_20, max_words=max_words_for_dur)
            llm_used = True
            print("[avatar_writer] using LLM-enhanced 20s script "
                  "(viral blueprint injected when available)",
                  file=sys.stderr)

    # Viral hook hardener — applies to the deterministic template path so
    # even offline users get an opener shaped by the blueprint. We skip when
    # LLM already ran (the blueprint was injected upstream into the prompt).
    if not llm_used:
        try:
            from src.agents.viral_transformer import transform_script
            bp = _load_user_blueprint(story)
            if bp and bp.get("sample_size"):
                ticker = _ticker_phrase(story.get("tickers") or []) or "Bitcoin"
                # transform_script preserves all but the first sentence —
                # the rest of the labels/structure stays intact.
                # Operate only on the [HOOK] line if labels are present.
                if "[HOOK]" in script_20:
                    new_hook_line = transform_script(
                        "PLACEHOLDER.", bp, ticker=ticker
                    )
                    # Replace just the first sentence after [HOOK]
                    script_20 = re.sub(
                        r"(\[HOOK\]\s*)([^\n]+)",
                        lambda m: m.group(1) + new_hook_line,
                        script_20, count=1,
                    )
                else:
                    script_20 = transform_script(script_20, bp, ticker=ticker)
                print("[avatar_writer] viral hook applied from blueprint "
                      f"(sample={bp.get('sample_size')})", file=sys.stderr)
        except Exception as exc:
            print(f"[avatar_writer] viral hook skipped: {exc}", file=sys.stderr)

    # Teleprompter (clean, sentence-per-line)
    tele_lines = data_20["teleprompter"]
    teleprompter = "\n".join(f"{i+1:02d}. {line}" for i, line in enumerate(tele_lines))

    # ElevenLabs SSML
    ssml = _build_ssml(data_20)

    # SRT captions — timed to the requested duration
    srt_20 = _build_srt(tele_lines, target_seconds)

    # Platform cuts
    cuts = _platform_cuts(data_20)

    # HeyGen / D-ID instructions
    heygen = _heygen_instructions(data_20, script_20)

    # Stamp the resolved duration onto the package so downstream code can
    # check whether to regenerate (e.g. user changed Settings → Duration).
    (out_dir / ".target_seconds").write_text(str(target_seconds), encoding="utf-8")

    # Write all files. Legacy 30s/60s aliases point at the same content so
    # older code paths (video_builder, slot.html) keep working.
    files = {
        "script_20s.txt":         script_20,
        "script_30s.txt":         script_20,   # legacy alias
        "script_60s.txt":         script_20,   # legacy alias
        "teleprompter.txt":       teleprompter,
        "heygen_instructions.md": heygen,
        "elevenlabs_ssml.xml":    ssml,
        "captions_20s.srt":       srt_20,
        "captions_30s.srt":       srt_20,      # legacy alias
        "captions_60s.srt":       srt_20,      # legacy alias
        "platform_cuts.json":     json.dumps(cuts, indent=2, ensure_ascii=False),
    }
    for fname, content in files.items():
        (out_dir / fname).write_text(content + "\n", encoding="utf-8")

    word_count = len(script_20.split())
    print(f"[avatar_writer] wrote {len(files)} files -> {out_dir} "
          f"(script: {word_count} words ~{word_count/2.3:.1f}s)",
          file=sys.stderr)
    return {"files": list(files.keys()), "mood": data_20["mood"],
            "hook": data_20["hook"], "out_dir": str(out_dir),
            "word_count": word_count,
            "estimated_seconds": round(word_count / 2.3, 1)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("top2_json")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--llm", action="store_true",
                    help="enhance script with Claude API (requires ANTHROPIC_API_KEY)")
    ap.add_argument("--seconds", type=int, default=None,
                    help="target clip duration in seconds (10..60)")
    args = ap.parse_args(argv[1:])

    data    = json.loads(Path(args.top2_json).read_text(encoding="utf-8"))
    stories = data.get("stories", data) if isinstance(data, dict) else data
    if not stories:
        print("[avatar_writer] no stories", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir).resolve() if args.out_dir else Path(args.top2_json).parent / "avatar"
    result  = write_package(stories[0], out_dir, use_llm=args.llm,
                             target_seconds=args.seconds)
    print(f"[avatar_writer] done -> {result['out_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
