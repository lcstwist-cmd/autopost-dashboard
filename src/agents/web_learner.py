"""Web Learner — autonomous daily internet research for every agent brain.

Each agent has a curated set of search queries for its domain.
Insights are extracted and written directly into each brain's knowledge base —
they show up as learning entries in the Agent Brains backoffice page.

Sources (all free — zero API keys required):
  • Google News RSS   — latest news per topic (worldwide coverage)
  • Bing News RSS     — second news source for diversity
  • HN Algolia API    — Hacker News: tech & startup community titles
  • Reddit JSON       — community top posts from topic-specific subreddits
  • Medium RSS        — long-form articles by tag
  • DEV.to API        — developer & tech articles by tag
  • Curated RSS       — premium blogs: HubSpot, Buffer, Hootsuite, Neil Patel,
                        Sprout Social, Smashing Magazine, The Block, Blockworks,
                        CoSchedule, Later, AdWeek, CoinDesk, Messari

Query format:
  {"q": "search query",  "src": "gnews|bing|hn",    "skill": "skill_name"}
  {"q": "",              "src": "reddit",  "param": "subreddit",  "skill": "…"}
  {"q": "",              "src": "medium",  "param": "tag-slug",   "skill": "…"}
  {"q": "",              "src": "devto",   "param": "tag",        "skill": "…"}
  {"q": "rss_url",       "src": "rss",                            "skill": "…"}

Schedule: runs twice per day — AM at 07:00 and PM at 19:00 (configured in scheduler.py)
CLI:
    python src/agents/web_learner.py               # full cycle, skip already-learned
    python src/agents/web_learner.py --force        # force re-run today
    python src/agents/web_learner.py --agent ranker # single agent only
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

_HERE      = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Per-agent research topics — each entry: query string, source, target skill
# ---------------------------------------------------------------------------

_WEB_QUERIES: dict[str, list[dict[str, str]]] = {
    "news_scout": [
        {"q": "best cryptocurrency news sources journalists follow 2025",  "src": "gnews",  "skill": "source_quality"},
        {"q": "top blockchain media RSS aggregators breaking crypto",      "src": "gnews",  "skill": "source_quality"},
        {"q": "crypto breaking news speed first mover advantage",         "src": "gnews",  "skill": "timing_awareness"},
        {"q": "",  "src": "reddit",  "param": "CryptoCurrency",           "skill": "timing_awareness"},
        {"q": "",  "src": "reddit",  "param": "Bitcoin",                  "skill": "source_quality"},
        {"q": "https://www.coindesk.com/arc/outboundfeeds/rss/",          "src": "rss",    "skill": "source_quality"},
        {"q": "https://blockworks.co/feed",                               "src": "rss",    "skill": "timing_awareness"},
        {"q": "https://www.theblock.co/rss.xml",                          "src": "rss",    "skill": "source_quality"},
    ],
    "ranker": [
        {"q": "most viral crypto blockchain news stories this week",       "src": "gnews",  "skill": "trend_prediction"},
        {"q": "what makes a crypto news article go viral social media",    "src": "gnews",  "skill": "story_selection"},
        {"q": "crypto trending topics Twitter Reddit 2025",                "src": "gnews",  "skill": "keyword_intuition"},
        {"q": "",  "src": "reddit",  "param": "investing",                "skill": "trend_prediction"},
        {"q": "",  "src": "reddit",  "param": "CryptoCurrency",           "skill": "story_selection"},
        {"q": "https://messari.io/rss",                                   "src": "rss",    "skill": "keyword_intuition"},
    ],
    "copywriter": [
        {"q": "viral social media post formulas high engagement 2025",     "src": "gnews",  "skill": "hook_mastery"},
        {"q": "best crypto Twitter hooks viral writing techniques",        "src": "gnews",  "skill": "hook_mastery"},
        {"q": "short form social media copywriting AIDA PAS BAB",         "src": "gnews",  "skill": "ad_formula_mastery"},
        {"q": "power words increase social media engagement clicks CTA",  "src": "bing",   "skill": "cta_effectiveness"},
        {"q": "brand voice tone social media content strategy",           "src": "gnews",  "skill": "brand_voice_match"},
        {"q": "",  "src": "reddit",  "param": "copywriting",              "skill": "hook_mastery"},
        {"q": "",  "src": "reddit",  "param": "socialmedia",              "skill": "cta_effectiveness"},
        {"q": "",  "src": "medium",  "param": "copywriting",              "skill": "ad_formula_mastery"},
        {"q": "",  "src": "medium",  "param": "social-media-marketing",   "skill": "brand_voice_match"},
        {"q": "https://blog.hubspot.com/marketing/rss.xml",               "src": "rss",    "skill": "hook_mastery"},
        {"q": "https://buffer.com/resources/rss/",                        "src": "rss",    "skill": "cta_effectiveness"},
    ],
    "image_gen": [
        {"q": "trending visual design styles social media 2025",           "src": "gnews",  "skill": "visual_impact"},
        {"q": "best thumbnail design tips viral YouTube TikTok",          "src": "gnews",  "skill": "prompt_craft"},
        {"q": "color psychology finance crypto content dark themes",      "src": "bing",   "skill": "brand_consistency"},
        {"q": "AI generated image prompt engineering tips photography",   "src": "gnews",  "skill": "prompt_craft"},
        {"q": "",  "src": "reddit",  "param": "graphic_design",           "skill": "visual_impact"},
        {"q": "",  "src": "medium",  "param": "design",                   "skill": "prompt_craft"},
        {"q": "https://www.smashingmagazine.com/feed/",                   "src": "rss",    "skill": "visual_impact"},
        {"q": "https://www.creativebloq.com/feeds/all",                   "src": "rss",    "skill": "brand_consistency"},
    ],
    "reel_writer": [
        {"q": "viral TikTok video script hook formula storytelling 2025", "src": "gnews",  "skill": "script_structure"},
        {"q": "YouTube Shorts viral pacing script 15 seconds structure",  "src": "gnews",  "skill": "pacing_control"},
        {"q": "short video emotional storytelling audience engagement",   "src": "bing",   "skill": "emotion_arc"},
        {"q": "",  "src": "reddit",  "param": "videography",              "skill": "script_structure"},
        {"q": "",  "src": "reddit",  "param": "NewTubers",                "skill": "pacing_control"},
        {"q": "",  "src": "medium",  "param": "video-marketing",          "skill": "emotion_arc"},
        {"q": "",  "src": "medium",  "param": "content-creation",         "skill": "script_structure"},
    ],
    "publisher": [
        {"q": "best time to post on social media 2025 crypto audience",   "src": "gnews",  "skill": "platform_knowledge"},
        {"q": "X Twitter algorithm crypto accounts reach improvement",    "src": "gnews",  "skill": "platform_knowledge"},
        {"q": "Instagram TikTok algorithm engagement boost tips 2025",    "src": "gnews",  "skill": "timing_optimization"},
        {"q": "",  "src": "reddit",  "param": "socialmedia",              "skill": "platform_knowledge"},
        {"q": "",  "src": "reddit",  "param": "marketing",                "skill": "timing_optimization"},
        {"q": "https://sproutsocial.com/insights/feed/",                  "src": "rss",    "skill": "platform_knowledge"},
        {"q": "https://later.com/blog/feed/",                             "src": "rss",    "skill": "timing_optimization"},
        {"q": "https://blog.hootsuite.com/feed/",                         "src": "rss",    "skill": "platform_knowledge"},
    ],
    "trend_predictor": [
        {"q": "crypto market trend analysis forecast this week",          "src": "gnews",  "skill": "market_reading"},
        {"q": "Bitcoin Ethereum DeFi price prediction outlook 2025",      "src": "gnews",  "skill": "prediction_accuracy"},
        {"q": "blockchain adoption signal momentum tracking on-chain",    "src": "gnews",  "skill": "velocity_tracking"},
        {"q": "",  "src": "reddit",  "param": "CryptoCurrency",           "skill": "market_reading"},
        {"q": "",  "src": "reddit",  "param": "Economics",                "skill": "prediction_accuracy"},
        {"q": "https://www.coindesk.com/arc/outboundfeeds/rss/",          "src": "rss",    "skill": "velocity_tracking"},
        {"q": "https://messari.io/rss",                                   "src": "rss",    "skill": "market_reading"},
    ],
    "engagement_optimizer": [
        {"q": "social media engagement rate improvement tactics 2025",    "src": "gnews",  "skill": "platform_intelligence"},
        {"q": "crypto community engagement strategy Twitter Telegram",    "src": "gnews",  "skill": "timing_mastery"},
        {"q": "best posting format social media virality research",       "src": "gnews",  "skill": "format_intelligence"},
        {"q": "",  "src": "reddit",  "param": "marketing",                "skill": "format_intelligence"},
        {"q": "",  "src": "medium",  "param": "social-media-marketing",   "skill": "platform_intelligence"},
        {"q": "https://coschedule.com/blog/feed/",                        "src": "rss",    "skill": "timing_mastery"},
        {"q": "https://www.socialmediatoday.com/rss.xml",                 "src": "rss",    "skill": "format_intelligence"},
    ],
    "ad_creator": [
        {"q": "viral ad copy formulas social media AIDA PAS 2025",        "src": "gnews",  "skill": "ad_copywriting"},
        {"q": "best performing Facebook Instagram ad scripts conversion",  "src": "gnews",  "skill": "conversion_optimization"},
        {"q": "brand storytelling techniques high conversion ad copy",    "src": "bing",   "skill": "brand_intelligence"},
        {"q": "emotional triggers marketing persuasion psychology ads",   "src": "bing",   "skill": "audience_targeting"},
        {"q": "crypto marketing campaign strategy social media 2025",     "src": "gnews",  "skill": "campaign_strategy"},
        {"q": "",  "src": "reddit",  "param": "advertising",              "skill": "ad_copywriting"},
        {"q": "",  "src": "reddit",  "param": "PPC",                      "skill": "conversion_optimization"},
        {"q": "",  "src": "medium",  "param": "marketing",                "skill": "campaign_strategy"},
        {"q": "https://adweek.com/feed/",                                 "src": "rss",    "skill": "brand_intelligence"},
        {"q": "https://neilpatel.com/blog/feed/",                         "src": "rss",    "skill": "audience_targeting"},
    ],
    "content_personalizer": [
        {"q": "crypto audience segmentation content personalization",     "src": "gnews",  "skill": "audience_profiling"},
        {"q": "niche content targeting social media strategy 2025",       "src": "gnews",  "skill": "niche_detection"},
        {"q": "buyer persona building content marketing crypto finance",  "src": "bing",   "skill": "buyer_persona_building"},
        {"q": "",  "src": "reddit",  "param": "entrepreneur",             "skill": "audience_profiling"},
        {"q": "",  "src": "medium",  "param": "content-strategy",         "skill": "niche_detection"},
        {"q": "https://contentmarketinginstitute.com/feed/",              "src": "rss",    "skill": "buyer_persona_building"},
    ],
    "ab_tester": [
        {"q": "A/B testing social media headlines engagement 2025",       "src": "gnews",  "skill": "variant_analysis"},
        {"q": "best social media hook optimization split testing",        "src": "gnews",  "skill": "hook_optimization"},
        {"q": "content format testing engagement learning social media",  "src": "bing",   "skill": "format_learning"},
        {"q": "",  "src": "reddit",  "param": "marketing",                "skill": "hook_optimization"},
        {"q": "",  "src": "devto",   "param": "testing",                  "skill": "variant_analysis"},
        {"q": "https://coschedule.com/blog/feed/",                        "src": "rss",    "skill": "format_learning"},
    ],
    "fact_checker": [
        {"q": "crypto news fact checking verification tools journalism",  "src": "gnews",  "skill": "source_trust"},
        {"q": "financial misinformation detection accuracy 2025",         "src": "gnews",  "skill": "accuracy_radar"},
        {"q": "crypto price data volatility verification methods",        "src": "gnews",  "skill": "volatility_awareness"},
        {"q": "",  "src": "reddit",  "param": "journalism",               "skill": "source_trust"},
        {"q": "",  "src": "reddit",  "param": "CryptoCurrency",           "skill": "volatility_awareness"},
        {"q": "https://www.theblock.co/rss.xml",                          "src": "rss",    "skill": "accuracy_radar"},
    ],
    "agent_supervisor": [
        {"q": "AI agent health monitoring distributed systems production", "src": "hn",    "skill": "agent_health_monitoring"},
        {"q": "software system failure prediction reliability patterns",  "src": "hn",    "skill": "failure_prediction"},
        {"q": "automated recovery distributed agent pipeline monitoring", "src": "bing",  "skill": "auto_recovery"},
        {"q": "performance benchmarking AI pipeline systems 2025",        "src": "hn",    "skill": "performance_benchmarking"},
        {"q": "",  "src": "devto",   "param": "devops",                   "skill": "performance_benchmarking"},
        {"q": "",  "src": "devto",   "param": "ai",                       "skill": "failure_prediction"},
    ],
    "web_learner": [
        {"q": "web scraping research automation best practices 2025",     "src": "hn",    "skill": "web_research"},
        {"q": "AI knowledge extraction NLP insights automation",         "src": "hn",    "skill": "insight_extraction"},
        {"q": "",  "src": "devto",   "param": "machinelearning",          "skill": "ai_summarisation"},
        {"q": "",  "src": "devto",   "param": "ai",                       "skill": "trend_awareness"},
    ],
    "competitor_tracker": [
        {"q": "competitor analysis social media strategy 2025",           "src": "gnews", "skill": "pattern_recognition"},
        {"q": "how to track competitor content marketing performance",    "src": "hn",    "skill": "data_collection"},
        {"q": "social media benchmarking engagement metrics industry",    "src": "bing",  "skill": "engagement_benchmark"},
        {"q": "competitor hashtag strategy social media growth",          "src": "gnews", "skill": "hashtag_intelligence"},
        {"q": "content frequency posting schedule best practices 2025",  "src": "hn",    "skill": "frequency_analysis"},
        {"q": "",  "src": "rss",  "url": "https://sproutsocial.com/insights/feed/",     "skill": "pattern_recognition"},
        {"q": "",  "src": "rss",  "url": "https://buffer.com/resources/rss/",           "skill": "engagement_benchmark"},
    ],
}

_REQUEST_DELAY = 1.5  # seconds between HTTP requests to avoid rate limiting

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _fetch(url: str, timeout: int = 10) -> str | None:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; AutoPostResearch/1.0)"
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(256 * 1024)  # max 256 KB
        for enc in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                return raw.decode(enc)
            except Exception:
                continue
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Google News RSS search
# ---------------------------------------------------------------------------

def _strip_html(text: str) -> str:
    """Remove HTML tags and decode basic entities."""
    import re
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace(
        "&gt;", ">").replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text).strip()


def _search_gnews(query: str, max_results: int = 5) -> list[str]:
    from urllib.parse import quote_plus
    url = (
        "https://news.google.com/rss/search"
        f"?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    xml_text = _fetch(url)
    if not xml_text:
        return []
    # Use regex to extract <title> tags — avoids XML parse failures
    # caused by unescaped & in Google News descriptions
    import re
    titles = re.findall(r"<title><!\[CDATA\[(.+?)\]\]></title>|<title>([^<]+)</title>",
                        xml_text, re.DOTALL)
    items: list[str] = []
    for cdata, plain in titles:
        t = _strip_html((cdata or plain).strip())
        # Skip the feed-level title (first item is usually "Google News")
        if t and len(t) > 15 and "google" not in t.lower():
            items.append(t[:200])
        if len(items) >= max_results:
            break
    return items


# ---------------------------------------------------------------------------
# Bing News RSS (free, no API key — reliable fallback to Google News)
# ---------------------------------------------------------------------------

def _search_bing(query: str, max_results: int = 5) -> list[str]:
    from urllib.parse import quote_plus
    import re
    url = (
        "https://www.bing.com/news/search"
        f"?q={quote_plus(query)}&format=RSS"
    )
    xml_text = _fetch(url)
    if not xml_text:
        return []
    titles = re.findall(
        r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", xml_text, re.DOTALL)
    items: list[str] = []
    q_lower = query.lower()
    for raw in titles:
        t = _strip_html(raw.strip())
        t_lower = t.lower()
        # Skip feed-level titles (Bing, RSS channel name) and query echoes
        if not t or len(t) < 16:
            continue
        if "bing" in t_lower or t_lower.startswith(q_lower[:20]):
            continue
        items.append(t[:200])
        if len(items) >= max_results:
            break
    return items


# ---------------------------------------------------------------------------
# Hacker News Algolia (free JSON — tech / startup community)
# ---------------------------------------------------------------------------

def _search_hn(query: str, max_results: int = 5) -> list[str]:
    from urllib.parse import quote_plus
    url = (
        "https://hn.algolia.com/api/v1/search"
        f"?query={quote_plus(query)}&tags=story&hitsPerPage={max_results}"
    )
    body = _fetch(url)
    if not body:
        return []
    try:
        data = json.loads(body)
        return [
            h["title"] for h in data.get("hits", [])
            if h.get("title") and len(h["title"]) > 10
        ][:max_results]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Reddit JSON (free — no API key, week-top posts from subreddit)
# ---------------------------------------------------------------------------

def _search_reddit(subreddit: str, max_results: int = 5) -> list[str]:
    url = (
        f"https://www.reddit.com/r/{subreddit}/top.json"
        f"?t=week&limit={max_results + 2}&raw_json=1"
    )
    body = _fetch(url)
    if not body:
        return []
    try:
        data = json.loads(body)
        posts = data.get("data", {}).get("children", [])
        items: list[str] = []
        for p in posts:
            title = p.get("data", {}).get("title", "")
            title = _strip_html(title).strip()
            if title and len(title) > 10:
                items.append(title[:200])
            if len(items) >= max_results:
                break
        return items
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Medium RSS (free — latest articles by tag)
# ---------------------------------------------------------------------------

def _search_medium(tag: str, max_results: int = 5) -> list[str]:
    import re
    url = f"https://medium.com/feed/tag/{tag}"
    xml_text = _fetch(url)
    if not xml_text:
        return []
    titles = re.findall(
        r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", xml_text, re.DOTALL)
    items: list[str] = []
    seen: set[str] = set()
    tag_lower = tag.lower()
    for raw in titles:
        t = _strip_html(raw.strip())
        t_lower = t.lower()
        if not t or len(t) < 15:
            continue
        # Skip feed-level titles: "Medium", "Tag on Medium", "Stories about Tag"
        if "medium" in t_lower or t_lower == tag_lower or tag_lower in t_lower and len(t) < 40:
            continue
        if t in seen:
            continue
        seen.add(t)
        items.append(t[:200])
        if len(items) >= max_results:
            break
    return items


# ---------------------------------------------------------------------------
# DEV.to JSON API (free — tech articles by tag)
# ---------------------------------------------------------------------------

def _search_devto(tag: str, max_results: int = 5) -> list[str]:
    url = (
        f"https://dev.to/api/articles"
        f"?tag={tag}&top=7&per_page={max_results}"
    )
    body = _fetch(url)
    if not body:
        return []
    try:
        articles = json.loads(body)
        items: list[str] = []
        for a in articles:
            title = a.get("title", "")
            title = _strip_html(title).strip()
            if title and len(title) > 10:
                items.append(title[:200])
        return items[:max_results]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Generic RSS feed (curated premium blogs)
# ---------------------------------------------------------------------------

def _search_rss(url: str, max_results: int = 5) -> list[str]:
    import re
    xml_text = _fetch(url)
    if not xml_text:
        return []
    titles = re.findall(
        r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", xml_text, re.DOTALL)
    items: list[str] = []
    for raw in titles:
        t = _strip_html(raw.strip())
        if not t or len(t) < 12:
            continue
        # Skip very short feed-level titles (blog name)
        if len(t) < 25 and items:
            continue
        items.append(t[:200])
        if len(items) >= max_results:
            break
    return items


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _search(item: dict) -> list[str]:
    src   = item.get("src", "")
    q     = item.get("q", "")
    param = item.get("param", "")
    if src == "gnews":
        return _search_gnews(q)
    if src == "bing":
        return _search_bing(q)
    if src == "hn":
        return _search_hn(q)
    if src == "reddit":
        return _search_reddit(param)
    if src == "medium":
        return _search_medium(param)
    if src == "devto":
        return _search_devto(param)
    if src == "rss":
        return _search_rss(q)
    return []


# ---------------------------------------------------------------------------
# Claude Haiku summariser (optional — only if ANTHROPIC_API_KEY is valid)
# ---------------------------------------------------------------------------

def _get_ai_key() -> str:
    """Return the admin's Anthropic API key, or empty string."""
    try:
        from src.dashboard.database import get_all_users, get_user_settings
        admins = [u for u in get_all_users() if u.get("is_admin") and u.get("is_approved")]
        if admins:
            return get_user_settings(admins[0]["id"]).get("anthropic_key", "")
    except Exception:
        pass
    return ""


