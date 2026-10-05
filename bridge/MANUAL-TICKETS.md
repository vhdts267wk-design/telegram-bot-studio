# Native MT5 ticket preparation

`mt5_manual_bridge.py` prepares the installed desktop terminal's **New Order** window for a human review and final native Buy/Sell click. It does not send an order, accept an execution request, modify a position, or close a trade. A `prepared` result means that the visible ticket fields were verified; it is not a trade confirmation.

Keep the intended Windows MT5 **Demo** terminal open, connected, and showing the exact broker gold symbol's **M15** chart. This workflow retains the existing **0.01 lot** limit and paired device/account identity. Algorithmic trading and external Python trading can remain disabled: the SDK is used only for prices, broker settings, and account checks.

Install the Windows helper dependencies from `bridge/requirements-manual.txt`. This includes the same MT5 SDK and `pywinauto`, used only to read the actual Stop levels setting in the Options dialog. Ticket fields use the targeted built-in Windows interface.

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

The price-mode check opens Options, selects the Trade tab, reads the Stop levels choice, then closes Options through Cancel without changing or saving any settings. Startup performs this read before registering or claiming that the helper is ready, and prints **Native price mode verified.** only after success. Choose **in prices** yourself if the helper reports `absolute_mode_unverified`. Another modal dialog or an independently opened order window blocks the check. The installed Order window is modeless, so the adapter freshly checks Options before opening it, after opening its own exact ticket, and before reporting success. It also checks the ticket's unit labels and numeric field readback. These checks establish the mode during preparation; review it again if you change settings afterward.

The fixed protection levels must still match the broker tick grid and minimum stop distances. The current quote must be recent, and spread plus movement from the proposal entry must remain within the existing 0.1R guard. These checks run during preparation. Prices continue changing while you review the ticket, so the reference entry is not a guaranteed fill price.

Press Ctrl+C to stop the foreground helper. It installs no background service or scheduled task. A successfully prepared window remains for your review or cancellation; an interrupted locked preparation follows the same guarded cancellation path.

Official references: [Native order window and submission](https://www.metatrader5.com/en/terminal/help/trading/performing_deals), [SL/TP prices versus points settings](https://www.metatrader5.com/en/terminal/help/startworking/settings), and [SDK function list](https://www.mql5.com/en/docs/python_metatrader5).
