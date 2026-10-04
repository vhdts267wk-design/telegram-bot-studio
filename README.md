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
- `/market` and `/news` work without screenshots; `/watch` sends a report every 15 minutes
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

PostgreSQL is optional when the bot runs without the admin panel. When
`DATABASE_URL` is set, the bot connects on startup and creates its tables
automatically. When `PANEL_PASSWORD` enables the panel, a working
`DATABASE_URL` is required and startup fails with a clear error if it is missing
or unreachable. This prevents the panel from silently running without its
command store.

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
| `Market` | XAUUSD prices and observed M15 data |
| `News` | Cited political and economic news |
| `Signals` | Experimental paper BUY/SELL setups |

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
| `/signals` | Inspect the current experimental setup; no journal entry is opened |
| `/reviews` | Recent 15-minute paper outcomes and review notes |
| `/watch` | Opt in to a report every 15 minutes in a private chat |
| `/unwatch` | Stop automatic reports |

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

Send `/market` or `/news` for a report. `/watch` enables automatic reports every
15 minutes, with the first report after 15 minutes; `/unwatch` stops them.
Subscriptions, delivery leases, collected prices and search usage survive restarts
in PostgreSQL. Reports are private-chat opt-ins. A report already being delivered
can finish its in-flight Telegram request when unsubscribing.

Reports also include **experimental paper signals**, requested with `/signals`.
The fixed rule is EMA9/EMA21 on consecutive completed M15 candles: BUY only when
the fast EMA crosses from at/below to above the slow EMA, SELL on the inverse,
otherwise no new setup. EMAs use SMA seeds; ATR14 uses true ranges and Wilder
smoothing. The reference entry is the triggering candle's close, the stop is
1.5 ATR away, and the target is 3 ATR away (a nominal 2:1 ratio). Levels rounded
to $0.01 must remain positive and correctly ordered; zero/tiny ATR suppresses
the setup.

At least **22 sufficiently covered, consecutive completed M15 candles** are
required, roughly 5.5 hours of collection plus an initial partial period or any
gaps. A fresh quote and a recent completed candle are also required. Reports
never force a BUY/SELL every 15 minutes, fill missing bars or use a forming
candle. Reference samples are not broker OHLC; spreads, fees, slippage and fills
are unmodelled. The rules have correctness tests with synthetic data, **not
historical profitability validation**, and are not ready for live trading.
The bot executes no orders and reads no balances or positions.

The setup identity includes source, exact symbol, strategy version, triggering
bar and direction. Its initially computed levels are saved. Worker delivery
remembers the last setup per chat across restarts; `/signals` deliberately lets
the user inspect the current result again. The same at-least-once delivery
limitation described below applies if a crash occurs after Telegram accepts a
message and before its acknowledgement. No paper fills or profits are claimed.

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

Successfully delivered automatic signals open immutable paper journals in
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

An optional [read-only Windows MT5 bridge](bridge/README.md) can use a connected
broker terminal later. Keep the Windows-only dependency out of Railway's server
requirements. Explicitly set `MARKET_SOURCE=mt5`, `MARKET_GOLD_SYMBOL` to the exact
broker gold symbol, and configure a private `MARKET_BRIDGE_KEY` of at least
32 characters on server and bridge. The protected POST endpoint `/api/market/feed`
is disabled without that key; it is served only with the panel enabled. No account
balances, positions, login details or trading operations are needed.

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
the bot runs as a plain poller. Once enabled, open your Railway service URL (or
`http://localhost:8080` locally) and sign in with `PANEL_USERNAME` / `PANEL_PASSWORD`.

For a service with the panel enabled, configure Railway's health check as
`/healthz`. It reports ready only while the bot is running, polling is active,
PostgreSQL responds, and Telegram accepts the bot token. Telegram checks time
out after three seconds and are cached for ten seconds. A rejected token needs
to be replaced privately in the active bot service, followed by a redeploy.
Leave this health check unset for a plain poller,
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
| `DATABASE_URL`     | For panel | -  | PostgreSQL connection string; required when `PANEL_PASSWORD` is set |
| `PANEL_PASSWORD`   | No  | -       | Enables Telegram Bot Studio when set; password to sign in     |
| `PANEL_USERNAME`   | No  | `admin` | Username for Telegram Bot Studio                              |
| `PANEL_SECRET_KEY` | No  | derived | Secret for signing panel session cookies (derived from password if empty) |
| `PANEL_SECURE_COOKIE` | No | Railway: `true` | Require HTTPS for panel session cookies |
| `PORT`             | No  | `8080`  | Port the panel binds to (Railway injects this automatically)  |
| `LOG_LEVEL`        | No  | `INFO`  | Logging level, such as `DEBUG`, `INFO`, or `ERROR`            |
| `MARKET_SOURCE` | No | `reference` | Keyless reference prices, or explicitly `mt5` for a configured broker bridge |
| `MARKET_BRIDGE_KEY` | For MT5 only | - | Private ingest key of at least 32 characters; never send in chat |
| `MARKET_GOLD_SYMBOL` | For MT5 only | `XAUUSD` | Exact broker gold symbol, including any suffix |

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

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
