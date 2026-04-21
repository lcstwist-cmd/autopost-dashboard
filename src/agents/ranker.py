"""Ranker — Agent 2 of the Crypto AutoPost pipeline.

Scores the raw news from News Scout and returns the TOP N stories with
a transparent rationale.

Scoring signals (each in [0, 1], combined with weights):

1. recency          — exponential decay from now (fresher = higher)
2. cluster          — how many distinct SOURCES reported the same story
                      uses duplicate_sources from news_scout dedup when available,
                      falls back to title-token Jaccard clustering (≥ 0.55)
3. keywords         — presence of high-impact tokens in the title
                      (ETF, SEC, halving, hack, record, flip $Xk, etc.)
4. tickers          — bias toward majors (BTC/ETH = 1.0, top-20 = 0.7, other = 0.4)
5. importance       — raw_importance signal boosted by source_tier
6. engagement       — CryptoPanic votes / LunarCrush interactions
7. reddit           — Reddit upvotes + comments + upvote_ratio from hot posts

Final score = weighted sum. Ties broken by recency.

Usage:
    python ranker.py raw_news.json > top2.json
    # or pipe:
    python news_scout.py | python ranker.py > top2.json
"""
from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime, timezone
from typing import Any

from dateutil import parser as dateparser

# --- Config ----------------------------------------------------------------

WEIGHTS = {
    "recency":    0.20,   # freshness matters most
    "cluster":    0.22,   # cross-source coverage = organic importance proxy
    "keywords":   0.18,   # breaking news signal
    "tickers":    0.10,
    "importance": 0.12,   # source signal boosted by source_tier
    "engagement": 0.08,   # CryptoPanic/LunarCrush vote counts
    "reddit":     0.10,   # Reddit hot score + comments + upvote ratio
}

# High-impact keywords (lowercase). Each hit adds weight (saturates at 3).
IMPACT_KEYWORDS = {
    # Regulatory / macro
    "etf", "sec", "cftc", "fed", "cpi", "fomc", "interest rate",
    "rate cut", "rate hike", "inflation", "jobs report", "gdp",
    "blackrock", "fidelity", "grayscale", "coinbase",
    # Price action
    "all-time high", "ath", "record", "crash", "plunge", "surge",
    "rally", "bull run", "bear market", "correction", "dump", "pump",
    "breakout", "support", "resistance",
    # Halving / supply
    "halving", "halvening", "mining reward",
    # Security incidents
    "hack", "exploit", "drain", "stolen", "breach", "attack",
    "rug pull", "exit scam", "phishing", "vulnerability",
    # Adoption
    "launch", "listing", "approved", "approval", "rejected", "rejects",
    "partnership", "integration", "mainnet", "upgrade", "fork",
    "institutional", "adoption", "payment", "stablecoin",
    # Whale / market
    "whale", "accumulation", "outflow", "inflow",
    "liquidation", "liquidated", "short squeeze",
    # Legal
    "lawsuit", "settlement", "indicted", "charged", "arrested",
    "banned", "ban", "regulation", "bill", "congress",
}

TIER1_TICKERS = {"BTC", "ETH"}
TIER2_TICKERS = {
    "SOL", "XRP", "BNB", "ADA", "DOGE", "TRX", "AVAX", "DOT", "LINK",
    "MATIC", "LTC", "BCH", "ATOM", "NEAR", "APT", "ARB", "OP", "TON",
}

# Stop words for title similarity
STOP_WORDS = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "as",
    "at", "by", "from", "after", "before", "this", "that", "with", "about",
    "will", "has", "have", "are", "was", "were", "been", "but", "not", "new",
    "says", "said", "reports", "report", "news", "update", "today",
}


# --- Helpers ---------------------------------------------------------------

