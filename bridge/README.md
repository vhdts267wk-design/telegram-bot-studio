# Read-only MT5 market bridge

This manually started Windows helper sends the selected broker's gold quote and up to 64 completed M15 candles to the Telegram bot on Railway once a minute. The bot can use these candles for educational updates every 15 minutes. This helper does not place, modify or close trades and does not read account numbers, balances, orders or positions.

## Before starting

Use an always-on Windows x64 computer or Windows VPS with the broker's MetaTrader 5 desktop terminal and a compatible x64 Python installation. The official Python package communicates with that local terminal; it is not a mobile/web MT5 connection and does not run inside the Railway server. If the computer sleeps or disconnects, fresh market uploads stop.

Start the **correct terminal yourself**, log in there, and check that it is connected. Prefer the broker's investor/read-only password when available. Leave **Disable automatic trading through the external Python API** enabled in MT5's Expert Advisors settings; this bridge does not need trading permission. No account login or password is accepted by this helper.

Find the exact gold symbol in the broker's Market Watch, including any suffix, and load its M15 chart with preferably 65 bars of history (at least five are required). The broker must identify the instrument's base currency as `XAU` and profit currency as `USD`. A symbol merely named `GOLD` or `XAUUSD` is insufficient if that metadata is missing or different. Unsupported metadata must be resolved with the broker instead of guessing a symbol or substituting a futures feed.

The selected `.exe` must exist. The helper passes that exact path to `initialize()` and verifies the connected terminal's installation path. MetaQuotes documents that `initialize()` **may launch a terminal** if necessary, so do not start the helper before opening and connecting the intended terminal. It does not select another account or supply account credentials.

## Manual setup and start

Install only the bridge's separate requirements on that Windows machine:

```powershell
py -3.12 -m venv bridge/.venv
bridge/.venv/Scripts/python.exe -m pip install -r bridge/requirements.txt
```

The bot defaults to the keyless reference source. For a future MT5 setup, explicitly set **`MARKET_SOURCE=mt5` on Railway** and set **`MARKET_GOLD_SYMBOL` on Railway to the exact broker symbol supplied with `--symbol`**, including its suffix. The symbols must match; starting this helper alone does not switch the bot's source.

Set `MARKET_BRIDGE_URL` on the Windows bridge machine to your Railway HTTPS address ending in `/api/market/feed`. Set `MARKET_BRIDGE_KEY` there to the same secret configured for that endpoint on Railway. Supply these as environment variables using your own trusted configuration; do not put keys in command arguments, URLs, screenshots, committed files or messages. The URL must contain no username, password, query parameters or fragment. Redirects are rejected so that the bearer key cannot be forwarded to another endpoint.

First run one manual upload, substituting the terminal path and exact symbol:

```powershell
bridge/.venv/Scripts/python.exe bridge/mt5_market_bridge.py --terminal "C:/Program Files/Your Broker MT5/terminal64.exe" --symbol "XAUUSD" --once
```

Check for `Market update sent.` and confirm that the bot received the expected broker symbol and timestamps. Then omit `--once` to keep the helper running in the foreground. Press **Ctrl+C** to stop. Nothing is installed as a service or started automatically in the background.

## Data and troubleshooting

The payload contains only `symbol`, `timeframe`, `source`, the quote's bid/ask/time, and candles' time/OHLC/tick volume. SDK timestamps are treated as UTC and encoded with `Z`. Candle zero is the forming bar and is excluded. The bridge requests 64 bars and requires at least four valid completed bars; indicators needing more history become available only when enough bars exist. It does not invent, interpolate or fill prices across gaps. Tick volume is the broker's tick count, not global gold trading volume.

Future quotes, quotes more than ten days old, zero/nonfinite prices, crossed quotes, unfinished or malformed candles, insufficient history, disconnected terminals and unsupported symbols are rejected. During weekends or session gaps, the helper may send the unchanged broker quote with its **original timestamp** so that setup can still be verified. An old price is displayed as **last known only**; uploading it never resets its market freshness or labels a closed market as live. The server must withhold fresh market conclusions and alerts when data is stale. Confirm the broker's actual symbol, quote conventions and market sessions during setup. Broker data rights govern sharing it with other recipients.

Failures report only an exception class, never the endpoint's response body, URL, bearer key or terminal details. `ConfigurationError` means to check the environment settings and terminal path; `MarketDataError` means to check the connected terminal, symbol metadata, fresh quotes and loaded M15 history. HTTP/network errors mean to check the Railway endpoint and matching key privately. Requests have a 15-second timeout, and foreground retries occur at most once a minute.

The official Windows SDK is intentionally absent from the bot server's main requirements. References: [Python integration](https://www.mql5.com/en/docs/python_metatrader5), [bar indexing](https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrompos_py), [UTC timestamps](https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesrange_py), [terminal initialization](https://www.mql5.com/en/docs/python_metatrader5/mt5initialize_py), [read-only investor access](https://www.metatrader5.com/en/terminal/help/startworking/authorization), [Python trading protection](https://www.metatrader5.com/en/terminal/help/startworking/settings).
