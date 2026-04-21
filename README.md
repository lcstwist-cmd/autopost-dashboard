# Crypto AutoPost System

Multi-agent pipeline that reads crypto news, picks the 2 most important stories of the day, and produces ready-to-publish content for X, Telegram, Instagram, TikTok and YouTube.

Target cadence: **2 posts/day** (morning ~08:00 EET, evening ~19:00 EET).

See `PLAN_Crypto_AutoPost_System.md` for the full architecture.

## Project structure

```
/
├── PLAN_Crypto_AutoPost_System.md   ← architecture + roadmap
├── requirements.txt                  ← Python deps
├── .env.example                      ← env vars template
├── README.md
├── src/
│   ├── agents/
│   │   ├── news_scout.py             ← Agent 1 — RSS + CryptoPanic + LunarCrush
│   │   ├── ranker.py                 ← Agent 2 — picks TOP 2 stories
│   │   ├── copywriter.py             ← Agent 3 — X A/B + TG posts (Anthropic API)
│   │   ├── image_gen.py              ← Agent 4 — HTML → PNG (Playwright)
│   │   ├── reel_writer.py            ← Agent 5 — reel script + CapCut brief
│   │   ├── publisher.py              ← Agent 6 — TG direct + X via Make.com
│   │   ├── analytics.py              ← Agent 7 — rollup across slots
│   │   ├── metrics_receiver.py       ← HTTP server + CLI → writes metrics.json
│   │   └── pipeline.py               ← end-to-end orchestrator
│   ├── dashboard/
│   │   ├── build.py                  ← inline rollup.json → index.html
│   │   └── index.html                ← generated, open in browser
│   └── templates/
│       └── news_card.html            ← 1200x675 / 1080x1080 card template
├── config/
│   └── make_blueprints/
│       ├── x_publisher.json          ← X publisher (webhook → tweet + media)
│       ├── x_metrics_puller.json     ← scheduled metrics fetcher → receiver
│       └── README.md                 ← import steps for both blueprints
├── analytics/                        ← generated after each publish
│   ├── rollup.json                   ← machine-readable aggregate
│   └── rollup.md                     ← human-readable summary
└── queue/                            ← daily output folder (auto-created)
    └── YYYY-MM-DD_morning|evening/
        ├── raw_news.json             ← everything Scout found
        ├── top2.json                 ← what Ranker picked
        ├── post_x.txt                ← X variant A (factual hook)
        ├── post_x_b.txt              ← X variant B (question hook)
        ├── post_telegram.md          ← ready for Telegram
        ├── image_prompt.txt          ← brief for image generator
        ├── image_x_1200x675.png      ← X/OG card
        ├── image_tg_1080x1080.png    ← Telegram square
        ├── reel/                     ← 7-file CapCut package
        ├── publish_log*.json         ← audit of publish attempts
        └── metrics.json              ← (optional) post-publish metrics
```

## Quick start

```bash
# 1. Install deps
pip install -r requirements.txt --break-system-packages
playwright install chromium

# 2. Configure env vars
cp .env.example .env
# edit .env and fill in ANTHROPIC_API_KEY, TELEGRAM_*, MAKE_X_WEBHOOK_URL
set -a; source .env; set +a

# 3. Dry-run the full pipeline (no posting)
python src/agents/pipeline.py --slot morning

# 4. When ready, publish for real
python src/agents/pipeline.py --slot morning --publish

# 5. Only run up to a given stage
python src/agents/pipeline.py --slot morning --stop-after rank
```

## Pipeline stages

| # | Agent | Stage flag | Output |
|---|---|---|---|
| 1 | News Scout | `scout` | `raw_news.json` |
| 2 | Ranker | `rank` | `top2.json`, `summary.txt` |
| 3 | Copywriter | `copy` | `post_x.txt` (variant A), `post_x_b.txt` (variant B), `post_telegram.md`, `image_prompt.txt` |
| 4 | Image Generator | `image` | `image_x_1200x675.png`, `image_tg_1080x1080.png` |
| 5 | Reel Writer | `reel` | `reel/` (7 files incl. CapCut brief) |
| 6 | Publisher | `publish` | `publish_log*.json` + actual posts |

