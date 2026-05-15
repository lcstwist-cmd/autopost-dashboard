# REZOLVARI — stare proiect AutoPost (23 Apr 2026)

Ultima actualizare: **audit complet + brand EMD Marcus + dashboard multi-user SaaS + fix avatar D-ID-only bug**
+ AI Avatar provider chain (Replicate primary → D-ID fallback → branded reel)
+ bubble layout monetization-grade.
Zero Make.com, zero Zapier. Postarea e 100% automatizata.

---

## 🎬 Sesiunea curenta (23 Apr 2026) — audit + EMD brand + dashboard SaaS

### Ce a cerut user-ul

1. Audit complet al proiectului — ce merge, ce nu.
2. Rezolva problema cu avatarul AI.
3. Creaza brand EMD: barbat alb in costum care prezinta stirile dimineata + seara.
4. Target: TikTok, Instagram, YouTube.
5. Dashboard mai user-friendly pentru multi-user SaaS.

### Ce s-a rezolvat

**A. Bug critic avatar — `app.py` `build_avatar_video_route`**
- Inainte: endpoint-ul returna HTTP 400 "D-ID API key not set" chiar si cand
  Replicate era configurat ca provider primar.
- Fix: verificam ambele chei; blocam doar daca *niciuna* nu e configurata:
  ```python
  did_key = settings.get("did_api_key") or os.environ.get("DID_API_KEY", "")
  replicate_key = settings.get("replicate_api_token") or os.environ.get("REPLICATE_API_TOKEN", "")
  if not did_key and not replicate_key:
      return JSONResponse({"error": "Niciun provider AI Avatar configurat..."}, status_code=400)
  ```

**B. Endpoint nou `POST /api/upload-presenter`**
- User-ul poate uploada o poza custom a prezentatorului brand.
- Valideaza cheia D-ID, salveaza fotografia in fisier temporar, apeleaza
  `upload_photo_to_did()`, salveaza URL-ul returnat ca `did_presenter_url` in settings.

**C. Brand EMD — Marcus (barbat alb in costum de afaceri)**
- `avatar_video.py` deja mapa `"emd"`, `"emd_brand"`, `"default"` la
  `matt.jpeg` din D-ID's public S3 (barbat alb, costum navy). Prezent,
  dar ne-expus in UI.
- **`slot.html`**: presenter dropdown are acum `"Marcus — EMD Brand (costum,
  recomandat)"` ca optiune default selectata:
  ```html
  <optgroup label="⭐ Brand EMD">
    <option value="emd" selected>Marcus — EMD Brand (costum, recomandat)</option>
    <option value="matt">Matt — costum business</option>
    <option value="josh">Josh — costum albastru</option>
  </optgroup>
  ```
- **`settings.html`**: sectiune noua "EMD Brand Presenter — Marcus" cu:
  - Preview circular al pozei prezentatorului (URL curent sau `matt.jpeg` default)
  - Badge-uri status: "Activ pe TikTok · Instagram · YouTube Shorts" +
    "Morning 08:00 + Evening 18:00"
  - Upload UI → apeleaza `/api/upload-presenter`
  - Camp manual URL override (`did_presenter_url`)
  - Eliminat campul duplicat `did_presenter_url` din sectiunea D-ID

**D. Dashboard rewrite — `index.html`**
- Header compact cu pill heartbeat scheduler (apeleaza `/api/scheduler/status`)
- Grid 6 coloane cu statistici top + pill-uri readiness per platforma
  (apeleaza `/api/scheduler/platforms`)
- Quick Video Builder cu `presenter='emd'` setat implicit pe butonul avatar
- Pill-uri platforme: Telegram, X, Instagram, TikTok, YouTube cu stari ✓/✗
- Labels UI in romana pentru context multi-user SaaS

**E. Slot.html — bara publish extinsa**
- 3 butoane noi platform toggle: Instagram (violet), TikTok (teal), YouTube (rosu)
- `togglePlatform()` JS actualizat cu color maps per platforma:
  ```javascript
  const _platColors = {
    instagram: {on:'#1a0b2e', border:'#7e22ce', text:'#c084fc'},
    tiktok:    {on:'#0a1a1a', border:'#0d9488', text:'#2dd4bf'},
    youtube:   {on:'#1c0a0a', border:'#b91c1c', text:'#fca5a5'},
  };
  ```
- Eliminat separatorul `<div class="w-px h-8 bg-gray-700">` orfan din flex wrapper

### Stare dupa audit

| Zona | Stare | Note |
|------|-------|------|
| Telegram posting | ✅ merge | Bot token + chat ID configurate |
| X posting (cookies) | ✅ merge | Prioritate 1: X_COOKIES_JSON |
| X posting (OAuth) | ✅ merge | Prioritate 2: PKCE flow |
| Instagram Graph API | ✅ implementat | Necesita IG_ACCESS_TOKEN + IG_USER_ID |
| TikTok Content API | ✅ implementat | Necesita TIKTOK_ACCESS_TOKEN |
| YouTube Data API v3 | ✅ implementat | Necesita YT_REFRESH_TOKEN |
| Avatar AI (D-ID) | ✅ fix aplicat | Bug D-ID-only rezolvat |
| Avatar AI (Replicate) | ✅ merge | SadTalker/Wav2Lip ca primar |
| Brand EMD Marcus | ✅ activ | matt.jpeg default, dropdown setat |
| Dashboard multi-user | ✅ imbunatatit | Platforme pills, scheduler widget |
| Scheduler 08:00+18:00 | ✅ merge | GMT+2, auto-detectie platforme |
| Provider chain avatar | ✅ merge | Replicate → D-ID → branded fallback |

