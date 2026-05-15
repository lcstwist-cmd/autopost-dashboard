# Postare pe X fara API — metoda gratuita cu cookies

Daca nu vrei sa faci un X Developer account (nu costa, dar dureaza ~10 min
de configurat), ai alternativa asta: **lipeste cookie-urile tale X in Settings
si gata**. Postarea se intampla automat de acolo inainte, fara sa mai faci nimic.

---

## De ce merge asta

Cand te loghezi la x.com, browser-ul primeste doua cookie-uri importante:
- `auth_token` — dovada ca esti logat
- `ct0` — token CSRF care insoteste fiecare actiune (post, like, follow...)

Le copiezi in aplicatie. Aplicatia porneste un browser invizibil (Playwright),
pune cookie-urile pe care i le-ai dat, si posteaza ca si cum ai fi tu logat.
**Nu stim parola ta** — nici nu ne trebuie.

---

## Efort total: 10 secunde, 3 click-uri

### Pasul 1 — Instaleaza Cookie-Editor (o singura data)
Extensie free, 100K+ useri, verificata de Google:
<https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm>

Click `Add to Chrome` → `Add extension`. Gata.

### Pasul 2 — Logheaza-te pe x.com
Deschide <https://x.com> si asigura-te ca esti logat. Daca ai 2FA, fa 2FA normal,
manual — o singura data.

### Pasul 3 — Exporteaza cookies
- Click pe iconita Cookie-Editor (sus dreapta in Chrome, puzzle → pin it)
- Cookie-Editor deschide un mini-panel cu toate cookie-urile x.com
- Jos, buton `Export` → alege `Export as JSON` → **cookie-urile se copie automat in clipboard**

### Pasul 4 — Lipeste in aplicatie
- Deschide dashboard-ul AutoPost → `Settings`
- La sectiunea `🍪 GRATUIT — Postare fara X API`
- Click in casuta mare si apasa `Ctrl+V`
- Click `💾 Salveaza cookies`

Aplicatia verifica imediat cu X ca sunt valide si afiseaza `✓ Salvat ca @handleul_tau`.

### Pasul 5 — Gata
De acum incolo orice clip se publica automat pe contul tau X. Nu mai faci nimic.

---

## Cum sa vezi ca functioneaza

**In Settings:**
- Eticheta verde `✓ salvat ca @user` in dreptul sectiunii.
- Buton `🩺 Testeaza` — face un ping rapid la X, iti spune daca mai sunt valide.

**In publish_log.json** dupa o publicare:
```json
{"status":"ok","url":"https://x.com/home","via":"cookies"}
```

**Pe X:** Deschide profilul tau si vezi tweet-ul.

---

## Cand reexpirti cookies

Cookie-urile X sunt valide ~30 zile de la ultima activitate. Daca testul pica
(`🩺 Testeaza` afiseaza rosu), inseamna ca X te-a delogat (poate te-ai logat din
alt device si au expirat sesiunile vechi). Soluta:

1. Logheaza-te din nou pe x.com in Chrome
2. Cookie-Editor → Export as JSON
3. Vino in Settings → lipeste peste ce ai → `Salveaza`

Dureaza 10 secunde.

---

## Securitate si best practices

**Ce stim noi:**
- Cookie-urile tale (criptate in DB, legate de userul tau si nimeni altcineva)
- Ce stii tu ca ti-ai dat

**Ce NU stim:**
- Parola ta de X (nici nu ne trebuie)
- Cookie-urile altor useri de pe acelasi calculator (Cookie-Editor exporta doar x.com)

**Poti revoca oricand:**
- In aplicatie: `Settings → Sterge` → cookie-urile sunt sterse complet din DB
- In X: `x.com → Settings → Sessions → Log out all other sessions` → toate cookie-urile
  vechi sunt invalidate global (inclusiv cele din aplicatia noastra)

**Nu ne trimite cookie-urile prin email / chat. Lipeste-le doar direct in Settings.**

---

## Cand cookie-urile NU merg

**X te flag-uieste pentru activitate automata:**
- Daca postezi prea multe tweet-uri pe zi (>15-20), X devine suspicios
- Solutie: rate limit in app la ~5-10/zi, spread pe ore diferite

**2FA per tweet:**
- Foarte rar, dar X poate cere 2FA pe anumite actiuni (conturi noi, IP nou)
- Dezactiveaza in x.com → Settings → Security → Two-factor authentication
  (sau foloseste calea OAuth 2.0 in loc)

**Cookie-urile se sterg la logout:**
- Daca te deloghezi manual de pe x.com, cookie-urile din aplicatie devin invalide
- Relogheaza-te, reexporta, lipeste

---

## Comparatie intre metode

| Metoda | Efort setup | Credentiale la noi | Durabilitate | Limite post |
|---|---|---|---|---|
| **Paste cookies** (asta) | 10 sec, 1x | nimic (doar cookies) | ~30 zile, reexpirti | 15-20/zi safe |
| OAuth 2.0 (admin setup) | 10 min, 1x admin | access+refresh token | nelimitat | 500/luna Free, 3K Basic |
| user+email+parola (twikit) | 30 sec, 1x user | parola (!) | ~zile, instabil | 15-20/zi |
| Tweepy BYOK | 10 min, 1x user | 4 chei API | nelimitat | 500/luna Free |

**Recomandare:**
- Daca esti admin si vrei cea mai buna experienta pentru useri: **OAuth 2.0**
- Daca vrei zero setup pentru admin: **Paste cookies**
- Daca userul are 2FA activ: **Paste cookies** (2FA e OK in login-ul manual)

---

## Troubleshooting

**"Missing required cookies: ['auth_token']"**
→ Nu esti logat pe x.com in browserul unde ai exportat. Logheaza-te, reexporta.

**"Invalid JSON"**
→ Ai lipit ceva care nu e JSON. Asigura-te ca folosesti `Export as JSON`,
nu `Export as Netscape` sau alte formate.

**"Cookies look valid but X rejects them"**
→ X te-a delogat. Logheaza-te din nou, re-exporta, re-paste.

**Butonul Save ramane la `Se verifica cu X…`**
→ Dashboard-ul nu poate ajunge la x.com din reteaua lui. Verifica conexiunea.

**Publishing pica cu `Cookies expired`**
→ Cookie-urile au expirat intre Save si Publish. Re-exporta.