def _summarise_batch(
    items: list[dict],  # each: {"query": str, "skill": str, "snippets": list[str]}
    agent: str,
    api_key: str,
) -> dict[str, str]:
    """One Claude call summarises ALL queries for an agent. Returns {skill: insight}."""
    if not api_key or not api_key.startswith("sk-"):
        return {}
    try:
        import anthropic
        sections = []
        for i, item in enumerate(items, 1):
            body = "\n".join(f"  - {s}" for s in item["snippets"][:4])
            sections.append(
                f"[{i}] SKILL={item['skill']} | TOPIC={item['query']}\n{body}"
            )
        block = "\n\n".join(sections)
        prompt = (
            f"You are the learning system for AI agent '{agent}'.\n"
            f"Below are {len(items)} research topics with raw search results.\n"
            f"For EACH topic, write exactly ONE sentence (max 25 words) with the "
            f"most actionable insight the agent can use.\n\n"
            f"{block}\n\n"
            f"Reply in this exact format (one line per topic):\n"
            + "\n".join(f"[{i}] <insight>" for i in range(1, len(items) + 1))
        )
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60 * len(items),
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        result: dict[str, str] = {}
        for line in raw.splitlines():
            m = re.match(r"^\[(\d+)\]\s*(.+)", line.strip())
            if m:
                idx = int(m.group(1)) - 1
                if 0 <= idx < len(items):
                    insight = m.group(2).strip()
                    if len(insight) > 15:
                        result[items[idx]["skill"]] = f"[WEB/AI] {insight}"
        return result
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

