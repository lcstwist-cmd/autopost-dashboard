"""Instagram Growth Engine — motor unificat pentru creșterea contului EliteMarginDesk.

Combină:
  - Meta Graph API v22 (cont propriu: metrici, audience, posts)
  - RapidAPI instagram-scraper-20251 (Reels din nișa trading/crypto)
  - Claude AI (strategii de conținut, recomandări, content briefs)

Toate funcțiile returnează date goale/fallback la erori — NICIODATĂ excepții
propagate. Dashboard-ul arată întotdeauna ceva util.

Cache: data/ig_engine/{key}.json
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ROOT      = Path(__file__).resolve().parent.parent.parent
_CACHE_DIR = _ROOT / "data" / "ig_engine"
_GRAPH     = "https://graph.facebook.com/v22.0"

# Conturi IG de top în nișa trading/finance/crypto — sursă pentru pattern analysis
_TRADING_IG_ACCOUNTS = [
    "investopedia", "tradingview", "forex.com", "cointelegraph",
    "coindesk", "binance", "thetraderlife", "forexsignals.com",
    "marketwatch", "bloomberg", "theeconomist", "wallstreetbets",
    "rich.btc", "cryptonews.com", "btcusd", "watcher.guru",
]

# Hashtag-uri relevante pentru nișa EliteMarginDesk
_TRADING_HASHTAGS = [
    "#trading", "#forex", "#crypto", "#daytrading", "#stockmarket",
    "#investimenti", "#tradingview", "#bitcoin", "#ethereum",
    "#passiveincome", "#financialfreedom", "#invest", "#trader",
    "#tradingstrategy", "#cryptotrading", "#forextrader",
]

CACHE_TTL_SHORT  = 3600      # 1h — date cont propriu
CACHE_TTL_MEDIUM = 10800     # 3h — date nișă
CACHE_TTL_LONG   = 21600     # 6h — recomandări AI
CACHE_TTL_DAILY  = 86400     # 24h — growth history


# ─────────────────────────────────────────────────────────────────────────────
# Cache helpers
# ─────────────────────────────────────────────────────────────────────────────

def _cache_path(key: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"{key}.json"


def _cache_read(key: str, ttl: int) -> Any | None:
    p = _cache_path(key)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if time.time() - raw.get("_ts", 0) < ttl:
            return raw.get("data")
    except Exception:
        pass
    return None


def _cache_write(key: str, data: Any) -> None:
    try:
        _cache_path(key).write_text(
            json.dumps({"_ts": time.time(), "data": data},
                       ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Meta Graph API helpers
# ─────────────────────────────────────────────────────────────────────────────

def _graph_get(path: str, params: dict | None = None) -> dict:
    import requests as _r
    tok = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    uid = os.environ.get("IG_USER_ID", "").strip()
    if not tok or not uid:
        return {}
    p = {"access_token": tok, **(params or {})}
    try:
        resp = _r.get(f"{_GRAPH}/{path}", params=p, timeout=15)
        d = resp.json()
        if "error" in d:
            print(f"[ig_engine] Graph API error on /{path}: {d['error'].get('message','')}")
            return {}
        return d
    except Exception as e:
        print(f"[ig_engine] Graph API request failed: {e}")
        return {}


def _sum_values(items: list[dict]) -> int:
    total = 0
    for item in items:
        for v in item.get("values", []):
            total += int(v.get("value", 0) or 0)
    return total


def _daily_series(items: list[dict]) -> list[dict]:
    for item in items:
        return [
            {"date": v["end_time"][:10], "value": int(v.get("value", 0) or 0)}
            for v in item.get("values", [])
        ]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# 1. Cont propriu — metrici Meta Graph API
# ─────────────────────────────────────────────────────────────────────────────

def get_own_account(force: bool = False) -> dict[str, Any]:
    """Date cont propriu: urmăritori, views, reach, engagement."""
    cached = None if force else _cache_read("own_account", CACHE_TTL_SHORT)
    if cached:
        return cached

    uid = os.environ.get("IG_USER_ID", "").strip()
    if not uid:
        return _empty_account()

    profile = _graph_get(uid, {
        "fields": "id,username,name,biography,followers_count,media_count,profile_picture_url,website"
    })
    if not profile:
        return _empty_account()

    since = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
    until = int(datetime.now(timezone.utc).timestamp())

    def _insight(metric: str) -> list[dict]:
        r = _graph_get(f"{uid}/insights", {
            "metric": metric, "period": "day",
            "since": since, "until": until,
        })
        return r.get("data", [])

    views_d    = _insight("views")
    reach_d    = _insight("reach")
    engaged_d  = _insight("accounts_engaged")
    follower_d = _insight("follower_count")

    # Metrici per post (top 20)
    posts = get_own_posts(force=force)
    total_eng = sum((p.get("likes", 0) + p.get("comments", 0)) for p in posts[:12])
    followers  = profile.get("followers_count", 0)
    er = round(total_eng / max(len(posts[:12]), 1) / max(followers, 1) * 100, 2) if posts else 0.0

    result: dict = {
        "connected":          True,
        "username":           profile.get("username", ""),
        "name":               profile.get("name", ""),
        "bio":                profile.get("biography", ""),
        "followers":          followers,
        "media_count":        profile.get("media_count", 0),
        "profile_pic":        profile.get("profile_picture_url", ""),
        "website":            profile.get("website", ""),
        "engagement_rate":    er,
        "views_30d":          _sum_values(views_d),
        "reach_30d":          _sum_values(reach_d),
        "accounts_engaged_30d": _sum_values(engaged_d),
        "follower_growth_30d":  _sum_values(follower_d),
        "views_series":       _daily_series(views_d),
        "reach_series":       _daily_series(reach_d),
        "fetched_at":         datetime.now(timezone.utc).isoformat(),
    }

    _cache_write("own_account", result)
    return result


def _empty_account() -> dict:
    return {
        "connected": False, "username": "", "name": "", "bio": "",
        "followers": 0, "media_count": 0, "profile_pic": "", "website": "",
        "engagement_rate": 0.0, "views_30d": 0, "reach_30d": 0,
        "accounts_engaged_30d": 0, "follower_growth_30d": 0,
        "views_series": [], "reach_series": [], "fetched_at": "",
    }


def get_own_posts(limit: int = 20, force: bool = False) -> list[dict]:
    """Postările proprii cu metrici per-post."""
    cached = None if force else _cache_read("own_posts", CACHE_TTL_SHORT)
    if cached is not None:
        return cached

    uid = os.environ.get("IG_USER_ID", "").strip()
    if not uid:
        return []

    media = _graph_get(f"{uid}/media", {
        "fields": "id,caption,media_type,thumbnail_url,media_url,permalink,timestamp,like_count,comments_count",
        "limit":  limit,
    })

    posts = []
    for m in media.get("data", []):
        p: dict = {
            "id":        m.get("id", ""),
            "caption":   (m.get("caption") or "")[:200],
            "type":      m.get("media_type", "IMAGE"),
            "thumbnail": m.get("thumbnail_url") or m.get("media_url", ""),
            "url":       m.get("permalink", ""),
            "date":      m.get("timestamp", "")[:10],
            "likes":     int(m.get("like_count") or 0),
            "comments":  int(m.get("comments_count") or 0),
            "views":     0, "reach": 0, "saves": 0, "watch_time_ms": 0,
        }
        # Insights per post
        metrics = "views,reach,saved"
        if p["type"] in ("REEL", "VIDEO"):
            metrics += ",ig_reels_avg_watch_time"
        ins = _graph_get(f"{m['id']}/insights", {"metric": metrics})
        for item in ins.get("data", []):
            name = item.get("name", "")
            val  = (item.get("values") or [{}])[0].get("value", 0) or item.get("value", 0)
            if name == "views":      p["views"]         = int(val or 0)
            elif name == "reach":    p["reach"]         = int(val or 0)
            elif name == "saved":    p["saves"]         = int(val or 0)
            elif name == "ig_reels_avg_watch_time":
                                     p["watch_time_ms"] = int(val or 0)

        reach = max(p["reach"], 1)
        p["engagement_rate"] = round((p["likes"] + p["comments"] + p["saves"]) / reach * 100, 2)
        posts.append(p)

    posts.sort(key=lambda x: x["views"], reverse=True)
    _cache_write("own_posts", posts)
    return posts


def get_audience(force: bool = False) -> dict[str, Any]:
    """Demografice audiență: țări, orașe, vârstă, gen."""
    cached = None if force else _cache_read("audience", CACHE_TTL_SHORT)
    if cached:
        return cached

    uid = os.environ.get("IG_USER_ID", "").strip()
    if not uid:
        return {}

    def _lifetime(metric: str) -> dict:
        r = _graph_get(f"{uid}/insights", {"metric": metric, "period": "lifetime"})
        for item in r.get("data", []):
            return (item.get("values") or [{}])[0].get("value", {})
        return {}

    countries  = _lifetime("audience_country")
    cities     = _lifetime("audience_city")
    gender_age = _lifetime("audience_gender_age")

    top_countries = sorted(countries.items(), key=lambda x: x[1], reverse=True)[:12]
    top_cities    = sorted(cities.items(),    key=lambda x: x[1], reverse=True)[:8]

    male = female = 0
    age_groups: dict[str, int] = {}
    for key, val in gender_age.items():
        parts = key.split(".")
        if len(parts) == 2:
            g, age = parts
            if g == "M":   male   += val
            elif g == "F": female += val
            age_groups[age] = age_groups.get(age, 0) + val

    result = {
        "countries":  [{"code": c, "count": n} for c, n in top_countries],
        "cities":     [{"city": c, "count": n} for c, n in top_cities],
        "gender":     {"male": male, "female": female},
        "age_groups": [{"range": a, "count": n}
                       for a, n in sorted(age_groups.items(), key=lambda x: x[1], reverse=True)],
    }
    _cache_write("audience", result)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 2. Snapshot zilnic urmăritori (apelat de scheduler)
# ─────────────────────────────────────────────────────────────────────────────

def record_daily_snapshot() -> None:
    """Salvează numărul de urmăritori azi (apelat la 23:55 de scheduler)."""
    uid = os.environ.get("IG_USER_ID", "").strip()
    if not uid:
        return
    profile = _graph_get(uid, {"fields": "followers_count"})
    count = int(profile.get("followers_count") or 0)
    if not count:
        return

    p = _CACHE_DIR / "growth_history.json"
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    history: list[dict] = []
    if p.exists():
        try:
            history = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for entry in history:
        if entry.get("date") == today:
            entry["followers"] = count
            break
    else:
        history.append({"date": today, "followers": count})

    history = sorted(history, key=lambda x: x["date"])[-90:]
    p.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def get_growth_history() -> list[dict]:
    p = _CACHE_DIR / "growth_history.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# 3. Conținut viral din nișă (RapidAPI — public IG Reels)
# ─────────────────────────────────────────────────────────────────────────────

def get_niche_viral_reels(niche: str = "crypto", limit: int = 30,
                          force: bool = False) -> list[dict]:
    """Reels-uri virale din nișa trading/crypto via RapidAPI.
    Returnează lista sortată după scor de viralitate (views + likes*5 + comments*10).
    """
    cache_key = f"niche_reels_{niche}"
    cached = None if force else _cache_read(cache_key, CACHE_TTL_MEDIUM)
    if cached is not None:
        return cached

    records: list[dict] = []
    try:
        from src.agents.viral_analyzer import fetch_instagram
        records = fetch_instagram("trading crypto finance", limit=limit, niche=niche)
    except Exception as e:
        print(f"[ig_engine] niche reels fetch failed: {e}")

    # Adaugă scor viral și normalizare
    for r in records:
        views    = int(r.get("views", 0) or 0)
        likes    = int(r.get("likes", 0) or 0)
        comments = int(r.get("comments", 0) or 0)
        r["viral_score"] = views + likes * 5 + comments * 10
        # Estimare engagement rate
        r["engagement_rate"] = round((likes + comments) / max(views, 1) * 100, 2)

    records.sort(key=lambda x: x.get("viral_score", 0), reverse=True)
    records = records[:limit]
    _cache_write(cache_key, records)
    return records


def get_competitor_reels(competitor_handles: list[str],
                         force: bool = False) -> list[dict]:
    """Reels-uri de pe conturile competitoare specificate de utilizator."""
    if not competitor_handles:
        return []

    cache_key = "competitor_reels_" + "_".join(sorted(competitor_handles))[:40]
    cached = None if force else _cache_read(cache_key, CACHE_TTL_MEDIUM)
    if cached is not None:
        return cached

    key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if not key:
        return []

    import requests as _r

    _HOST = "instagram-scraper-20251.p.rapidapi.com"
    headers = {
        "x-rapidapi-key":  key,
        "x-rapidapi-host": _HOST,
    }

    records: list[dict] = []
    for handle in competitor_handles[:6]:
        username = handle.lstrip("@")
        try:
            resp = _r.get(
                f"https://{_HOST}/userposts/",
                headers=headers,
                params={"username_or_id": username},
                timeout=12,
            )
            if resp.status_code != 200:
                continue
            items = resp.json().get("data", {}).get("items") or []
            for it in items[:10]:
                if not isinstance(it, dict):
                    continue
                if it.get("media_type") != 2:  # numai video/Reels
                    continue
                cap_obj = it.get("caption") or {}
                caption = (cap_obj.get("text", "") if isinstance(cap_obj, dict)
                           else str(cap_obj or ""))
                code    = it.get("code") or str(it.get("id") or "")
                views   = int(it.get("play_count") or it.get("view_count") or 0)
                likes   = int(it.get("like_count") or 0)
                comments= int(it.get("comment_count") or 0)
                duration= float(it.get("video_duration") or 0)
                records.append({
                    "platform":       "instagram",
                    "account":        username,
                    "id":             str(it.get("id") or code),
                    "caption":        caption[:300],
                    "duration":       duration,
                    "views":          views,
                    "likes":          likes,
                    "comments":       comments,
                    "viral_score":    views + likes * 5 + comments * 10,
                    "engagement_rate": round((likes + comments) / max(views, 1) * 100, 2),
                    "tags":           _extract_tags(caption),
                    "url":            f"https://www.instagram.com/reel/{code}/",
                })
        except Exception as e:
            print(f"[ig_engine] competitor {handle} failed: {e}")

    records.sort(key=lambda x: x.get("viral_score", 0), reverse=True)
    _cache_write(cache_key, records)
    return records


def _extract_tags(text: str) -> list[str]:
    import re
    return re.findall(r"#\w+", text or "")[:10]


# ─────────────────────────────────────────────────────────────────────────────
# 4. Analiza pattern-urilor virale + strategii AI
# ─────────────────────────────────────────────────────────────────────────────

def analyze_viral_patterns(reels: list[dict]) -> dict[str, Any]:
    """Extrage pattern-uri din Reels-urile virale: durată, hashtag-uri, hook-uri."""
    if not reels:
        return {
            "avg_duration": 0, "top_hashtags": [], "hook_words": [],
            "avg_views": 0, "avg_engagement_rate": 0, "best_duration_range": "15-30s",
        }

    durations = [r.get("duration", 0) for r in reels if r.get("duration")]
    views     = [r.get("views", 0) for r in reels if r.get("views")]
    ers       = [r.get("engagement_rate", 0) for r in reels if r.get("engagement_rate")]

    # Top hashtag-uri
    from collections import Counter
    all_tags: list[str] = []
    for r in reels:
        all_tags.extend(r.get("tags", []))
    top_tags = [t for t, _ in Counter(all_tags).most_common(10)]

    # Hook words din caption-uri
    import re
    hook_patterns = ["nu știai", "secretul", "greșeala", "adevărul", "nimeni",
                     "motivul", "cum să", "strategy", "mistake", "secret",
                     "nobody", "truth", "never", "always", "the real", "stop",
                     "alert", "warning", "breaking", "hack", "trick"]
    captions = " ".join(r.get("caption", "") or r.get("title", "") for r in reels).lower()
    found_hooks = [h for h in hook_patterns if h in captions]

    avg_dur = round(sum(durations) / len(durations), 1) if durations else 0
    if avg_dur < 15:
        best_range = "sub 15s"
    elif avg_dur < 30:
        best_range = "15-30s"
    elif avg_dur < 60:
        best_range = "30-60s"
    else:
        best_range = "60-90s"

    return {
        "avg_duration":       avg_dur,
        "best_duration_range": best_range,
        "top_hashtags":       top_tags if top_tags else _TRADING_HASHTAGS[:8],
        "hook_words":         found_hooks[:6],
        "avg_views":          int(sum(views) / len(views)) if views else 0,
        "avg_engagement_rate": round(sum(ers) / len(ers), 2) if ers else 0,
        "total_analyzed":     len(reels),
    }


def generate_content_briefs(own_account: dict, viral_reels: list[dict],
                            patterns: dict, force: bool = False) -> list[dict]:
    """Generează brief-uri de conținut specifice bazate pe pattern-urile virale."""
    cached = None if force else _cache_read("content_briefs", CACHE_TTL_LONG)
    if cached:
        return cached

    try:
        import anthropic
        client = anthropic.Anthropic()
    except Exception:
        return _fallback_briefs()

    top_reels_summary = "\n".join(
        f"- {r.get('views',0):,} views | {r.get('likes',0):,} likes | "
        f"{r.get('comments',0):,} comments | "
        f"durată: {r.get('duration','?')}s | "
        f"caption: {(r.get('caption') or r.get('title',''))[:100]}"
        for r in (viral_reels or [])[:8]
    ) or "Nicio dată disponibilă"

    prompt = f"""Ești expert în creșterea conturilor Instagram pentru nișa trading/crypto/educație financiară.
