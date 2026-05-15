# Configurare X OAuth 2.0 — ghid pas cu pas (pentru admin)

Documentul asta ti se adreseaza TIE, proprietarul aplicatiei. Il faci O SINGURA DATA.
Dupa ce il termini, utilizatorii tai (abonatii) pot conecta contul lor X cu un singur click —
**ei nu trebuie sa creeze niciun developer account, nu trebuie sa introduca niciun API key**.

---

## De ce OAuth 2.0 in loc de user+parola

| Metoda veche (twikit) | Metoda noua (OAuth 2.0) |
|---|---|
| Utilizatorul iti da user+parola X | Utilizatorul da click pe buton, autorizeaza in X |
| Risc de ban (X detecteaza ca e bot) | 100% oficial, zero ToS risk |
| Cookie-ul expira in cateva zile | Refresh_token valabil pana utilizatorul il revoca manual |
| Nu functioneaza pentru 10+ useri simultan | Scaleaza la mii de useri |
| X breaks twikit la fiecare 2-3 luni | API stabil |

---

## Pasul 1 — Creaza un X Developer App

1. Mergi la <https://developer.x.com/en/portal/projects-and-apps>
2. Sign in cu contul X al aplicatiei tale (NU contul personal).
3. `+ Add Project` → da-i un nume (ex: "AutoPost SaaS")
4. In proiect: `+ Add App` → nume (ex: "AutoPost prod")
5. Free tier e OK pentru testare. Cand ai primii useri platitori, upgrade la
   **Basic ($100/luna)** pentru 3.000 tweets/luna si media upload OAuth 2.0.

---

## Pasul 2 — Activeaza OAuth 2.0

In app-ul creat:

1. `User authentication settings` → click **Set up**
2. **App permissions**: `Read and write` (bifeaza Tweet + Media)
3. **Type of App**: `Web App, Automated App or Bot`
4. **App info**:
   - **Callback URI / Redirect URL**:
     ```
     https://domeniultau.com/x/callback
     http://localhost:8000/x/callback
     ```
     (adauga-le pe ambele — prima pentru productie, a doua pentru dev local)
   - **Website URL**: `https://domeniultau.com`
   - **Terms of service** / **Privacy policy**: URL-urile tale (X le cere)
5. `Save`

---

## Pasul 3 — Copiaza credentialele

Dupa save X iti arata:
- **Client ID** (formatul `abc123XYZ...` ~30 caractere)
- **Client Secret** (formatul `def456UVW...` ~50 caractere)

**Atentie:** Client Secret e afisat O SINGURA DATA. Daca inchizi tab-ul fara sa-l copiezi,
trebuie sa-l regenerezi.

---

## Pasul 4 — Seteaza-le in `.env`

Deschide `.env` in root-ul proiectului si adauga:

```
X_OAUTH_CLIENT_ID=abc123XYZ...
X_OAUTH_CLIENT_SECRET=def456UVW...
X_OAUTH_REDIRECT_URI=https://domeniultau.com/x/callback
```

Daca testezi local: `X_OAUTH_REDIRECT_URI=http://localhost:8000/x/callback`

**DUPA ASTA RESTARTEZI DASHBOARD-UL** ca sa ia env vars noi.

---

## Pasul 5 — Test cu propriul tau cont X

1. Deschide `http://localhost:8000/settings` (sau URL-ul tau in prod)
2. La sectiunea `✖ X / Twitter` ar trebui sa vezi butonul albastru
   `🔗 Conecteaza contul X`
3. Click → te duce la X → autorizezi app-ul → te aduce inapoi
4. Ar trebui sa vezi `✓ X conectat cu succes`
5. Inapoi la Settings: eticheta verde `✓ conectat ca @username`

---

## Pasul 6 — Test post

1. Mergi la un slot cu `post_x.txt` si `image_x_1200x675.png`
2. Click `Publish X`
3. In `publish_log.json` ar trebui sa vezi:
   ```json
   {"status":"ok","tweet_id":"...","url":"https://x.com/i/web/status/...","via":"oauth2"}
   ```
4. Verifica pe x.com ca tweet-ul e acolo.

---

## Ce experimenteaza utilizatorul tau platitor (abonatul)

1. Se inregistreaza pe aplicatia ta → tu il aprobi
2. Deschide **Settings** → vede butonul `🔗 Conecteaza contul X`
3. Click → X ii deschide "Authorize AutoPost SaaS?" cu lista scopes
4. El apasa Authorize → revine in aplicatia ta cu confirmare
5. Gata. Toate clipurile urmatoare se posteaza automat pe contul lui, fara sa-l mai atinga.

**Tokenul lui se reimproapteaza automat** prin `_ensure_x_oauth_fresh()` la fiecare publish.

---

## Scope-urile pe care le cerem si de ce

| Scope | Motiv |
|---|---|
| `tweet.read` | Obligatoriu pentru `/users/me` (ca sa afisam "@conectat") |
| `tweet.write` | Postare tweets |
| `users.read` | Identificarea contului |
| `offline.access` | Primim `refresh_token` → postare permanenta fara re-login |
| `media.write` | Upload imagini (endpointul `/2/media/upload`) |

---

## Limite (Free tier, cum sunt mai 2025)

- **500 tweets/LUNA pe toata aplicatia** (nu per user) pe Free
- **3.000/luna** pe Basic ($100/luna)
- **300.000/luna** pe Pro (pret variabil)
- **Rate limit per user**: 17 requests/24h

Pentru SaaS real cu 10+ abonati activi: Basic e minimul; pentru 50+ probabil Pro.

---

## Troubleshooting

**"X_OAUTH_CLIENT_ID missing in environment"**
→ `.env` nu e incarcat. Verifica ca fisierul exista in root si dashboardul a fost restartat.

**"invalid_grant"** la callback
→ Redirect URI-ul din cod NU coincide cu ce ai setat in X Developer Portal. Trebuie sa fie
IDENTIC caracter-cu-caracter, inclusiv `http` vs `https` si slash-ul final.

**"invalid_scope"**
→ Aplicatia X nu are scopes activate. In User authentication settings, bifeaza Read+Write.

**Callback da 403 "User mismatch"**
→ Cookie-ul tau de sesiune in aplicatia noastra a expirat in timpul redirectarii prin X.
Re-logheaza-te in aplicatie si reincearca.

**401 cand postezi dupa cateva ore**
→ Refresh-ul a esuat. Clickt `🔄 Reconecteaza X` in Settings.
Daca se intampla frecvent, userul si-a revocat consimtamantul in x.com → Settings → Connected apps.

---

## Checklist final

- [ ] App creat in X Developer Portal
- [ ] Permissions: Read and write
- [ ] OAuth 2.0 activat cu PKCE
- [ ] Callback URL = `https://domeniul.tau/x/callback`
- [ ] `X_OAUTH_CLIENT_ID` in `.env`
- [ ] `X_OAUTH_CLIENT_SECRET` in `.env`
- [ ] `X_OAUTH_REDIRECT_URI` in `.env`
- [ ] Dashboard restart
- [ ] Test cu propriul cont: click Connect → Authorize → ✓ conectat
- [ ] Test post pe un slot real
