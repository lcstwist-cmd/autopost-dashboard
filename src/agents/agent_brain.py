"""Agent Brain — autonomous continuous learning for every pipeline agent.

Agents run two kinds of learning:
  • LOCAL  — analyse past queue outputs (every cycle)
  • WEB    — fetch live RSS feeds / public APIs for fresh signals (every cycle)

Every 10 minutes a full learning cycle fires automatically.
Learnings accumulate as XP that levels up skill bars in the backoffice.
Each agent also writes "learned parameters" to disk — files the pipeline
reads on the next run so quality actually improves over time.

Storage
───────
  analytics/agent_brains/<name>/brain.json         XP + learnings
  analytics/agent_brains/<name>/learned_params.json pipeline-quality params
  analytics/agent_brains/_global_summary.json       dashboard snapshot
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT  = Path(__file__).resolve().parent.parent.parent
_BRAINS_DIR = _REPO_ROOT / "analytics" / "agent_brains"
_QUEUE_ROOT = _REPO_ROOT / "queue"

# ── RSS feeds each agent monitors ──────────────────────────────────────────
_CRYPTO_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://bitcoinmagazine.com/.rss/full/",
    "https://cryptopanic.com/news/rss/",
    "https://www.newsbtc.com/feed/",
    "https://ambcrypto.com/feed/",
]

_GENERAL_FEEDS = [
    "https://feeds.feedburner.com/entrepreneur/latest",
    "https://hbr.org/feed",
    "https://techcrunch.com/feed/",
    "https://feeds.reuters.com/reuters/businessNews",
    "https://www.socialmediaexaminer.com/feed/",
    "https://contentmarketinginstitute.com/feed/",
    "https://adweek.com/feed/",
    "https://www.marketingweek.com/feed/",
]

# ── Industry keyword map (for ad_creator + general learning) ────────────────
_INDUSTRY_KEYWORDS: dict[str, list[str]] = {
    "crypto":      ["bitcoin", "ethereum", "crypto", "blockchain", "defi", "nft", "web3", "token"],
    "real_estate": ["real estate", "property", "mortgage", "housing", "apartment", "rental"],
    "fitness":     ["workout", "fitness", "gym", "weight loss", "muscle", "diet", "wellness"],
    "ecommerce":   ["shopify", "amazon", "ecommerce", "store", "product launch", "dropship", "sales"],
    "finance":     ["stock", "investment", "trading", "portfolio", "interest rate", "fund", "etf"],
    "tech":        ["ai", "software", "startup", "saas", "automation", "machine learning", "app"],
    "food":        ["restaurant", "food", "recipe", "nutrition", "chef", "cooking", "meal prep"],
    "fashion":     ["fashion", "style", "clothing", "luxury", "designer", "brand", "wardrobe"],
    "travel":      ["travel", "hotel", "flight", "vacation", "destination", "tourism", "booking"],
    "education":   ["course", "learning", "training", "certification", "skill", "online school"],
    "healthcare":  ["health", "medical", "wellness", "therapy", "clinic", "supplement", "doctor"],
    "marketing":   ["marketing", "advertising", "campaign", "brand", "social media", "seo", "lead gen"],
}

# ── Skill catalogue ────────────────────────────────────────────────────────
AGENTS: dict[str, dict] = {
    "news_scout": {
        "icon": "🔍", "color": "#3b82f6",
        "desc": "Scans 40+ crypto news sources for breaking stories",
        "skills": {
            "source_quality":   "Identifies which sources produce top-ranked stories",
            "timing_awareness": "Detects when breaking news hits for maximum impact",
            "topic_velocity":   "Tracks which crypto topics are gaining search heat",
        },
    },
    "ranker": {
        "icon": "📊", "color": "#8b5cf6",
        "desc": "Scores and ranks stories by viral potential",
        "skills": {
            "keyword_intuition": "Knows which keywords drive the highest engagement",
            "story_selection":   "Consistently picks the highest-impact story",
            "trend_prediction":  "Predicts which headlines will dominate next 24 h",
        },
    },
    "copywriter": {
        "icon": "✍️", "color": "#ec4899",
        "desc": "Writes social posts and ad copy that stop the scroll for any brand",
        "skills": {
            "hook_mastery":         "Crafts irresistible opening sentences",
            "hashtag_intelligence": "Uses hashtags proven to expand reach",
            "tone_calibration":     "Matches tone to the audience's current emotion",
            "cta_effectiveness":    "Writes CTAs that actually convert",
            "brand_voice_match":    "Adapts copy style to any company's brand guidelines",
            "ad_formula_mastery":   "Applies AIDA, PAS, BAB and other proven ad frameworks",
        },
    },
    "reel_writer": {
        "icon": "🎬", "color": "#f59e0b",
        "desc": "Produces 20-second video scripts with viral hooks for any product or service",
        "skills": {
            "script_structure": "Masters the hook → story → CTA formula",
            "pacing_control":   "Optimal word density for 20-second clips",
            "emotion_arc":      "Creates a full emotional journey in 20 seconds",
            "product_showcase": "Presents any product or service compellingly in video format",
        },
    },
    "avatar_writer": {
        "icon": "🗣️", "color": "#10b981",
        "desc": "Writes narration that sounds natural on camera",
        "skills": {
            "narration_style": "Conversational delivery that feels live",
            "word_economy":    "Maximum impact with minimum words",
            "voice_sync":      "Scripts that lip-sync perfectly at 2.3 wps",
        },
    },
    "viral_analyzer": {
        "icon": "🔥", "color": "#ef4444",
        "desc": "Studies viral content across industries to extract repeatable formulas",
        "skills": {
            "pattern_detection":    "Spots viral formulas before they explode",
            "platform_knowledge":   "Deep understanding of each platform's algorithm",
            "trend_surfing":        "Catches emerging trends at exactly the right moment",
            "industry_benchmarking":"Learns what goes viral in any specific niche or industry",
        },
    },
    "image_gen": {
        "icon": "🎨", "color": "#06b6d4",
        "desc": "Generates scroll-stopping visual content and ad creatives for any brand",
        "skills": {
            "visual_impact":       "Creates images that halt the scroll instantly",
            "brand_consistency":   "Maintains cohesive visual identity per company",
            "prompt_craft":        "Writes prompts that reliably produce viral visuals",
            "product_visual_design":"Showcases products attractively for any industry",
        },
    },
    "engagement_optimizer": {
        "icon": "🎯", "color": "#f97316",
        "desc": "Learns when and how to post for maximum engagement",
        "skills": {
            "timing_mastery":       "Discovers the exact time windows that drive peak reach",
            "format_intelligence":  "Identifies which post formats outperform consistently",
            "platform_intelligence": "Understands which platforms amplify different content",
        },
    },
    "trend_predictor": {
        "icon": "📈", "color": "#22d3ee",
        "desc": "Predicts which coins and topics will trend in the next 2–4 hours",
        "skills": {
            "market_reading":     "Interprets Fear & Greed Index and price momentum together",
            "prediction_accuracy": "Checks if past predictions matched actual top stories",
            "velocity_tracking":   "Spots coins accelerating before the crowd notices",
        },
    },
    "content_personalizer": {
        "icon": "🧬", "color": "#a78bfa",
        "desc": "Customizes content tone, niche, and style per company and audience for higher relevance",
        "skills": {
            "audience_profiling":    "Builds a precise model of what each user's audience wants",
            "niche_detection":       "Identifies which content niches drive engagement for each user",
            "tone_calibration":      "Selects the exact tone that resonates with current market mood",
            "buyer_persona_building":"Creates detailed buyer personas for any company's target market",
        },
    },
    "fact_checker": {
        "icon": "🔎", "color": "#4ade80",
        "desc": "Validates price and percentage claims before publishing",
        "skills": {
            "accuracy_radar":        "Gets sharper at detecting wrong numbers in posts",
            "source_trust":          "Learns which sources publish accurate vs inflated prices",
            "volatility_awareness":  "Knows which coins need extra scrutiny during high volatility",
        },
    },
    "ab_tester": {
        "icon": "⚗️", "color": "#fb923c",
        "desc": "Tests post formats A vs B and discovers which drives more engagement",
        "skills": {
            "variant_analysis":  "Compares A vs B engagement scores with statistical rigor",
            "hook_optimization": "Finds which hook style (question/number/urgency) gets most clicks",
            "format_learning":   "Identifies which post features (emojis, CTAs, cashtags) win",
        },
    },
    "ad_creator": {
        "icon": "📢", "color": "#f43f5e",
        "desc": "Creates high-converting ads for any company, product or service in any industry",
        "skills": {
            "brand_intelligence":       "Learns any company's brand voice, tone, and visual identity",
            "ad_copywriting":           "Masters AIDA, PAS, BAB and proven ad copy formulas",
            "audience_targeting":       "Identifies and segments ideal customer avatars for any niche",
            "campaign_strategy":        "Plans multi-platform campaigns with budget-optimal sequencing",
            "conversion_optimization":  "Discovers which hooks, visuals and CTAs drive actual sales",
        },
    },
    "agent_supervisor": {
        "icon": "🛡️", "color": "#6366f1",
        "desc": "Watches over all other agents, detects failures, auto-heals and alerts",
        "skills": {
            "agent_health_monitoring": "Tracks health scores of every agent across hundreds of check cycles",
            "failure_prediction":      "Predicts which agents are at risk before they actually fail",
            "auto_recovery":           "Learns which recovery actions resolve each failure type fastest",
            "performance_benchmarking":"Builds baseline norms for every agent — detects anomalies instantly",
            "cross_agent_correlation": "Discovers how one agent's failure cascades through the pipeline",
        },
    },
    "web_learner": {
        "icon": "🌐", "color": "#0ea5e9",
        "desc": "Searches the internet daily to keep every agent brain updated with the latest techniques",
        "skills": {
            "web_research":      "Finds the most relevant articles and insights for each agent's domain",
            "insight_extraction":"Distills raw search results into actionable learning entries",
            "knowledge_routing": "Routes each web discovery to the right agent brain skill",
            "trend_awareness":   "Tracks which topics, tactics and tools are gaining traction online",
            "ai_summarisation":  "Uses Claude Haiku to compress long articles into single-sentence insights",
        },
    },
    "competitor_tracker": {
        "icon": "🕵️", "color": "#f59e0b",
        "desc": "Monitors competitor accounts on X, RSS feeds and YouTube to extract content patterns and engagement benchmarks",
        "skills": {
            "data_collection":      "Fetches fresh posts and engagement data from every tracked competitor account",
            "pattern_recognition":  "Identifies which content formats, hashtags and topics drive competitor engagement",
            "frequency_analysis":   "Learns optimal posting cadence by benchmarking against top competitors",
            "hashtag_intelligence": "Tracks competitor hashtag strategies and discovers high-performing tags",
            "engagement_benchmark": "Builds a live baseline of competitor average likes, shares and reach",
        },
    },
}

LEVEL_XP = [
    # Lv 0-10  (Bronze — Recruit → Analyst)
         0,    100,    250,    500,    900,  1_400,  2_100,  3_000,  4_200,  5_700,
    # Lv 10-20 (Iron — Specialist → Director)
     7_500,  9_700, 12_300, 15_400, 19_100, 23_500, 28_600, 34_500, 41_300, 49_100,
    # Lv 20-30 (Silver — Executive → Premier)
    58_100, 68_500, 80_500, 94_300, 110_200, 128_500, 149_000, 172_000, 197_800, 226_700,
    # Lv 30-40 (Gold — Virtuoso → Grand Master)
   259_100, 295_400, 336_100, 381_700, 432_800, 490_000, 554_100, 625_900, 706_300, 796_300,
    # Lv 40-50 (Platinum — Mythic → Oracle)
   897_100, 1_008_000, 1_130_000, 1_264_200, 1_411_800,
 1_574_200, 1_752_800, 1_949_300, 2_165_500, 2_403_300,
    # Lv 50-60 (Diamond — Seer → Quantum)
 2_664_900, 2_952_700, 3_269_300, 3_617_600, 4_000_700,
 4_422_100, 4_885_600, 5_395_500, 5_956_400, 6_573_400,
    # Lv 60-70 (Legendary — Neural → Ultimate)
 7_252_100,  7_985_100,  8_776_700,  9_631_600, 10_554_900,
11_552_100, 12_629_100, 13_792_300, 15_048_600, 16_405_400,
    # Lv 70-80 (Mythic — Absolute → Titan Elite)
17_870_700, 19_453_200, 21_162_300, 23_008_100, 25_001_600,
27_154_600, 29_479_800, 31_991_000, 34_703_100, 37_632_200,
    # Lv 80-90 (Cosmic — Deus → Celestial God)
40_795_600, 44_148_800, 47_703_200, 51_470_900, 55_464_700,
59_698_100, 64_185_500, 68_942_100, 73_984_100, 79_328_600,
    # Lv 90-100 (OmniAgent tier)
 84_993_800,  90_998_900,  97_364_300, 104_111_600, 111_263_700,
118_844_900, 126_881_000, 135_399_300, 144_428_700, 153_999_900,
    # Lv 100 — OmniAgent ⚡
164_145_400,
]  # 101 thresholds → max displayable level = 100

def _xp_to_level(xp: int) -> int:
    for lvl in range(len(LEVEL_XP) - 1, -1, -1):
        if xp >= LEVEL_XP[lvl]:
            return max(1, lvl)
    return 1

def _level_pct(xp: int) -> float:
    lvl = _xp_to_level(xp)
    if lvl >= len(LEVEL_XP) - 1:
        return 100.0
    lo, hi = LEVEL_XP[lvl], LEVEL_XP[lvl + 1]
    return round(max(0.0, (xp - lo) / max(1, hi - lo) * 100), 1)

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _now_display() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ══════════════════════════════════════════════════════════════════════════════
# AgentBrain — one instance per agent
# ══════════════════════════════════════════════════════════════════════════════
class AgentBrain:
    def __init__(self, name: str) -> None:
        self.name  = name
        self.path  = _BRAINS_DIR / name / "brain.json"
        self.ppath = _BRAINS_DIR / name / "learned_params.json"
        self._data = self._load()

    def _load(self) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                meta = AGENTS.get(self.name, {})
                # Migrate: add newly defined skills that don't exist yet
                for sk in meta.get("skills", {}):
                    if sk not in data.get("skills", {}):
                        data.setdefault("skills", {})[sk] = {"xp": 0, "level": 1, "pct": 0.0}
                # Always refresh desc/icon/color from catalogue
                for field in ("icon", "color", "desc"):
                    if meta.get(field):
                        data[field] = meta[field]
                data["skill_descs"] = meta.get("skills", {})
                return data
            except Exception:
                pass
        meta = AGENTS.get(self.name, {})
        return {
            "name": self.name, "icon": meta.get("icon", "🤖"),
            "color": meta.get("color", "#6b7280"), "desc": meta.get("desc", ""),
            "total_xp": 0, "total_runs": 0, "total_learnings": 0,
            "cycles_completed": 0, "last_active": None, "status": "idle",
            "skills": {k: {"xp": 0, "level": 1, "pct": 0.0}
                       for k in meta.get("skills", {})},
            "skill_descs": meta.get("skills", {}),
            "learnings": [], "weights": {},
        }

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")

    def save_params(self, params: dict) -> None:
        """Write learned parameters that the pipeline reads on next run."""
        existing: dict = {}
        if self.ppath.exists():
            try:
                existing = json.loads(self.ppath.read_text(encoding="utf-8"))
            except Exception:
                pass
        existing.update(params)
        existing["_updated"] = _now_iso()
        self.ppath.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")

    def load_params(self) -> dict:
        if self.ppath.exists():
            try:
                return json.loads(self.ppath.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    @property
    def data(self) -> dict:
        d = dict(self._data)
        d["level"]     = _xp_to_level(d["total_xp"])
        d["level_pct"] = _level_pct(d["total_xp"])
        return d

    def add_xp(self, amount: int, skill: str | None = None) -> None:
        self._data["total_xp"] += amount
        if skill and skill in self._data["skills"]:
            sk = self._data["skills"][skill]
            sk["xp"]   += amount
            sk["level"] = _xp_to_level(sk["xp"])
            sk["pct"]   = _level_pct(sk["xp"])

    def record_learning(self, text: str, skill: str | None = None, xp: int = 15) -> None:
        entry = {"ts": _now_iso(), "time": _now_display(),
                 "skill": skill or "general", "text": text[:220]}
        self._data["learnings"].insert(0, entry)
        self._data["learnings"] = self._data["learnings"][:40]
        self._data["total_learnings"] += 1
        self._data["last_active"] = _now_iso()
        self.add_xp(xp, skill)

    def set_weight(self, key: str, value: Any) -> None:
        self._data["weights"][key] = value

    def get_weight(self, key: str, default: Any = None) -> Any:
        return self._data["weights"].get(key, default)

    def mark_run(self) -> None:
        self._data["total_runs"] += 1
        self._data["last_active"] = _now_iso()
        self._data["status"] = "idle"
        self.add_xp(25)

    def set_status(self, status: str) -> None:
        self._data["status"] = status
        self._data["last_active"] = _now_iso()


# ══════════════════════════════════════════════════════════════════════════════
# Web helpers
# ══════════════════════════════════════════════════════════════════════════════
def _fetch_rss(url: str, timeout: int = 8) -> list[dict]:
    """Fetch an RSS feed and return a list of {title, link, published} dicts."""
    try:
        import feedparser
        feed = feedparser.parse(url)
        items = []
        for e in (feed.entries or [])[:30]:
            items.append({
                "title": getattr(e, "title", ""),
                "link":  getattr(e, "link", ""),
                "published": getattr(e, "published", ""),
            })
        return items
    except Exception:
        return []


def _fetch_all_feeds() -> list[dict]:
    """Fetch crypto + general business RSS feeds in parallel threads."""
    results: list[dict] = []
    lock = threading.Lock()

    def _worker(url: str) -> None:
        items = _fetch_rss(url)
        with lock:
            results.extend(items)

    all_feeds = _CRYPTO_FEEDS + _GENERAL_FEEDS
    threads = [threading.Thread(target=_worker, args=(u,), daemon=True)
               for u in all_feeds]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    return results


_STOP_WORDS = {
    "that", "this", "with", "from", "have", "will", "been", "they",
    "their", "says", "said", "what", "when", "here", "just", "more",
    "about", "after", "into", "over", "than", "then", "also", "could",
    "would", "should", "which", "there", "where", "your", "news",
}

# Known emotional triggers that drive engagement in crypto headlines
_TRIGGERS = {
    "bullish": 3, "bearish": 3, "crash": 4, "surge": 4, "soar": 4,
    "plunge": 4, "explode": 4, "pump": 3, "dump": 3, "rally": 3,
    "ath": 5, "record": 3, "ban": 4, "approval": 4, "approved": 5,
    "rejected": 4, "warning": 3, "urgent": 4, "breaking": 5,
    "alert": 3, "massive": 3, "huge": 3, "shocking": 4, "first": 3,
    "billion": 4, "trillion": 5, "million": 3, "whale": 4,
    "sec": 4, "etf": 5, "regulation": 3, "defi": 3, "nft": 3,
}

def _score_headline(title: str) -> int:
    """Score a headline for viral potential (higher = more viral)."""
    score = 0
    low = title.lower()
    for word, pts in _TRIGGERS.items():
        if word in low:
            score += pts
    if "?" in title:
        score += 2
    if any(c.isdigit() for c in title):
        score += 2
    return score


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _collect_slot_dirs(limit: int = 40) -> list[Path]:
    dirs: list[Path] = []
    if not _QUEUE_ROOT.exists():
        return dirs
    for child in _QUEUE_ROOT.iterdir():
        if child.is_dir():
            for sub in child.iterdir():
                if sub.is_dir():
                    dirs.append(sub)
            if re.search(r"\d{4}-\d{2}-\d{2}", child.name):
                dirs.append(child)
    dirs.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return dirs[:limit]


# ══════════════════════════════════════════════════════════════════════════════
# Per-agent learning  (LOCAL + WEB)
# ══════════════════════════════════════════════════════════════════════════════

def _learn_news_scout(brain: AgentBrain, slots: list[Path],
                      live_feed: list[dict]) -> None:
    # ── LOCAL: source win-rate from past runs ──────────────────────────────
    source_wins:  Counter = Counter()
    source_total: Counter = Counter()
    topic_hits:   Counter = Counter()

    for slot in slots:
        raw = _read_json(slot / "raw_news.json")
        top = _read_json(slot / "top2.json")
        if not raw or not top:
            continue
        raw_list = raw if isinstance(raw, list) else []
        top_list = top if isinstance(top, list) else top.get("stories", [])
        top_urls = {s.get("url", "") for s in top_list if isinstance(s, dict)}
        top_txt  = " ".join(s.get("title", "") for s in top_list
                            if isinstance(s, dict)).lower()

        for story in raw_list:
            if not isinstance(story, dict):
                continue
            src = str(story.get("source", story.get("feed", "?"))).split("/")[0].replace("www.", "")[:30]
            source_total[src] += 1
            if story.get("url", "") in top_urls:
                source_wins[src] += 1

        for word in re.findall(
                r"\b[A-Z]{2,}\b|\b(?:bitcoin|ethereum|solana|xrp|defi|nft|etf|sec|fed)\b",
                top_txt):
            topic_hits[word.upper()] += 1

    best = sorted([(s, source_wins[s], source_total[s])
                   for s in source_wins if source_total[s] >= 2],
                  key=lambda x: x[1] / max(1, x[2]), reverse=True)[:5]
    if best:
        summary = ", ".join(f"{s}({w}/{t})" for s, w, t in best)
        brain.record_learning(f"Source win-rates: {summary}", skill="source_quality", xp=20)
        brain.set_weight("top_sources", [s for s, _, _ in best])
        brain.save_params({"source_weights": {s: round(w/max(1,t), 3)
                                               for s, w, t in best}})

    hot = [w for w, _ in topic_hits.most_common(8)]
    if hot:
        brain.record_learning(f"Hot keywords in past top stories: {', '.join(hot[:6])}",
                              skill="topic_velocity", xp=15)

    # ── WEB: scan live feeds for velocity ─────────────────────────────────
    if live_feed:
        scored = sorted([(item["title"], _score_headline(item["title"]))
                         for item in live_feed if item.get("title")],
                        key=lambda x: x[1], reverse=True)[:5]
        if scored:
            titles_str = " | ".join(t[:50] for t, _ in scored[:3])
            brain.record_learning(f"Top viral headlines NOW: {titles_str}",
                                  skill="timing_awareness", xp=25)

        # Topic velocity from live feed
        live_topics: Counter = Counter()
        for item in live_feed:
            title = item.get("title", "").lower()
            for kw in re.findall(
                    r"\b(?:bitcoin|btc|ethereum|eth|solana|sol|xrp|ripple|"
                    r"ltc|litecoin|defi|nft|etf|sec|fed|blackrock|coinbase|"
                    r"binance|rate|halving|miner|approval|crash|surge)\b", title):
                live_topics[kw] += 1

        hot_live = [w for w, _ in live_topics.most_common(6)]
        if hot_live:
            brain.record_learning(f"Live trending topics: {', '.join(hot_live)}",
                                  skill="topic_velocity", xp=20)
            brain.set_weight("live_hot_topics", hot_live)
            brain.save_params({"live_trending": hot_live,
                               "live_updated": _now_iso()})


def _learn_ranker(brain: AgentBrain, slots: list[Path],
                  live_feed: list[dict]) -> None:
    keyword_freq: Counter = Counter()
    trigger_freq: Counter = Counter()
    pick_positions: list[int] = []

    for slot in slots:
        raw = _read_json(slot / "raw_news.json")
        top = _read_json(slot / "top2.json")
        if not raw or not top:
            continue
        raw_list = raw if isinstance(raw, list) else []
        top_list = top if isinstance(top, list) else top.get("stories", [])

        if top_list and raw_list:
            top_url = (top_list[0] if isinstance(top_list[0], dict) else {}).get("url", "")
            for i, s in enumerate(raw_list):
                if isinstance(s, dict) and s.get("url") == top_url:
                    pick_positions.append(i + 1)
                    break

        for story in top_list:
            if not isinstance(story, dict):
                continue
            title = story.get("title", story.get("headline", "")).lower()
            for kw in re.findall(r"\b\w{4,}\b", title):
                if kw not in _STOP_WORDS:
                    keyword_freq[kw] += 1
            for trig in _TRIGGERS:
                if trig in title:
                    trigger_freq[trig] += 1

    top_kw = [w for w, _ in keyword_freq.most_common(10)]
    if top_kw:
        brain.record_learning(f"Top keywords in #1 stories: {', '.join(top_kw[:6])}",
                              skill="keyword_intuition", xp=20)
        # Build keyword boost map (normalized score 1.0–3.0)
        max_c = max(keyword_freq.values()) if keyword_freq else 1
        kw_boosts = {w: round(1.0 + 2.0 * keyword_freq[w] / max_c, 2)
                     for w in top_kw}
        brain.save_params({"keyword_boosts": kw_boosts})

    top_trigs = [t for t, _ in trigger_freq.most_common(5)]
    if top_trigs:
        brain.record_learning(f"Most effective trigger words: {', '.join(top_trigs)}",
                              skill="trend_prediction", xp=20)
        brain.save_params({"trigger_boosts": {t: trigger_freq[t] for t in top_trigs}})

    if pick_positions:
        avg = sum(pick_positions) / len(pick_positions)
        brain.record_learning(
            f"Best stories average at feed position {avg:.0f} (look deeper!)",
            skill="story_selection", xp=15)

    # WEB: score live headlines to update trigger weights
    if live_feed:
        live_scored = Counter()
        for item in live_feed:
            title = item.get("title", "").lower()
            for trig in _TRIGGERS:
                if trig in title:
                    live_scored[trig] += _TRIGGERS[trig]
        live_top = [t for t, _ in live_scored.most_common(5)]
        if live_top:
            brain.record_learning(
                f"Live high-score triggers: {', '.join(live_top)}",
                skill="trend_prediction", xp=20)
            brain.save_params({"live_trigger_scores": dict(live_scored.most_common(8))})


def _learn_copywriter(brain: AgentBrain, slots: list[Path],
                      live_feed: list[dict]) -> None:
    lengths: list[int] = []
    hashtag_freq: Counter = Counter()
    hook_patterns: Counter = Counter()
    cta_patterns: Counter = Counter()

    for slot in slots:
        post = slot / "post_x.txt"
        if not post.exists():
            continue
        text = post.read_text(encoding="utf-8", errors="replace").strip()
        words = text.split()
        lengths.append(len(words))

        for tag in re.findall(r"#\w+", text):
            hashtag_freq[tag.lower()] += 1

        # Hook: first 6 words
        if len(words) >= 6:
            hook_start = " ".join(words[:3]).lower().rstrip(".,!:")
            hook_patterns[hook_start] += 1

        # CTA: last sentence
        sentences = [s.strip() for s in text.replace("\n", " ").split(".") if s.strip()]
        if sentences:
            last = sentences[-1].lower()
            for cta in ("follow", "like", "share", "comment", "subscribe",
                        "click link", "check out", "watch", "don't miss"):
                if cta in last:
                    cta_patterns[cta] += 1

    if lengths:
        avg = sum(lengths) / len(lengths)
        best_len = sorted(lengths)[len(lengths) // 2]  # median
        brain.record_learning(
            f"Optimal X post: {best_len} words (median), avg {avg:.0f} words "
            f"across {len(lengths)} posts",
            skill="hook_mastery", xp=15)
        brain.save_params({"optimal_post_length": best_len,
                           "avg_post_length": round(avg)})

    top_tags = [t for t, _ in hashtag_freq.most_common(10)]
    if top_tags:
        brain.record_learning(f"Best hashtags: {' '.join(top_tags[:6])}",
                              skill="hashtag_intelligence", xp=20)
        brain.set_weight("preferred_hashtags", top_tags)
        brain.save_params({"top_hashtags": top_tags[:8]})

    top_cta = [c for c, _ in cta_patterns.most_common(3)]
    if top_cta:
        brain.record_learning(f"Most used CTAs: {', '.join(top_cta)}",
                              skill="cta_effectiveness", xp=15)
        brain.save_params({"preferred_ctas": top_cta})

    # WEB: learn tone from live headlines (emotional temperature of the market)
    if live_feed:
        bullish_cnt = bearish_cnt = neutral_cnt = 0
        for item in live_feed:
            t = item.get("title", "").lower()
            if any(w in t for w in ("surge","soar","rally","ath","record","gain","bull","up")):
                bullish_cnt += 1
            elif any(w in t for w in ("crash","plunge","fall","drop","bear","down","loss")):
                bearish_cnt += 1
            else:
                neutral_cnt += 1
        total = bullish_cnt + bearish_cnt + neutral_cnt or 1
        sentiment = "bullish" if bullish_cnt > bearish_cnt else ("bearish" if bearish_cnt > bullish_cnt else "neutral")
        brain.record_learning(
            f"Market tone NOW: {sentiment} "
            f"({bullish_cnt} bullish / {bearish_cnt} bearish / {neutral_cnt} neutral headlines)",
            skill="tone_calibration", xp=20)
        brain.save_params({"market_sentiment": sentiment,
                           "sentiment_counts": {"bullish": bullish_cnt,
                                                "bearish": bearish_cnt,
                                                "neutral": neutral_cnt},
                           "sentiment_updated": _now_iso()})


def _learn_reel_writer(brain: AgentBrain, slots: list[Path],
                       live_feed: list[dict]) -> None:
    word_counts: list[int] = []
    hook_starts: Counter = Counter()
    hook_emotions: Counter = Counter()

    _emotion_words = {
        "shocking": "shock", "breaking": "urgency", "finally": "relief",
        "warning": "fear", "massive": "scale", "first": "novelty",
        "secret": "curiosity", "why": "curiosity", "how": "education",
        "just": "urgency",
    }

    for slot in slots:
        sc = slot / "reel" / "01_script.txt"
        if not sc.exists():
            continue
        text = sc.read_text(encoding="utf-8", errors="replace")
        wc = len(text.split())
        if 20 < wc < 600:
            word_counts.append(wc)
            first = text.strip().split(".")[0].lower()
            first_word = first.split()[0].rstrip(".,!()[]") if first.split() else ""
            if first_word:
                hook_starts[first_word] += 1
            for ew, emotion in _emotion_words.items():
                if ew in first:
                    hook_emotions[emotion] += 1

    if word_counts:
        avg = sum(word_counts) / len(word_counts)
        p25 = sorted(word_counts)[len(word_counts) // 4]
        p75 = sorted(word_counts)[3 * len(word_counts) // 4]
        brain.record_learning(
            f"Script sweet spot: {p25}–{p75} words (avg {avg:.0f}) "
            f"→ {p25/2.3:.0f}–{p75/2.3:.0f}s narration",
            skill="pacing_control", xp=20)
        brain.save_params({"optimal_words_min": p25, "optimal_words_max": p75,
                           "optimal_words_avg": round(avg)})

    top_hooks = [w for w, _ in hook_starts.most_common(5)]
    if top_hooks:
        brain.record_learning(f"Best hook openers: {', '.join(top_hooks[:4])}",
                              skill="script_structure", xp=15)
        brain.save_params({"best_hook_starters": top_hooks})

    top_emotions = [e for e, _ in hook_emotions.most_common(3)]
    if top_emotions:
        brain.record_learning(
            f"Dominant hook emotions: {', '.join(top_emotions)} → highest engagement",
            skill="emotion_arc", xp=20)
        brain.save_params({"preferred_hook_emotions": top_emotions})

    # WEB: learn which live headlines have the best hook structures to imitate
    if live_feed:
        strong = [item["title"] for item in live_feed
                  if _score_headline(item.get("title", "")) >= 6][:5]
        if strong:
            brain.record_learning(
                f"Strong headline structures to imitate: "
                f"{strong[0][:80]}{'...' if len(strong[0])>80 else ''}",
                skill="script_structure", xp=20)
            brain.save_params({"reference_headlines": strong[:5],
                               "headlines_updated": _now_iso()})


def _learn_avatar_writer(brain: AgentBrain, slots: list[Path],
                         live_feed: list[dict]) -> None:
    word_counts: list[int] = []
    sentence_counts: list[int] = []
    question_rate = 0
    total = 0

    for slot in slots:
        for fname in ("script_20s.txt", "script_30s.txt"):
            sc = slot / "avatar" / fname
            if sc.exists():
                text = sc.read_text(encoding="utf-8", errors="replace")
                wc = len(text.split())
                if 10 < wc < 200:
                    word_counts.append(wc)
                    sents = len(re.findall(r"[.!?]", text))
                    sentence_counts.append(sents)
                    if "?" in text:
                        question_rate += 1
                    total += 1
                break

    if word_counts:
        avg  = sum(word_counts) / len(word_counts)
        lo   = min(word_counts)
        hi   = max(word_counts)
        dur  = avg / 2.3
        brain.record_learning(
            f"Narration stats: avg {avg:.0f} words ≈ {dur:.1f}s "
            f"(range {lo}–{hi} words, {total} scripts)",
            skill="voice_sync", xp=20)
        brain.save_params({"narration_avg_words": round(avg),
                           "narration_min_words": lo, "narration_max_words": hi,
                           "ideal_duration_s": round(dur, 1)})

    if sentence_counts:
        avg_sents = sum(sentence_counts) / len(sentence_counts)
        brain.record_learning(
            f"Ideal narration: {avg_sents:.1f} sentences for natural pacing",
            skill="narration_style", xp=15)
        brain.save_params({"ideal_sentence_count": round(avg_sents)})

    if total > 0:
        q_pct = round(question_rate / total * 100)
        brain.record_learning(
            f"Scripts with questions: {q_pct}% — "
            f"{'good engagement hook' if q_pct > 30 else 'could use more questions'}",
            skill="word_economy", xp=10)
        brain.save_params({"question_usage_pct": q_pct})


def _learn_viral_analyzer(brain: AgentBrain, slots: list[Path],
                          live_feed: list[dict]) -> None:
    all_topics: Counter = Counter()
    velocities: dict[str, int] = {}

    for i, slot in enumerate(slots[:30]):
        top = _read_json(slot / "top2.json")
        if not top:
            continue
        top_list = top if isinstance(top, list) else top.get("stories", [])
        for story in top_list:
            if not isinstance(story, dict):
                continue
            title = (story.get("title") or story.get("headline") or "").lower()
            for kw in re.findall(
                    r"\b(?:bitcoin|btc|ethereum|eth|solana|sol|xrp|ripple|ltc|"
                    r"litecoin|defi|nft|etf|sec|fed|rate|approval|ban|crash|surge|"
                    r"ath|whale|pump|dump|halving|blackrock|coinbase|binance)\b",
                    title):
                all_topics[kw] += 1
                # Recency-weighted (recent slots score higher)
                w = max(1, 30 - i)
                velocities[kw] = velocities.get(kw, 0) + w

    if velocities:
        hot = sorted(velocities.items(), key=lambda x: x[1], reverse=True)[:8]
        hot_str = ", ".join(f"{k}({v}pts)" for k, v in hot[:5])
        brain.record_learning(f"Trending topics (velocity): {hot_str}",
                              skill="trend_surfing", xp=25)
        brain.set_weight("trending_topics", [k for k, _ in hot])
        brain.save_params({"trending_topics": [k for k, _ in hot],
                           "velocity_scores": {k: v for k, v in hot}})

    # ── WEB: live trending scan ────────────────────────────────────────────
    if live_feed:
        live_topics: Counter = Counter()
        scored_items = [(item, _score_headline(item.get("title", "")))
                        for item in live_feed]
        top_items = sorted(scored_items, key=lambda x: x[1], reverse=True)[:10]

        for item, score in top_items:
            title = item.get("title", "").lower()
            for kw in re.findall(
                    r"\b(?:bitcoin|btc|ethereum|eth|solana|sol|xrp|ltc|"
                    r"defi|nft|etf|sec|blackrock|coinbase|binance|halving)\b",
                    title):
                live_topics[kw] += score

        live_hot = [w for w, _ in live_topics.most_common(6)]
        if live_hot:
            brain.record_learning(
                f"Live viral topics (score-weighted): {', '.join(live_hot)}",
                skill="trend_surfing", xp=25)
            brain.save_params({"live_viral_topics": live_hot,
                               "live_scan_ts": _now_iso()})

        # Pattern detection from top live headlines
        patterns: list[str] = []
        for item, score in top_items[:3]:
            title = item.get("title", "")
            if score >= 5:
                patterns.append(f'"{title[:60]}" (score {score})')
        if patterns:
            brain.record_learning(
                f"Live viral patterns: {' | '.join(patterns[:2])}",
                skill="pattern_detection", xp=20)
            brain.save_params({"top_viral_patterns": patterns[:5]})


def _update_avoid_style_indices(brain: AgentBrain) -> None:
    """Read style_history.json and tell future generations to avoid recently used styles."""
    style_log = _BRAINS_DIR / "image_gen" / "style_history.json"
    if not style_log.exists():
        return
    try:
        history: dict = json.loads(style_log.read_text(encoding="utf-8"))
    except Exception:
        return

    from datetime import datetime as _dt
    today_ord = _dt.now().toordinal()
    cutoff_ord = today_ord - 7  # last 7 days

    avoid_by_mood: dict[str, set[int]] = {"bull": set(), "bear": set(), "neutral": set()}
    for key, indices in history.items():
        try:
            mood, date_str = key.rsplit("_", 1)
            entry_ord = _dt.strptime(date_str, "%Y-%m-%d").toordinal()
        except ValueError:
            continue
        if entry_ord >= cutoff_ord and mood in avoid_by_mood:
            avoid_by_mood[mood].update(int(i) for i in indices)

    params: dict = {}
    for mood, indices in avoid_by_mood.items():
        params[f"avoid_style_indices_{mood}"] = sorted(indices)

    if any(v for v in avoid_by_mood.values()):
        total = sum(len(v) for v in avoid_by_mood.values())
        brain.record_learning(
            f"Style diversity: {total} unique styles used in last 7 days — "
            f"avoiding repetition across all moods",
            skill="visual_impact", xp=10)
        brain.save_params(params)


def _learn_image_gen(brain: AgentBrain, slots: list[Path],
                     live_feed: list[dict]) -> None:
    style_freq: Counter = Counter()
    emotion_freq: Counter = Counter()
    prompts_seen = 0

    for slot in slots:
        pp = slot / "image_prompt.txt"
        if not pp.exists():
            continue
        text = pp.read_text(encoding="utf-8", errors="replace").lower()
        prompts_seen += 1
        for w in re.findall(
                r"\b(?:dark|neon|glow|gradient|vibrant|gold|silver|blue|green|"
                r"red|purple|futuristic|modern|minimal|bold|dramatic|cinematic|"
                r"professional|sharp|blurred|bokeh|dramatic|stunning|epic)\b",
                text):
            style_freq[w] += 1
        for w in re.findall(
                r"\b(?:bull|bear|rocket|moon|crash|fire|lightning|storm|"
                r"explosion|rise|fall|chart|graph|arrow|coin|token)\b",
                text):
            emotion_freq[w] += 1

    top_styles  = [w for w, _ in style_freq.most_common(8)]
    top_symbols = [w for w, _ in emotion_freq.most_common(6)]

    if top_styles:
        brain.record_learning(f"Most effective visual styles: {', '.join(top_styles[:5])}",
                              skill="visual_impact", xp=15)
        brain.save_params({"top_visual_styles": top_styles[:6]})

    if top_symbols:
        brain.record_learning(f"Best visual symbols: {', '.join(top_symbols[:5])}",
                              skill="prompt_craft", xp=20)
        brain.save_params({"top_visual_symbols": top_symbols[:5]})

    if prompts_seen > 5:
        brain.record_learning(
            f"Built visual vocabulary from {prompts_seen} prompts — "
            f"brand consistency improving",
            skill="brand_consistency", xp=15)
        brain.save_params({"prompts_analysed": prompts_seen})

    # WEB: theme from live feed (market mood → visual direction)
    if live_feed:
        bullish = sum(1 for i in live_feed
                      if any(w in i.get("title","").lower()
                             for w in ("surge","soar","rally","ath","bull","gain")))
        bearish = sum(1 for i in live_feed
                      if any(w in i.get("title","").lower()
                             for w in ("crash","plunge","fall","drop","bear","loss")))
        if bullish > bearish:
            brain.record_learning(
                f"Market mood: BULLISH — use gold/green gradients, rocket imagery",
                skill="visual_impact", xp=15)
            brain.save_params({"recommended_palette": "bullish",
                               "palette_hint": "gold, bright green, upward arrows"})
        elif bearish > bullish:
            brain.record_learning(
                f"Market mood: BEARISH — use red/dark gradients, storm imagery",
                skill="visual_impact", xp=15)
            brain.save_params({"recommended_palette": "bearish",
                               "palette_hint": "deep red, dark blue, downward arrows"})

    # Style diversity tracking: read style_history.json, build avoid lists (last 7 days)
    _update_avoid_style_indices(brain)


def _learn_engagement_optimizer(brain: AgentBrain, slots: list[Path],
                                  live_feed: list[dict]) -> None:
    # ── LOCAL: analyse timing + platform success from publish logs ─────────
    tod_ok:    Counter = Counter()    # time-of-day successes
    tod_total: Counter = Counter()
    platform_ok:    Counter = Counter()
    platform_total: Counter = Counter()
    variant_ok:     Counter = Counter()
    variant_total:  Counter = Counter()

    for slot in slots:
        log = _read_json(slot / "publish_log.json")
        if not log:
            continue
        slot_name = slot.name
        # Determine time-of-day
        if "morning" in slot_name:
            tod = "morning"
        elif "evening" in slot_name:
            tod = "evening"
        else:
            tod = "other"

        variant = log.get("variant", "?")
        results = log.get("results", {})

        any_ok = False
        for plat, res in results.items():
            platform_total[plat] += 1
            if isinstance(res, dict) and res.get("status") == "ok":
                platform_ok[plat] += 1
                any_ok = True

        tod_total[tod] += 1
        if any_ok:
            tod_ok[tod] += 1

        if variant in ("A", "B"):
            variant_total[variant] += 1
            if any_ok:
                variant_ok[variant] += 1

    # Best time-of-day
    best_tod = max(
        ((t, tod_ok[t] / max(1, tod_total[t])) for t in ("morning", "evening")),
        key=lambda x: x[1], default=("morning", 0.0)
    )
    if tod_total["morning"] + tod_total["evening"] >= 3:
        rate = round(best_tod[1] * 100)
        brain.record_learning(
            f"Best posting time: {best_tod[0]} ({rate}% success rate from "
            f"{tod_total[best_tod[0]]} slots)",
            skill="timing_mastery", xp=25)
        brain.save_params({"best_tod": best_tod[0], "tod_success_rates": {
            t: round(tod_ok[t] / max(1, tod_total[t]), 3)
            for t in ("morning", "evening", "other")
        }})

    # Best variant
    va = round(variant_ok["A"] / max(1, variant_total["A"]) * 100)
    vb = round(variant_ok["B"] / max(1, variant_total["B"]) * 100)
    if variant_total["A"] + variant_total["B"] >= 3:
        winner = "A" if va >= vb else "B"
        brain.record_learning(
            f"A/B test: variant {winner} wins — A={va}% B={vb}% success",
            skill="format_intelligence", xp=20)
        brain.save_params({"preferred_variant": winner,
                           "variant_success": {"A": va, "B": vb}})

    # Platform breakdown
    plat_report = {
        p: round(platform_ok[p] / max(1, platform_total[p]) * 100)
        for p in platform_total if platform_total[p] >= 2
    }
    if plat_report:
        top_plat = max(plat_report, key=lambda x: plat_report[x])
        brain.record_learning(
            f"Platform success rates: " +
            ", ".join(f"{p}={v}%" for p, v in sorted(plat_report.items(), key=lambda x: -x[1])[:4]),
            skill="platform_intelligence", xp=20)
        brain.save_params({"platform_success_rates": plat_report,
                           "top_performing_platform": top_plat})

    # ── WEB: market hours vs engagement signal ─────────────────────────────
    if live_feed:
        hour = datetime.now().hour
        session = ("asian" if 0 <= hour < 8 else
                   "european" if 8 <= hour < 15 else
                   "american" if 15 <= hour < 22 else "overnight")
        feed_volume = len(live_feed)
        brain.record_learning(
            f"Current market session: {session} | Live feed volume: {feed_volume} articles "
            f"→ {'high' if feed_volume > 80 else 'moderate' if feed_volume > 40 else 'low'} activity",
            skill="timing_mastery", xp=15)
        brain.save_params({"current_market_session": session,
                           "feed_volume": feed_volume})


def _learn_trend_predictor(brain: AgentBrain, slots: list[Path],
                            live_feed: list[dict]) -> None:
    # ── LOCAL: check if trending coins appeared in our top stories ─────────
    import urllib.request as _ur
    forecast_file = _REPO_ROOT / "data" / "trend_forecast.json"
    if forecast_file.exists():
        try:
            fc = json.loads(forecast_file.read_text(encoding="utf-8"))
            top_predicted = {c["symbol"].upper() for c in fc.get("top_coins", [])[:5]}
            fg = fc.get("fear_greed", {})

            # Check how many predicted coins appeared in recent top stories
            actual_tickers: Counter = Counter()
            for slot in slots[:15]:
                top = _read_json(slot / "top2.json")
                if not top:
                    continue
                for story in (top.get("stories", []) if isinstance(top, dict) else []):
                    for t in (story.get("tickers") or []):
                        actual_tickers[t.upper()] += 1

            hits = top_predicted & set(actual_tickers.keys())
            total_pred = len(top_predicted)
            hit_rate = round(len(hits) / max(1, total_pred) * 100)

            brain.record_learning(
                f"Prediction accuracy: {hit_rate}% ({len(hits)}/{total_pred} predicted coins "
                f"appeared in top stories). Hits: {', '.join(hits) or 'none yet'}",
                skill="prediction_accuracy", xp=25)
            brain.save_params({"last_hit_rate": hit_rate,
                               "predicted_tickers": list(top_predicted),
                               "matched_tickers": list(hits)})

            if fg.get("value") is not None:
                fgv = int(fg["value"])
                signal = ("extreme_greed" if fgv >= 75 else
                          "greed" if fgv >= 55 else
                          "neutral" if fgv >= 45 else
                          "fear" if fgv >= 25 else "extreme_fear")
                brain.record_learning(
                    f"Fear & Greed: {fg.get('label')} ({fgv}) → market signal: {signal}",
                    skill="market_reading", xp=20)
                brain.save_params({"fear_greed_value": fgv, "market_signal": signal})
        except Exception:
            pass

    # ── LOCAL: velocity from queue history ─────────────────────────────────
    ticker_velocity: dict[str, float] = {}
    for i, slot in enumerate(slots[:20]):
        top = _read_json(slot / "top2.json")
        if not top:
            continue
        for story in (top.get("stories", []) if isinstance(top, dict) else []):
            recency_weight = max(1, 20 - i)  # newer = higher weight
            for t in (story.get("tickers") or []):
                sym = t.upper()
                ticker_velocity[sym] = ticker_velocity.get(sym, 0) + recency_weight

    if ticker_velocity:
        hot = sorted(ticker_velocity.items(), key=lambda x: x[1], reverse=True)[:8]
        hot_str = ", ".join(f"${k}({int(v)}pts)" for k, v in hot[:5])
        brain.record_learning(f"Historical velocity leaders: {hot_str}",
                              skill="velocity_tracking", xp=25)
        brain.save_params({"velocity_leaders": {k: v for k, v in hot},
                           "velocity_updated": _now_iso()})

    # ── WEB: live feed velocity ────────────────────────────────────────────
    if live_feed:
        live_tickers: Counter = Counter()
        for item in live_feed:
            title = item.get("title", "").upper()
            for sym in re.findall(r"\b(?:BTC|ETH|SOL|BNB|XRP|ADA|DOT|MATIC|LINK|"
                                  r"AVAX|UNI|ATOM|DOGE|LTC|TRX|NEAR|OP|ARB)\b", title):
                live_tickers[sym] += 1

        live_hot = [sym for sym, _ in live_tickers.most_common(6)]
        if live_hot:
            brain.record_learning(
                f"Live feed ticker mentions: {', '.join('$'+s for s in live_hot[:5])}",
                skill="velocity_tracking", xp=20)
            brain.save_params({"live_mentioned_tickers": live_hot,
                               "live_ts": _now_iso()})


def _learn_content_personalizer(brain: AgentBrain, slots: list[Path],
                                  live_feed: list[dict]) -> None:
    # ── LOCAL: which niches appear most in successful posts ────────────────
    niche_keywords = {
        "bitcoin":  ["bitcoin", "btc", "satoshi", "lightning", "halving"],
        "defi":     ["defi", "protocol", "tvl", "yield", "uniswap", "aave", "dex"],
        "nft":      ["nft", "ordinal", "opensea", "mint", "floor"],
        "altcoins": ["altcoin", "alt season", "solana", "avalanche", "polygon"],
        "trading":  ["trade", "liquidation", "futures", "leverage", "short", "long"],
        "macro":    ["fed", "rate", "inflation", "cpi", "treasury", "dollar"],
    }

    niche_ok:    Counter = Counter()
    niche_total: Counter = Counter()

    for slot in slots:
        log = _read_json(slot / "publish_log.json")
        top = _read_json(slot / "top2.json")
        if not log or not top:
            continue

        any_ok = any(
            r.get("status") == "ok"
            for r in log.get("results", {}).values()
            if isinstance(r, dict)
        )

        stories = top.get("stories", []) if isinstance(top, dict) else []
        for story in stories:
            text = (story.get("title", "") + " " + story.get("summary", "")).lower()
            for niche, kws in niche_keywords.items():
                if any(kw in text for kw in kws):
                    niche_total[niche] += 1
                    if any_ok:
                        niche_ok[niche] += 1

    niche_rates = {
        n: round(niche_ok[n] / max(1, niche_total[n]) * 100)
        for n in niche_total if niche_total[n] >= 2
    }
    if niche_rates:
        top_niche = max(niche_rates, key=lambda x: niche_rates[x])
        brain.record_learning(
            f"Niche success rates: " +
            ", ".join(f"{n}={v}%" for n, v in sorted(niche_rates.items(), key=lambda x: -x[1])[:5]),
            skill="niche_detection", xp=25)
        brain.save_params({"niche_success_rates": niche_rates,
                           "recommended_niche": top_niche})

    # ── LOCAL: detect dominant tone from post text ─────────────────────────
    tone_counts: Counter = Counter()
    for slot in slots[:20]:
        post = slot / "post_x.txt"
        if not post.exists():
            continue
        text = post.read_text(encoding="utf-8", errors="replace").lower()
        if any(w in text for w in ("!", "🚀", "🔥", "massive", "explod", "pump", "moon")):
            tone_counts["hype"] += 1
        elif any(w in text for w in ("analysis", "according", "report", "data", "official")):
            tone_counts["professional"] += 1
        elif any(w in text for w in ("hey", "let's", "check out", "you know", "pretty")):
            tone_counts["casual"] += 1
        else:
            tone_counts["educational"] += 1

    top_tone = tone_counts.most_common(1)
    if top_tone:
        brain.record_learning(
            f"Dominant post tone: {top_tone[0][0]} ({top_tone[0][1]} posts) — "
            f"audience profile: {', '.join(t for t, _ in tone_counts.most_common(3))}",
            skill="tone_calibration", xp=20)
        brain.save_params({"dominant_tone": top_tone[0][0],
                           "tone_distribution": dict(tone_counts)})

    # ── WEB: live feed category distribution ──────────────────────────────
    if live_feed:
        live_niches: Counter = Counter()
        for item in live_feed:
            text = item.get("title", "").lower()
            for niche, kws in niche_keywords.items():
                if any(kw in text for kw in kws):
                    live_niches[niche] += 1

        if live_niches:
            hot_niche = live_niches.most_common(1)[0][0]
            brain.record_learning(
                f"Trending content niche RIGHT NOW: {hot_niche} "
                f"({live_niches[hot_niche]} live articles) | "
                f"top 3: {', '.join(n for n, _ in live_niches.most_common(3))}",
                skill="audience_profiling", xp=20)
            brain.save_params({"trending_niche_now": hot_niche,
                               "live_niche_counts": dict(live_niches.most_common(6))})


def _learn_fact_checker(brain: AgentBrain, slots: list[Path],
                        live_feed: list[dict]) -> None:
    # ── LOCAL: scan past posts for price claims and check accuracy ─────────
    _PRICE_PAT = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*[Kk]?\b")

    price_claims_found = 0
    potential_issues: Counter = Counter()

    for slot in slots[:20]:
        post = slot / "post_x.txt"
        if not post.exists():
            continue
        text = post.read_text(encoding="utf-8", errors="replace")
        matches = _PRICE_PAT.findall(text)
        price_claims_found += len(matches)
        for m in matches:
            try:
                val = float(m.replace(",", ""))
                # Categorize price range
                if val > 50000:
                    potential_issues["btc_range"] += 1
                elif val > 1000:
                    potential_issues["eth_range"] += 1
                elif val > 100:
                    potential_issues["mid_cap_range"] += 1
            except ValueError:
                pass

    if price_claims_found > 0:
        brain.record_learning(
            f"Scanned {len(slots[:20])} posts: found {price_claims_found} price claims. "
            f"BTC-range: {potential_issues.get('btc_range', 0)}, "
            f"ETH-range: {potential_issues.get('eth_range', 0)}, "
            f"mid-cap: {potential_issues.get('mid_cap_range', 0)}",
            skill="accuracy_radar", xp=20)
        brain.save_params({"total_price_claims_scanned": price_claims_found,
                           "price_range_distribution": dict(potential_issues)})

    # ── LOCAL: high-volatility ticker detection from queue history ─────────
    ticker_mentions: Counter = Counter()
    for slot in slots[:30]:
        top = _read_json(slot / "top2.json")
        if not top:
            continue
        for story in (top.get("stories", []) if isinstance(top, dict) else []):
            for t in (story.get("tickers") or []):
                ticker_mentions[t.upper()] += 1

    if ticker_mentions:
        # Tickers that appear frequently = need stricter checks
        high_freq = [t for t, n in ticker_mentions.most_common(10) if n >= 3]
        if high_freq:
            brain.record_learning(
                f"High-frequency tickers in posts (need active price validation): "
                f"{', '.join('$'+t for t in high_freq[:8])}",
                skill="volatility_awareness", xp=20)
            brain.save_params({"high_priority_tickers": high_freq[:10],
                               "ticker_frequency": {t: n for t, n in ticker_mentions.most_common(10)}})

    # ── WEB: live prices from CoinGecko for trust-building ─────────────────
    try:
        req = __import__("urllib.request").request.Request(
            "https://api.coingecko.com/api/v3/simple/price?"
            "ids=bitcoin,ethereum,solana,binancecoin,ripple"
            "&vs_currencies=usd",
            headers={"User-Agent": "AutoPost/1.0", "Accept": "application/json"}
        )
        with __import__("urllib.request").request.urlopen(req, timeout=8) as resp:
            prices = json.loads(resp.read().decode("utf-8"))

        price_snapshot = {coin: data["usd"] for coin, data in prices.items() if "usd" in data}
        price_str = ", ".join(f"{k[:3].upper()}=${int(v):,}" for k, v in price_snapshot.items())
        brain.record_learning(
            f"Live price snapshot for cross-referencing: {price_str}",
            skill="accuracy_radar", xp=25)
        brain.save_params({"live_price_snapshot": price_snapshot,
                           "snapshot_ts": _now_iso()})

        # Learn volatility: detect big intraday swings
        if "bitcoin" in price_snapshot:
            btc = price_snapshot["bitcoin"]
            brain.record_learning(
                f"BTC live: ${btc:,.0f} — claims outside ${btc*0.85:,.0f}–${btc*1.15:,.0f} "
                f"(±15%) should be flagged",
                skill="volatility_awareness", xp=15)
            brain.save_params({"btc_live_price": btc,
                               "btc_warn_range": [round(btc*0.85), round(btc*1.15)]})
    except Exception:
        # Network failure — learn from what we have locally
        if price_claims_found > 0:
            brain.record_learning(
                f"CoinGecko offline — relying on cached prices. "
                f"Scanned {price_claims_found} historical claims for baseline.",
                skill="source_trust", xp=10)


def _learn_ab_tester(brain: AgentBrain, slots: list[Path],
                     live_feed: list[dict]) -> None:
    # ── LOCAL: analyse A vs B variant records from queue ──────────────────
    a_scores: list[float] = []
    b_scores: list[float] = []
    hook_scores: dict[str, list[float]] = {}
    feature_hits: dict[str, int] = {"has_cashtag": 0, "has_emoji": 0,
                                     "has_question": 0, "has_cta": 0}
    feature_total = 0

    for slot in slots:
        log = _read_json(slot / "publish_log.json")
        if not log:
            continue
        variant = log.get("variant")
        if variant not in ("A", "B"):
            continue

        metrics = _read_json(slot / "metrics.json") or {}
        metrics = {k: v for k, v in metrics.items() if not k.startswith("_")}

        # Simple engagement proxy: x impressions + telegram views
        eng = 0.0
        x = metrics.get("x", {})
        if x:
            eng += float(x.get("impressions", 0) or 0) / 100
            eng += float(x.get("likes", 0) or 0) * 3
        tg = metrics.get("telegram", {})
        if tg:
            eng += float(tg.get("views", 0) or 0) / 50

        if variant == "A":
            a_scores.append(eng)
        else:
            b_scores.append(eng)

        # Feature analysis
        post = slot / "post_x.txt"
        if post.exists():
            text = post.read_text(encoding="utf-8", errors="replace")
            feature_total += 1
            if re.search(r"\$[A-Z]{2,}", text):
                feature_hits["has_cashtag"] += 1
            if re.search(r"[\U0001F300-\U0001FAFF]", text):
                feature_hits["has_emoji"] += 1
            if "?" in text.split(".")[0]:
                feature_hits["has_question"] += 1
            if re.search(r"\b(follow|like|share|subscribe|watch)\b", text.lower()):
                feature_hits["has_cta"] += 1

            # Hook type
            first = text.strip().split(".")[0].lower()
            if "?" in first:
                ht = "question"
            elif re.search(r"\b\d+", first):
                ht = "number"
            else:
                ht = "statement"
            if ht not in hook_scores:
                hook_scores[ht] = []
            if eng > 0:
                hook_scores[ht].append(eng)

    # Report A vs B
    if len(a_scores) >= 2 and len(b_scores) >= 2:
        a_avg = sum(a_scores) / len(a_scores)
        b_avg = sum(b_scores) / len(b_scores)
        winner = "A" if a_avg > b_avg else ("B" if b_avg > a_avg else "tie")
        brain.record_learning(
            f"A/B comparison: variant {winner} leads — "
            f"A avg={a_avg:.1f} ({len(a_scores)} posts) vs "
            f"B avg={b_avg:.1f} ({len(b_scores)} posts)",
            skill="variant_analysis", xp=30)
        brain.save_params({
            "preferred_variant": winner if winner != "tie" else None,
            "variant_scores": {"A": round(a_avg, 2), "B": round(b_avg, 2)},
        })

    # Best hook type
    if hook_scores:
        best_hook = max(hook_scores,
                        key=lambda h: sum(hook_scores[h]) / max(1, len(hook_scores[h])))
        hook_summary = {h: round(sum(s)/len(s), 2) for h, s in hook_scores.items() if s}
        brain.record_learning(
            f"Best hook type: '{best_hook}' | scores: "
            + ", ".join(f"{h}={v}" for h, v in sorted(hook_summary.items(), key=lambda x: -x[1])),
            skill="hook_optimization", xp=25)
        brain.save_params({"best_hook_type": best_hook, "hook_scores": hook_summary})

    # Feature wins
    if feature_total >= 3:
        wins = {f: round(n / feature_total * 100) for f, n in feature_hits.items()}
        top_feature = max(wins, key=lambda x: wins[x])
        brain.record_learning(
            f"Feature usage rates: " +
            ", ".join(f"{f.replace('has_', '')}={v}%" for f, v in wins.items()),
            skill="format_learning", xp=20)
        brain.save_params({"feature_usage_rates": wins,
                           "most_used_feature": top_feature})

    # ── WEB: live feed provides tone signal for A/B context ────────────────
    if live_feed:
        urgent   = sum(1 for i in live_feed
                       if any(w in i.get("title", "").lower()
                              for w in ("breaking", "urgent", "alert", "warning")))
        question = sum(1 for i in live_feed if "?" in i.get("title", ""))
        total_lf = max(1, len(live_feed))
        brain.record_learning(
            f"Live feed hook mix: {urgent}/{total_lf} urgency hooks, "
            f"{question}/{total_lf} question hooks — "
            f"{'urgency' if urgent > question else 'question'} dominant now",
            skill="hook_optimization", xp=15)
        brain.save_params({"live_hook_mix": {
            "urgency_pct": round(urgent / total_lf * 100),
            "question_pct": round(question / total_lf * 100),
        }})


def _learn_ad_creator(brain: AgentBrain, slots: list[Path],
                      live_feed: list[dict]) -> None:
    # ── LOCAL: learn from past copy what ad patterns work ─────────────────────
    hook_types:    Counter = Counter()
    ad_formulas:   Counter = Counter()
    cta_words:     Counter = Counter()
    brand_tones:   Counter = Counter()
    platform_hits: Counter = Counter()

    for slot in slots:
        for fname in ["ig_copy.txt", "twitter_copy.txt", "tiktok_copy.txt",
                      "post_body.txt", "caption.txt"]:
            fpath = slot / fname
            if not fpath.exists():
                continue
            text = fpath.read_text(encoding="utf-8", errors="replace")[:600]
            lines = text.strip().split("\n")
            if not lines:
                continue
            hook = lines[0].lower()
            full = text.lower()

            # Hook type detection
            if "?" in hook:
                hook_types["question"] += 1
            elif re.match(r"^\d+", hook.strip()):
                hook_types["numbered_list"] += 1
            elif any(w in hook for w in ["breaking", "urgent", "alert", "warning"]):
                hook_types["urgency"] += 1
            elif any(w in hook for w in ["imagine", "what if", "picture this"]):
                hook_types["imagination"] += 1
            elif any(w in hook for w in ["you", "your", "you're", "you've"]):
                hook_types["direct_address"] += 1
            else:
                hook_types["statement"] += 1

            # Ad formula fingerprints
            if any(w in full for w in ["problem", "struggle", "frustrated", "tired of"]):
                ad_formulas["PAS"] += 1
            if any(w in full for w in ["attention", "discover", "introducing", "announcing"]):
                ad_formulas["AIDA"] += 1
            if any(w in full for w in ["before", "after", "transform", "bridge"]):
                ad_formulas["BAB"] += 1
            if any(w in full for w in ["feature", "advantage", "benefit", "result"]):
                ad_formulas["FAB"] += 1

            # CTA patterns
            for cta in ["click", "buy now", "get", "start", "join", "try",
                        "discover", "grab", "claim", "sign up", "book", "shop"]:
                if cta in full:
                    cta_words[cta] += 1

            # Brand tone fingerprint
            if any(w in full for w in ["exclusive", "premium", "luxury", "elite", "vip"]):
                brand_tones["premium"] += 1
            elif any(w in full for w in ["easy", "simple", "quick", "fast", "finally"]):
                brand_tones["casual"] += 1
            elif any(w in full for w in ["proven", "data", "research", "backed", "fact"]):
                brand_tones["authoritative"] += 1
            elif any(w in full for w in ["limited", "deadline", "last chance", "expires"]):
                brand_tones["scarcity"] += 1
            else:
                brand_tones["neutral"] += 1

        pub = _read_json(slot / "publish_result.json")
        if pub and isinstance(pub, dict):
            for plat in ["instagram", "twitter", "tiktok", "youtube", "telegram"]:
                if isinstance(pub.get(plat), dict) and pub[plat].get("success"):
                    platform_hits[plat] += 1

    if hook_types:
        best = hook_types.most_common(1)[0]
        brain.record_learning(f"Top hook type: '{best[0]}' — used {best[1]}x in local posts",
                              skill="ad_copywriting", xp=25)
        brain.save_params({"preferred_hook": best[0],
                           "hook_distribution": dict(hook_types.most_common(6))})

    if ad_formulas:
        top = ad_formulas.most_common(1)[0][0]
        brain.record_learning(f"Most effective ad formula in local content: {top}",
                              skill="ad_copywriting", xp=20)
        brain.save_params({"top_ad_formula": top,
                           "formula_distribution": dict(ad_formulas)})

    if cta_words:
        top_ctas = [w for w, _ in cta_words.most_common(5)]
        brain.record_learning(f"High-performing CTAs detected: {', '.join(top_ctas)}",
                              skill="conversion_optimization", xp=20)
        brain.save_params({"top_ctas": top_ctas})

    if brand_tones:
        dominant = brand_tones.most_common(1)[0][0]
        brain.record_learning(f"Brand tone fingerprint in local content: {dominant}",
                              skill="brand_intelligence", xp=15)
        brain.save_params({"dominant_tone": dominant})

    if platform_hits:
        top_plats = [p for p, _ in platform_hits.most_common(3)]
        brain.record_learning(f"Best performing platforms: {', '.join(top_plats)}",
                              skill="campaign_strategy", xp=20)
        brain.save_params({"best_platforms": top_plats})

    # ── WEB: scan live feeds for industry ad opportunities ────────────────────
    if live_feed:
        industry_hits: Counter = Counter()
        ad_opp_scores: list[tuple[str, int]] = []

        for item in live_feed:
            title = item.get("title", "")
            title_low = title.lower()
            for industry, kws in _INDUSTRY_KEYWORDS.items():
                if any(kw in title_low for kw in kws):
                    industry_hits[industry] += 1
            score = _score_headline(title)
            if score > 0:
                ad_opp_scores.append((title, score))

        if industry_hits:
            hot = [i for i, _ in industry_hits.most_common(4)]
            brain.record_learning(f"Trending industries for ads: {', '.join(hot)}",
                                  skill="audience_targeting", xp=25)
            brain.save_params({"trending_industries": hot,
                               "industry_updated": _now_iso()})

        top_opps = sorted(ad_opp_scores, key=lambda x: x[1], reverse=True)[:3]
        if top_opps:
            opp_str = " | ".join(t[:60] for t, _ in top_opps)
            brain.record_learning(f"Top ad opportunity topics now: {opp_str}",
                                  skill="campaign_strategy", xp=20)
            brain.save_params({"ad_opportunity_topics": [t for t, _ in top_opps],
                               "opportunity_updated": _now_iso()})


def _learn_supervisor(brain: "AgentBrain", slots: list[Path],
                       live_feed: list[dict]) -> None:
    """Supervisor learns from its own check history to improve predictions."""
    import json as _json

    report_path = _REPO_ROOT / "analytics" / "supervisor_report.json"
    if not report_path.exists():
        brain.record_learning("No supervision report yet — first cycle pending.",
                              skill="agent_health_monitoring", xp=5)
        brain.add_xp(5, "agent_health_monitoring")
        return

    try:
        report = _json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return

    agents_data  = report.get("agents", {})
    summary      = report.get("summary", {})
    total        = summary.get("total", 0)
    ok_count     = summary.get("ok", 0)
    warn_count   = summary.get("warn", 0)
    fail_count   = summary.get("fail", 0)
    overall      = report.get("overall", "unknown")
    critical     = report.get("critical_issues", [])

    # Base XP for running a check cycle
    brain.add_xp(5, "agent_health_monitoring")

    # XP for issues found — supervisor learns by detecting problems
    for agent, result in agents_data.items():
        issues = result.get("issues", [])
        status = result.get("status", "ok")
        if status == "fail":
            brain.add_xp(15, "failure_prediction")
            brain.record_learning(
                f"FAIL detected on {agent}: {'; '.join(issues[:2])}",
                skill="failure_prediction", xp=0)
        elif status == "warn":
            brain.add_xp(5, "agent_health_monitoring")

    # XP for overall fleet health knowledge
    if total > 0:
        health_pct = round(ok_count / total * 100)
        brain.record_learning(
            f"Fleet health: {health_pct}% OK ({ok_count}/{total} agents). "
            f"Warns: {warn_count} · Fails: {fail_count} · Overall: {overall}",
            skill="performance_benchmarking", xp=10)
        brain.add_xp(10, "performance_benchmarking")

    # Critical issues → learn failure cascades
    if critical:
        brain.record_learning(
            f"{len(critical)} critical issue(s): {critical[0][:80]}",
            skill="cross_agent_correlation", xp=20)
        brain.add_xp(20, "cross_agent_correlation")

    # Detect agents with recurring warns/fails → early prediction
    problem_agents = [a for a, r in agents_data.items() if r.get("status") != "ok"]
    if problem_agents:
        brain.record_learning(
            f"Agents needing attention: {', '.join(problem_agents)}",
            skill="failure_prediction", xp=8)

    # All-clean bonus — reward stability
    if fail_count == 0 and warn_count <= 1:
        brain.add_xp(25, "auto_recovery")
        brain.record_learning(
            "All agents healthy this cycle — pipeline stability confirmed.",
            skill="auto_recovery", xp=0)

    # Save learned params for dashboard consumption
    brain.save_params({
        "last_check_overall":   overall,
        "fleet_health_pct":     round(ok_count / max(total, 1) * 100),
        "problem_agents":       problem_agents,
        "critical_count":       len(critical),
        "last_updated":         _now_iso(),
    })


def _learn_web_learner(brain: "AgentBrain", slots: list[Path],
                        live_feed: list[dict]) -> None:
    """Web Learner brain learns from its own research history."""
    params = brain.load_params()
    snippets    = params.get("web_latest_snippets", [])
    ai_insights = params.get("web_ai_insights", [])
    queries_run = params.get("web_queries_run", 0)
    last_run    = params.get("web_last_updated", "")

    if not snippets and not ai_insights:
        brain.record_learning(
            "No web research runs yet — first cycle pending.",
            skill="web_research", xp=5)
        return

    # XP for breadth of research
    brain.add_xp(queries_run * 5, "web_research")

    # Reward AI summarisation quality
    if ai_insights:
        brain.record_learning(
            f"AI-distilled {len(ai_insights)} insights from web research — "
            f"last run: {last_run[:10]}",
            skill="ai_summarisation", xp=15 * len(ai_insights))
        brain.add_xp(10, "knowledge_routing")

    # Reward raw snippet volume — trend awareness
    if snippets:
        brain.add_xp(len(snippets) * 2, "trend_awareness")
        brain.record_learning(
            f"Collected {len(snippets)} web snippets across all agent domains — "
            f"knowledge base growing",
            skill="insight_extraction", xp=20)

    brain.save_params({
        "total_research_cycles":
            params.get("total_research_cycles", 0) + 1,
    })


def _learn_competitor_tracker(brain: "AgentBrain", slots: list[Path],
                               live_feed: list[dict]) -> None:
    """Competitor Tracker brain learns from competitor post history stored in DB."""
    # ── LOCAL: read all competitor posts from DB ───────────────────────────
    try:
        from src.dashboard.database import get_all_competitors, get_competitor_posts
        all_comps = get_all_competitors()
    except Exception:
        brain.record_learning(
            "No competitor accounts tracked yet — add accounts to start learning.",
            skill="data_collection", xp=5)
        return

    if not all_comps:
        brain.record_learning(
            "No competitor accounts tracked yet — add accounts to start learning.",
            skill="data_collection", xp=5)
        return

    # Aggregate all posts across all competitors
    all_posts: list[dict] = []
    hashtag_counter: Counter = Counter()
    word_counter:    Counter = Counter()
    engagement_by_comp: dict[str, dict] = {}
    freq_by_comp:       dict[str, float] = {}

    _stop = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "or",
             "is", "are", "was", "this", "that", "it", "at", "by", "from",
             "with", "have", "will", "been", "they", "their", "says", "said"}

    for comp in all_comps:
        try:
            posts = get_competitor_posts(comp["id"], limit=50)
        except Exception:
            continue
        if not posts:
            continue

        all_posts.extend(posts)
        handle = comp.get("handle", str(comp["id"]))

        # Per-competitor engagement
        likes   = [p.get("likes",  0) for p in posts if p.get("likes",  0) > 0]
        shares  = [p.get("shares", 0) for p in posts if p.get("shares", 0) > 0]
        views   = [p.get("views",  0) for p in posts if p.get("views",  0) > 0]
        engagement_by_comp[handle] = {
            "avg_likes":  round(sum(likes)  / max(1, len(likes)),  1),
            "avg_shares": round(sum(shares) / max(1, len(shares)), 1),
            "avg_views":  round(sum(views)  / max(1, len(views)),  1),
            "post_count": len(posts),
        }

        # Posting frequency
        dates = []
        for p in posts:
            raw = p.get("posted_at", "")
            if raw:
                try:
                    from dateutil import parser as _dp
                    dt = _dp.parse(raw)
                    dates.append(dt)
                except Exception:
                    pass
        if len(dates) >= 2:
            dates.sort()
            span_days = (dates[-1] - dates[0]).total_seconds() / 86400
            if span_days > 0:
                freq_by_comp[handle] = round(len(posts) / (span_days / 7), 1)

        # Global hashtag + keyword extraction
        for p in posts:
            text = p.get("content", "")
            for tag in re.findall(r"#\w+", text):
                hashtag_counter[tag.lower()] += 1
            for word in re.findall(r"\b[a-zA-Z]{4,}\b", text):
                w = word.lower()
                if w not in _stop:
                    word_counter[w] += 1

    if not all_posts:
        brain.record_learning(
            f"Tracking {len(all_comps)} competitor(s) — no posts yet. Run Scan All Now.",
            skill="data_collection", xp=10)
        return

    # ── LEARNING: hashtag intelligence ────────────────────────────────────
    top_tags = [t for t, _ in hashtag_counter.most_common(15)]
    if top_tags:
        brain.record_learning(
            f"Top competitor hashtags: {', '.join(top_tags[:8])}",
            skill="hashtag_intelligence", xp=25)
        brain.save_params({"competitor_top_hashtags": top_tags})

    # ── LEARNING: content themes ───────────────────────────────────────────
    top_words = [w for w, _ in word_counter.most_common(20)]
    if top_words:
        brain.record_learning(
            f"Dominant competitor content themes: {', '.join(top_words[:10])}",
            skill="pattern_recognition", xp=20)
        brain.save_params({"competitor_content_themes": top_words})

    # ── LEARNING: engagement benchmark ────────────────────────────────────
    if engagement_by_comp:
        all_likes  = [v["avg_likes"]  for v in engagement_by_comp.values() if v["avg_likes"]  > 0]
        all_shares = [v["avg_shares"] for v in engagement_by_comp.values() if v["avg_shares"] > 0]
        all_views  = [v["avg_views"]  for v in engagement_by_comp.values() if v["avg_views"]  > 0]

        top_eng = max(engagement_by_comp.items(),
                      key=lambda x: x[1]["avg_likes"] + x[1]["avg_shares"] * 3,
                      default=None)

        bench = {
            "benchmark_avg_likes":  round(sum(all_likes)  / max(1, len(all_likes)),  1) if all_likes  else 0,
            "benchmark_avg_shares": round(sum(all_shares) / max(1, len(all_shares)), 1) if all_shares else 0,
            "benchmark_avg_views":  round(sum(all_views)  / max(1, len(all_views)),  1) if all_views  else 0,
            "top_competitor":       top_eng[0] if top_eng else "",
            "engagement_by_account": engagement_by_comp,
            "updated": _now_iso(),
        }
        summary_parts = []
        if bench["benchmark_avg_likes"]  > 0: summary_parts.append(f"avg likes={bench['benchmark_avg_likes']}")
        if bench["benchmark_avg_shares"] > 0: summary_parts.append(f"avg shares={bench['benchmark_avg_shares']}")
        if bench["benchmark_avg_views"]  > 0: summary_parts.append(f"avg views={bench['benchmark_avg_views']}")
        if top_eng:
            summary_parts.append(f"leader={top_eng[0]}")

        brain.record_learning(
            f"Competitor engagement benchmark ({len(engagement_by_comp)} accounts): "
            + (", ".join(summary_parts) or "data collected"),
            skill="engagement_benchmark", xp=30)
        brain.save_params(bench)

    # ── LEARNING: posting frequency ───────────────────────────────────────
    if freq_by_comp:
        top_poster = max(freq_by_comp.items(), key=lambda x: x[1])
        avg_freq   = round(sum(freq_by_comp.values()) / len(freq_by_comp), 1)
        brain.record_learning(
            f"Competitor posting frequency: avg {avg_freq} posts/week — "
            f"most active: {top_poster[0]} ({top_poster[1]} posts/week)",
            skill="frequency_analysis", xp=25)
        brain.save_params({
            "competitor_avg_posts_per_week": avg_freq,
            "most_active_competitor":        top_poster[0],
            "frequency_by_account":          freq_by_comp,
        })

    # ── LEARNING: data collection quality ─────────────────────────────────
    brain.record_learning(
        f"Analyzed {len(all_posts)} posts from {len(all_comps)} competitor account(s) — "
        f"{len(hashtag_counter)} unique hashtags found",
        skill="data_collection", xp=15)

    # XP bonus proportional to data richness
    brain.add_xp(min(100, len(all_posts) * 2), "pattern_recognition")


_LEARN_FNS = {
    "news_scout":          _learn_news_scout,
    "ranker":              _learn_ranker,
    "copywriter":          _learn_copywriter,
    "reel_writer":         _learn_reel_writer,
    "avatar_writer":       _learn_avatar_writer,
    "viral_analyzer":      _learn_viral_analyzer,
    "image_gen":           _learn_image_gen,
    "engagement_optimizer": _learn_engagement_optimizer,
    "trend_predictor":     _learn_trend_predictor,
    "content_personalizer": _learn_content_personalizer,
    "fact_checker":        _learn_fact_checker,
    "ab_tester":           _learn_ab_tester,
    "ad_creator":          _learn_ad_creator,
    "agent_supervisor":    _learn_supervisor,
    "web_learner":         _learn_web_learner,
    "competitor_tracker":  _learn_competitor_tracker,
}


# ══════════════════════════════════════════════════════════════════════════════
# BrainManager
# ══════════════════════════════════════════════════════════════════════════════
class BrainManager:
    def __init__(self) -> None:
        self._brains: dict[str, AgentBrain] = {}
        self._lock   = threading.Lock()
        self._thread: threading.Thread | None = None
        _BRAINS_DIR.mkdir(parents=True, exist_ok=True)

    def _get(self, name: str) -> AgentBrain:
        with self._lock:
            if name not in self._brains:
                self._brains[name] = AgentBrain(name)
            return self._brains[name]

    def all_brains(self) -> list[dict]:
        return [self._get(n).data for n in AGENTS]

    # ── core learning cycle ─────────────────────────────────────────────────
    def run_learning_cycle(self, verbose: bool = True) -> None:
        if verbose:
            print("[agent_brain] === learning cycle start ===")

        # 1. Fetch live RSS feeds once, share across all agents
        if verbose:
            print("[agent_brain] fetching live RSS feeds...")
        live_feed = _fetch_all_feeds()
        if verbose:
            print(f"[agent_brain] {len(live_feed)} live articles fetched")

        # 2. Collect local slot data
        slots = _collect_slot_dirs(limit=40)
        if verbose:
            print(f"[agent_brain] analysing {len(slots)} local slot dirs")

        # 3. Run each agent's learning function
        for name, fn in _LEARN_FNS.items():
            brain = self._get(name)
            try:
                brain.set_status("learning")
                brain.save()
                fn(brain, slots, live_feed)
                brain._data["cycles_completed"] = brain._data.get("cycles_completed", 0) + 1
                brain.set_status("idle")
                brain.save()
                if verbose:
                    b = brain.data
                    print(f"[agent_brain] {name}: Lv.{b['level']} "
                          f"XP={b['total_xp']} learnings={b['total_learnings']}")
            except Exception as exc:
                brain.set_status("idle")
                brain.save()
                print(f"[agent_brain] {name} failed: {exc}")

        self._write_summary()
        if verbose:
            print("[agent_brain] === cycle complete ===")

    def record_pipeline_run(self, slot_dir: Path) -> None:
        """Call at end of every pipeline run — awards XP and triggers mini-cycle."""
        for name in AGENTS:
            b = self._get(name)
            b.mark_run()
            b.save()
        t = threading.Thread(target=self.run_learning_cycle,
                             kwargs={"verbose": False}, daemon=True)
        t.start()

    def _write_summary(self) -> None:
        try:
            (_BRAINS_DIR / "_global_summary.json").write_text(
                json.dumps({"last_cycle": _now_iso(),
                            "agents": [self._get(n).data for n in AGENTS]},
                           indent=2, ensure_ascii=False),
                encoding="utf-8")
        except Exception:
            pass

    # ── background auto-loop ─────────────────────────────────────────────────
    def start_background_loop(self, interval_minutes: int = 10) -> None:
        """Continuous learning loop — fires every `interval_minutes` minutes."""
        if self._thread and self._thread.is_alive():
            return

        def _loop() -> None:
            time.sleep(30)          # give app time to fully start
            cycle = 0
            while True:
                cycle += 1
                print(f"[agent_brain] auto-cycle #{cycle}")
                try:
                    self.run_learning_cycle(verbose=True)
                except Exception as exc:
                    print(f"[agent_brain] cycle #{cycle} error: {exc}")
                time.sleep(interval_minutes * 60)

        self._thread = threading.Thread(target=_loop, name="agent_brain_loop",
                                        daemon=True)
        self._thread.start()
        print(f"[agent_brain] background loop started (every {interval_minutes} min)")


# Module-level singleton
manager = BrainManager()


def add_learning(agent_name: str, skill: str, text: str,
                 source: str = "auto", dedupe: bool = True, xp: int = 10) -> bool:
    """Add a learning entry to an agent brain from any module.

    Args:
        agent_name: e.g. "copywriter", "ranker", "image_gen"
        skill:      skill key from agent's skill catalogue
        text:       learning text (max 220 chars stored)
        source:     origin label for debugging ("market_intelligence", "feedback_loop", etc.)
        dedupe:     skip if a learning with the same first 60 chars already exists today
        xp:         XP to award (default 10 for external signals, less than local 15)

    Returns True if learning was added, False if skipped (dedupe).
    """
    try:
        brain = manager._get(agent_name)
        entry_text = f"[{source}] {text}"[:220]
        if dedupe:
            prefix = entry_text[:60].lower()
            from datetime import date
            today = date.today().isoformat()
            for existing in brain._data.get("learnings", []):
                if existing.get("ts", "").startswith(today):
                    if existing.get("text", "")[:60].lower() == prefix:
                        return False
        brain.record_learning(entry_text, skill=skill, xp=xp)
        brain.save()
        return True
    except Exception as exc:
        print(f"[agent_brain] add_learning failed for {agent_name}: {exc}")
        return False
