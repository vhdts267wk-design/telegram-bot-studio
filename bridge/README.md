# Windows MT5 bridges

Two manually started helpers connect to the selected MetaTrader 5 desktop terminal:

| Helper | Purpose | Orders |
| --- | --- | --- |
| `mt5_market_bridge.py` | Upload gold quotes and completed M15 candles | Read-only; never places, changes or closes orders |
| `start_mt5_bridge.ps1` / `mt5_trade_bridge.py` | Upload market data and process owner-approved Telegram requests | Demo account only, exactly 0.01 lot per accepted offer |

The bot defaults to paper signals and `MT5_TRADING_ENABLED=false`. The Demo helper needs explicit enablement on the server and a local confirmation. This release does not support Real accounts or other lot sizes. Keep the SDK on Windows; Railway never connects directly to MT5 or sends an order.

## Shared requirements

Use an awake Windows x64 computer or Windows VPS, the broker's MT5 desktop terminal, and compatible x64 Python. Open the **exact intended terminal yourself**, log in there and confirm that it is connected. Neither helper accepts an account password or changes the logged-in account. This is not a mobile or web MT5 connection.

Find the exact broker gold symbol in Market Watch, including any suffix, and load its M15 chart with preferably 65 bars of history. The instrument must explicitly identify base currency `XAU` and profit currency `USD`. A name resembling gold is insufficient if that metadata is missing or different.

From the repository root, create the bridge's separate environment:

```powershell
py -3.12 -m venv bridge/.venv
bridge/.venv/Scripts/python.exe -m pip install -r bridge/requirements.txt
```

Configure the bot service on Railway:

| Variable | Value |
| --- | --- |
| `DATABASE_URL` | Working PostgreSQL connection, such as the Railway Postgres reference |
| `MARKET_SOURCE` | `mt5` |
| `MARKET_GOLD_SYMBOL` | Exact broker symbol, matching the helper's symbol argument |
| `MARKET_BRIDGE_KEY` | Private shared key of at least 32 characters |
| `MT5_TRADING_ENABLED` | `false` for read-only data, `true` only for the Demo approval workflow |

Redeploy after changing the server settings. A valid bridge key enables `/api/market/feed` and `/healthz` even when `PANEL_PASSWORD` is empty and the admin panel is disabled. Bridge HTTP requires PostgreSQL; set Railway's health check to `/healthz` for this mode.

Use the same private bridge key locally. Keep it out of command arguments, URLs, screenshots, committed files and Telegram messages. The feed URL must be your service's HTTPS address ending in `/api/market/feed`, without a username, password, query or fragment. Redirects are rejected. The temporary pairing code described below is different from this private key.

## Demo 0.01-lot helper

Use a **Demo account's trading login** in the selected terminal. Enable Algo Trading and permit external Python trading in MT5 yourself; the helper does not change those settings. The read-only helper's recommendation to disable external trading does not apply here. The local checks reject a Real account, changed account identity, unsupported volume, or a terminal that does not permit trading.

Start the PowerShell launcher from the repository root, replacing the path, symbol and service address:

```powershell
& .\bridge\start_mt5_bridge.ps1 `
  -TerminalPath "C:\Program Files\Your Broker MT5\terminal64.exe" `
  -Symbol "XAUUSD.m" `
  -FeedUrl "https://YOUR-SERVICE.up.railway.app/api/market/feed"
