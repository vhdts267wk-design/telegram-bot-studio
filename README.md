# Telegram Bot Studio

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/telegram-bot-studio?referralCode=asepsp&utm_medium=integration&utm_source=template&utm_campaign=generic)

An open-source Telegram bot and web studio built with
[`python-telegram-bot`](https://python-telegram-bot.org/), FastAPI, PostgreSQL,
Docker, and Railway.

![Telegram Bot Demo](img/bot.png)

## Features

- Persistent chat menu buttons after `/start`
- `/start`, `/help`, `/about`, and `/ping` commands
- `/gold`, `/market`, `/signals`, `/reviews` and `/news` work without OpenAI or screenshots
- `/market` reads completed MT5 M15/M5/M1 candles; `/news` remains a separate request
- Empirically gated **Demo MT5 proposals at 0.01 lot**, with a native ticket draft and the final Buy/Sell click made by the human
- Echo replies for normal text messages
- Fallback handler for unknown commands
- Error logging
- **PostgreSQL persistence** (asyncpg): users are stored/updated on `/start`
- Per-process message counter for echoed messages
- **Telegram Bot Studio**: a password-protected web dashboard to add/edit/delete dynamic
  commands (text, photo/document, and reply keyboards) without redeploying
- Configuration loaded from a local `.env` file or Railway variables
- Ready to run with Docker and Railway

## Data Store

PostgreSQL is optional for a plain bot without the admin panel or bridge HTTP. When
`DATABASE_URL` is set, the bot connects on startup and creates its tables
automatically. When `PANEL_PASSWORD` enables the panel, a working
`DATABASE_URL` is required and startup fails with a clear error if it is missing
or unreachable. A configured `MARKET_BRIDGE_KEY` of at least 32 characters also
enables HTTP and requires PostgreSQL, even with the admin panel disabled.
Automatic subscriptions, paper reviews, device pairing and approved trade
requests use that durable store.

- **PostgreSQL** — a `users` table is created automatically on first run. `/start` inserts
  a new user or refreshes `username`, `first_name`, and `last_seen` for an existing one.
  Without a database, `/start` still greets the user but nothing is persisted.

Connections are pooled with asyncpg, reused across updates, and closed cleanly
on shutdown.

## Chat Menu Buttons

The bot shows a persistent reply keyboard after `/start` with these buttons:

| Button  | Action                           |
| ------- | -------------------------------- |
| `Help`  | Show available commands          |
| `About` | Show short bot information       |
| `Ping`  | Check whether the bot is running |
| `Market` | XAUUSD M15 direction, M5 confirmation and M1 timing |
| `News` | Cited political and economic news |
| `Signals` | A qualified manual Demo proposal or an explicit waiting reason |

Telegram bots cannot display custom buttons before a user starts or messages the bot. The keyboard appears after the bot replies, then stays available in supported Telegram clients.

## Bot Commands

| Command  | Description                      |
| -------- | -------------------------------- |
| `/start` | Show the welcome message         |
| `/help`  | Show available commands          |
| `/about` | Show short bot information       |
| `/ping`  | Check whether the bot is running |
| `/gold`  | Market report when connected; optional chart-photo guidance otherwise |
| `/market` | XAUUSD data without screenshots |
| `/news` | Cited political and macroeconomic developments |
| `/signals` | Inspect a qualified M15/M5/M1 proposal or its blocking reason; no order is sent |
| `/reviews` | Legacy reference-paper observation records; these are not the MTF qualification study |
| `/watch` | Opt in to private monitoring; MT5 status updates are sent when the state or reason changes |
| `/unwatch` | Stop reports and cancel pending preparation requests; close any prepared window yourself |
| `/connect_mt5 CODE` | Pair the locally started Demo bridge in its owner's private chat |

The built-in commands above always take precedence. Any other `/command` is
resolved dynamically from commands you create in Telegram Bot Studio.

The default configuration makes no OpenAI requests, even when an API key is
already present. `OPENAI_ENABLED=false` and `NEWS_SOURCE=rss` keep price
collection, signals, reviews and public news independent of OpenAI billing.
Photo messages point users to the screenshot-free commands.

The legacy chart-photo feature is disabled unless an owner explicitly sets
`OPENAI_ENABLED=true` and an API key. Paid web search additionally requires
`NEWS_SOURCE=openai`; an RSS error never falls back to a paid provider.

## Market and news monitoring without screenshots

Send `/market` or `/news` for a report. With MT5, `/watch` checks for qualified
opportunities every five seconds; status delivery is checked each minute and
unchanged states/reasons are suppressed. The separate reference workflow keeps
its 15-minute report interval. `/unwatch` stops monitoring.
Subscriptions, delivery leases, collected prices and search usage survive restarts
in PostgreSQL. Reports are private-chat opt-ins. A report already being delivered
can finish its in-flight Telegram request when unsubscribing.

The current MT5 strategy is `mtf-ema-pullback-60m-v1`: **M15 direction → M5
pullback/recovery confirmation → M1 close beyond the previous candle's range**.
It uses EMA9/EMA21 and ATR14 from actual completed broker candles. Each stream
contains at most 64 actual bars and needs a contiguous suffix of at least 22;
any gap resets indicator warmup. Missing bars are never filled, and no forming
candle or later M15/M5 close can confirm an earlier M1 decision.

Entry uses the current executable Ask for BUY or Bid for SELL, rounded adversely
to the broker tick grid. The stop uses a recent five-bar M5 swing plus a 0.2 ATR
buffer; TP1 is 2R and the supplementary TP2 is 3R. The static reference entry
zone is entry ±0.1R, rounded inward. Published entry, SL and TP remain fixed;
the live spread-plus-price-drift guard still applies and a displayed entry is
not a guaranteed fill.

**No BUY/SELL alert, chart proposal or native preparation is available until
the empirical gate passes.** A server-local artifact, pinned separately by its
SHA256, must match the exact strategy/policy/implementation, broker and verified
cost model. It must contain at least **200 independent, nonoverlapping
out-of-sample trades** per required cost scenario and a **lower endpoint of the
two-sided 95% Wilson confidence interval of at least 70%**, after costs. An
observed 70% win rate alone is insufficient. A separately evidenced independence
assessment must establish that the effective independent sample size equals the
raw OOS trade count; missing or reduced effective sample size blocks
qualification. Nonoverlap alone does not establish independence. A historical
confidence bound is neither a probability for the next trade nor a guarantee.
Feed-provided reports, AI confidence and provisional research cannot qualify.

Success means TP1 is reached before SL with positive modeled net cash within
**60 minutes from actual entry in the tick replay**, rather than from the signal
candle or message. A timeout is a nonwin. Entry must be the next executable
tick after the decision within ten seconds; the horizon is
purged across dataset splits. TP2 does not count as the success target. Candle
OHLC can give provisional first-barrier bounds, but cannot resolve intrabar
ordering or prove fills and therefore cannot produce a qualified artifact.

The default has **unverified costs and no pinned evidence artifact**, so MT5
proposals remain blocked. Unknown costs are not treated as zero. The live
filters require a quote at most ten seconds old and cache/snapshot/risk data at
most 30 seconds old. Every actionable alert, chart proposal and native ticket
preparation must remain within **ten seconds of the triggering M1 candle's
close**. Technical observations may use that closed bar for up to 75 seconds,
but that observation window never extends trade validity. Quote clock skew is
bounded to five seconds; forming bars have no grace.
They also require Demo 0.01 lot, no open positions or pending orders, estimated
loss including costs ≤1% equity, free margin ≥2 times required margin, effective
reward/risk ≥1.5, and spread ≤min(0.1R, 0.15 M1 ATR). The weekday UTC 06–19
window must accommodate the 60-minute horizon plus ten seconds of entry delay.
Broker holidays/session closures still require the broker's actual availability
checks. Tick activity must be at least half the preceding 20-bar median; it is
a price-update proxy, not verified traded volume or market depth.

Historical qualification requires same-broker UTC Bid/Ask tick history,
completed M1/M5/M15 history, file fingerprints, verified timestamp/DST provenance,
chronological development/validation/untouched OOS splits, and commission,
slippage, financing and 0.01-lot contract/cash-conversion assumptions covering
the tested period. Record historical tick size, point, digits and stop/margin
rules. Verified historical execution specifications must cover the full tested
period and match the live symbol specifications exactly; verified costs and
timestamp/timezone/DST provenance must cover that same period. A current symbol
setting or today's UTC offset cannot establish old specifications. Zero candle
spreads, zero real volume, missing ticks or declared
simulated costs must not be presented as verified execution evidence. Keep raw
history and private broker metadata local; publish only a reviewed summary.

By default `MARKET_SOURCE=reference` uses keyless USD gold reference prices from
[GoldAPI](https://gold-api.com/llms.txt). This independent reference feed is not a
broker execution price. The bot records at most one observed quote per minute
while subscribed, or when `/market` is requested. It preserves the provider's
UTC timestamp and reports old or unavailable prices explicitly.

M15 bars are built from these observed samples, not downloaded historical broker
OHLC. Collection needs time to warm up. A completed 15-minute interval needs at
least ten samples, coverage near both edges and no gap over three minutes before
its observed range is described. Missing prices are never filled. Hourly/four-hour
comparisons require contiguous sufficiently covered intervals. GoldAPI's
[terms](https://gold-api.com/terms) provide reference data as-is; availability or a
fresh provider timestamp does not establish that a market is open.

News defaults to public official RSS from the [Federal Reserve](https://www.federalreserve.gov/feeds/feeds.htm),
[ECB](https://www.ecb.europa.eu/rss/press.html), and [UN News](https://news.un.org/feed/subscribe/en/news/all/rss.xml).
The bot shares at most six linked headlines with publication dates within the
last 24 hours. Headlines remain in the source language; no AI translation or
price-impact prediction is made. The UN feed is filtered for politics and
macroeconomics. This is limited coverage, not a comprehensive news service.
An empty or unavailable bulletin is reported explicitly; older headlines are
never presented as new. Results are cached for 15 minutes by provider.

The separate legacy reference workflow can open immutable paper journals in
PostgreSQL. The observation window starts after Telegram accepts the signal and
lasts **15 minutes**. The worker checks collected reference samples every minute;
it reports the first observed SL/TP crossing or reviews expiry. A missing gap
of more than three minutes or an old expiry sample produces an inconclusive
review. Sampled observations cannot rule out an earlier unobserved crossing and
are not order fills. `/reviews` displays recent records and outcome-specific
notes; `/signals` only inspects a setup and does not open a journal record.

A fixed experimental risk policy pauses new automatic signals for **45 minutes**
after three consecutive fully covered observed stops for the same source and
strategy in a chat. An inconclusive, expired or target result breaks the streak.
This cutoff adapts alert availability to the recorded results; it does not
optimize EMA/ATR values, alter previous levels, or establish better returns.

Price/news failures do not block Telegram polling, `/ping`, or Railway readiness.
Telegram rate limits delay deliveries rather than retrying continuously.
Delivery leases reduce duplicate reports during deployment overlap, but a crash
between Telegram accepting a message and database acknowledgement can cause a
duplicate; delivery is not an exactly-once guarantee. Paper-review delivery
also assumes one polling replica. A failure after a signal is accepted but
before journal persistence can leave that signal without a review record.

The current [Windows manual helper](bridge/MANUAL-TICKETS.md) uses an already
running connected MT5 terminal, reads the actual account and broker settings,
and uploads the three closed candle streams. Install its Windows dependencies
separately from server requirements. Use `MARKET_SOURCE=mt5`,
`MT5_MANUAL_TICKETS_ENABLED=true`, `MT5_TRADING_ENABLED=false` and a distinct
private `MT5_MANUAL_BRIDGE_KEY` of at least 32 characters. PostgreSQL is required
for pairing and private bridge HTTP. The manual key never falls back to the
legacy bridge key. Old automatic Accept execution is not part of this workflow.

Pair the user-started helper in its owner's private chat with `/connect_mt5 CODE`.
Only a still-qualified, fresh device-bound proposal can expose **جهّز على
اللابتوب**. That action fills a visible native MT5 ticket at Demo 0.01 lot with
the frozen SL/TP; it sends no order. Review the direction, quote, volume and
protection, then make the final native Buy/Sell click yourself while the signal
remains valid. Preparation is refused after ten seconds from M1 close. If that
deadline passes before your final click, cancel the draft and wait for a fresh
qualified signal. Expiry or `/unwatch` does not close a successfully prepared
window or an open position. Keep Windows and the selected terminal
awake, retain the local attempt ledger, and do not share pairing codes or keys.

## Telegram Bot Studio

A lightweight web dashboard (FastAPI) runs **in the same process** as the bot —
no extra service required. Use it to manage dynamic commands at runtime:

- Add, edit, enable/disable, and delete commands without redeploying.
- Reply types: **text**, **photo**, or **document** (media via URL or a Telegram
  `file_id`), each with an optional **reply keyboard**.
- Changes apply immediately: the in-process registry and the Telegram command
  menu are refreshed on every save.
- Reply-keyboard changes appear after the user runs `/start` again; Telegram
  clients do not expose a remote keyboard-refresh API.
- Schema changes are managed with Alembic and applied automatically on startup.

**Enabling it:** the panel is served only when `PANEL_PASSWORD` is set, and it
requires `DATABASE_URL` (Postgres is the command store). Without `PANEL_PASSWORD`
the admin pages are disabled; a configured private bridge can still serve HTTP.
With neither the panel nor bridge HTTP enabled, the bot is a plain poller.
Once the panel is enabled, open your Railway service URL (or
`http://localhost:8080` locally) and sign in with `PANEL_USERNAME` / `PANEL_PASSWORD`.

For a service with the panel or private bridge HTTP enabled, configure Railway's health check as
`/healthz`. It reports ready only while the bot is running, polling is active,
PostgreSQL responds, and Telegram accepts the bot token. Telegram checks time
out after three seconds and are cached for ten seconds. A rejected token needs
to be replaced privately in the active bot service, followed by a redeploy.
Leave this health check unset for a plain poller with neither HTTP feature enabled,
which does not serve HTTP. Run only one polling deployment for each bot token;
disconnect automatic deployments on any retired duplicate service.

Security: credentials are checked in constant time, sessions use signed cookies,
and all state-changing forms are CSRF-protected. Always use a strong
`PANEL_PASSWORD` since the panel is reachable on your public Railway domain.

## Project Structure

```text
.
├── bot/
│   ├── __init__.py
│   ├── commands.py     # In-process registry for dynamic commands
│   ├── config.py
│   ├── db.py           # PostgreSQL pool and queries
│   ├── handlers.py
│   ├── main.py
│   └── panel/          # Telegram Bot Studio (FastAPI: app, auth, templates, static)
│       ├── app.py
│       ├── auth.py
│       ├── templates/
│       └── static/
├── .env
├── .env.example
├── .dockerignore
├── compose.yaml
├── CONTRIBUTING.md
├── .gitignore
├── Dockerfile
├── LICENSE
├── SECURITY.md
├── railway.json
├── README.md
└── requirements.txt
```

## Set Up the Bot Token

1. Create a bot with Telegram `@BotFather`.
2. Copy the bot token.
3. Add the token to `.env`:

## Environment Variables

| Name           | Required | Default | Description                                        |
| -------------- | -------- | ------- | -------------------------------------------------- |
| `BOT_TOKEN`        | Yes | -       | Bot token from `@BotFather`                                   |
| `OPENAI_ENABLED` | No | `false` | Explicit opt-in for legacy paid features; all OpenAI requests disabled by default |
| `NEWS_SOURCE` | No | `rss` | Public official headlines; `openai` requires explicit paid opt-in |
| `OPENAI_API_KEY` | Legacy paid features only | - | Not needed for prices, paper signals, reviews or RSS |
| `OPENAI_MODEL`     | No | `gpt-4.1-mini` | Image-capable Responses API model available to your OpenAI project |
| `DATABASE_URL`     | For panel or bridge HTTP | - | PostgreSQL store; also needed for automatic reports and review history |
| `PANEL_PASSWORD`   | No  | -       | Enables Telegram Bot Studio when set; password to sign in     |
| `PANEL_USERNAME`   | No  | `admin` | Username for Telegram Bot Studio                              |
| `PANEL_SECRET_KEY` | No  | derived | Secret for signing panel session cookies (derived from password if empty) |
| `PANEL_SECURE_COOKIE` | No | Railway: `true` | Require HTTPS for panel session cookies |
| `PORT`             | No  | `8080`  | HTTP port for panel/bridge endpoints (Railway injects this automatically) |
| `LOG_LEVEL`        | No  | `INFO`  | Logging level, such as `DEBUG`, `INFO`, or `ERROR`            |
| `MARKET_SOURCE` | No | `reference` | Keyless reference prices, or explicitly `mt5` for a configured broker bridge |
| `MARKET_BRIDGE_KEY` | For MT5 only | - | Private ingest/control key of at least 32 characters; enables HTTP and requires PostgreSQL; never send in chat |
| `MARKET_GOLD_SYMBOL` | For MT5 only | `XAUUSD` | Exact broker gold symbol, including any suffix |
| `MT5_TRADING_ENABLED` | No | `false` | Retired automatic order path; keep false for the MTF/manual workflow |
| `MT5_MANUAL_TICKETS_ENABLED` | No | `false` | Allow gated Demo 0.01 native preparation; human final click |
| `MT5_MANUAL_BRIDGE_KEY` | Manual MT5 only | empty | Distinct private manual bridge key; never put it in chat or a public artifact |
| `MT5_EVIDENCE_PATH` | Qualified MT5 proposals only | empty | Server-local reviewed empirical artifact; never accepted from feed/API |
| `MT5_EVIDENCE_SHA256` | Qualified MT5 proposals only | empty | Operator-controlled SHA256 pin for that exact artifact |
| `MT5_COST_MODEL_VERIFIED` | Windows helper | `false` | True only after the operator verifies the explicit cost model |
| `MT5_COMMISSION_ROUND_TURN_PER_LOT` | Windows helper | unknown | Round-trip commission in account currency per lot; no missing-to-zero default |
| `MT5_SLIPPAGE_PRICE` | Windows helper | unknown | Conservative per-side price allowance, matched to the reviewed study |
| `MT5_BROKER_UTC_OFFSET_MINUTES` | Windows helper | verify before use | Explicit current terminal time normalization; historical offsets need separate period verification |

On Railway, add a **PostgreSQL** service and reference its connection string
from the bot service:

```text
DATABASE_URL=${{ Postgres.DATABASE_URL }}
```

The service name in the expression is case-sensitive. If your Railway database
service has a different name, replace `Postgres` with that exact name. After
changing variables, redeploy the bot service.

For local development you can run PostgreSQL with Docker:

```bash
docker run -d --name pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16
```

## Install and Run Locally

Make sure Python 3.12 or newer is installed.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m bot.main
```

For Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m bot.main
```

## Run with Docker

```bash
docker build -t telegram-bot-studio .
docker run --env-file .env telegram-bot-studio
```

Run the bot and PostgreSQL together:

```bash
docker compose up --build
```

## Public Docker image

Prebuilt images are published to GitHub Container Registry:

```bash
docker pull ghcr.io/vhdts267wk-design/telegram-bot-studio:latest
```

Available tags include `latest`, semantic release tags such as `1.0.0`, and
immutable commit tags such as `sha-abc1234`.

Bot-only mode:

```bash
docker run --rm \
  -e BOT_TOKEN="your_bot_token" \
  ghcr.io/vhdts267wk-design/telegram-bot-studio:latest
```

Studio mode with an existing PostgreSQL database:

```bash
docker run --rm \
  -p 8080:8080 \
  -e BOT_TOKEN="your_bot_token" \
  -e DATABASE_URL="postgresql://user:pass@host:5432/db" \
  -e PANEL_USERNAME="admin" \
  -e PANEL_PASSWORD="change-me" \
  -e PANEL_SECRET_KEY="replace-with-a-random-secret" \
  ghcr.io/vhdts267wk-design/telegram-bot-studio:latest
```

## Deploy the GHCR image on Railway

Create an image-based Railway service using:

```text
ghcr.io/vhdts267wk-design/telegram-bot-studio:latest
```

Minimum variables for bot-only mode:

```text
BOT_TOKEN=...
LOG_LEVEL=INFO
```

Variables for Studio mode:

```text
BOT_TOKEN=...
DATABASE_URL=${{ Postgres.DATABASE_URL }}
PANEL_USERNAME=admin
PANEL_PASSWORD=...
PANEL_SECRET_KEY=...
LOG_LEVEL=INFO
```

Railway injects `PORT` automatically, so it does not need to be configured
manually.

> **Polling deployment:** run exactly one replica/instance. Telegram
> `getUpdates` supports only one active consumer for a bot token. Multi-instance
> deployments require webhook mode or queue/leader-election coordination.

## Publishing releases

The GitHub Actions workflow publishes to GHCR on every push to `main`, on
manual dispatch, and for tags matching `v*.*.*`.

To publish version `1.0.0`:

```bash
git tag v1.0.0
git push origin v1.0.0
```

After the workflow completes, verify the tags:

```bash
docker pull ghcr.io/vhdts267wk-design/telegram-bot-studio:latest
docker pull ghcr.io/vhdts267wk-design/telegram-bot-studio:1.0.0
docker image inspect ghcr.io/vhdts267wk-design/telegram-bot-studio:1.0.0
```

The package must have public visibility in the repository or organization
package settings before Railway can pull it without registry credentials.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and
[SECURITY.md](SECURITY.md) for private vulnerability reporting.

## Manual MT5 tickets

Set `MARKET_SOURCE=mt5`, `MT5_MANUAL_TICKETS_ENABLED=true`,
`MT5_TRADING_ENABLED=false` and a distinct `MT5_MANUAL_BRIDGE_KEY` of at least
32 characters. The paired Windows helper uploads broker candles and quotes,
and the bot's **جهّز على اللابتوب** button requests a visible native MT5 ticket
with Volume, Stop Loss and Take Profit filled. The human reviews the ticket
and clicks Buy/Sell inside MT5. Preparation never sends an order or confirms
execution. Old automatic Accept buttons and execution endpoints are blocked.

Run migration `20261005_05` before use. Local setup and supported UI checks are
described in [bridge/MANUAL-TICKETS.md](bridge/MANUAL-TICKETS.md).

The display-only `bridge/MT5BotLevels.mq5` indicator shows the latest valid
proposal's Entry Zone, reference Entry, green TP1 and red SL on the XAUUSD **M1**
chart. Use indicator version **2**; old M15/CSV-v1 snapshots are rejected. Its
levels match the immutable qualified Telegram proposal. The helper reads a
separate authenticated `/api/mt5/manual/chart` endpoint without claiming a
ticket and publishes an expiring local snapshot. Missing, expired or unsafe
data clears the indicator's own levels. The native Buy/Sell click stays with
the human; attaching this indicator does not enable automatic trading.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