Use `--stop-after <stage>` to stop early.

## Environment variables

See `.env.example`. Required for a full live run:

- `ANTHROPIC_API_KEY` — for the Copywriter (Claude Sonnet 4.5 by default)
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` — for direct Telegram posting
- `MAKE_X_WEBHOOK_URL` — for X posting via Make.com (see `config/make_blueprints/README.md`)

Optional:

- `CRYPTOPANIC_API_KEY` — better ranking signal; falls back to RSS without it.
- `LUNARCRUSH_API_KEY` — social-momentum boost + synthesized candidates for
  trending tickers with no news coverage.
- `METRICS_WEBHOOK_SECRET` — shared secret for `metrics_receiver.py`
  (required only if you expose the receiver).
- `CLAUDE_MODEL` — override copywriter model.
- `AUTOPOST_QUEUE` — move the `queue/` directory elsewhere.

## News sources

| Source | Access | Auth |
|---|---|---|
| CoinDesk | RSS | none |
| Cointelegraph | RSS | none |
| CryptoPanic | REST API | free key (optional) |
| LunarCrush | REST API v4 | paid key (optional) — boosts socially-hot tickers + synthesizes momentum candidates |

## Publishing model

Hybrid by design:

- **Telegram** — posted directly by `publisher.py` using the Bot API (`sendPhoto`
  with Markdown caption, or `sendPhoto` + `sendMessage` when the caption exceeds
  Telegram's 1024-char limit).
- **X (Twitter)** — posted via a Make.com scenario reachable at a custom webhook.
  The webhook receives the tweet body + base64-encoded PNG and handles OAuth +
  media upload. Import `config/make_blueprints/x_publisher.json` into Make.com
  and paste the generated URL into `MAKE_X_WEBHOOK_URL`.

Default mode is **dry-run**: payloads are written to `publish_log_dryrun.json`
inside the queue folder. Add `--publish` to actually send.



## A/B testing (X)

Every slot generates two variants of the X post:
- `post_x.txt`  — **variant A**, factual hook (leads with the number)
- `post_x_b.txt` — **variant B**, question hook (reframes the news)

The publisher picks A or B deterministically per slot (stable hash of the slot
name, so re-runs never flip the choice), or you can force one with
`--variant A` / `--variant B`. The chosen variant is written to
`publish_log.json` under the `variant` key.

## Analytics + dashboard

After each publish the pipeline automatically refreshes:

- `analytics/rollup.json` + `analytics/rollup.md` — cross-slot aggregates,
  success rates, variant distribution, A/B performance (if metrics available)
- `src/dashboard/index.html` — a single-file static dashboard (open in browser)

A/B performance is computed only if you drop a `metrics.json` file in the slot
folder, shaped like:

```json
{
  "x":        { "impressions": 1450, "likes": 38, "retweets": 5 },
  "telegram": { "views": 820 }
}
```

Populate it manually after reading X / TG analytics, or auto-populate it:

- **CLI (manual)** —
  ```bash
  python src/agents/metrics_receiver.py set \
      --date 2026-04-20 --slot morning \
      --x-impressions 2100 --x-likes 72 --x-retweets 14 --tg-views 950
  ```
- **Webhook (auto)** — run `python src/agents/metrics_receiver.py serve`,
  expose it with ngrok, then import `config/make_blueprints/x_metrics_puller.json`
  in Make.com. Full instructions in `config/make_blueprints/README.md`.

Either path triggers an analytics + dashboard refresh automatically.

Run analytics + dashboard standalone:

```bash
python src/agents/analytics.py
python src/dashboard/build.py
```

## Scheduling

Two Cowork scheduled tasks trigger the pipeline automatically:

- **Morning** — 06:30 UTC (≈ 09:30 EET), runs `--slot morning --publish`.
- **Evening** — 17:30 UTC (≈ 20:30 EET), runs `--slot evening --publish`.

You can list / edit them via the Cowork scheduled-tasks manager.
