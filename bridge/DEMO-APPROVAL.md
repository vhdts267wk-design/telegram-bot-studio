# Telegram approval for Demo MT5

This workflow lets the paired owner choose **Accept** or **Decline** beneath a
current Telegram opportunity. Accept authorizes one market-order attempt on
the bound Demo terminal, at exactly 0.01 lots, with the proposal's fixed SL and
primary TP attached in the initial order. The market fill can differ from the
reference entry. TP2 remains informational; this helper does not split positions
or manage an existing trade.

The current strategy uses M30 for trend and observed support/resistance, M15 for
confirmation, and M5/M1 for entry timing. H1 and H4 are not read, hashed or required.
Each of the four streams needs 22 real completed bars, including earlier
sessions. Indicators use the actual broker bar sequence without filling or
modifying any candle. Timestamp gaps are preserved as history diagnostics;
they no longer discard older bars from warmup. The v6
qualified and v7 experimental identities reject earlier H1 and five-frame proposals
and qualification artifacts. M30 levels use its last 20 completed bars and carry
`levels_timeframe: M30` in the context contract.

Use display indicator 2.16 for the four-frame panel. It shows the live connection,
M30/M15/M5/M1 closed-candle readiness and the specific warmup count, plus only
those four chart-view buttons. A full 22/22 readiness tile means history is
available, not that a signal or order has been approved. Telegram Accept remains
the sole owner approval for a Demo attempt; the panel cannot submit one.

The local helper writes a separate expiring `readiness_<terminal-key>.csv`
snapshot beside the existing proposal file. This display-only snapshot uses the
same terminal/account binding and four actual usable closed-bar counts.
Its version is 2, with counts ordered M30/M15/M5/M1; version 1 H1 readiness
rows are incompatible. The server feed uses schema 3 with the same four frames.
Its deadline cannot exceed ten seconds after the captured feed or quote. Missing,
invalid or stale readiness is shown as unavailable. The original 18-field
proposal CSV and Demo execution safeguards retain their existing contracts.
The strategy fingerprint changes, invalidating earlier offers and qualification
artifacts. Qualified mode needs freshly reviewed evidence; the explicit
experimental Demo mode remains provisional and does not certify performance.

The helper already requests the latest 64 actual closed bars per timeframe at
startup and every five seconds. At least 22 actual closed bars per frame now
satisfy warmup immediately, including bars before a daily break. The forming
candle is never requested. Each new closed candle enters the rolling snapshot;
there is no incremental cache or requirement to rebuild 11 hours of M30 bars
after each restart. EMA uses the real earlier closes and ATR retains the price
gap at reopening. This bar-sequence policy does not certify uninterrupted
historical data or infer a closure calendar.

The engine, local context validator, Telegram analysis and native panel use the
same completed-bar count. Trend, spread, session, freshness, exposure and risk
conditions can still block a proposal after history is ready. The changed
strategy fingerprint invalidates prior qualification evidence; it is never
automatically repinned. This installation remains analysis-only and cannot
prepare or send an order.

Local display samples are collected before the network cycle and again after
the chart response, with Demo/account checks around each collection. Feed
uploads are scheduled from collection time, rather than upload completion.
Refreshing readiness alone never extends the proposal file's expiry. Slow
requests or unavailable broker quotes can still exceed the unchanged ten-second
freshness limit; LIVE then disappears until verified fresh data returns.

After a local quote/history failure, the helper retries collection on its
existing one-second loop and uploads as soon as a guarded snapshot succeeds.
It does not wait an additional five seconds after local recovery. Network
failures retain their five-second backoff. Local failures log a fixed reason
code and UTC time without raw SDK messages, credentials or account identifiers.

