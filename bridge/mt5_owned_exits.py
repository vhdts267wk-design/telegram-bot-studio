"""Durable timed exits for new, individually identified Demo helper fills.

This module never adopts pre-existing positions. A local intent is recorded
before the one entry send, and broker history must prove ownership before any
close request is possible. Read-only reconciliation can repeat; a close send
has one durable reservation and is never retried after an uncertain outcome.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from uuid import uuid4

try:
    from bridge import mt5_market_bridge as market
    from bridge import mt5_trade_bridge as trade
except ModuleNotFoundError:
    import mt5_market_bridge as market
    import mt5_trade_bridge as trade


MAX_HOLD_SECONDS = 3600


class ExitLedger:
    """Additive schema on the existing account-bound, synchronous WAL ledger."""

    def __init__(self, ledger):
        self.ledger, self.db = ledger, ledger.db
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS demo_owned_exits (
                offer_id TEXT PRIMARY KEY, intent TEXT NOT NULL,
                state TEXT NOT NULL, entry_order INTEGER NOT NULL DEFAULT 0,
                entry_deal INTEGER NOT NULL DEFAULT 0, position_id INTEGER NOT NULL DEFAULT 0,
                position_ticket INTEGER NOT NULL DEFAULT 0, filled_at TEXT,
                exit_at TEXT, close_attempted INTEGER NOT NULL DEFAULT 0,
                result TEXT NOT NULL DEFAULT '{}')""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS demo_exit_events (
                event_id TEXT PRIMARY KEY, offer_id TEXT NOT NULL,
                event_key TEXT NOT NULL, payload TEXT NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0,
                UNIQUE(offer_id,event_key))""")

    def get(self, offer_id):
        row = self.db.execute("SELECT * FROM demo_owned_exits WHERE offer_id=?", (offer_id,)).fetchone()
        if row is None:
            return None
        keys = ("offer_id", "intent", "state", "entry_order", "entry_deal", "position_id",
                "position_ticket", "filled_at", "exit_at", "close_attempted", "result")
        value = dict(zip(keys, row))
        value["intent"], value["result"] = json.loads(value["intent"]), json.loads(value["result"])
        return value

    def active(self):
        # Unresolved historic attempts must never hide a new owned fill. Keep
        # the newest work in every state, plus a bounded set of older urgent
        # deadlines. One account position is required before each new entry.
        rows = self.db.execute("""WITH latest AS (
                SELECT rowid AS sequence,offer_id,state,exit_at FROM demo_owned_exits
                WHERE state IN ('awaiting_fill','armed','unknown') ORDER BY rowid DESC LIMIT 100
            ), urgent AS (
                SELECT rowid AS sequence,offer_id,state,exit_at FROM demo_owned_exits
                WHERE state='armed' ORDER BY exit_at, rowid DESC LIMIT 20
            ), selected AS (
                SELECT * FROM latest UNION SELECT * FROM urgent
            ) SELECT offer_id FROM selected
            ORDER BY CASE state WHEN 'armed' THEN 0 WHEN 'awaiting_fill' THEN 1 ELSE 2 END,
                CASE WHEN state='armed' THEN exit_at ELSE NULL END, sequence DESC""").fetchall()
        return [self.get(row[0]) for row in rows]

    def stage(self, offer, request, now):
        from bot.trade_timing import validate_trade_timing
        timing = offer["payload"].get("trade_timing")
        if not validate_trade_timing(timing, now, offer["payload"].get("bar_time")):
            raise trade.GuardError("A frozen one-hour Demo exit policy is required.")
        session_exit = trade.utc_date(timing.get("session_exit_at"))
        latest_exit = trade.utc_date(timing.get("latest_exit_at"))
        expires = trade.utc_date(offer["expires_at"])
        if (not now < expires < session_exit or not now < latest_exit
                or expires != trade.utc_date(timing["entry_deadline"])):
            raise trade.GuardError("The frozen Demo exit window is no longer valid.")
        intent = {"binding": self.ledger.value("binding"), "created_at": trade.iso_date(now),
                  "entry_expires_at": trade.iso_date(expires), "symbol": request["symbol"],
                  "direction": offer["payload"]["direction"], "volume": str(trade.DEMO_VOLUME),
                  "magic": request["magic"], "comment": request["comment"],
                  "stop": str(trade.positive_decimal(request.get("sl"))),
                  "target": str(trade.positive_decimal(request.get("tp"))),
                  "session_exit_at": trade.iso_date(session_exit), "latest_exit_at": trade.iso_date(latest_exit)}
        if not intent["binding"]:
            raise trade.GuardError("The Demo exit account binding is unavailable.")
        with self.db:
            inserted = self.db.execute("""INSERT OR IGNORE INTO demo_owned_exits
                (offer_id,intent,state) VALUES (?,?,'awaiting_fill')""",
                (trade.uuid_text(offer["id"]), json.dumps(intent, sort_keys=True)))
            if inserted.rowcount != 1:
                raise trade.GuardError("An existing Demo exit intent cannot be replaced.")

    def entry_response(self, offer_id, response):
        order = trade.safe_integer(getattr(response, "order", None))
        deal = trade.safe_integer(getattr(response, "deal", None))
        with self.db:
            self.db.execute("UPDATE demo_owned_exits SET entry_order=?,entry_deal=? WHERE offer_id=?",
                            (order, deal, offer_id))

    def entry_rejected(self, offer_id):
        with self.db:
            self.db.execute("UPDATE demo_owned_exits SET state='entry_failed' WHERE offer_id=?", (offer_id,))

    def event(self, row, key, status, now, *, code=0, reason=None):
        with self.db:
            self._insert_event(row, key, status, now, code=code, reason=reason)

    def _insert_event(self, row, key, status, now, *, code=0, reason=None):
        """Insert into the caller's transaction; never commit a state change."""
        deadline = row["exit_at"] or min(row["intent"]["latest_exit_at"], row["intent"]["session_exit_at"])
        if reason is None:
            reason = "session_cutoff" if deadline == row["intent"]["session_exit_at"] else "holding_limit"
            if status == "closed" and now < trade.utc_date(deadline):
                reason = "position_closed"
        payload = {"event_id": str(uuid4()), "status": status, "observed_at": trade.iso_date(now),
                   "deadline": deadline, "reason": reason, "code": trade.safe_integer(code),
                   "deal": row["entry_deal"], "order": row["entry_order"]}
        self.db.execute("INSERT OR IGNORE INTO demo_exit_events (event_id,offer_id,event_key,payload) VALUES (?,?,?,?)",
                        (payload["event_id"], row["offer_id"], key, json.dumps(payload)))

    def arm(self, row, now):
        """Persist ownership and its notification in one durable transaction."""
        with self.db:
            changed = self.db.execute("""UPDATE demo_owned_exits SET state='armed',entry_order=?,entry_deal=?,position_id=?,
                position_ticket=?,filled_at=?,exit_at=? WHERE offer_id=? AND state='awaiting_fill'""",
                (row["entry_order"], row["entry_deal"], row["position_id"], row["position_ticket"],
                 row["filled_at"], row["exit_at"], row["offer_id"]))
            if changed.rowcount == 1 and now <= trade.utc_date(row["exit_at"]):
                self._insert_event(row, "armed", "armed", now)
        return self.get(row["offer_id"])

    def pending_events(self):
        rows = self.db.execute("""SELECT events.offer_id,offers.claim_id,events.payload FROM demo_exit_events AS events
            JOIN offers ON offers.id=events.offer_id WHERE events.acknowledged=0 ORDER BY events.rowid LIMIT 100""").fetchall()
        return [{"offer_id": row[0], "claim_id": row[1], "event": json.loads(row[2])} for row in rows]

    def acknowledge(self, event_id):
        with self.db:
            self.db.execute("UPDATE demo_exit_events SET acknowledged=1 WHERE event_id=?", (event_id,))

    def reserve_close(self, row):
        with self.db:
            changed = self.db.execute("""UPDATE demo_owned_exits SET close_attempted=1,state='unknown'
                WHERE offer_id=? AND state='armed' AND close_attempted=0""", (row["offer_id"],))
        return changed.rowcount == 1

    def finish(self, row, state, now, code=0, *, reason=None):
        result = {"status": state, "occurred_at": trade.iso_date(now), "code": trade.safe_integer(code)}
        with self.db:
            self.db.execute("UPDATE demo_owned_exits SET state=?,result=? WHERE offer_id=?",
                            (state, json.dumps(result), row["offer_id"]))
            self._insert_event(row, state, state, now, code=code, reason=reason)