---

## 🎬 Sesiunea anterioara (25 Apr 2026, partea 4) — caption fix + avatar diagnostics

### Probleme descoperite la verificarea finala

1. **Captions cu fonturi gigantice** care acopereau bubble-ul — la
   FontSize=18 in libass cu PlayResY default=288, textul se randa
   ~120px pe canvas-ul nostru de 1920px.
2. **Captions taiate** la marginea stanga si dreapta cand textul nu
   incapea pe o linie (WrapStyle=2 = no wrap).
3. **Bubble cu fundal transparent** — `alphamerge` lua luma celui de-al
   doilea input (mask) ca alpha; mask-ul nostru avea `r='r(X,Y)'` pe
   un canvas negru → luma = 0 → totul transparent.
4. **User-ul nu stia DE CE nu apare avatarul** — pipeline cadea silent
   pe branded fallback fara nicio diagnostic.

### Ce s-a rezolvat

**A. Captions: SRT → ASS pixel-accurate** (`_srt_to_ass()` nou in
`src/agents/avatar_video.py`)
- Generam un fisier `.ass` cu `[Script Info]: PlayResX=1080, PlayResY=1920`
  → FontSize devine pixel-perfect.
- Style: `Arial 38px`, outline 4px negru, shadow 1px, bottom-center,
  `MarginL/R=80`, `MarginV=120` → captions stau la `y ~ 1800` (220px
  sub bubble bottom, fara overlap).
- `WrapStyle=0` (smart wrap) → propozitiile lungi se sparg pe 2 linii
  centrate, nu mai ies din canvas.

**B. Bubble cu alpha corect** (geq direct pe avatar, fara alphamerge)
- Inainte: `[avsq][mask]alphamerge[avc]` — picătura fragila, nu mergea
  cu yuv420p.
- Acum: `[1:v]format=rgba,geq=...:a='if(lte(hypot(X-cx,Y-cy),r-6),255,0)'`
  cuteaza direct cercul intr-un singur pas.
- Inelul colorat e o annulus prin `between(hypot(X-cx,Y-cy), r-12, r-2)`.
- Verificat vizual: bubble circular, plin, cu inel portocaliu, fara
  bleed-through din imaginea de news.

**C. Diagnostics provider-chain** (`_avatar_status.json` per slot)
- `generate_with_chain()` acum accepta un dict `status` pe care il
  populeaza cu per-provider attempts: `name`, `available`, `ok`,
  `error`, `elapsed_s`.
- `build_avatar_reel()` salveaza JSON-ul ca `slot_dir/_avatar_status.json`
  dupa fiecare run.
- `_explain_unavailability()` da hint-uri umane: "REPLICATE_API_TOKEN
  not set — add it via Settings → AI Avatar (or .env)".

**D. Surfacing in dashboard** (`templates/slot.html`)
- Card nou sub video-ul generat: ✓ verde cand avatarul a venit din
  Replicate/D-ID; ⚠ amber cu lista de attempts cand a fallback-uit la
  branded.
- Link direct catre `/settings#avatar` cu motivul exact (token lipsa /
  auth fail / network).
- `_get_slot_detail()` in `app.py` citeste `_avatar_status.json` si il
  trimite ca `avatar_status` in template.

### Verificat end-to-end

- `_composite_ffmpeg()` cu fake_avatar.mp4 + capții reale →
  `/tmp/cap_test/reel_test.mp4` (2.4 MB, 20s):
  - frame t=2s: bubble circular cu gradient avatar, inel portocaliu,
    captions "Someone just bought $237M worth of ETH — here's why it
    matters." pe 2 linii centrate sub bubble. ✓
  - frame t=8s: "Grayscale stakes 102,400 ETH via Ethereum Staking
    ETF, valued at $237M." pe 2 linii, fara overlap. ✓
- `build_avatar_reel(slot, presenter='emd')` rulat in sandbox cu
  REPLICATE_API_TOKEN dezactivat si proxy blocand D-ID:
  - chain incercat: `replicate skipped (token not set) → did FAIL
    (proxy 403)` → fallback la branded.
  - `_avatar_status.json` scris cu detalii complete.
  - `reel_avatar.mp4` (2.4 MB) generat cu intro card + Ken Burns +
    captions + outro. ✓

### Ramane pentru user (in productie, nu sandbox)