def _tokenize_title(title: str) -> set[str]:
    """Lowercase word tokens with stop-words removed, length ≥ 3."""
    words = re.findall(r"[A-Za-z0-9$%]+", title.lower())
    return {w for w in words if len(w) >= 3 and w not in STOP_WORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = dateparser.parse(s)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return None


# --- Scoring components ----------------------------------------------------

def score_recency(story: dict, now: datetime) -> float:
    """Exponential decay: 1.0 now, 0.7 at 3h, 0.5 at 6h, ~0.22 at 12h.
    Breaking news bonus: stories under 1h get a 20% boost."""
    dt = _parse_iso(story.get("published_at"))
    if not dt:
        return 0.3
    hours = max(0.0, (now - dt).total_seconds() / 3600.0)
    score = math.exp(-hours / 8.0)
    if hours < 1.0:
        score = min(1.0, score * 1.2)  # breaking news bonus
    return score


def score_keywords(story: dict) -> float:
    title_lc = story["title"].lower()
    summary_lc = (story.get("summary") or "").lower()
    haystack = f"{title_lc} {summary_lc}"
    hits = sum(1 for kw in IMPACT_KEYWORDS if kw in haystack)
    # saturate at 3 hits
    return min(1.0, hits / 3.0)


def score_tickers(story: dict) -> float:
    tickers = set(story.get("tickers", []))
    if tickers & TIER1_TICKERS:
        return 1.0
    if tickers & TIER2_TICKERS:
        return 0.7
    if tickers:
        return 0.5
    return 0.3


def score_importance(story: dict) -> float:
    imp = story.get("raw_importance")
    base = float(imp) if imp is not None else 0.5
    # Boost by source tier (tier 1 = established outlets, tier 3 = aggregators)
    tier = story.get("source_tier", 3)
    tier_boost = {1: 0.20, 2: 0.08, 3: 0.0}.get(int(tier), 0.0)
    return min(1.0, base + tier_boost)


def score_engagement(story: dict) -> float:
    """Explicit engagement signals: CryptoPanic votes, LunarCrush interactions.

    Normalised to [0, 1].  Returns 0.5 (neutral) when no data is available.
    """
    # CryptoPanic vote fields stored under 'votes' key by news_scout
    votes = story.get("votes") or {}
    positive  = int(votes.get("positive",  0))
    important = int(votes.get("important", 0))
    liked     = int(votes.get("liked",     0))
    total_votes = positive + important * 2 + liked
    if total_votes:
        return min(1.0, total_votes / 30.0)

    # LunarCrush interactions_24h stored in 'signals'
    for sig in story.get("signals") or []:
        interactions = sig.get("interactions_24h")
        if interactions:
            return min(1.0, float(interactions) / 500_000.0)

    # X/Twitter likes + retweets (from API v2 public_metrics)
    x_likes    = story.get("x_likes") or 0
    x_retweets = story.get("x_retweets") or 0
    if x_likes or x_retweets:
        # 10k likes = 1.0; retweets worth 3× likes (wider reach)
        return min(1.0, (x_likes + x_retweets * 3) / 10_000.0)

    return 0.5  # no engagement data — neutral


def score_reddit(story: dict) -> float:
    """Reddit hot score: upvotes + comments + upvote ratio.

    Uses fields added by the improved news_scout:
      reddit_score   — raw upvote count (int)
      reddit_comments — comment count (int)
      reddit_ratio   — upvote ratio 0.0-1.0 (float)

    Returns 0.0 if no Reddit data (not 0.5) so stories without Reddit data
    don't get artificially boosted — the 0.10 weight is pure upside.
    """
    score = story.get("reddit_score") or 0
    comments = story.get("reddit_comments") or 0
    ratio = story.get("reddit_ratio") or 0.0

    if not score and not comments:
        return 0.0

    score_norm    = min(1.0, score / 8000.0)
    comments_norm = min(1.0, comments / 400.0)
    # Ratio from 0.5 to 1.0 maps to 0.0-1.0 (anything below 50% is noise)
    ratio_norm    = max(0.0, (float(ratio) - 0.5) * 2.0)

    return 0.55 * score_norm + 0.30 * comments_norm + 0.15 * ratio_norm


# --- Clustering for cluster score ------------------------------------------

def cluster_stories(stories: list[dict], threshold: float = 0.55) -> list[list[int]]:
    """Greedy clustering by title Jaccard similarity. Returns clusters of indices."""
    tokens = [_tokenize_title(s["title"]) for s in stories]
    clusters: list[list[int]] = []
    assigned = [False] * len(stories)

    for i, toks_i in enumerate(tokens):
        if assigned[i]:
            continue
        cluster = [i]
        assigned[i] = True
        for j in range(i + 1, len(stories)):
            if assigned[j]:
                continue
            if _jaccard(toks_i, tokens[j]) >= threshold:
                cluster.append(j)
                assigned[j] = True
        clusters.append(cluster)
    return clusters


def score_cluster(cluster_size: int, distinct_sources: int) -> float:
    """Bigger cluster & more distinct sources = higher score."""
    size_score = min(1.0, (cluster_size - 1) / 4.0)     # 1 item=0, 5+ items=1
    src_score = min(1.0, (distinct_sources - 1) / 2.0)  # 1 src=0, 3+ srcs=1
    return 0.4 * size_score + 0.6 * src_score


def _cluster_sources_from_story(story: dict) -> int:
    """Return distinct source count from news_scout dedup field when available."""
    dup = story.get("duplicate_sources")
    if dup and isinstance(dup, list):
        # duplicate_sources lists the *other* sources; add 1 for the story itself
        return len(set(dup)) + 1
    return 1


# --- Rank ------------------------------------------------------------------

def rank(stories: list[dict], top_n: int = 2) -> list[dict]:
    """Score + rank. Returns enriched copies of top_n stories."""
    if not stories:
        return []

    now = datetime.now(timezone.utc)
    clusters = cluster_stories(stories)

    # Build lookup: story_index -> (cluster_size, distinct_sources)
    cluster_info: dict[int, tuple[int, int]] = {}
    for cl in clusters:
        size = len(cl)
        sources = {stories[k]["source"] for k in cl}
        info = (size, len(sources))
        for idx in cl:
            cluster_info[idx] = info

    # Score every story
    scored: list[dict] = []
    for i, s in enumerate(stories):
        cl_size, cl_distinct = cluster_info[i]
        # Prefer duplicate_sources from news_scout dedup (more accurate than
        # title Jaccard clustering done here post-hoc on already-deduped list)
        scout_distinct = _cluster_sources_from_story(s)
        if scout_distinct > 1:
            cl_distinct = max(cl_distinct, scout_distinct)
        components = {
            "recency":    score_recency(s, now),
            "cluster":    score_cluster(cl_size, cl_distinct),
            "keywords":   score_keywords(s),
            "tickers":    score_tickers(s),
            "importance": score_importance(s),
            "engagement": score_engagement(s),
            "reddit":     score_reddit(s),
        }
        total = sum(WEIGHTS[k] * v for k, v in components.items())
        enriched = dict(s)
        enriched["_scores"] = {k: round(v, 3) for k, v in components.items()}
        enriched["_score_total"] = round(total, 3)
        enriched["_cluster_size"] = cl_size
        enriched["_cluster_sources"] = cl_distinct
        scored.append(enriched)

    # Sort by score desc, then recency desc
    scored.sort(
        key=lambda x: (x["_score_total"], x.get("published_at") or ""),
        reverse=True,
    )

    # Pick top_n but avoid same cluster twice (diversity)
    picked: list[dict] = []
    used_clusters: set[int] = set()
    # map story id -> cluster index
    story_to_cluster: dict[str, int] = {}
    for ci, cl in enumerate(clusters):
        for idx in cl:
            story_to_cluster[stories[idx]["id"]] = ci

    for s in scored:
        ci = story_to_cluster.get(s["id"])
        if ci in used_clusters:
            continue
        picked.append(_add_rationale(s))
        if ci is not None:
            used_clusters.add(ci)
        if len(picked) >= top_n:
            break

    # Fallback: if filtering removed too many, top up without diversity
    if len(picked) < top_n:
        for s in scored:
            if s not in picked:
                picked.append(_add_rationale(s))
                if len(picked) >= top_n:
                    break
    return picked


def _add_rationale(story: dict) -> dict:
    s = dict(story)
    parts = []
    scores = s["_scores"]
    if scores["cluster"] >= 0.5:
        parts.append(f"reported by {s['_cluster_sources']} sources")
    if scores["keywords"] >= 0.5:
        parts.append("high-impact keywords in title")
    if scores["tickers"] >= 0.8:
        parts.append("involves BTC/ETH")
    if scores["importance"] >= 0.6:
        tier = s.get("source_tier", 3)
        x_acct = s.get("x_account")
        if x_acct:
            parts.append(f"posted by @{x_acct} on X")
        else:
            label = {1: "tier-1 outlet", 2: "established outlet"}.get(int(tier), "flagged important")
            parts.append(label)
    if scores["recency"] >= 0.8:
        parts.append("very recent")
    if scores["reddit"] >= 0.4:
        rs = s.get("reddit_score", 0)
        parts.append(f"trending on Reddit ({rs:,} upvotes)")
    s["_rationale"] = "; ".join(parts) or "top aggregate score"
    return s


# --- CLI -------------------------------------------------------------------

def _load_input(argv: list[str]) -> list[dict]:
    if len(argv) > 1 and argv[1] not in ("-",):
        with open(argv[1], "r", encoding="utf-8") as f:
            return json.load(f)
    return json.load(sys.stdin)


def main(argv: list[str]) -> int:
    stories = _load_input(argv)
    top_n = int(argv[2]) if len(argv) > 2 else 2
    picked = rank(stories, top_n=top_n)
    out: dict[str, Any] = {
        "picked_at": datetime.now(timezone.utc).isoformat(),
        "total_candidates": len(stories),
        "top_n": top_n,
        "stories": picked,
    }
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