Contul analizat: @{own_account.get('username','EliteMarginDesk')}
Urmăritori: {own_account.get('followers',0):,} | Engagement rate: {own_account.get('engagement_rate',0)}%

Top Reels virale din nișa noastră (analizate acum):
{top_reels_summary}

Pattern-uri identificate:
- Durată medie optimă: {patterns.get('best_duration_range','15-30s')}
- Top hashtag-uri: {', '.join(patterns.get('top_hashtags',[])[:6])}
- Hook-uri care funcționează: {', '.join(patterns.get('hook_words',[])[:4]) or 'secretul, greșeala, adevărul'}

Generează 5 brief-uri de conținut SPECIFICE și ACȚIONABILE pentru @EliteMarginDesk.
Fiecare brief trebuie să copieze CE FUNCȚIONEAZĂ din datele de mai sus.

Pentru fiecare brief:
- title: titlu scurt al videoclipului (max 8 cuvinte)
- hook: primul rând din caption / primele 3 secunde video (ce spui/arăți ca să oprești scroll-ul)
- concept: descriere 2 rânduri — exact ce filmezi și cum
- duration: durata recomandată în secunde (ex: "20s", "45s")
- caption: caption complet Instagram (incluzând emoji și hashtag-uri)
- format: "Reel" / "Carousel" / "Story"
- priority: "urgent" / "high" / "medium"
- why_it_works: 1 propoziție — de ce va performa bine conform datelor