_TODAY_KEY = "web_learned_at"


def _session_key() -> str:
    """Return a session identifier: 'YYYY-MM-DD_am' before 13:00, '_pm' after."""
    now = datetime.now()
    session = "am" if now.hour < 13 else "pm"
    return f"{now.strftime('%Y-%m-%d')}_{session}"


def run_web_learning(force: bool = False, only_agent: str = "") -> dict[str, int]:
    """Run one web-learning cycle across all (or one) agent brains.

    Runs up to twice per day: AM session (before 13:00) and PM session (after).
    Returns {agent_name: lesson_count}.
    """
    from src.agents.agent_brain import AgentBrain, AGENTS

    session_key = _session_key()   # e.g. "2026-05-05_pm"
    results: dict[str, int] = {}

    queries_map = (
        {only_agent: _WEB_QUERIES[only_agent]}
        if only_agent and only_agent in _WEB_QUERIES
        else _WEB_QUERIES
    )

    for agent_name, queries in queries_map.items():
        if agent_name not in AGENTS:
            print(f"[web_learner] {agent_name}: not in AGENTS catalogue — skip", file=sys.stderr)
            continue

        brain = AgentBrain(agent_name)

        # Skip if already learned in this session (AM or PM)
        if not force:
            last = brain.load_params().get(_TODAY_KEY, "")
            if last == session_key:
                print(f"[web_learner] {agent_name}: already learned this session ({session_key}) — skip", file=sys.stderr)
                results[agent_name] = 0
                continue

        print(f"[web_learner] === {agent_name} ===", file=sys.stderr)
        lessons      = 0
        all_snippets: list[str] = []
        ai_insights:  list[str] = []

        # --- Phase 1: collect all search results (no API calls yet) ---
        batch_items: list[dict] = []
        for q_item in queries:
            skill     = q_item["skill"]
            display_q = (q_item.get("q") or q_item.get("param", ""))[:55]

            if skill not in brain._data.get("skills", {}):
                continue

            print(f"[web_learner]   {q_item['src']}: {display_q}...", file=sys.stderr)
            snippets = _search(q_item)
            time.sleep(_REQUEST_DELAY)

            if not snippets:
                print(f"[web_learner]   -> no results", file=sys.stderr)
                continue

            all_snippets.extend(snippets)
            batch_items.append({"query": display_q, "skill": skill, "snippets": snippets})

        # --- Phase 2: ONE batch Claude call for all queries this agent ---
        ai_key = _get_ai_key()
        batch_insights = _summarise_batch(batch_items, agent_name, ai_key) if batch_items else {}
        if batch_insights:
            print(f"[web_learner]   AI batch: {len(batch_insights)} insights in 1 call", file=sys.stderr)

        # --- Phase 3: record insights ---
        for item in batch_items:
            skill    = item["skill"]
            snippets = item["snippets"]
            if skill in batch_insights:
                insight_text = batch_insights[skill]
                ai_insights.append(insight_text)
            else:
                insight_text = f"[WEB] {item['query']}: {snippets[0][:180]}"
            xp = 25 + len(snippets) * 3
            brain.record_learning(insight_text[:220], skill=skill, xp=xp)
            lessons += 1
            print(f"[web_learner]   -> +{xp} XP to {skill}", file=sys.stderr)

        # Save metadata so the brain cycle (and next run) can read them
        if all_snippets:
            brain.save_params({
                _TODAY_KEY:            session_key,
                "web_latest_snippets": all_snippets[:20],
                "web_ai_insights":     ai_insights[:10],
                "web_queries_run":     len(queries),
                "web_last_updated":    datetime.now().isoformat(timespec="seconds"),
            })

        brain.save()  # persist brain.json (XP + learnings)
        print(f"[web_learner] {agent_name}: {lessons} lesson(s) saved", file=sys.stderr)
        results[agent_name] = lessons

    total = sum(results.values())
    print(f"[web_learner] DONE — {total} total lessons across {len(results)} agent(s)", file=sys.stderr)

    # Update web_learner's own brain with a run summary so its dashboard card shows real data
    if "web_learner" in AGENTS:
        own_brain = AgentBrain("web_learner")
        agents_updated = [n for n, c in results.items() if c > 0]
        own_brain.record_learning(
            f"Daily web research complete: {total} lessons across "
            f"{len(agents_updated)} agents — {', '.join(agents_updated[:6])}",
            skill="web_research", xp=total * 2,
        )
        own_brain.save_params({
            _TODAY_KEY:             session_key,
            "web_latest_snippets":  [f"{n}: {c} lesson(s)" for n, c in results.items() if c > 0],
            "web_ai_insights":      agents_updated,
            "web_queries_run":      sum(len(v) for v in _WEB_QUERIES.values()),
            "web_last_updated":     datetime.now().isoformat(timespec="seconds"),
            "total_lessons_today":  total,
            "total_research_cycles": own_brain.load_params().get("total_research_cycles", 0) + 1,
        })
        own_brain.save()

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Run web learning cycle for agent brains")
    ap.add_argument("--force", action="store_true", help="Re-run even if already learned today")
    ap.add_argument("--agent", default="",           help="Run only for this agent name")
    args = ap.parse_args()

    res = run_web_learning(force=args.force, only_agent=args.agent)
    print("\nResults:")
    for name, count in sorted(res.items()):
        status = "✓" if count > 0 else ("skip" if count == 0 else "✗")
        print(f"  {status} {name}: {count} lesson(s)")
