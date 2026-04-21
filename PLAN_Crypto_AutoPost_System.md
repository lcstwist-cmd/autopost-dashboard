# Crypto AutoPost System — Plan si Arhitectura

**Obiectiv:** Echipa de agenti AI care citeste stirile din piata crypto, selecteaza cele mai importante 2 in fiecare zi, si genereaza automat continut pentru 5 platforme: X, Telegram (post + imagine), Instagram, TikTok, YouTube (reel/short cu avatar AI CapCut).

**Cadenta:** 2 postari/zi — una dimineata (~08:00 EET) si una seara (~19:00 EET). O stire per interval.

**Limba continutului:** Engleza (audienta globala crypto).

---

## 1. Arhitectura echipei de agenti

Sistemul e compus din 7 agenti specializati care lucreaza in pipeline. Fiecare are un singur rol clar si paseaza output-ul la urmatorul.

### Agent 1 — News Scout
**Rol:** strange stiri din toate sursele configurate, le normalizeaza intr-un format comun (titlu, link, sursa, timestamp, rezumat).
**Trigger:** ruleaza la 06:30 si 17:30 (cu 1h30min inaintea postarii).
**Output:** lista bruta de 30-80 stiri din ultimele 12 ore.

### Agent 2 — Ranker / Curator
**Rol:** din lista bruta, alege TOP 2 stiri (una pentru dimineata, una pentru seara, sau amandoua intr-un batch).
**Criterii de scoring:**
- Impact de piata (miscari de pret peste ±3% pe BTC/ETH/top 20)
- Frecventa mentionarii (cate surse diferite reporteaza aceeasi stire)
- Relevanta sociala (scor din LunarCrush/CryptoPanic)
- Noutate (preferam ultimele 6h)
- Evitam duplicate cu stirile postate in ultimele 48h
**Output:** 2 stiri selectate cu justificare + punctele cheie extrase (3-5 bullet points).

