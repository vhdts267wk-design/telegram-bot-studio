# Native MT5 ticket preparation

`mt5_manual_bridge.py` prepares the installed desktop terminal's **New Order** window for a human review and final native Buy/Sell click. It does not send an order, accept an execution request, modify a position, or close a trade. A `prepared` result means that the visible ticket fields were verified; it is not a trade confirmation.

Keep the intended Windows MT5 **Demo** terminal open, connected, and showing the exact broker gold symbol's **M1** chart. M15 determines direction, M5 confirms a pullback/recovery, and a completed M1 close determines timing. This workflow retains the **0.01 lot** limit and paired device/account identity. Algorithmic trading and external Python trading can remain disabled: the SDK is used only for prices, broker settings, and account checks.

## Explicit Demo experimental profile

The server defaults to `MT5_SIGNAL_MODE=qualified`. An operator can explicitly choose `MT5_SIGNAL_MODE=experimental_demo` for provisional Demo signals with estimated costs. This profile does not certify a win rate or mark broker costs as verified. It is limited to the same paired **Demo account and 0.01 lot**, with the same M15 direction, M5 confirmation, M1 trigger, price grid, fresh quote, account exposure, cash risk, reward/risk, margin and spread/drift checks. It never enables automatic order execution.

Experimental payloads use the complete distinct profile `strategy_id=mtf-ema-pullback-60m-demo-v2`, `strategy_version=2`, `policy_id=mtf-manual-demo-estimated-cost-risk-v2`, `signal_mode=experimental_demo`, `provisional=true`, and `entry_window_seconds=30`. They omit `qualification_id` and empirical evidence metrics. A partial profile, a certified hash attached to this profile, or an unknown mode is rejected. Default qualified payloads keep their original IDs, verified costs, qualification hash and ten-second action window.

The estimate method is `spread_tick_floor_v1`. For exactly 0.01 lot, round-trip commission in account currency is at least the larger of reported commission and `loss_cash_per_price_unit × max(spread, 10 × tick_size)`; per-side price slippage is at least the larger of reported slippage, half the spread and two ticks. Missing costs can use these floors only when the source explicitly marks costs unverified. Every experimental proposal includes the values and method in `cost_assumptions` with `verified=false`. The local helper recalculates cash risk with the larger of the frozen proposal estimates, current reported costs and current spread/tick floors. Missing live costs cannot erase the saved estimates. These are assumptions for Demo experimentation, not measured trading costs.

Experimental alerts, chart levels and ticket preparation expire **30 seconds after the triggering M1 close**. Quotes still expire after ten seconds; the wider entry window does not admit stale quotes. Review the `Demo EXP` ticket comment and current direction/volume/SL/TP yourself. A prepared native window remains open after expiry; cancel it if the signal is no longer valid.

## Default qualified profile

The current strategy is `mtf-ema-pullback-60m-v1`. Each stream uses at most 64 actual completed bars and requires at least 22 contiguous bars after its latest gap. Gaps are never filled and forming candles are never used. M15/M5 references must already be closed at the M1 decision. Entry is the fresh executable Ask/Bid rounded adversely to the tick grid; SL uses a recent M5 swing plus a 0.2 ATR buffer. TP1 is 2R and supplementary TP2 is 3R. Published prices remain fixed.

BUY/SELL alerts, preparation and chart levels require a reviewed server-local evidence artifact with a separate SHA256 pin. It must match the strategy implementation, policy, broker, execution specifications and verified costs, and contain at least **200 independent, nonoverlapping OOS trades** per required cost scenario with the **lower endpoint of the two-sided 95% Wilson interval ≥70% after costs**. A separately evidenced independence assessment must establish effective independent sample size equal to raw OOS count; missing or reduced effective sample size blocks qualification. An observed 70% alone does not pass. Nonoverlap alone does not prove independence; the historical bound is not a probability or guarantee for the next trade. AI confidence, bridge-supplied certificates and provisional research cannot qualify.

Success means TP1 before SL with positive modeled net cash within **60 minutes from actual entry in the tick replay**, rather than from the signal candle or message. Entry must be the next executable tick after the decision within ten seconds; timeout is a nonwin. TP2 is not the success target. Same-broker UTC Bid/Ask tick history is required for verified execution ordering. Candles-only replay with simulated spreads/fees can report provisional lower/upper bounds, but cannot unlock proposals.

Defaults are `MT5_COST_MODEL_VERIFIED=false` and no `MT5_EVIDENCE_PATH`/`MT5_EVIDENCE_SHA256`, so waiting with no actionable levels is expected. Do not relabel assumed costs as verified. Qualification also needs documented UTC/timezone/DST provenance, chronological development/validation/untouched OOS splits, data hashes, and evidenced commissions/slippage/financing and 0.01-lot contract, cash-conversion, grid and margin specifications covering the full tested period. Historical execution specifications must match the live symbol exactly; today's specifications and UTC offset cannot establish historical values. Verified zero financing still needs evidence of its applicability. Keep raw history and private metadata local.

Quotes must be at most **10 seconds** old, with a maximum five-second quote clock lead. Cache/feed/risk snapshots must be at most **30 seconds** old. Actionable alerts, chart proposals and native preparation expire **10 seconds after the triggering M1 candle closes**. Technical observations can use the completed bar for 75 seconds; they do not extend that action deadline. Filters require no open positions or pending orders, estimated loss including costs ≤1% equity, free margin ≥2× required margin, effective reward/risk ≥1.5, and spread ≤min(0.1R, 0.15 M1 ATR). The weekday UTC 06–19 window must fit the 60-minute horizon plus ten seconds of entry delay; actual broker session/holiday availability still needs broker checks. M1 tick activity must be at least half the preceding 20-bar median: it is a price-update proxy, not actual traded volume or market depth.

