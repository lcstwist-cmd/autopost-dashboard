# CLAUDE.md — AutoPost Dashboard / EliteMarginDesk Memory

> Acest fișier este memoria proiectului. Claude îl citește la fiecare sesiune nouă.
> Actualizat: 2026-06-23 | Surse verificate de pe internet.

---

## 1. Ce este proiectul

**AutoPost Dashboard** — platformă multi-tenant de content marketing AI pentru contul
Instagram/X/Telegram **@EliteMarginDesk** (nișă: trading, crypto, educație financiară).

- **Entrypoint**: `src/dashboard/app.py` (FastAPI, ~5100 linii)
- **Baza de date**: SQLite la `data/autopost.db` via `src/dashboard/database.py`
- **Pornire**: `uvicorn src.dashboard.app:app --host 0.0.0.0 --port 8000`
- **Deployment**: Railway (`railway.toml`, `Procfile`)
- **Branch de dezvoltare activ**: `claude/instagram-growth-analytics-6aup6r`

---

## 2. Stack tehnic

| Strat | Tehnologie | Versiune |
|---|---|---|
| Backend | Python | 3.11.15 |
| Web framework | FastAPI | ≥ 0.111 |
| ASGI server | Uvicorn | standard |
| Template engine | Jinja2 | 3.1.6 |
| Frontend CSS | Tailwind CSS Play CDN | **v3** (local `/static/tailwind.js`) |
| Design system propriu | `elite.css` | v3 "Neon Matrix" |
| AI (recomandări) | Anthropic Python SDK | ≥ 0.39 |
| HTTP client | requests | 2.33 |
| Scheduler | `schedule` | cron-style |
| Video | moviepy, edge-tts, Pillow | — |
| Social | tweepy (X), python-telegram-bot | — |
| Browser automation | playwright | — |

### Tailwind CSS — versiunea din proiect
Proiectul folosește **Tailwind v3 (Play CDN)** servit local ca `/static/tailwind.js`.
**NU** schimba la v4. V4 are CDN diferit (`@tailwindcss/browser@4`) și sintaxă incompatibilă.
Dacă adaugi pagini noi, folosește `<script src="/static/tailwind.js"></script>`.

---

## 3. Sistem de design — "Neon Matrix" (elite.css)

**Fișier**: `src/dashboard/static/elite.css` — importat în paginile noi ca:
```html
<link rel="stylesheet" href="/static/elite.css">
<script src="/static/tailwind.js"></script>
```

### Paleta de culori (CSS variables)
```css
--bg:        #020806;          /* fundal pagină */
--surface:   rgba(4,13,7,.95); /* card background */
--border:    rgba(0,255,120,.1);
--border-h:  rgba(0,255,120,.38);
--neon:      #00ff7a;          /* verde neon — accent primar */
--cyan:      #06d6a0;          /* accent secundar */
--gold:      #fbbf24;          /* accent premium */
--red:       #f87171;
--purple:    #c084fc;
--text:      #dff7e8;          /* text principal */
--muted:     #3d7a55;          /* text secundar */
--dim:       #1a3d26;          /* text dezactivat */
```

### Clase de componente (din elite.css, preferă-le față de Tailwind inline)
```
.ecard          — card standard cu border neon + hover glow
.ecard-hi       — card evidențiat (border mai vizibil)
.estat          — stat card cu blob decorativ
.estat-val      — număr mare în stat card (.blue / .gold / .cyan / .neon)
.ebtn           — buton de bază
.ebtn-primary   — buton verde principal
.ebtn-gold      — buton gold premium
.ebtn-ghost     — buton transparent cu border
.ebtn-danger    — buton roșu
.ebtn-sm / .ebtn-lg — sizing variante
.einput         — input field stilizat
.elabel         — label uppercase pentru form
.epill          — badge/pill inline
.epill-blue/gold/cyan/red/muted
.edot           — status dot animat (.edot-live / .edot-gold / .edot-red)
.eprogress      — progress bar container
.eprogress-fill — fill (.gold variant disponibil)
.etabs / .etab  — tab navigation
.esec-title     — section title cu linie decorativă
.ealert-ok/warn/err/info — notificări
.hud            — container cu colțuri HUD decorative
.grid-floor     — efect de perspectivă 3D ambient
.snav-item      — sidebar nav link
.main-wrap      — content area (margin-left: 200px pentru sidebar)
```