def _integer(obj, key):
    return trade.safe_integer(getattr(obj, key, None))


def _volume(obj):
    return trade.positive_decimal(getattr(obj, "volume", None))


def _deal_time(deal, settings):
    seconds = market.broker_timestamp_utc(getattr(deal, "time", None), settings.market)
    milliseconds = getattr(deal, "time_msc", None)
    if milliseconds is not None:
        if type(milliseconds) is not int or milliseconds <= 0:
            raise trade.GuardError("The owned Demo fill millisecond timestamp is invalid.")
        normalized = milliseconds - settings.market.broker_utc_offset_minutes * 60000
        if normalized // 1000 != seconds:
            raise trade.GuardError("The owned Demo fill timestamps are inconsistent.")
        return datetime.fromtimestamp(normalized / 1000, timezone.utc)
    # Older SDK records supply whole seconds only. The server admits a one
    # second lower-bound precision tolerance, never a later exit deadline.
    return datetime.fromtimestamp(seconds, timezone.utc)


def _entry_matches(deal, row, settings):
    intent = row["intent"]
    start = trade.utc_date(intent["created_at"]) - timedelta(seconds=5)
    end = trade.utc_date(intent["entry_expires_at"]) + timedelta(seconds=60)
    return (getattr(deal, "symbol", None) == intent["symbol"]
            and _integer(deal, "magic") == intent["magic"]
            and getattr(deal, "comment", None) == intent["comment"]
            and getattr(deal, "entry", None) == 0  # DEAL_ENTRY_IN
            and getattr(deal, "reason", None) == 3  # DEAL_REASON_EXPERT
            and getattr(deal, "type", None) == (0 if intent["direction"] == "BUY" else 1)
            and _volume(deal) == trade.DEMO_VOLUME
            and _integer(deal, "ticket") and _integer(deal, "order") and _integer(deal, "position_id")
            and (not row["position_id"] or _integer(deal, "position_id") == row["position_id"])
            and (not row["entry_order"] or _integer(deal, "order") == row["entry_order"])
            and (not row["entry_deal"] or _integer(deal, "ticket") == row["entry_deal"])
            and start <= _deal_time(deal, settings) <= end)


