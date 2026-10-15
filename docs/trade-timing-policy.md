# MT5 entry and exit timing policy

The strategy rejects a fixed primary target when a known opposing closed M30
level lies between entry and that target, including either boundary. BUY checks
resistance; SELL checks support. If live entry is already beyond the M30 range,
the latest completed M1 must also close strictly beyond that range extreme;
a wick or live quote alone does not confirm a break. Targets remain fixed at 2R/3R. This filter has
not been shown to improve profitability by an independent historical study.

Every new proposal requires a fresh, account-bound native
`SymbolInfoSessionTrade` snapshot. The Python SDK cannot supply this schedule
directly, so MT5 Bot Panel 2.17 answers a bounded request in common Files. Raw
account identity stays in memory; request and response use a nonce and binding
hash. Zero-length native intervals provide no usable opening evidence. Missing,
stale or mismatched evidence blocks proposals while candle telemetry stays live.

The policy requires a full 60 minutes through the latest allowed entry, ending
before the earlier of the broker session close minus 15 minutes and the existing
weekday 19:00 UTC analysis cutoff. The existing analysis window starts at 06:00
UTC. These conservative UTC bounds are additional restrictions, not fabricated
broker session hours.

Telegram displays the signal candle close, entry expiry, maximum holding time,
latest planned exit and session cutoff in dated Asia/Beirut time with the actual
UTC offset. The offer's latest planned exit assumes entry at the end of its
10-second qualified or 30-second experimental window. An armed Demo receipt
supplies the actual fill-based exit deadline.

Manual tickets require the owner to close the position. The analysis-only
startup helper does not execute trades or exits. Automated timed closing is
implemented only in the explicitly enabled, updated Demo execution helper,
following an owner-approved entry. New strategy profiles are incompatible with
older helpers that do not manage timed exits.

For future Demo fills, a durable intent is saved before the one FOK entry
attempt. The exit engine identifies the exact owned position through its
account binding, order/deal identifiers, symbol, direction, volume, comment and
magic. It never adopts an old, manual or modified position. The actual deadline
is fill + 3600 seconds, capped by the frozen latest and session cutoffs. Local
checks run before each HTTP operation and can recover from a server outage or
helper restart. A suspended computer, disconnected terminal or missing/changed
ownership can prevent timely execution; attached broker SL/TP remain in place.

Each close has one durable send reservation. Partial or uncertain close results
are reported for owner attention and are never resent automatically. Ownership
checks and preflight reads may repeat before any send. Owner-bound Telegram
events distinguish protection armed, confirmed closed and unconfirmed; they do
not expose account or broker identifiers.

Validation uses synthetic SDKs and isolated ledgers. No live entry or exit order
is necessary to test this policy. Native calendar boundary tests likewise use
synthetic schedules, alongside a read-only check against the actual terminal.