### Structura sidebar (_sidebar.html)
Toate paginile cu sidebar includ:
```jinja2
{% include "_sidebar.html" %}
<div class="main-wrap">
  <main class="p-5 md:p-8 ...">
    ...
  </main>
</div>
```
Sidebar-ul necesită `current_user` și opțional `pending_count` în context.

---

## 4. Arhitectura aplicației

### Auth / multi-tenant
- Session cookie: `ap_session` (30 zile)
- Fiecare user are un folder de queue: `queue/{user_id}/`
- `request.state.user` — injectat de middleware pe orice rută protejată
- `_uenv(settings)` — context manager care setează variabilele de mediu ale userului
  (IG_USER_ID, IG_ACCESS_TOKEN, X_API_KEY, etc.) izolat per request
- `_user_ctx(request)` — returnează `{current_user, pending_count}` pentru template

### Adăugare rută nouă — pattern standard
```python
@app.get("/my-page", response_class=HTMLResponse)
async def my_page(request: Request):
    settings = get_user_settings(request.state.user["id"])
    return _render("my_page.html", **_user_ctx(request),
                   some_data=..., user=request.state.user)
```

### Template HTML nou — structura minimă
```html
<!DOCTYPE html>
<html lang="ro">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Titlu — EliteMarginDesk</title>
  <link rel="stylesheet" href="/static/elite.css">
  <script src="/static/tailwind.js"></script>
</head>
<body>
{% include "_sidebar.html" %}
<div class="main-wrap">
  <main class="p-6 md:p-8 space-y-6 max-w-7xl mx-auto relative z-10">
    <!-- conținut -->
  </main>
</div>
<script src="/static/i18n.js"></script>
</body>
</html>
```

### Cache și date
- Date în `data/` (ignorat de git, nu comite!)
- Cache Instagram analytics: `data/ig_analytics/*.json` (TTL 1h)
- Viral blueprint: `data/viral_blueprint.json`
- Content kit: `data/content_kit/` (videos, carousel, images)

---

## 5. Instagram Graph API — Stare 2026 (CRITIC)

**Versiune curentă**: v22.0 (April 2025+)
**Base URL**: `https://graph.facebook.com/v22.0/`
**Rate limit**: 200 cereri/oră/cont Instagram conectat

### Credențiale necesare în .env
```
IG_USER_ID=<instagram business account id>
IG_ACCESS_TOKEN=<long-lived page access token — NU EXPIRĂ>
META_APP_ID=<facebook app id>
META_APP_SECRET=<facebook app secret>
```
Setup token: `python src/agents/instagram_oauth_setup.py`

### Permisiuni OAuth necesare
```
instagram_basic
instagram_graph_user_profile
instagram_manage_insights
pages_show_list
pages_read_engagement
business_management
```

### Metrici VALIDE pentru user insights (GET /{ig-user-id}/insights)
```
reach           — conturi unice care au văzut orice conținut (period: day/week/month)
views           — total vizualizări (înlocuiește impressions — deprecated)
accounts_engaged — conturi care au interacționat (period: day/week/month)
follower_count  — creștere/scădere urmăritori zilnică (period: day)
```
Metrici lifetime (period=lifetime) pentru demografice:
```
audience_country      — distribuție pe țări
audience_city         — distribuție pe orașe
audience_gender_age   — distribuție gen + vârstă (ex: "M.25-34": 1234)
```