def _position(mt5, row):
    positions = mt5.positions_get()
    if positions is None:
        raise trade.GuardError("Complete position identity is required for a timed Demo exit.")
    matches = [position for position in positions if _integer(position, "identifier") == row["position_id"]]
    if len(matches) > 1:
        raise trade.GuardError("The owned Demo position identity is ambiguous.")
    if not matches:
        return None
    position, intent = matches[0], row["intent"]
    if (not _integer(position, "ticket")
            or (row["position_ticket"] and _integer(position, "ticket") != row["position_ticket"])
            or getattr(position, "symbol", None) != intent["symbol"]
            or _integer(position, "magic") != intent["magic"]
            or getattr(position, "comment", None) != intent["comment"]
            or getattr(position, "type", None) != (0 if intent["direction"] == "BUY" else 1)
            or _volume(position) != trade.DEMO_VOLUME
            or trade.positive_decimal(getattr(position, "sl", None)) != trade.positive_decimal(intent.get("stop"))
            or trade.positive_decimal(getattr(position, "tp", None)) != trade.positive_decimal(intent.get("target"))):
        raise trade.GuardError("The owned Demo position changed; a timed close is blocked.")
    return position


def _owned_history(mt5, row, settings):
    deals = mt5.history_deals_get(position=row["position_id"])
    if deals is None or not deals:
        raise trade.GuardError("Complete owned Demo deal history is required.")
    entries = [deal for deal in deals if getattr(deal, "entry", None) in (0, 2)]
    if len(entries) != 1 or not _entry_matches(entries[0], row, settings):
        raise trade.GuardError("The Demo position has another entry or changed ownership.")
    exits = [deal for deal in deals if getattr(deal, "entry", None) in (1, 3)]
    if len(entries) + len(exits) != len(deals):
        raise trade.GuardError("The owned Demo history contains an unsupported adjustment.")
    for deal in exits:
        if (getattr(deal, "symbol", None) != row["intent"]["symbol"]
                or _integer(deal, "position_id") != row["position_id"]
                or getattr(deal, "type", None) != (1 if row["intent"]["direction"] == "BUY" else 0)):
            raise trade.GuardError("The owned Demo exit history is inconsistent.")
    return exits


