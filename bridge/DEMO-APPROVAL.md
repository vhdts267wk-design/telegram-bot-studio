# Telegram approval for Demo MT5

This workflow lets the paired owner choose **Accept** or **Decline** beneath a
current Telegram opportunity. Accept authorizes one market-order attempt on
the bound Demo terminal, at exactly 0.01 lots, with the proposal's fixed SL and
primary TP attached in the initial order. The market fill can differ from the
reference entry. TP2 remains informational; this helper does not split positions
or manage an existing trade.

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
The helper rechecks five completed histories, their frozen SHA256, current
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