### Metrici VALIDE pentru media insights (GET /{media-id}/insights)
```
views              — vizualizări totale (înlocuiește impressions + video_views)
reach              — conturi unice
saved              — salvări
shares             — distribuiri
total_interactions — likes + comments + saves + shares (aggregate)
comments           — (unele endpoints)
likes              — (unele endpoints)
```
Pentru **Reels** specific:
```
ig_reels_avg_watch_time       — timp mediu de vizionare (ms)
ig_reels_video_view_total_time — timp total de vizionare (ms)
```
Pentru **Stories** specific:
```
link_clicks, navigation, replies
```

### Metrici DEPRECATE (nu le mai folosi — returnează eroare!)
```
impressions          — deprecated pentru MEDIA după July 2, 2024; → folosește views
profile_views        — deprecated Jan 8, 2025; fără înlocuitor
website_clicks       — deprecated Jan 8, 2025; fără înlocuitor
phone_call_clicks    — deprecated
text_message_clicks  — deprecated
video_views          — deprecated pentru non-Reels; → folosește views
plays                — deprecated April 21, 2025
clips_replays_count  — deprecated
ig_reels_aggregated_all_plays_count — deprecated
email_contacts       — deprecated (time series)
```

### Câmpuri profil (GET /{ig-user-id})
```
id, username, name, biography, followers_count, media_count,
profile_picture_url, website
```

### Media list (GET /{ig-user-id}/media)
```
id, caption, media_type, media_url, thumbnail_url,
permalink, timestamp, like_count, comments_count
```
`media_type` valori: `IMAGE`, `VIDEO`, `REEL`, `CAROUSEL_ALBUM`

---

## 6. Anthropic Claude API — Modele curente (iunie 2026)

**SDK**: `pip install anthropic>=0.39.0`

| Model API ID | Tier | Input $/MTok | Output $/MTok | Context |
|---|---|---|---|---|
| `claude-fable-5` | Best | $10 | $50 | 1M |
| `claude-opus-4-8` | Opus | $5 | $25 | 1M |
| `claude-sonnet-4-6` | Sonnet | $3 | $15 | 1M |
| `claude-haiku-4-5-20251001` | Haiku | $1 | $5 | 200K |

**Recomandare pentru acest proiect**:
- Recomandări AI, copywriting: `claude-haiku-4-5-20251001` (cel mai ieftin, rapid)
- Analiză complexă, strategii: `claude-sonnet-4-6`
- Folosit în agent: `src/agents/instagram_analytics.py` → `claude-haiku-4-5-20251001`

```python
import anthropic
client = anthropic.Anthropic()  # citește ANTHROPIC_API_KEY din env
msg = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=1024,
    messages=[{"role": "user", "content": "..."}],
)
```

---

## 7. Modulul Instagram Growth Analytics

**Pagină**: `/instagram-growth`
**Agent**: `src/agents/instagram_analytics.py`
**Template**: `src/dashboard/templates/instagram_growth.html`

### Funcții principale în agent
```python
fetch_account_overview(force=False)   # profil + metrici 30 zile + ER
fetch_recent_posts(limit=20, force=False)  # ultimele posturi cu insights
fetch_audience_demographics(force=False)   # țări, orașe, vârstă, gen
fetch_growth_history()                 # snapshots zilnice followers
record_daily_snapshot()                # apelat de scheduler la 23:55
generate_recommendations(overview, posts, audience)  # AI tips (cache 6h)
list_content_kit_items()               # scan data/content_kit/
refresh_all(force=True)                # refresh complet din API
```

### Cache files (data/ig_analytics/)
```
overview.json       — TTL 1h
posts.json          — TTL 1h
audience.json       — TTL 1h
recommendations.json — TTL 6h
growth_history.json  — permanent, append-only (90 days)
```

### API endpoints disponibile
```
GET  /instagram-growth              — pagina principală
GET  /api/instagram/overview        — account stats JSON
GET  /api/instagram/posts           — recent posts JSON
GET  /api/instagram/audience        — demographics JSON
GET  /api/instagram/growth-chart    — history JSON
GET  /api/instagram/recommendations — AI tips JSON (?force=1 forțează refresh)
POST /api/instagram/refresh         — forțează refresh complet din Meta API
POST /api/instagram/queue-content   — loghează un content kit item în history
```