## Starting and preparing a ticket

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

Use the existing paired private Telegram chat to request preparation. Preparation is refused after **10 seconds from the triggering M1 close in qualified mode, or 30 seconds in explicit experimental Demo mode**. When the native window appears, verify its symbol, volume, Stop Loss, Take Profit, current price, and the suggested direction in its comment. Its comment also gives the proposal's expiry in UTC and marks experimental drafts `Demo EXP`. **Only you click the native Buy/Sell button, while the signal remains valid.** If the deadline passes before your click, cancel the draft and wait for a fresh signal. Cancelling the window places no order. A successfully prepared window does not expire or cancel itself.

An existing native order window is never overwritten. Close or cancel it yourself before requesting another proposal. Already attempted proposal IDs are not replayed, even after cancellation or a helper restart. Request a new proposal rather than deleting the journal.

Before populating fields, the adapter verifies the selected terminal process, executable path, local Demo account, active symbol/M1 chart, complete signal profile and three completed-bar references, native controls, and **absolute-price** SL/TP mode. Unsupported layouts and unverified price/points mode fail closed. It disables its own verified Order window during preparation, writes only Volume, Stop Loss, Take Profit, and the bounded direction/expiry comment, then reads them back. It enables the completed window only after the final expiry, account, quote, spread, and protection checks pass. It never sends Enter or clicks native trading buttons.

If preparation fails after the lock, the adapter cancels only that exact owned ticket, after verifying the same terminal, account binding, and single order window. It never closes an independently opened or replacement window. If account/window ownership changes, or MT5 refuses cancellation, the owned draft stays blocked and the helper reports failure; resolve that window in MT5 before requesting a fresh proposal.

The price-mode check opens Options, locates its native Trade page by the exact visible Stop levels label and combo, reads the real combo text, then closes Options through Cancel without changing or saving any settings. If another page is selected, bounded native tab navigation selects Trade through normal selection notifications; no tab index is assumed. Startup performs this read before registering or claiming that the helper is ready, and prints **Native price mode verified.** only after success. Choose **in prices** yourself if the helper reports `absolute_mode_unverified`. Another modal dialog or an independently opened order window blocks the check. The installed Order window is modeless, so the adapter freshly checks Options before opening it, after opening its own exact ticket, and before reporting success. It also checks the ticket's unit labels and numeric field readback. These checks establish the mode during preparation; review it again if you change settings afterward.

The fixed protection levels must still match the broker tick grid and minimum stop distances. The current quote must be recent, and spread plus movement from the proposal entry must remain within the existing 0.1R guard. These checks run during preparation. Prices continue changing while you review the ticket, so the reference entry is not a guaranteed fill price.

Press Ctrl+C to stop the foreground helper. It installs no background service or scheduled task. A successfully prepared window remains for your review or cancellation; an interrupted locked preparation follows the same guarded cancellation path.

## Proposal levels on the chart

Compile `bridge/MT5BotLevels.mq5` in MetaEditor and copy the resulting
`MT5BotLevels.ex5` into `MQL5/Indicators/MT5Bot` under this terminal's **File >
Open Data Folder**. Refresh MT5's Navigator and attach **MT5BotLevels** to
the intended **XAUUSD M1** chart. Use indicator **version 2.10**; old M15/CSV-v1
snapshots are rejected. It is a display-only custom indicator and needs
no DLLs, WebRequest permissions or algorithmic-trading setting. Keep the
paired manual helper running.

CSV version 2 remains exactly 18 fields. Its state is `active` for a qualified proposal, `experimental` for an explicit experimental Demo proposal, or `waiting`. Version 2.10 displays **Demo experimental**, **Estimated costs**, and **No certified win rate** on experimental levels, and overwrites those labels when the mode changes. Older version 2.00 indicators reject the experimental state and show no experimental levels until updated.

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
owner-bound proposal before checking status, subscription, the selected signal profile (empirical qualification in default qualified mode), risk pause, Demo
0.01 binding, receipt/quote freshness, broker metadata and explicit expiry.
A newer rejected or expired proposal cannot expose an older one again.

With chart export active, the helper refreshes the market feed and reads display
updates every **5 seconds**. It atomically publishes one headerless ASCII row
in the verified terminal common folder's
`Files/MT5Bot/levels_<terminal-data-folder-name>.csv`. The row expires within
25 seconds and no later than the proposal or **10-second quote** deadline. It contains a
random nonce and salted account-binding hash, never the raw account login,
server, Telegram chat/user, device ID or API key. The indicator checks its
current Demo account, terminal, XAUUSD M1 chart, UTC expiry, price grid and
completed broker chart bar before drawing. The broker UTC offset affects
chart coordinates only; expiry uses `TimeGMT()`.

Missing, malformed, stale or unavailable data clears only the indicator's
own objects and displays a waiting message. Account changes require reloading
the indicator. User drawings, orders, positions and an already prepared
native ticket are untouched. Stopping or crashing the helper cannot leave
active levels displayed past the short local expiry. When no valid proposal
exists, the indicator waits; it does not invent a trade.

Official references: [Native order window and submission](https://www.metatrader5.com/en/terminal/help/trading/performing_deals), [SL/TP prices versus points settings](https://www.metatrader5.com/en/terminal/help/startworking/settings), [SDK function list](https://www.mql5.com/en/docs/python_metatrader5), [Custom indicators](https://www.metatrader5.com/en/terminal/help/charts_analysis/indicators), and [MetaEditor compilation](https://www.metatrader5.com/en/metaeditor/help/development/compile).