### Agent 3 — X/Telegram Copywriter
**Rol:** transforma stirea intr-un post optim pentru fiecare platforma.
**Formate:**
- X: max 280 caractere, 2-3 hashtag-uri (#Bitcoin #Crypto #NomeTicker), emoji moderat, cu link
- Telegram: format lung (500-1000 caractere), bullets permise, link la finalul postului, **bold** pe titlu
**Output:** 2 blocuri de text (unul X, unul Telegram) + prompt pentru imaginea aferenta.

### Agent 4 — Image Generator
**Rol:** creeaza o imagine tip "news card" de 1200x675 (OG image) pentru X si 1080x1080 pentru Telegram.
**Tehnologie:** template HTML/CSS randat local cu Puppeteer/Playwright → PNG. Template-urile contin:
- Background degrade tematic (verde daca pret ↑, rosu daca ↓, albastru neutru)
- Titlu mare (max 80 caractere)
- Logo/ticker al coin-ului (BTC, ETH, SOL etc.)
- Mini-grafic 24h (optional, din Crypto.com MCP)
- Footer cu logo-ul brandului + data
**Output:** 2 fisiere PNG pe postare (1200x675 si 1080x1080).

### Agent 5 — Reel Script Writer (CapCut)
**Rol:** transforma stirea intr-un script de 30-45 secunde pentru avatar AI + pachet de assets pentru CapCut.
**Structura script:**
1. **Hook (0-3s):** "Bitcoin just broke $X — here's why it matters."
2. **Context (3-15s):** 2-3 fapte cheie
3. **Impact (15-30s):** ce inseamna pentru holders/traderi
4. **CTA (30-40s):** "Follow for daily crypto updates"
**Output livrat:**
- `reel_script.txt` — textul de citit de avatar
- `reel_scenes.json` — lista de scene cu durate si cue-uri de taietura
- `broll_prompts.txt` — prompt-uri pentru B-roll (stock footage, grafice)
- `captions.srt` — subtitrari ready pentru CapCut
- `thumbnail_prompt.txt` — descrierea thumbnail-ului YouTube Short

### Agent 6 — Publisher / Scheduler
**Rol:** livreaza continutul la platforme la orele programate.
**Mecanism (2 optiuni):**
- **Varianta A (recomandat) — via Make.com MCP / Zapier MCP:** scenariu care primeste payload JSON (text + imagine) si posteaza pe X si Telegram prin integrarile lor native. Aceste platforme au conectori pt. X, Telegram Bot API, Instagram Graph, TikTok Business, YouTube Data API.
- **Varianta B — manual-assisted:** agentul pregateste un "Publishing Queue" intr-un folder dedicat cu sub-foldere `pending/`, `posted/`, iar tu deschizi pachetul de continut si postezi manual (sau folosesti Buffer / Later / Metricool).

### Agent 7 — Analytics & Feedback
**Rol (optional, faza 2):** dupa 24h, citeste engagement (likes, shares, vizionari) si raporteaza ce tip de stire / ora / format performeaza cel mai bine. Ajusteaza scoringul Agentului 2.
**Sursa date:** LunarCrush MCP pentru X, API-urile native ale platformelor.

---

## 2. Sursele de stiri — cum accesam fiecare

| Sursa | Metoda | Cost | Note |
|---|---|---|---|
| **CoinDesk** | RSS: `https://www.coindesk.com/arc/outboundfeeds/rss/` | Gratis | Fiabil, update la 15 min |
| **Cointelegraph** | RSS: `https://cointelegraph.com/rss` | Gratis | Categorie separata RSS pt. Bitcoin/Altcoins |
| **CryptoPanic** | API cu key (free tier 500 req/zi) — `https://cryptopanic.com/api/developer/v2/posts/` | Free tier OK | Agregator cu "importance" score — crucial pentru ranker |
| **Twitter / X accounts** | 2 variante: (a) **LunarCrush MCP** — are deja "Post" si "Topic_Posts" tools; (b) X API v2 — 100$/luna pt. Basic | LunarCrush recomandat | Conturi de urmarit: @WatcherGuru, @WuBlockchain, @DocumentingBTC, @AltcoinDaily, @APompliano, @cz_binance |
| **CoinGecko / CoinMarketCap** | CoinGecko API gratis (50 req/min) — `/coins/markets?order=market_cap_desc` + `/trending` | Gratis | Pt. preturi + trending tokens |
| **MT Newswires MCP** (bonus) | Conector existent in registry | Necesita enable | Stiri financiare incl. crypto, real-time |
| **Crypto.com MCP** (bonus) | Conector existent in registry | Necesita enable | Preturi live, candlestick — util pt. Image Generator |

**Recomandare stack minim pentru start:** CoinDesk RSS + Cointelegraph RSS + CryptoPanic API + LunarCrush MCP. Aceasta combinatie acopera 95% din news flow-ul crypto relevant.

---

## 3. Pipeline X + Telegram (morning / evening)

```
06:30  News Scout   →  ~50 stiri crude (JSON)
06:45  Ranker       →  1 stire selectata pentru dimineata (JSON + rationale)
06:55  Copywriter   →  post_x.txt + post_telegram.txt + image_prompt.txt
07:05  Image Gen    →  news_card_1200x675.png + news_card_1080x1080.png
07:15  QA Gate      →  (optional) human-in-the-loop — tu aprobi/editezi in 30 min
08:00  Publisher    →  POST pe X + Telegram simultan
```

Acelasi flow la ora 17:30 pentru postarea de seara, cu publicare la 19:00.

### Structura output-ului (directorul final de livrare)
```
/queue/
  2026-04-19_morning/
    news_source.json        ← datele brute ale stirii
    post_x.txt              ← tweetul final
    post_telegram.md        ← mesajul Telegram
    image_x.png             ← 1200x675
    image_telegram.png      ← 1080x1080
    publish_manifest.json   ← ce/unde/cand se posteaza
  2026-04-19_evening/
    ...
```

### Sample post (pentru validare format)
**X post (277 chars):**
> 🚨 Bitcoin just flipped $72K — first time since March. ETF inflows hit $1.2B yesterday, the highest single-day since Jan.
>
> Spot demand is back. Miners are holding.
>
> Next resistance: $75K.
>
> #Bitcoin #BTC #Crypto
> → coindesk.com/xyz

**Telegram post:**
> **🚨 Bitcoin Breaks $72K — ETF Inflows Hit $1.2B**
>
> Bitcoin crossed the $72,000 mark at 04:00 UTC, reclaiming a level last seen in March. The move follows a record $1.2B single-day inflow into spot Bitcoin ETFs on Friday.
>
> **Key takeaways:**
> • BlackRock's IBIT alone absorbed $540M
> • Miner reserves hit a 6-month high (bullish — they're holding)
> • Funding rates still neutral — not overheated
>
> **Next levels to watch:** $75K resistance, $68K support.
>
> [Read the full story →](https://coindesk.com/xyz)

---

## 4. Pipeline Reels / Shorts (IG + TikTok + YouTube)

Reels-ul e 1 pe zi (cel mai impactant) sau 2/zi daca vrei fiecare stire sa devina reel. Recomand 1/zi pentru a mentine calitatea.

### Flow complet
```
07:00  Reel Script Writer  →  script.txt + scenes.json + captions.srt + broll_prompts
07:10  (tu sau Agent 6)    →  deschizi CapCut, folosesti AI Avatar feature
                              — paste script, alegi avatar, alegi voce
07:30  CapCut               →  exporti MP4 9:16 (1080x1920)
07:45  Publisher             →  upload pe IG Reels + TikTok + YT Shorts
                              (via Make.com / Zapier sau manual)
```

### Ce primesti de la Agent 5 — Reel Package
Un folder complet:
```
/reels/2026-04-19/
  01_script.txt              ← textul pentru avatar (250-350 cuvinte)
  02_scenes.json             ← [{scene:1, duration:3, overlay:"$72K", broll:"bitcoin chart"}]
  03_captions.srt            ← subtitrarile la secunda, ready de import in CapCut
  04_broll_prompts.txt       ← 5-6 prompt-uri de cautare in stock libraries (Pexels, Storyblocks)
  05_thumbnail_prompt.txt    ← pt. thumbnail YT Short (text + visual direction)
  06_platform_metadata.json  ← titluri diferite pt. IG/TikTok/YT + hashtags + descriere
  07_capcut_instructions.md  ← pas cu pas ce faci in CapCut (avatar selection, voice, timing)
```

### Sample reel script (40 sec)
```
[HOOK — 0-3s, on camera, avatar zoom-in]
"Bitcoin just broke 72 thousand dollars. Here's what nobody is telling you."

[CONTEXT — 3-15s, avatar + B-roll chart overlay]
"Friday's ETF inflows hit one-point-two billion dollars. That's a record since January.
BlackRock alone took half a billion. This isn't retail hype — this is institutional money moving back in."

[IMPACT — 15-30s, avatar + miner reserve graphic]
"Miners are holding their coins. Funding rates are neutral. Translation: demand is real,
not leveraged. The last time we saw this setup, Bitcoin ran another 20 percent in two weeks."

[CTA — 30-40s, on camera close-up]
"Next resistance is 75 thousand. Follow for the 7 PM update on what to watch tomorrow."
```

### Hashtags pre-baked per platforma
- **Instagram:** `#bitcoin #crypto #btc #cryptonews #etf #bullrun #tradingview #blockchain #cryptoupdate #satoshi`
- **TikTok:** `#crypto #bitcoin #fyp #cryptotok #btc #investing #money #financetok`
- **YouTube Shorts:** `#Shorts #Bitcoin #Crypto #BTC #CryptoNews`

---

## 5. Schedule recomandat (EET timezone — Romania)

| Ora | Eveniment |
|---|---|
| 06:30 | News Scout scanare dimineata |
| 06:55 | Copywriter X/TG (morning news) |
| 07:05 | Image Gen (morning) |
| 07:10 | Reel Script Writer (morning reel) |
| 07:15 | Notificare catre tine (review optional) |
| 08:00 | **PUBLISH** X + Telegram (morning) |
| 10:00 | Reel postat pe IG/TikTok/YT (dupa ce ai randat in CapCut) |
| 17:30 | News Scout scanare seara |
| 18:00 | Copywriter + Image Gen + Reel Writer (evening) |
| 19:00 | **PUBLISH** X + Telegram (evening) |
| 21:00 | Evening reel pe IG/TikTok/YT |

---

## 6. Stack tehnic concret

### MCPs de instalat/conectat
1. **LunarCrush MCP** — crypto social signals + X posts din conturi crypto
2. **Crypto.com MCP** — preturi live + candlesticks pentru grafice
3. **MT Newswires MCP** — stiri financiare in timp real
4. **Make.com MCP** (sau Zapier MCP) — pentru publicarea automata pe X/TG/IG/TT/YT
5. **(Optional) n8n** — daca preferi self-hosted

### APIs / feeds direct (fara MCP)
- CoinDesk RSS, Cointelegraph RSS — gratis, fara auth
- CryptoPanic API — necesita key (free)
- CoinGecko API — gratis

### Rendering imagine
- Puppeteer / Playwright (Node.js) pentru a randa HTML template → PNG
- Alternativ: Python + Pillow pentru compozitii simple
- Template-urile le voi putea genera cu Claude in faza 2

### CapCut
- Folosesti **feature-ul AI Avatar** din CapCut PC sau Mobile
- Input: script text + alegi avatar + alegi voce
- Output: MP4 9:16 pe care il exporti si distribui

### Publisher (auto)
- **Make.com** este cea mai simpla — are module native pt. X, Telegram, IG, TikTok, YouTube
- Un scenariu Make primeste webhook JSON, extrage campurile si posteaza in paralel pe toate platformele
- Cost estimat: ~10$/luna pt. 2000 operatii (suficient pt. 2 postari/zi × 5 platforme × 30 zile = 300 operatii)

---

## 7. Roadmap de implementare (faze)

### Faza 1 — Fundatia (1-2 zile) — RECOMANDATA PENTRU START
- [ ] Instaleaza LunarCrush MCP + Crypto.com MCP in Cowork
- [ ] Scriu un script Python/Node care ruleaza News Scout + Ranker si scoate un JSON cu TOP 2 stiri
- [ ] Scriu Copywriter-ul (prompt Claude + template)
- [ ] Rulez manual de 2x/zi timp de 3 zile → validam calitate
- **Livrabil:** fisiere text/md in `/queue/` gata de copy-paste pe X/Telegram

### Faza 2 — Vizualul (2-3 zile)
- [ ] Creez templates HTML/CSS pentru news cards
- [ ] Adaug Image Generator la pipeline
- [ ] Configurez CapCut prompts + Reel Script Writer
- **Livrabil:** pachet complet (text + imagini + reel package) gata de CapCut

### Faza 3 — Automatizarea completa (3-5 zile)
- [ ] Configurez Make.com cu scenarii pentru X + Telegram (entry level)
- [ ] Adaug scheduling via Cowork scheduled tasks sau cron
- [ ] Configurez Publisher pt. IG/TikTok/YT (dupa ce randezi in CapCut)
- **Livrabil:** sistem care posteaza automat la 08:00 si 19:00 fara interventie manuala

### Faza 4 — Optimizare (continuu)
- [ ] Agent Analytics — citeste engagement si ajusteaza scoring
- [ ] A/B testing pe titluri si ore
- [ ] Fine-tuning tone of voice

---

## 8. Riscuri si decizii deschise

1. **Publicarea pe X** — API-ul X costa min. 100$/luna Basic. Alternativa: Make.com are modul nativ X (nu necesita API separat — foloseste OAuth). Recomand Make.
2. **Publicarea pe TikTok** — e cea mai dificila de automatizat. TikTok Business API necesita aprobare. Plan B: postare manuala zilnica de la telefon (3 min/zi).
3. **Avatar CapCut** — feature-ul AI Avatar e in CapCut PC (PRO) si mobile. Nu exista API public pt. rendering automat. Plan B pentru totala automatizare: HeyGen API sau Synthesia API (cost: ~30-90$/luna).
4. **Legal / disclaimer** — postarile despre crypto pot fi interpretate ca "investment advice". Adaugam disclaimer sistematic: "Not financial advice. DYOR." in toate postarile.
5. **Rate limits** — CryptoPanic free = 500 req/zi. Suficient pt. 2 rulari/zi (~50 req fiecare = 100 total).
6. **Aprobare umana sau full-auto?** — recomand pt. prima luna sa lasi un "QA gate" de 30 min unde tu aprobi/editezi inainte de publicare. Dupa ce ai incredere in sistem, elimini gate-ul.

---

## 9. Urmatorul pas — ce iti propun

Cand esti gata, putem sa:

**A)** Treci la Faza 1 — scriu acum codul Python pentru News Scout + Ranker si rulez un test live cu stirile de azi (19 Apr 2026). Iti livrez un JSON + 2 postari ready.

**B)** Creez template-urile HTML pentru Image Generator (news cards) + un reel script de exemplu pe o stire reala de azi.

**C)** Configuram Make.com impreuna — iti fac scenariul care primeste JSON si posteaza pe X + Telegram.

**D)** Facem toate cele de mai sus pe rand.

Spune-mi cu ce continuam.

---

*Document generat: 19 aprilie 2026*