def reconcile_fill(mt5, settings, exits, row, now):
    """Repeat only reads until a unique new broker fill can be identified."""
    intent = row["intent"]
    if row["entry_deal"]:
        deals = mt5.history_deals_get(ticket=row["entry_deal"])
    else:
        offset = timedelta(minutes=settings.market.broker_utc_offset_minutes)
        start = trade.utc_date(intent["created_at"]) - timedelta(seconds=5) + offset
        end = min(now, trade.utc_date(intent["entry_expires_at"]) + timedelta(seconds=60)) + offset
        deals = mt5.history_deals_get(start, end)
    if deals is None:
        return row
    matches = [deal for deal in deals if _entry_matches(deal, row, settings)]
    if len(matches) != 1:
        if now >= trade.utc_date(intent["latest_exit_at"]):
            exits.event(row, "ownership_unverified", "missing", now, reason="ownership_unverified")
        return row
    deal = matches[0]
    filled = _deal_time(deal, settings)
    deadline = min(filled + timedelta(seconds=MAX_HOLD_SECONDS), trade.utc_date(intent["session_exit_at"]),
                   trade.utc_date(intent["latest_exit_at"]))
    row.update(entry_order=_integer(deal, "order"), entry_deal=_integer(deal, "ticket"),
               position_id=_integer(deal, "position_id"), filled_at=trade.iso_date(filled), exit_at=trade.iso_date(deadline))
    _owned_history(mt5, row, settings)
    position = _position(mt5, row)
    if position is not None:
        row["position_ticket"] = _integer(position, "ticket")
    return exits.arm(row, now)


def _close_request(mt5, settings, row, now):
    position = _position(mt5, row)
    if position is None or not row["position_ticket"]:
        raise trade.GuardError("A confirmed owned Demo position ticket is required.")
    if _owned_history(mt5, row, settings):
        raise trade.GuardError("A previously reduced Demo position cannot be closed again.")
    symbol = mt5.symbol_info(row["intent"]["symbol"])
    tick = mt5.symbol_info_tick(row["intent"]["symbol"])
    if symbol is None or tick is None:
        raise trade.GuardError("A fresh broker quote is required for the Demo exit.")
    stamp = market.broker_timestamp_utc(getattr(tick, "time", None), settings.market)
    bid, ask = (trade.positive_decimal(getattr(tick, key, None)) for key in ("bid", "ask"))
    if ask <= bid or not -trade.EXECUTABLE_QUOTE_FUTURE_TOLERANCE_SECONDS <= now.timestamp() - stamp <= 10:
        raise trade.GuardError("A fresh uncrossed broker quote is required for the Demo exit.")
    filling, execution = getattr(symbol, "filling_mode", None), getattr(symbol, "trade_exemode", None)
    if type(filling) is not int or type(execution) is not int or execution not in (0, 1, 2, 3):
        raise trade.GuardError("The broker Demo exit filling policy is unavailable.")
    if execution in (0, 1) or filling & 1:
        policy = mt5.ORDER_FILLING_FOK
    elif filling & 2:
        policy = mt5.ORDER_FILLING_IOC
    else:
        raise trade.GuardError("No bounded broker Demo exit filling policy is available.")
    request = {"action": mt5.TRADE_ACTION_DEAL, "symbol": row["intent"]["symbol"],
               "position": row["position_ticket"], "volume": float(trade.DEMO_VOLUME),
               "type": mt5.ORDER_TYPE_SELL if row["intent"]["direction"] == "BUY" else mt5.ORDER_TYPE_BUY,
               "deviation": 0, "magic": row["intent"]["magic"],
               "comment": "tgx:" + row["intent"]["comment"][4:],
               "type_time": mt5.ORDER_TIME_GTC, "type_filling": policy}
    if execution != 2:
        request["price"] = float(bid if row["intent"]["direction"] == "BUY" else ask)
    return request