1. **Daca user-ul vrea avatarul vorbitor** — sa upload-eze
   `REPLICATE_API_TOKEN` in Settings → AI Avatar. Cu el, chain-ul va
   alege Replicate primul (~$0.01-0.05/clip vs D-ID's $0.20+).
2. Daca prefera D-ID-only (ce e configurat acum) — ok, dar e mai scump.
3. **Presenter EMD-brand** — deja default = `matt.jpeg` din D-ID's
   public S3 bucket (white male in business suit). Mapat ca `default`,
   `emd`, `emd_brand`. Override via `EMD_PRESENTER_URL` in `.env` sau
   Settings.

---

## 🎬 Sesiunea curenta (25 Apr 2026, partea 3) — AI Avatar provider chain + monetization

### Ce a cerut user-ul

1. "asigurate ca clipurile finale cu avatare ai arata cum am zis: textul in
   poza, avatarul AI care sa aibe pe fundal poza cu textul stirii, avatarul
   sa prezinte stirea, totul perfect ca sa monetizam aplicatia."
2. "incearca sa faci un bot care face avatare AI, sa fie optiunea a 2-a D-ID."

### Ce s-a facut

**A. Layout monetization-grade in `_composite_ffmpeg()` (src/agents/avatar_video.py)**
- Avatarul nu mai acopera jumatate de imagine cu un strip de 540px.
- E acum un **bubble circular 460×460** in colt dreapta-jos, cu un inel
  colorat (brand color) — facut prin geq + alphamerge in FFmpeg.
- Textul stirii (titlu + summary din imagine) ramane 100% vizibil.
- Position: x=580, y=1140 (40px padding dreapta, 320px deasupra captions).
- Captions raman in safe-zone jos, intro/outro cards la fel ca inainte.

**B. Provider chain (NOU — src/agents/avatar_providers/)**
```
src/agents/avatar_providers/
├── __init__.py            ← orchestratorul: get_provider_chain(), generate_with_chain()
├── replicate_provider.py  ← PRIMARY: SadTalker / Wav2Lip pe Replicate (~$0.01-0.05/clip)
└── did_provider.py        ← FALLBACK: wrapper subtire peste generate_avatar_clip() existent
```

Lant de fallback executat de `build_avatar_reel()`:
1. **Replicate** — daca `REPLICATE_API_TOKEN` setat: edge-tts → SadTalker
   (sau Wav2Lip ca fallback intern daca SadTalker e overloaded). Audio
   trimis ca data URI inline (< 5 MB) — fara nevoie de S3/imgbb extern.
2. **D-ID** — daca Replicate nu merge sau nu e configurat. Aceeasi
   functie `generate_avatar_clip()` ca inainte, dar acum invocata ca
   provider in chain.
3. **Branded fallback** — daca ambele fail, apelam tot
   `_composite_ffmpeg()` dar cu `avatar_path=None` si `audio_path=tts.mp3`.
   Iese un reel polish cu image + Ken Burns + intro/outro + BGM + captions
   (DOAR fara avatar). Slot-ul nu mai ramane gol cand provider-ele cad.

Selectie via `AVATAR_PROVIDER` env var: `auto` (default) / `replicate` / `did`.

**C. Functie noua `build_branded_reel()`**
- Apelabila din UI separat ("Build branded clip" buton) pentru cazul cand
  user-ul vrea un clip fara avatar dar tot polish (e.g. clipuri scurte
  la deadline, sau cand ne ramane fara credite la ambele provider-e).
- Foloseste exact aceeasi logica de composite, output -> `reel_branded.mp4`.

**D. UI Settings (templates/settings.html)**
- Card nou "🤖 AI Avatar — provider chain" pus DEASUPRA cardului D-ID.
- Campuri:
  * `replicate_api_token` (password) — link direct la replicate.com pentru
    generare token + nota despre $1-5 free credit la signup.
  * `replicate_presenter_url` (text) — opțional, fallback la URL D-ID.
  * `avatar_provider` (select) — auto / replicate / did.
- Cardul D-ID e pastrat intact, doar etichetat ca "fallback provider".

**E. Schema DB + dashboard env injection**
- Adaugat in `database.py`:
  * `replicate_api_token TEXT DEFAULT ''`
  * `replicate_presenter_url TEXT DEFAULT ''`
  * `avatar_provider TEXT DEFAULT 'auto'`
- Adaugat la whitelist-ul `save_user_settings()`.
- `_uenv()` din `dashboard/app.py` exporteaza acum si:
  * `REPLICATE_API_TOKEN`, `REPLICATE_PRESENTER_URL`, `AVATAR_PROVIDER`.
- `_with_did()` (folosit de endpoint-urile preview/test) extins similar.

### Setup pt. user

```bash
# 1. Inregistrare cont gratis
open https://replicate.com

# 2. Genereaza token (da $1-5 credit la signup ≈ 25-500 clipuri)
open https://replicate.com/account/api-tokens

# 3. Pune token-ul in Settings -> AI Avatar -> Replicate API Token
#    (sau in .env ca REPLICATE_API_TOKEN=r8_...)

# 4. Test: ruleaza pipeline-ul, ar trebui sa vezi:
#    [avatar_providers] trying provider: replicate
#    [replicate] submitting SadTalker job...
#    [replicate] avatar saved -> _avatar_raw.mp4 (XXX KB)
#    [avatar_video] avatar via replicate: XX.Xs
```

### Cost benchmark

| Provider   | Cost/clip | Latenta  | Free credit |
|------------|-----------|----------|-------------|
| Replicate (SadTalker) | $0.01–0.05 | 30–60s | $1-5 ≈ 25-500 clipuri |
| Replicate (Wav2Lip)   | $0.005–0.02 | 15–30s | la fel |
| D-ID Talks | ~$0.20+   | 15–25s   | 5 trial videos |

La 2 clipuri/zi cu Replicate: ~$0.60-3/luna vs ~$12+/luna la D-ID.

---

## 🚀 Sesiunea anterioara (25 Apr 2026, partea 2) — Viral 3-agent pipeline

User a cerut: "fa-mi agentul care se ocupa cu analiza clipurilor de pe IG,
TikTok si YT — sa urmareasca viralele, alt bot sa le analizeze, sa transforme
clipurile NOASTRE in unele virale, dar strict pe ce facem noi (crypto, stiri)".

### Arhitectura 3-bot

```
┌─────────────────┐  scrape   ┌──────────────────┐  blueprint  ┌──────────────────────┐
│ viral_scout.py  │──────────▶│ viral_analyzer.py│────────────▶│ viral_transformer.py │
│ (orchestreaza)  │           │  (extrage tipare)│             │  (rescrie copy-ul)   │
└─────────────────┘           └──────────────────┘             └──────────────────────┘
        │                                                                │
        │ ruleaza zilnic 06:30                                           │ injecteaza in
        │ via scheduler.py                                               │ avatar_writer
        ▼                                                                ▼
   DB viral_patterns                                          script + caption viral
```

### 1) `src/agents/viral_scout.py` (NOU, 200 lines)
- Mirror pe `news_scout.py`. Ruleaza pe ciclu propriu (zilnic 06:30 GMT+2).
- Topicuri **lock-uite** pe nisa noastra (env override `VIRAL_TOPICS=...`):
  `crypto, bitcoin, ethereum, crypto news, altcoin`
- Per user, per topic: cheama `viral_analyzer.scan_viral` (YT API + TikTok
  Discover scrape + IG hashtag scrape) si salveaza blueprint-ul in
  `viral_patterns` (TTL 24h, skip rescrape daca exista cache fresh).
- API: `scan_topics()`, `refresh_for_user(uid)`, `refresh_all_admins()`,
  `run_once()` (entry point pt. scheduler).
- CLI: `python src/agents/viral_scout.py [--force] [--user-id N]
  [--topic crypto] [--print]`

### 2) `src/agents/viral_transformer.py` (NOU, 250 lines)
- "Bot-ul al doilea" cerut de user — ia draft-ul nostru + blueprint si
  rescrie pentru a deveni viral.
- **Topic guard**: filtreaza tag-uri si hook-uri off-topic (fashion, gym, fyp,
  food, gaming) inainte sa le injecteze. Lista de keyword-uri ON_TOPIC
  (crypto, btc, eth, defi, etf, sec, fed, market, …) si OFF_TOPIC
  (#fyp, #ootd, #fashion, #fitness, …).
- Trei moduri:
  - `transform_script(script, bp, ticker)` — rule-based, inlocuieste hook-ul
    in prima propozitie cu unul din blueprint (sau template fallback).
  - `transform_caption(caption, bp)` — injecteaza top hashtags + clamp la
    median lungime caption.
  - `build_viral_prompt(bp, ticker)` — produce un fragment de prompt Claude
    cu paternuri (sample size, durata mediana, hook-uri top, hashtags top)
    + reguli ("stay on topic, viral != fake, we are crypto news").

### 3) Wire-up in `avatar_writer.py`
- Functie noua `_load_user_blueprint(story)`: cauta blueprint pentru
  ticker-ul story-ului, apoi fallback "crypto news" → "crypto".
- `_llm_script()` injecteaza acum `build_viral_prompt(bp, ticker)` la
  finalul prompt-ului — Claude vede patternurile direct si le imita.
- `write_package()` — daca **nu** a rulat LLM-ul (offline mode), ruleaza
  `transform_script()` ca hook-hardener pe template-ul determinist.
  Astfel useri fara `ANTHROPIC_API_KEY` capata si ei hook-uri virale.

### 4) Schedule in `scheduler.py`
- Slot nou zilnic la `06:30` GMT+2 (override env `VIRAL_SCOUT_TIME`)
  → cheama `run_viral_scout()` care apeleaza `viral_scout.run_once()`.
- **Warm-up la startup**: daca nu exista blueprint cache, scout-ul ruleaza
  o data imediat dupa ce porneste schedulerul. Asta ca primul slot 08:00 sa
  aiba date.
- CLI extins: `python -m src.agents.scheduler --now viral` ruleaza scout-ul
  on-demand.

### Cum verifici (dupa restart scheduler)
```cmd
python -m src.agents.scheduler --now viral
```
Apoi in DB / dashboard la /api/viral-blueprint?topic=crypto vei vedea
blueprint-ul cu sample_size, hook_patterns, top_hashtags.

### Important — protectie topic
Filtrul anti off-topic functioneaza pe doua niveluri:
1. La scrape: `viral_analyzer.fetch_youtube` foloseste deja
   `relevanceLanguage=en` + topic exact ("crypto", "bitcoin", …).
2. La transform: `_is_on_topic()` rejecteaza hook-uri ca "Fashion week was
   crazy" chiar daca apar in blueprint, si `_filter_topic_hashtags()`
   rejecteaza #fyp / #ootd / #fashion / #fitness / #gaming.

Test rulat in sesiune (vezi log) — toate cele 4 cazuri trec:
- `_is_on_topic('Bitcoin just hit a new ATH #btc')` → True
- `_is_on_topic('My new outfit #ootd #fashion')` → False
- `_filter_topic_hashtags(...)` rejecteaza #fyp, #fashion; keeps #crypto, #btc, #xrp
- `build_viral_prompt(bp)` exclude "Fashion week was crazy" din hook-uri

---

## 🔥 Sesiunea curenta (25 Apr 2026, partea 1) — fix X automation

**Problema raportata:** ieri (24 Apr) autopostul a rulat doar pe Telegram. Pe X
am postat manual. Trebuia sa posteze automat de 2 ori pe zi pe X + Telegram.

### Cauze identificate
1. **Scheduler avea `platforms={"telegram", "x"}` hardcodat** — IG/TikTok/YouTube
   nici nu erau incercate, iar X cadea pe twikit (rupt).
2. **`_inject_admin_settings` nu injecta `X_COOKIES_JSON`** in env-ul slot-ului.
   Cand publisher-ul ajungea sa posteze pe X, calea cookies (Priority 1)
   era sarita pentru ca env var-ul lipsea, si cadea pe twikit care esueaza
   sistematic ("Couldn't get KEY_BYTE indices").
3. **Twikit insusi e rupt** — Twitter/X-ul a actualizat anti-bot-ul. Twikit nu
   mai poate decoda cheile API. Calea user/parola e moarta.

### Ce am facut
- **Sters complet codul twikit** din 11 fisiere:
  - `publisher.py`: scoasa functia `publish_x_twikit()`, `_cookies_path()`,
    si block-ul Priority 3 din `publish_x()`
  - `requirements.txt`: scos `twikit>=2.3.0`
  - `check_health.py`: scoasa verificarea modulului twikit
  - `.gitignore`: scos `x_twikit_cookies.json`
  - `x_oauth.py`: scos comentariul despre twikit
  - `dashboard/app.py`: scos branch-ul `else "twikit" if has(...)` din preferred_path
  - `settings.html`: scos panel-ul "X user/parola/email" + warning-ul twikit
- **`scheduler.py` — auto-detectie platforme + injectie completa env:**
  - Functie noua `_resolve_enabled_platforms()`: detecteaza credentialele si
    onoreaza `enabled_platforms` opt-in (din Settings)
  - `_inject_admin_settings()` extins cu: `X_COOKIES_JSON`, `X_OAUTH_*`,
    `IG_*`, `TIKTOK_*`, `YOUTUBE_*`, `IMGBB_API_KEY`, `PUBLIC_BASE_URL`,
    `DATA_DIR`
  - `platforms={"telegram", "x"}` → `platforms=enabled` (rezultatul resolverului)
- **`dashboard/app.py`** — endpoint nou `GET /api/scheduler/platforms` care
  returneaza readiness-ul fiecarei platforme. `settings.html` are acum un
  card "Scheduler Status" care arata chip-uri ✓/✗ per platforma.

### Lantul de publicare X dupa cleanup
```
publish_x():
  1. cookies (X_COOKIES_JSON env / DB)        ← path PRIMAR, fiabil
  2. OAuth 2.0 PKCE (token in DB)             ← SaaS multi-user
  3. Make.com webhook (daca configurat)       ← bypass de back-up
  4. browser Playwright (cookies dashboard)   ← fallback heavy
  5. tweepy BYOK (consumer key + secret)      ← daca user are dev account
```

### Ce trebuie sa faci tu (1 minut)
1. **Asigura-te ca ai cookies salvate** in dashboard:
   `Settings → X / Twitter → Cookies (JSON)` → paste din extensia "Cookie Editor"
   pe x.com → Save. (Daca azi ai postat manual, ai sesiune valida — trebuie doar
   sa exporti cookies-urile.)
2. **Restart scheduler** ca sa preia env vars-urile noi:
   ```cmd
   taskkill /PID 19764 /F
   start_all.bat
   ```
   Sau din dashboard: Settings → Scheduler Status → Stop → Start.
3. Mergi pe `/api/scheduler/platforms` in browser dupa restart — daca X arata
   `ready: true`, urmatoarea rulare 08:00 / 18:00 GMT+2 va posta si pe X.

### Slot times
- 08:00 GMT+2 (morning) → override cu env `AUTOPOST_MORNING=HH:MM`
- 18:00 GMT+2 (evening) → override cu env `AUTOPOST_EVENING=HH:MM`

---

## 🎯 Cum rulezi totul acum (drumul scurt)

| Situatie | Comanda | Ce face |
|---|---|---|
| Prima data | `setup_wizard.bat` | Te ghideaza prin .env + deps + test D-ID |
| Clip acum | `make_clip.bat` | Un clip de la A la Z (~3 min) |
| Doar refa clipul | `python make_clip.py --slot-dir queue\<slot>` | Reporneste VIDEO + AVATAR pe slot existent |
| Dashboard + scheduler | `start_all.bat` | Porneste web + auto 08:00 + 18:00 |
| Diagnostic D-ID | `diagnose_avatar.bat` | Izoleaza eroarea D-ID, scrie log |
| Health check | `python check_health.py` | Verifica 8 zone |

---

## 1. Ce am reparat automat in aceasta sesiune

### 1.1 Bug safety-rail `publisher.py` — prag 280 chars pentru X (era 25000)
### 1.2 `playwright` adaugat in `requirements.txt` + `.gitignore` curatat
### 1.3 Screenshot debug sters din repo (244 KB)
### 1.4 Automatizare browser X rescrisa complet (navigare directa la `/compose/tweet`, `Ctrl+Enter` ca submit, detectie cookies expirate, fara debug)
### 1.5 Spam 401 X API suprimat in `news_scout.py` (un singur warning per run)
### 1.6 D-ID: pre-flight credits + decodare corecta cheie `email:secret` → base64 + mesaje 401/402/403/500 clare
### 1.7 `check_health.py` — script nou care verifica 8 zone: deps, env, Telegram, Claude, D-ID, X, queue, DB
### 1.8 `cleanup_queue.py` — 62.7 MB recuperati din queue-ul murdar
### 1.9 publishers native IG / TikTok / YouTube

### 1.10 **NOU (sesiunea curenta)**: fix-uri pentru clipul cu avatar AI
- `extract_narration` acum strip-uieste `[LABEL]`-urile global (regex `\[[^\]]*\]`) — functioneaza si cand labels sunt pe linii separate si cand sunt inline
- `avatar_writer._cap_words_20s` nu mai aplatizeaza scriptul template (doar output-ul LLM)
- `avatar_video.py`:
  - Preseteri schimbati de la Pexels la poze native D-ID (`create-images-results.d-id.com/DefaultPresenters/...`) — ~90% din erorile D-ID 500 erau cauzate de Pexels CDN (hotlink blocat, redirect, rate limit)
  - Provider TTS comutat la `microsoft` (Azure Neural) — mai stabil decat `edge`
  - Handling explicit pentru HTTP 400 cu auto-fallback la preseter built-in daca eroarea vizeaza imaginea
  - Print de debug eliminat; mesaje de logging mai clare
- `test_did.py` + `diagnose_avatar.bat` — diagnostic izolat care separa D-ID de restul pipeline-ului, scrie log intr-un .txt ca sa nu-l pierzi cand se inchide consola

### 1.11 **NOU**: baza pentru SaaS multi-user
- `user_settings` extins cu coloane pentru IG / TikTok / YouTube / hosting + plan + stripe_customer_id + monthly_clips_used (migration automata)
- `save_user_settings()` rescris cu UPSERT dinamic — updateaza doar ce trimiti, nu distruge campurile existente
- `setup_wizard.py` + `.bat` — onboarding interactiv in romana pentru utilizatori non-tehnici (5 min)
- `start_all.bat` — booteaza dashboard + scheduler in background + deschide browser

### 1.13 **NOU (24 Apr 2026)**: X posting zero-API prin "paste cookies"
- `src/agents/x_cookies.py` — modul care valideaza JSON-ul de Cookie-Editor, face whoami via HTTP (sa confirme ca sunt valide), si posteaza prin Playwright headless folosind cookies (nu user+parola)
- `publish_x_cookies()` in `publisher.py` — prioritate #2 in lantul de fallback (dupa OAuth)
- Rute noi in dashboard: `POST /api/x/cookies-save` (valideaza + whoami + salveaza), `POST /api/x/cookies-test` (verifica live), `POST /x/cookies-clear`
- 3 coloane noi in `user_settings`: `x_cookies_json`, `x_cookies_updated_at`, `x_cookies_handle`
- Sectiune noua in Settings UI cu ghid in 4 pasi + butoane Save/Test/Clear + feedback live
- Doc: `docs/X_POSTING_NO_API.md` pentru abonati (tutorial 10-sec setup)
- Efort user: 10 sec + 3 click-uri (instaleaza Cookie-Editor, export JSON, Ctrl+V, Save)
- **Zero user+parola stocata**, doar cookies (revocabile oricand din x.com → Sessions)
- Cookies expira ~30 zile → aplicatia afiseaza eticheta rosie cand testul pica

### 1.12 **NOU (24 Apr 2026)**: X posting multi-user SaaS-grade via OAuth 2.0 PKCE
- `src/agents/x_oauth.py` — flow complet OAuth 2.0 PKCE (authorize, exchange, refresh, /users/me, media upload v2, post tweet)
- `publish_x_oauth2()` in `publisher.py` — prioritate #1 in lantul de fallback-uri
- Rute noi in dashboard: `GET /x/connect` (start OAuth), `GET /x/callback` (schimb token), `POST /x/disconnect`, `GET /api/x/status`
- Tokenele access + refresh se salveaza **per-user** in `user_settings` (6 coloane noi: `x_oauth_access_token`, `x_oauth_refresh_token`, `x_oauth_expires_at`, `x_oauth_scope`, `x_oauth_username`, `x_oauth_user_id`)
- Refresh automat la fiecare publish: `_ensure_x_oauth_fresh()` verifica expiry cu 90s inainte si reinnoieste via refresh_token daca trebuie — zero re-logare manuala
- `_uenv()` seteaza acum `DATA_DIR=queue/<user_id>` — cookie-urile twikit/browser raman izolate per user, chiar daca 2 useri publica simultan
- Settings page are buton "🔗 Conecteaza contul X" care porneste flow-ul (1-click, fara user+parola)

---

## 2. Arhitectura noua — publicare automata fara Make.com

```
          ┌──────────────────────────────────────────────────────┐
          │          publisher.py --publish --platforms          │
          │   telegram,x,instagram,tiktok,youtube                │
          └──────────────────────────────────────────────────────┘
                 │           │              │           │          │
                 ▼           ▼              ▼           ▼          ▼
         Telegram Bot    X browser   Instagram Graph  TikTok    YouTube
              API        /tweepy      API (Meta)     Content    Data API
                                                     Posting       v3
                                                       API
                                         │
                                         └── image publicly hosted via
                                             imgbb / catbox / ngrok
```

Fisiere noi:
- `src/agents/social_publishers.py` — apeluri API native (IG, TikTok, YT)
- `src/agents/media_host.py`       — helper care genereaza URL public pentru imagine/video
- `src/agents/instagram_oauth_setup.py` — one-time token setup IG
- `src/agents/tiktok_oauth_setup.py`    — one-time OAuth TikTok
- `src/agents/youtube_oauth_setup.py`   — one-time OAuth Google/YT
- `src/dashboard/app.py` — endpoint `/media/<user_id>/<slot>/<file>` public pentru fetch-urile Meta/TikTok

---

## 3. Pasii pentru fiecare platforma

### 3.1 Instagram — Meta Graph API

**Te costa: 0 $**. Durata setup: ~15 min o singura data.

Pasi:
1. Instagram app (telefon) → Settings → Account type → comuta in Business
   sau Creator
2. Leaga contul IG de o pagina de Facebook (din Settings → Account → Linked
   accounts → Facebook)
3. developers.facebook.com → My Apps → **Create App** → tip **Business**
4. Din panoul app-ului: **Add Product** → **Instagram Graph API** (+ **Pages API** daca cere)
5. Pe **App Settings → Basic** — noteaza `App ID` si `App Secret`
6. **Tools → Graph API Explorer** → selecteaza app-ul tau → **Get User Access
   Token** cu scopes:
   - `pages_show_list`
   - `pages_read_engagement`
   - `instagram_basic`
   - `instagram_content_publish`
   - `business_management`
7. Copiaza token-ul scurt rezultat
8. In terminal:
   ```
   python src/agents/instagram_oauth_setup.py
   ```
   iti cere `META_APP_ID`, `META_APP_SECRET`, si token-ul scurt. La final
   iti afiseaza:
   ```
   IG_USER_ID=17841...
   IG_ACCESS_TOKEN=EAAG...
   ```
9. Pune-le in `.env` (sau Settings → Instagram in dashboard)

**Nota importanta**: Page tokens obtinute pornind de la un long-lived user
token NU EXPIRA. E setup de o singura data.

**Hosting imagine**: Instagram trebuie sa poata descarca imaginea. Optiuni (in
ordinea preferintei din `media_host.py`):
- `IMGBB_API_KEY` — inregistrare gratuita la api.imgbb.com
- `catbox.moe` — functioneaza anonim fara cheie, automat folosit
- `PUBLIC_BASE_URL` — daca ai ngrok pornit, pui URL-ul ngrok aici si
  dashboard-ul serveste `/media/...`

Pentru zero-config, catbox merge out-of-the-box.

---

### 3.2 TikTok — Content Posting API

**Te costa: 0 $**. Durata setup: ~20 min o singura data (plus review TikTok pentru productie).

Pasi:
1. developers.tiktok.com → Login cu contul TikTok → **Manage apps** → **Connect app**
2. Adauga produse: **Login Kit** + **Content Posting API**
3. In `Login Kit`:
   - Redirect URL: `http://localhost:8765/callback`
   - Scopes: `user.info.basic`, `video.upload`, `video.publish`
4. In `Content Posting API` → solicita acces la `Video.Upload` si `Video.Publish`
5. Copiaza **Client Key** + **Client Secret**
6. Ruleaza:
   ```
   python src/agents/tiktok_oauth_setup.py
   ```
   Iti deschide browserul, te logezi pe TikTok cu contul pe care vrei sa
   postezi, aprobi scope-urile. La final iti afiseaza:
   ```
   TIKTOK_CLIENT_KEY=...
   TIKTOK_CLIENT_SECRET=...
   TIKTOK_ACCESS_TOKEN=...
   TIKTOK_REFRESH_TOKEN=...
   ```
7. Pune in `.env` (sau Settings → TikTok in dashboard)

**Nota**: Cat timp app-ul e in Sandbox, privacy-ul impus e `SELF_ONLY` (doar
tu vezi postarea). Dupa ce trece review-ul TikTok pentru productie, poti
seta `TIKTOK_PRIVACY=PUBLIC_TO_EVERYONE` in `.env`.

**Refresh**: access token-ul dureaza 24h, dar codul face refresh automat daca
`TIKTOK_REFRESH_TOKEN` e setat.

---

### 3.3 YouTube — Data API v3

**Te costa: 0 $**. Durata setup: ~15 min o singura data.
Quota default: **10.000 unitati/zi** = ~6 uploads/zi (fiecare upload costa 1600).

Pasi:
1. console.cloud.google.com → **Create project**
2. **APIs & Services → Library** → cauta **YouTube Data API v3** → **Enable**
3. **APIs & Services → Credentials** → **Create Credentials** → **OAuth Client
   ID** → tip **Desktop app**
4. Descarca JSON-ul sau copiaza `client_id` + `client_secret`
5. **OAuth consent screen** → User type **External** → adauga contul tau
   Google ca **Test user** (optional, pastreaza in Testing mode — refresh
   token-ul nu expira daca e setat sa persiste)
   - Pentru refresh token permanent, fa **Publish App** (necesita un review
     ~1 saptamana daca scope-urile sunt sensitive). Pentru `youtube.upload`
     NU e review necesar — e scope standard.
6. Ruleaza:
   ```
   python src/agents/youtube_oauth_setup.py
   ```
   iti deschide browserul, te logezi cu contul Google al canalului. La
   final vezi:
   ```
   YT_CLIENT_ID=...
   YT_CLIENT_SECRET=...
   YT_REFRESH_TOKEN=...
   ```
7. Pune in `.env`

---

### 3.4 Telegram — deja merge

`TELEGRAM_BOT_TOKEN` si `TELEGRAM_CHAT_ID` sunt deja in `.env`. Nu mai e
nimic de facut.

### 3.5 X / Twitter — 3 alternative (in ordine de try)

1. **Browser automation** (default, zero cost): ruleaza o singura data
   `python src/agents/x_login_helper.py` — logheaza-te manual in browser, se
   salveaza cookies (~30 zile)
2. **Make.com webhook** (optional, ramas de la versiunea veche): daca ai deja
   `MAKE_X_WEBHOOK_URL` setat, inca functioneaza
3. **Tweepy API** (costa 100 $/luna): completeaza toate 4 variabilele
   `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`

---

## 4. Cum sa rulezi tot sistemul

### 4.1 Setup initial (o singura data)

```bash
# 1. Instaleaza dependente
pip install -r requirements.txt
playwright install chromium

# 2. Creeaza DB + admin user
python seed_db.py

# 3. Seteaza credentialele (scripturi one-time)
python src/agents/instagram_oauth_setup.py
python src/agents/tiktok_oauth_setup.py
python src/agents/youtube_oauth_setup.py
python src/agents/x_login_helper.py

# 4. Verifica ca totul e OK
python check_health.py
```

### 4.2 Rulare zilnica (scheduler)

```bash
# Local + ngrok + dashboard
start.bat
```
Sau manual:
```bash
python run_local.py
```

### 4.3 Publicare pe toate platformele simultan

```bash
# 1. Genereaza slot nou (scout → ranker → copywriter → image → video)
python src/agents/pipeline.py --slot morning

# 2. Publica pe TOATE
python src/agents/publisher.py queue/2026-04-23_morning \
    --publish --platforms telegram,x,instagram,tiktok,youtube
```

Un log JSON se scrie automat in `queue/<slot>/publish_log.json`.

---

## 5. `.env` — toate variabilele

```env
# Telegram (obligatorii)
TELEGRAM_BOT_TOKEN=8687967862:AAGah...
TELEGRAM_CHAT_ID=@elitemargindesk

# X / Twitter — browser (zero cost)
X_USERNAME=ELITEMARGINDESK
X_EMAIL=lcstwist@gmail.com
X_PASSWORD=Alice2@2

# Instagram (Meta Graph API)
IG_USER_ID=17841...
IG_ACCESS_TOKEN=EAAG...
META_APP_ID=...            # optional, doar pentru re-run setup
META_APP_SECRET=...

# TikTok
TIKTOK_CLIENT_KEY=...
TIKTOK_CLIENT_SECRET=...
TIKTOK_ACCESS_TOKEN=...
TIKTOK_REFRESH_TOKEN=...
TIKTOK_PRIVACY=PUBLIC_TO_EVERYONE   # optional

# YouTube
YT_CLIENT_ID=...
YT_CLIENT_SECRET=...
YT_REFRESH_TOKEN=...

# Image hosting pentru IG (alege una sau niciuna — catbox e default)
IMGBB_API_KEY=               # optional
PUBLIC_BASE_URL=             # ex: https://xxxxxxxx.ngrok.io (optional)

# Claude (copywriter)
ANTHROPIC_API_KEY=sk-ant-...

# D-ID (avatar video)
DID_API_KEY=bGNzdHdpc3RA...

# Surse de stiri (optional, imbunatatesc calitatea)
CRYPTOPANIC_API_KEY=
LUNARCRUSH_API_KEY=
```

---

## 6. Troubleshooting rapid

| Simptom | Cauza | Rezolvare |
|---|---|---|
| `IG: Cannot produce public image URL` | nici `IMGBB_API_KEY`, nici `PUBLIC_BASE_URL`, catbox cazut | pune `IMGBB_API_KEY` (gratuit la api.imgbb.com) |
| `IG 190: access token invalid` | Page token a expirat sau a fost revocat | re-ruleaza `instagram_oauth_setup.py` |
| `TikTok init HTTP 401` | access_token expirat si refresh nu e setat | re-ruleaza `tiktok_oauth_setup.py` |
| `TikTok SEND_TO_USER_INBOX` in loc de `PUBLISH_COMPLETE` | app inca in sandbox / audit | normal in sandbox; posteaza dar e `SELF_ONLY` |
| `YT quotaExceeded` | peste 6 uploads in 24h | normal — cere quota marire in console.cloud |
| `IG container ERROR` | imaginea nu e accesibila de Meta / nu e JPEG/PNG | verifica cu `curl -I <public_url>` |
| `TikTok upload HTTP 403` | `privacy_level` neautorizat pentru acest scope | default e `SELF_ONLY`, schimba numai dupa productia app-ului |

---

## 7. Roadmap pentru SaaS public (abonament lunar)

### Ce merge deja pentru multi-user
- ✅ Auth (email + parola, bcrypt/PBKDF2) cu aprobare admin
- ✅ Izolare queue per-user: `queue/<user_id>/<slot>/...`
- ✅ `user_settings` per-user cu coloane pentru toate API-urile
- ✅ Context manager `_uenv()` injecteaza env per-request fara sa strice alte threaduri

### TODO — blocante pentru lansare plata
- [ ] **Billing (Stripe)** — webhook, checkout, tier-uri (trial/free/pro), portal customer
- [ ] **Verificare email** — link unic la signup (zero typo, zero hijack)
- [ ] **Rate limiting per plan** — X clipuri/luna, Y publicari, Z MB storage
- [ ] **Decriptare la rest** — coloanele *token/*key in SQLite sa fie criptate (fernet+MASTER_KEY env)
- [ ] **Audit log** — tabela `activity_log(user_id, action, resource, ts)` + view la dashboard
- [ ] **Forgot password** — email + token de reset
- [ ] **GDPR** — buton "exporta datele" + "sterge cont"
- [ ] **Staging env** — un Railway/Fly gratis pentru test inainte de prod
- [ ] **ToS + Privacy Policy** + DPA cu sub-procesorii (Anthropic, D-ID, OpenAI)

### Nice-to-have ulterior
- [ ] Retry automat la fail (peste 15 min, max 3 tentative)
- [ ] Instagram Reels nativ (deja exista `publish_instagram_reel_api()` — de conectat)
- [ ] Facebook Page posts (acelasi token IG merge — Meta Graph `/feed`)
- [ ] Threads (daca API-ul devine public)
- [ ] Editor WYSIWYG pentru post, nu doar preview
- [ ] Multi-channel pe platforma (ex: 3 canale Telegram diferite/user)

Bafta! Cand rulezi prima data, foloseste `check_health.py` sa vezi exact
ce-ti lipseste, sau direct `setup_wizard.bat` ca sa treci prin tot pas cu pas.