```

The launcher verifies that this exact executable is already running. It defaults to `bridge/.venv/Scripts/python.exe`; use `-PythonPath` only if you installed the bridge SDK in another compatible environment. It asks for the same Railway bridge key using hidden input, then requires the exact local phrase **`ENABLE DEMO ORDERS`**. It starts the foreground Demo helper with fixed `--account-mode demo --volume 0.01 --enable-orders`. Press **Ctrl+C** to stop; no service, scheduled task or background installation is created.

The private state directory defaults to `mt5-private` beside the repository; `-StateDirectory` selects another private location. The launcher restricts access to the current Windows user and saves the bridge key with Windows DPAPI. `-ReplaceBridgeKey` replaces that saved key through hidden input. Keep the state directory and its `trade-ledger.sqlite3` intact: it preserves the device identity, account binding and already attempted offers across restarts.

### Pair and approve

An unpaired helper prints `/connect_mt5 CODE`. Send that command to the bot **in your own private chat** within ten minutes. The code is single-use and binds the device to that chat and user; the saved pairing cannot be transferred by registering a new code. A restarted paired helper reports that it is already paired. Pairing enables alerts; `/unwatch` stops them and `/watch` enables them again.

The helper uploads broker data once a minute and polls accepted requests approximately every ten seconds. Offers require the paired device's recent quote, receipt and heartbeat, a valid completed M15 setup, and an active subscription. SL/TP are normalized to the broker's price grid **before the offer is displayed and saved**. The displayed levels, symbol, Demo mode and volume are frozen for that offer. Unsupported or collapsed levels suppress it.

An offer has **at most five minutes** for approval and may expire earlier because of the signal's age. Accept queues one authorized request; it does not mean the order has filled. Reject submits no order. Buttons are bound to the original bot, device, owner, chat and message, and cannot approve an expired or already decided offer.

Before its single submission, the local helper checks the same Demo account and terminal, exact 0.01-lot size, symbol metadata, broker volume step, SL/TP grid and stop-distance restrictions. On a netting account it also requires no existing position or pending order for this symbol, so a new order cannot intentionally reduce or alter an existing position. A current executable quote must be no more than 30 seconds old. Spread plus the distance from the reference entry must stay within **0.1 of the original stop distance (0.1R)**, with SL/TP still surrounding the current executable price.

These are checks **before submission**. They do not guarantee the fill price, and market-execution brokers may ignore the deviation setting. The helper performs MT5 preflight, repeats the guards, and sends once. It never automatically modifies the accepted protection levels, closes a position or resends an uncertain order.

### Results and stopping

`filled` requires MT5's full-execution return code, the full confirmed 0.01-lot volume and a valid order ticket. A partial, placed, timed-out or otherwise uncertain result is **unknown**, not a confirmed fill. The local ledger reserves an offer before sending; a crash or lost response cannot trigger another order attempt. Reporting the same result can retry safely without resending the order. A server claim without a confirmed result for more than two minutes becomes unknown and is never reclaimed.

Check MT5 when an outcome is unknown; do not treat an absent Telegram confirmation as proof that no order exists. Retain the ledger rather than deleting state to force another attempt.

`/unwatch` cancels unclaimed pending requests, including an accepted request that has not reached execution. It cannot cancel a request already executing or close an open position. Final filled/failed/unknown acknowledgments can still arrive after unsubscribing. An approved request cancelled or expired before any execution claim gets a clear no-order acknowledgment; unclicked expired offers stay silent. The bot's separate **15-minute paper review does not close an MT5 position**.

Keep Windows awake, MT5 connected and this foreground helper open. Closing the selected terminal stops the helper; sleep or disconnection stops fresh updates and execution processing. It reads the account identity locally to verify Demo mode and stores only a salted account-binding hash, not the account number or server name. Account passwords, balances, positions and account identifiers are not uploaded. Changing the bound account, terminal, symbol, volume or service origin blocks execution and requires deliberate local setup again.

## Read-only helper

Use `mt5_market_bridge.py` when you only want market data and paper analysis. Prefer the broker's investor/read-only password when available. Leave **Disable automatic trading through the external Python API** enabled in MT5's Expert Advisors settings; this helper needs no trading permission and does not read account numbers, balances, orders or positions.

Set `MARKET_BRIDGE_URL` and the same `MARKET_BRIDGE_KEY` privately in the Windows process environment through your trusted configuration. Keep `MT5_TRADING_ENABLED=false` on the server. Run one manual upload first:

```powershell
bridge/.venv/Scripts/python.exe bridge/mt5_market_bridge.py --terminal "C:/Program Files/Your Broker MT5/terminal64.exe" --symbol "XAUUSD.m" --once
```

Check for `Market update sent.` and inspect the bot's symbol and original timestamps. Omit `--once` to upload once a minute in the foreground; **Ctrl+C** stops it. The selected executable must exist and its connected installation path must match. MetaQuotes documents that SDK initialization may launch a terminal, so open and connect the correct terminal first. The Demo helper adds an explicit running-process check.

## Market data and troubleshooting

Market payloads contain the symbol, source, M15 timeframe, quote bid/ask/time and completed candle time/OHLC/tick volume. The Demo helper also adds its device UUID and broker grid restrictions. They contain no account credentials. SDK timestamps are UTC; the forming candle is excluded. Up to 64 completed bars are sent, with at least four valid bars required. The EMA9/21 signal needs 22 consecutive completed bars. No missing candles or prices are invented, interpolated or filled. Tick volume is the broker's tick count, not global gold volume.

Old quotes retain their **original timestamp**. Uploading an unchanged weekend or session-gap quote does not refresh its market age. Stale data can support a last-known-price display but cannot authorize a fresh offer or order. Confirm the broker's symbol, metadata and sessions; broker data rights govern sharing prices with other recipients.

Setup or guard failures mean to check the selected terminal, Demo account, permissions, exact symbol, volume support, loaded M15 history, current quotes and matching private endpoint key. HTTP/network failures can retry data uploads or result delivery; they never retry an order. Network requests have a 15-second timeout. Diagnostics avoid keys, account identities and endpoint response bodies.

Official SDK references: [Python integration](https://www.mql5.com/en/docs/python_metatrader5), [bar indexing](https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrompos_py), [UTC timestamps](https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesrange_py), [initialization](https://www.mql5.com/en/docs/python_metatrader5/mt5initialize_py), [order preflight](https://www.mql5.com/en/docs/python_metatrader5/mt5ordercheck_py), [order submission](https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py), [read-only investor access](https://www.metatrader5.com/en/terminal/help/startworking/authorization), [Python trading protection](https://www.metatrader5.com/en/terminal/help/startworking/settings).
