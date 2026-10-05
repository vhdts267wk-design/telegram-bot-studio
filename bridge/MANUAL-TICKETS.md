# Native MT5 ticket preparation

`mt5_manual_bridge.py` prepares the installed desktop terminal's **New Order** window for a human review and final native Buy/Sell click. It does not send an order, accept an execution request, modify a position, or close a trade. A `prepared` result means that the visible ticket fields were verified; it is not a trade confirmation.

Keep the intended Windows MT5 **Demo** terminal open, connected, and showing the exact broker gold symbol's **M15** chart. This workflow retains the existing **0.01 lot** limit and paired device/account identity. Algorithmic trading and external Python trading can remain disabled: the SDK is used only for prices, broker settings, and account checks.

Install the Windows helper dependencies from `bridge/requirements-manual.txt`. The helper uses the same MT5 SDK and the targeted built-in Windows interface for ticket fields and the actual Stop levels setting; it needs no extra UI framework.

Before starting this helper, stop the existing Demo execution helper. Both helpers share the same exclusive local process lock and identity ledger. Keep the private state directory intact; the manual helper keeps its draft attempts in a separate table and never processes the old automatic order outbox.

The server must use its mutually exclusive manual-ticket mode and a separate `MT5_MANUAL_BRIDGE_KEY`. The launcher supplies that key privately through the process environment as `MARKET_BRIDGE_KEY`; do not put it in command arguments or Telegram. `MARKET_BRIDGE_URL` remains the HTTPS `/api/market/feed` address. Preserve the verified `MT5_BROKER_UTC_OFFSET_MINUTES` setting used by the current terminal.

From the repository root, after the launcher has supplied those private environment variables:

```powershell
bridge/.venv/Scripts/python.exe bridge/mt5_manual_bridge.py `
  --terminal "C:/Program Files/Your Broker MT5/terminal64.exe" `
  --symbol "XAUUSD" `
  --account-mode demo --volume 0.01 `
  --state-directory "../mt5-private" `
  --enable-manual-tickets
```

Use the existing paired private Telegram chat to request preparation. When the native window appears, verify its symbol, volume, Stop Loss, Take Profit, current price, and the suggested direction in its comment. Its comment also gives the proposal's expiry in UTC. **Only you click the native Buy/Sell button.** Cancelling the window places no order. A prepared window does not expire or cancel itself; after the comment's deadline, cancel it and request a fresh proposal.

An existing native order window is never overwritten. Close or cancel it yourself before requesting another proposal. Already attempted proposal IDs are not replayed, even after cancellation or a helper restart. Request a new proposal rather than deleting the journal.

Before populating fields, the adapter verifies the selected terminal process, executable path, local Demo account, active symbol/M15 chart, native controls, and **absolute-price** SL/TP mode. Unsupported layouts and unverified price/points mode fail closed. It disables its own verified Order window during preparation, writes only Volume, Stop Loss, Take Profit, and the bounded direction/expiry comment, then reads them back. It enables the completed window only after the final expiry, account, quote, spread, and protection checks pass. It never sends Enter or clicks native trading buttons.

If preparation fails after the lock, the adapter cancels only that exact owned ticket, after verifying the same terminal, account binding, and single order window. It never closes an independently opened or replacement window. If account/window ownership changes, or MT5 refuses cancellation, the owned draft stays blocked and the helper reports failure; resolve that window in MT5 before requesting a fresh proposal.

The price-mode check opens Options, locates its native Trade page by the exact visible Stop levels label and combo, reads the real combo text, then closes Options through Cancel without changing or saving any settings. If another page is selected, bounded native tab navigation selects Trade through normal selection notifications; no tab index is assumed. Startup performs this read before registering or claiming that the helper is ready, and prints **Native price mode verified.** only after success. Choose **in prices** yourself if the helper reports `absolute_mode_unverified`. Another modal dialog or an independently opened order window blocks the check. The installed Order window is modeless, so the adapter freshly checks Options before opening it, after opening its own exact ticket, and before reporting success. It also checks the ticket's unit labels and numeric field readback. These checks establish the mode during preparation; review it again if you change settings afterward.

The fixed protection levels must still match the broker tick grid and minimum stop distances. The current quote must be recent, and spread plus movement from the proposal entry must remain within the existing 0.1R guard. These checks run during preparation. Prices continue changing while you review the ticket, so the reference entry is not a guaranteed fill price.

Press Ctrl+C to stop the foreground helper. It installs no background service or scheduled task. A successfully prepared window remains for your review or cancellation; an interrupted locked preparation follows the same guarded cancellation path.

## Proposal levels on the chart

Compile `bridge/MT5BotLevels.mq5` in MetaEditor and copy the resulting
`MT5BotLevels.ex5` into `MQL5/Indicators/MT5Bot` under this terminal's **File >
Open Data Folder**. Refresh MT5's Navigator and attach **MT5BotLevels** to
the intended XAUUSD M15 chart. It is a display-only custom indicator and needs
no DLLs, WebRequest permissions or algorithmic-trading setting. Keep the
paired manual helper running.

Each latest valid published proposal appears automatically, before a ticket
preparation request: shaded Entry Zone, gold reference Entry, green TP and red
SL, with direction and prices. The reference zone is entry plus/minus 0.1 times
the original stop distance, rounded inward to the frozen broker tick grid.
Telegram and the chart use the same calculation and original SL/TP; spread
is not included in that reference zone. Native preparation retains its
existing spread-plus-drift guard and never guarantees a fill at Entry.

The separate manual-key-authenticated `POST /api/mt5/manual/chart` accepts
only the paired `device_id`. It neither claims nor changes a proposal, and
does not refresh the device heartbeat. It selects the latest published
owner-bound proposal before checking status, subscription, risk pause, Demo
0.01 binding, receipt/quote freshness, broker metadata and explicit expiry.
A newer rejected or expired proposal cannot expose an older one again.

The helper refreshes the market feed every 20 seconds and reads display
updates every 10 seconds. It atomically publishes one headerless ASCII row
in the verified terminal common folder's
`Files/MT5Bot/levels_<terminal-data-folder-name>.csv`. The row expires within
25 seconds and no later than the proposal or quote deadline. It contains a
random nonce and salted account-binding hash, never the raw account login,
server, Telegram chat/user, device ID or API key. The indicator checks its
current Demo account, terminal, XAUUSD M15 chart, UTC expiry, price grid and
completed broker chart bar before drawing. The broker UTC offset affects
chart coordinates only; expiry uses `TimeGMT()`.

Missing, malformed, stale or unavailable data clears only the indicator's
own objects and displays a waiting message. Account changes require reloading
the indicator. User drawings, orders, positions and an already prepared
native ticket are untouched. Stopping or crashing the helper cannot leave
active levels displayed past the short local expiry. When no valid proposal
exists, the indicator waits; it does not invent a trade.

Official references: [Native order window and submission](https://www.metatrader5.com/en/terminal/help/trading/performing_deals), [SL/TP prices versus points settings](https://www.metatrader5.com/en/terminal/help/startworking/settings), [SDK function list](https://www.mql5.com/en/docs/python_metatrader5), [Custom indicators](https://www.metatrader5.com/en/terminal/help/charts_analysis/indicators), and [MetaEditor compilation](https://www.metatrader5.com/en/metaeditor/help/development/compile).