Răspunde DOAR cu JSON array valid, fără markdown:
[{{"title":"...","hook":"...","concept":"...","duration":"...","caption":"...","format":"...","priority":"...","why_it_works":"..."}}]"""

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json\n"):
                raw = raw[5:]
        briefs = json.loads(raw)
        _cache_write("content_briefs", briefs)
        return briefs
    except Exception as e:
        print(f"[ig_engine] briefs generation failed: {e}")
        return _fallback_briefs()


def _fallback_briefs() -> list[dict]:
    return [
        {
            "title":        "Cea mai mare greșeală în trading",
            "hook":         "90% din traderi fac asta și pierd bani 🚨",
            "concept":      "Filmare față la cameră, 20-25s. Explici greșeala #1 (overtrading) cu o grafică simplă în spate.",
            "duration":     "25s",
            "caption":      "90% din traderi fac asta și pierd bani 🚨\n\nOvertrading-ul este motivul #1 pentru care conturile se golesc.\n\nSalvează asta dacă vrei să nu mai faci aceeași greșeală ✅\n\n#trading #forex #crypto #daytrading #investitii #tradingstrategy",
            "format":       "Reel",
            "priority":     "urgent",
            "why_it_works": "Conținut 'greșeală' generează de 3x mai multe salvări și comentarii decât tutoriale.",
        },
        {
            "title":        "Regula 1% care schimbă totul",
            "hook":         "Această regulă simplă mi-a salvat contul de trading 📈",
            "concept":      "Screen recording pe TradingView + voiceover. Arăți cum limitezi riscul la 1% per trade cu un exemplu real.",
            "duration":     "30s",
            "caption":      "Această regulă simplă mi-a salvat contul de trading 📈\n\nRisci maxim 1% din capital per trade.\nNimic mai mult.\n\nPare simplu dar 95% nu o respectă 👇\n\nCommentează '1%' dacă vrei să ți trimit strategia completă!\n\n#riskmanagement #trading #forex #investitii #crypto",
            "format":       "Reel",
            "priority":     "urgent",
            "why_it_works": "CTA cu comentariu specific crește engagementul cu 60% și algoritmul îl pushează mai mult.",
        },
        {
            "title":        "3 indicatori pe care îi folosesc zilnic",
            "hook":         "Eu folosesc doar 3 indicatori. Iată care sunt 👇",
            "concept":      "Carousel 6 slide-uri. Slide 1: hook. Slide 2-4: câte un indicator cu screenshot real. Slide 5: cum le combini. Slide 6: CTA.",
            "duration":     "Carousel",
            "caption":      "Eu folosesc doar 3 indicatori. Iată care sunt 👇\n\nMulți traderi complică prea mult analiza.\nEu am simplificat totul la 3 tool-uri.\n\nSalvează pentru data viitoare când analizezi un trade ✅\n\n#technicalanalysis #trading #forex #tradingstrategy #investitii",
            "format":       "Carousel",
            "priority":     "high",
            "why_it_works": "Caruselele educaționale sunt salvate de 4x mai des — algoritmul le recompensează cu reach organic.",
        },
        {
            "title":        "Ce fac în primele 5 minute dimineața",
            "hook":         "Rutina mea de dimineață de trader (5 min) ☕",
            "concept":      "POV vlog-style, 20-30s. Arăți rapid: deschizi charts, verifici news, setezi alertele. Muzică lo-fi în fundal.",
            "duration":     "25s",
            "caption":      "Rutina mea de trading de dimineață ☕ (doar 5 minute)\n\n1️⃣ Verifici piețele deschise\n2️⃣ Citești news-ul economic\n3️⃣ Setezi alertele pentru ziua de azi\n\nAtât. Nu mai mult.\n\nCe faci tu dimineața? Spune jos 👇\n\n#traderroutine #trading #forex #morning #daytrader",
            "format":       "Reel",
            "priority":     "high",
            "why_it_works": "Conținut 'zi din viața unui trader' generează identificare și comentarii personale — semnale puternice pentru algoritm.",
        },
        {
            "title":        "Myth busted: poți trăi din trading?",
            "hook":         "Poți trăi din trading? Răspunsul sincer 🤔",
            "concept":      "Talking head 30-40s. Raspuns onest: da, dar nu cum crezi. Explici ce realist înseamnă (ani de practică, capital necesar, risc).",
            "duration":     "35s",
            "caption":      "Poți trăi din trading? Răspunsul sincer 🤔\n\nDa — dar nu după 2 luni de cursuri online.\n\nAdevărul pe care nimeni nu ți-l spune:\n• 3-5 ani de practică constantă\n• Capital de minim 10,000€\n• Gestionarea emoțiilor > tehnica\n\nSuntem de acord? Spune în comentarii 👇\n\n#trading #realitate #forex #investitii #financialfreedom",
            "format":       "Reel",
            "priority":     "medium",
            "why_it_works": "Conținut 'myth busted' polarizează — generează comentarii pro și contra, exact ce vrea algoritmul.",
        },
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 5. Plan de postare 7 zile
# ─────────────────────────────────────────────────────────────────────────────

def generate_weekly_plan(briefs: list[dict], own_posts: list[dict]) -> list[dict]:
    """Generează un calendar de postare pentru 7 zile."""
    today = datetime.now(timezone.utc)

    # Calculează cel mai bun slot orar din postările proprii
    best_hours = _best_posting_hours(own_posts)
    primary_hour   = best_hours[0] if best_hours else 18
    secondary_hour = best_hours[1] if len(best_hours) > 1 else 20

    plan = []
    brief_idx = 0
    for day_offset in range(7):
        day = today + timedelta(days=day_offset)
        day_name = ["Luni", "Marți", "Miercuri", "Joi", "Vineri", "Sâmbătă", "Duminică"][day.weekday()]

        # Luni-Vineri: 2 posturi/zi; Weekend: 1 post/zi
        slots = [
            {"time": f"{primary_hour:02d}:00", "type": "Reel principal"},
            {"time": f"{secondary_hour:02d}:00", "type": "Story / Carousel"},
        ] if day.weekday() < 5 else [
            {"time": f"{primary_hour:02d}:00", "type": "Reel"},
        ]

        for slot in slots:
            brief = briefs[brief_idx % len(briefs)] if briefs else {}
            plan.append({
                "day":     day_name,
                "date":    day.strftime("%Y-%m-%d"),
                "time":    slot["time"],
                "slot":    slot["type"],
                "title":   brief.get("title", "Post planificat"),
                "format":  brief.get("format", "Reel"),
                "priority": brief.get("priority", "medium"),
            })
            brief_idx += 1

    return plan


def _best_posting_hours(own_posts: list[dict]) -> list[int]:
    """Identifică orele cu cel mai mare engagement din postările proprii."""
    from collections import defaultdict
    hour_eng: dict[int, list[float]] = defaultdict(list)
    for p in own_posts:
        ts = p.get("date", "")
        if not ts:
            continue
        try:
            hour = int(ts[11:13]) if len(ts) > 13 else None
        except (ValueError, TypeError):
            continue
        if hour is not None:
            hour_eng[hour].append(p.get("engagement_rate", 0))

    if not hour_eng:
        return [18, 20, 9]  # default pentru România

    avg_by_hour = {h: sum(vals) / len(vals) for h, vals in hour_eng.items()}
    return [h for h, _ in sorted(avg_by_hour.items(), key=lambda x: x[1], reverse=True)][:3]


# ─────────────────────────────────────────────────────────────────────────────
# 6. Content Kit
# ─────────────────────────────────────────────────────────────────────────────

def get_content_kit() -> list[dict]:
    """Lista asset-urilor EliteMarginDesk din data/content_kit/."""
    kit_dir = _ROOT / "data" / "content_kit"
    if not kit_dir.exists():
        return []

    items: list[dict] = []
    carousel_dirs: set[Path] = set()

    for f in sorted(kit_dir.rglob("*")):
        if f.is_dir():
            continue
        ext = f.suffix.lower()
        if ext in (".mp4", ".mov"):
            media_type = "REEL"
        elif ext in (".jpg", ".jpeg", ".png"):
            parent = f.parent
            if parent.name.lower() in ("carousel", "slides"):
                carousel_dirs.add(parent)
                continue
            media_type = "IMAGE"
        else:
            continue

        items.append({
            "name":       f.stem.replace("_", " ").replace("-", " "),
            "file":       f.name,
            "path":       str(f.relative_to(_ROOT)),
            "media_type": media_type,
            "size_kb":    round(f.stat().st_size / 1024),
        })

    for d in carousel_dirs:
        slides = list(d.glob("*.png")) + list(d.glob("*.jpg")) + list(d.glob("*.jpeg"))
        if slides:
            items.append({
                "name":       d.name.replace("_", " ").title(),
                "file":       f"{len(slides)} slide-uri",
                "path":       str(d.relative_to(_ROOT)),
                "media_type": "CAROUSEL",
                "size_kb":    sum(round(s.stat().st_size / 1024) for s in slides),
                "slides":     len(slides),
            })

    return items


# ─────────────────────────────────────────────────────────────────────────────
# 7. Full refresh — apelat de butonul "Actualizează"
# ─────────────────────────────────────────────────────────────────────────────

def full_refresh(niche: str = "crypto",
                 competitor_handles: list[str] | None = None) -> dict[str, Any]:
    """Actualizează toate datele din sursele externe."""
    results: dict[str, Any] = {"ok": True, "errors": [], "refreshed": []}

    for fn, key in [
        (lambda: get_own_account(force=True),           "own_account"),
        (lambda: get_own_posts(force=True),             "own_posts"),
        (lambda: get_audience(force=True),              "audience"),
        (lambda: get_niche_viral_reels(niche, force=True), "niche_reels"),
    ]:
        try:
            fn()
            results["refreshed"].append(key)
        except Exception as e:
            results["errors"].append(f"{key}: {e}")
            results["ok"] = False

    if competitor_handles:
        try:
            get_competitor_reels(competitor_handles, force=True)
            results["refreshed"].append("competitor_reels")
        except Exception as e:
            results["errors"].append(f"competitor_reels: {e}")

    try:
        record_daily_snapshot()
        results["refreshed"].append("snapshot")
    except Exception as e:
        results["errors"].append(f"snapshot: {e}")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# 8. Date complete pentru pagina principală
# ─────────────────────────────────────────────────────────────────────────────

def load_dashboard_data(niche: str = "crypto",
                        competitor_handles: list[str] | None = None) -> dict[str, Any]:
    """Returnează TOATE datele necesare pentru instagram_growth.html.
    Nicio excepție nu se propagă afară — întotdeauna returnează un dict valid.
    """
    own_account  = get_own_account()
    own_posts    = get_own_posts()
    audience     = get_audience()
    growth       = get_growth_history()
    niche_reels  = get_niche_viral_reels(niche)
    comp_reels   = get_competitor_reels(competitor_handles or [])
    all_reels    = (niche_reels + comp_reels)[:40]
    patterns     = analyze_viral_patterns(all_reels)
    briefs       = generate_content_briefs(own_account, all_reels, patterns)
    weekly_plan  = generate_weekly_plan(briefs, own_posts)
    kit          = get_content_kit()

    return {
        "own_account":    own_account,
        "own_posts":      own_posts[:12],
        "audience":       audience,
        "growth_history": growth,
        "niche_reels":    niche_reels[:15],
        "comp_reels":     comp_reels[:10],
        "patterns":       patterns,
        "briefs":         briefs[:5],
        "weekly_plan":    weekly_plan,
        "content_kit":    kit,
        "hashtags":       patterns.get("top_hashtags") or _TRADING_HASHTAGS[:10],
    }