The server opt-in is `MT5_DEMO_ORDERS_ENABLED=true`, together with
`MARKET_SOURCE=mt5`, `MT5_MANUAL_TICKETS_ENABLED=true`, and the existing private
`MT5_MANUAL_BRIDGE_KEY`. Both workflows are disabled in a clean example
configuration. An existing manual-ticket setup remains manual when
`MT5_DEMO_ORDERS_ENABLED=false`. The new workflow uses `mt5demo:` callback data,
`/api/mt5/demo/*` endpoints, and a separate
`demo_execution` payload. Legacy automatic routes remain disabled. While Demo
execution is enabled, the manual preparation endpoints and old manual buttons
cannot authorize a new action.

Use compatible Windows x64 Python and the local MT5 SDK dependencies. From the
repository root, install them into the Python environment selected for this
helper if they are not already installed:

```powershell
python -m pip install -r bridge/requirements.txt
```

Run the source helper from the same release as the deployed bot, using that
same Python environment:

```powershell
python bridge/mt5_demo_bridge.py --terminal "C:\Program Files\MetaTrader 5\terminal64.exe" --symbol XAUUSD --account-mode demo --volume 0.01 --state-directory "PATH_TO_EXISTING_PRIVATE_STATE" --enable-demo-orders
```

The local process environment uses `MARKET_BRIDGE_URL` for the existing HTTPS
feed URL ending in `/api/market/feed`, `MARKET_BRIDGE_KEY` containing the same
private key as the server's `MT5_MANUAL_BRIDGE_KEY`, and the verified
`MT5_BROKER_UTC_OFFSET_MINUTES`. Do not substitute the server's distinct legacy
`MARKET_BRIDGE_KEY`. The source helper does not automatically load a `.env` file.
Keep secrets out of arguments and logs. Reuse the existing identity/state
directory; its process lock prevents simultaneous manual and Demo execution
runners. Stop the existing manual helper before starting this one.

The terminal must already be connected to its bound Demo account with the
required external Python trading permission. This is an existing prerequisite;
the helper never logs in or changes terminal or security settings. Keep Windows
awake, the internet and MT5 connected, and the foreground helper running. A
phone Accept cannot execute while the laptop is asleep, offline, or stopped.

Include the complete source release locally, especially
`bridge/mt5_history_contract.py`, `bot/multi_timeframe.py`, and
`bot/paper_signals.py`. Strategy fingerprints read the exact source files; a
mismatched or incomplete release fails closed.

Every approval is tied to the private owner, bot message, device and immutable
proposal. Server publication, approval and claim recheck current eligibility.
The helper rechecks four completed histories, their frozen SHA256, current
technical conditions, quote age, spread, price drift, broker stop rules, cash
risk, free margin and account identity. Existing positions or pending orders
block a new order. The original M1 deadline remains 10 seconds for qualified
signals or 30 seconds for explicitly selected experimental Demo signals.

A durable local reservation precedes the single `order_send`. A crash,
timeout or ambiguous response becomes unknown and is never automatically
retried. Telegram reports filled only after a full MT5 execution receipt;
otherwise it reports failed or unknown. Check an unknown outcome in MT5.
Decline rejects an unaccepted proposal. `/unwatch` stops new opportunity
notifications and cancels unclaimed requests; final filled, failed or unknown
receipts can still arrive. It does not undo an in-flight attempt or close an
existing position. The 60-minute signal-evaluation horizon does not automatically
close a position either.

The feature does not make unavailable market histories usable or create signals
when the strategy/risk conditions fail. Experimental costs and performance
remain unverified. Real-money accounts are always rejected.

### Panel candle counts

Panel 2.16 counts completed M30/M15/M5/M1 candles directly from MT5 when the
helper's readiness update is unavailable. `LOCAL HISTORY` identifies this
display: the counts describe terminal history, not a live bot connection or a
valid signal. A frame still loading shows `--/22` and retries on the next timer
tick. Counts include real closed bars from previous sessions and are capped at `22/22`, the
strategy's minimum history requirement.

The panel continues to show `SIGNAL: UNAVAILABLE` when the bot update is missing
or expired. Native counts never create or extend proposals, notifications or
orders. Fresh authenticated helper updates retain the existing `LIVE` display
and all existing eligibility checks.
