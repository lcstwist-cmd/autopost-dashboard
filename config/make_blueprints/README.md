# Make.com blueprints

This folder contains two Make.com scenario blueprints:

| File | Purpose |
|---|---|
| `x_publisher.json`     | Receives a webhook from `publisher.py`, uploads the image to X, posts the tweet. |
| `x_metrics_puller.json` | Runs every 2h, reads recent tweet IDs from a Data Store, fetches `public_metrics`, POSTs them to `metrics_receiver.py`. |

---

# 1. `x_publisher.json` — X publisher

Receives a webhook from `src/agents/publisher.py` and publishes the tweet +
image on X (Twitter).

## Import steps

1. Go to https://eu1.make.com/ (or your region) → **Scenarios** → **Create a new scenario**.
2. In the scenario editor, click the `⋯` menu at the bottom → **Import Blueprint**.
3. Upload `x_publisher.json` from this folder.
4. Open the **Upload Media** and **Create a Tweet** modules, and bind each to
   your X (Twitter) OAuth connection (read + write + media scopes).
5. Click the **Custom webhook** module (first one) and copy the generated URL.
6. Set that URL as `MAKE_X_WEBHOOK_URL` in your `.env` (see `.env.example`).
7. Toggle the scenario **ON** (scheduling: immediately).

## What the webhook expects

The publisher sends a JSON body:

```json
{
  "text":          "tweet body, already under 280 weighted chars",
  "image_base64":  "<base64-encoded PNG>",
  "image_name":    "image_x_1200x675.png",
  "image_mime":    "image/png",
  "posted_at_utc": "2026-04-19T06:30:00+00:00"
}
```

And receives back:

```json
{ "ok": true, "tweet_id": "...", "media_id": "..." }
```

## Why a webhook, not direct X API

The free X API tier is rate-limited in ways that break automation reliably, and
media upload requires multi-part OAuth 1.0a signing. Make.com handles both for
free on its starter plan and gives you a visible log of every post.

---

# 2. `x_metrics_puller.json` — Auto-populate metrics.json

Closes the analytics loop: pulls `public_metrics` (impressions, likes, retweets)
for each posted tweet and sends them to the local receiver, which writes
`queue/<date>_<slot>/metrics.json` and rebuilds the dashboard.

## Prerequisites

1. **Create a Make.com Data Store** called `CryptoAutoPost_Tweets` with these
   columns (Data Stores → Add):

   | Column     | Type   |
   |------------|--------|
   | `tweet_id` | Text   |
   | `date`     | Text (YYYY-MM-DD) |
   | `slot`     | Text (`morning` / `evening`) |
   | `posted_at`| Date   |

2. **Populate it from `x_publisher.json`** — edit that scenario and add one
   module AFTER `Create a Tweet`:

   - Module: **Data Stores → Add a Record**
   - Data Store: `CryptoAutoPost_Tweets`
   - Key: `{{4.id}}` (the tweet_id from Create a Tweet)
   - Record:
     - `tweet_id`  → `{{4.id}}`
     - `date`      → `{{formatDate(now; "YYYY-MM-DD")}}`
     - `slot`      → parse from `{{1.posted_at_utc}}` or pass `slot` through the webhook (update `publisher.py` to include it — see note below)
     - `posted_at` → `{{now}}`

   > Note: to know the slot at Make.com time, have `publisher.py` include
   > `"slot": "morning"` (or evening) in the webhook JSON. The webhook already
   > accepts arbitrary extra keys, so adding it is a 1-line change in
   > `_x_webhook_payload()`.

3. **Generate an X API v2 Bearer Token** — go to
   https://developer.twitter.com/ → your App → Keys & Tokens → **Bearer Token**.
   Read-only is enough for `/tweets/:id`.

4. **Expose `metrics_receiver.py` to the internet** — run locally:
   ```bash
   export METRICS_WEBHOOK_SECRET="pick-a-long-random-string"
   python src/agents/metrics_receiver.py serve
   # then in another terminal:
   ngrok http 8787
   # copy the https://xxxx.ngrok.app URL
   ```

## Import steps

1. In Make.com, **Scenarios → Create a new scenario → Import Blueprint**.
2. Upload `x_metrics_puller.json`.
3. Click the scenario title → **Scenario variables**, add three:
   - `X_BEARER_TOKEN`        = the bearer token from step 3 above
   - `METRICS_RECEIVER_URL`  = `https://xxxx.ngrok.app` (no trailing slash, no `/metrics`)
   - `METRICS_WEBHOOK_SECRET`= same secret you put in `.env`
4. Open the **Search Records** module and confirm the Data Store binding.
5. Toggle scenario **ON**. Default schedule: every 2 hours.

## Flow summary

```
[Timer every 2h]
    → [Data Store: Search Records]       ← tweet_ids from last 72h
    → [HTTP: GET api.twitter.com/2/tweets/:id?tweet.fields=public_metrics]
    → [Compose JSON]                     ← {date, slot, x:{...}}
    → [HTTP: POST {receiver}/metrics]    ← Bearer {METRICS_WEBHOOK_SECRET}
```

Each iteration updates one slot's `metrics.json`; the receiver rebuilds the
dashboard once per POST. Variant-A vs variant-B winners will populate the
**A/B Performance** section of `analytics/rollup.md` automatically.

## Without Make.com: manual entry

You don't have to set any of this up. You can populate `metrics.json` by hand:

```bash
python src/agents/metrics_receiver.py set \
    --date 2026-04-19 --slot morning \
    --x-impressions 1450 --x-likes 38 --x-retweets 5 \
    --tg-views 820
```

This writes the file and refreshes analytics + the dashboard in one step.