def _confirmed_absent(mt5, settings, row):
    if _position(mt5, row) is not None:
        return False
    closed = sum((_volume(deal) for deal in _owned_history(mt5, row, settings)), Decimal(0))
    return closed == trade.DEMO_VOLUME


def process_exits(mt5, settings, ledger, account_guard, clock=trade.now_utc):
    """Local watcher, independent of HTTP and candle-feed availability."""
    exits = ExitLedger(ledger)
    for row in exits.active():
        account_guard(mt5, settings, ledger, require_empty=False)
        if ledger.value("binding") != row["intent"]["binding"]:
            raise trade.GuardError("The saved Demo exit account binding changed.")
        now = clock()
        try:
            if row["state"] == "awaiting_fill":
                row = reconcile_fill(mt5, settings, exits, row, now)
            if row["state"] == "awaiting_fill":
                continue
            if _confirmed_absent(mt5, settings, row):
                # Discovery time cannot reveal whether SL, TP, a manual action
                # or a previous uncertain attempt actually closed the trade.
                exits.finish(row, "closed", now, reason="position_closed")
                continue
            if row["state"] == "unknown":
                exits.event(row, "unconfirmed", "unknown", now, reason="close_not_confirmed")
                continue  # NEVER resend an interrupted/partial close.
            if now < trade.utc_date(row["exit_at"]):
                continue
            request = _close_request(mt5, settings, row, now)
            checked = mt5.order_check(request)
            if checked is None or type(getattr(checked, "retcode", None)) is not int or checked.retcode != 0:
                exits.event(row, "preflight_blocked", "missing", now, reason="exit_preflight_rejected")
                continue  # No send happened; later fresh conditions may be checked.
            account_guard(mt5, settings, ledger, require_empty=False)
            final = _close_request(mt5, settings, row, clock())
            if final != request:
                exits.event(row, "quote_changed", "missing", now, reason="exit_quote_changed")
                continue
            if not exits.reserve_close(row):
                continue
            try:
                response = mt5.order_send(final)  # One durable close attempt.
                code = trade.safe_integer(getattr(response, "retcode", None))
                account_guard(mt5, settings, ledger, require_empty=False)
                if _confirmed_absent(mt5, settings, row):
                    exits.finish(row, "closed", clock(), code, reason="position_closed")
                elif code in trade.REJECTION_CODES:
                    exits.finish(row, "failed", clock(), code)
                else:
                    exits.event(row, "unconfirmed", "unknown", clock(), code=code, reason="close_not_confirmed")
            except trade.GuardError:
                raise
            except Exception:
                exits.event(row, "unconfirmed", "unknown", clock(), reason="close_not_confirmed")
        except (trade.GuardError, market.MarketDataError):
            # Unavailable identity, stale quote, manual changes or incomplete
            # history cannot authorize a send. The next loop may repeat reads.
            account_guard(mt5, settings, ledger, require_empty=False)
            exits.event(row, "guard_blocked", "missing", now, reason="exit_guard_blocked")
    return exits