### Content Kit EliteMarginDesk
Locație: `data/content_kit/` (ignorat de git — copiat la setup)
```
videos/     — 12 clipuri MP4 (Reels): BoringIsTheEdge, MythBusted_*, etc.
carousel/   — 6 slide-uri PNG
images/     — imagini pentru posts/Stories
```

---

## 8. Regulile agentului — ce să faci și ce să eviți

### Adaugă în sidebar când creezi o pagină nouă
Editează `src/dashboard/templates/_sidebar.html`:
```html
<a href="/noua-pagina" class="snav-item" data-path="/noua-pagina">
  🆕 <span data-i18n="nav.noua_pagina">Pagina Nouă</span>
</a>
```

### Variabile de mediu — user settings
Setările utilizatorului (Settings page) sunt salvate în DB și injectate via `_uenv(settings)`.
Chei relevante:
```python
settings = get_user_settings(user_id)
with _uenv(settings):
    # os.environ["IG_USER_ID"] setat automat din settings["ig_user_id"]
    # os.environ["IG_ACCESS_TOKEN"] setat din settings["ig_access_token"]
    # os.environ["ANTHROPIC_API_KEY"] setat din settings["anthropic_api_key"]
```

### Operații async cu executor
Apelurile blocante (API calls, generare video) se fac cu:
```python
loop = asyncio.get_event_loop()
result = await loop.run_in_executor(_publish_executor, lambda: blocking_function())
```

### Nu comite în git
- `data/` (gitignored) — conține baza de date, cache, content kit
- `.env` — credențiale
- `queue/` — posturi generate

### Stiluri inline vs elite.css
- **Preferă clasele din `elite.css`** pentru componente noi (ecard, ebtn, estat etc.)
- Tailwind utility classes (inline) sunt OK pentru layout și spacing
- Stiluri custom `:root` vars inline sunt OK în `<style>` per-pagină pentru variante

### Testare sintaxă înainte de push
```bash
python -c "import ast; ast.parse(open('src/dashboard/app.py').read()); print('OK')"
python -c "import ast; ast.parse(open('src/agents/instagram_analytics.py').read()); print('OK')"
```

---

## 9. Flux de lucru git

```bash
# Branch activ
git checkout claude/instagram-growth-analytics-6aup6r

# Push
git push -u origin claude/instagram-growth-analytics-6aup6r
```

**Nu pusha pe `main` fără confirmare explicită.**

---

## 10. Fișiere cheie de referință

| Fișier | Rol |
|---|---|
| `src/dashboard/app.py` | Toate rutele FastAPI, auth, middleware |
| `src/dashboard/database.py` | Schema SQLite + toate funcțiile DB |
| `src/dashboard/templates/_sidebar.html` | Sidebar nav (include în orice pagină) |
| `src/dashboard/static/elite.css` | Design system complet |
| `src/agents/instagram_analytics.py` | Agent Meta Graph API |
| `src/agents/scheduler.py` | Cron jobs (viral scout, IG snapshot, etc.) |
| `src/agents/publisher.py` | Publicare pe IG/X/TG |
| `src/agents/agent_brain.py` | Learning loop AI |
| `src/agents/instagram_oauth_setup.py` | Setup one-time token Meta |
| `data/content_kit/` | Asset-uri EliteMarginDesk (gitignored) |

---

*Surse verificate: [Meta Developers](https://developers.facebook.com/docs/instagram-platform/insights/),
[Graph API v22 Changelog](https://developers.facebook.com/docs/graph-api/changelog/version22.0/),
[Anthropic Models](https://platform.claude.com/docs/en/about-claude/models/overview),
[Tailwind CSS Play CDN](https://tailwindcss.com/docs/installation/play-cdn)*
