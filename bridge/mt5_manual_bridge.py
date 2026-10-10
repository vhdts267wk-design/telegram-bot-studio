"""User-started Demo bridge that prepares native MT5 tickets for a human click.

The SDK is used for data and checks only. This helper never calls order_send,
order_check, execute_offer, or a native Buy/Sell button. Its result means a
verified visible draft, never an executed trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import hmac
import importlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit

try:
    from bridge import mt5_market_bridge as market
    from bridge import mt5_trade_bridge as trade
    from bridge import mt5_native_ticket as native
    from bridge import mt5_chart_overlay as chart
except ModuleNotFoundError:
    import mt5_market_bridge as market
    import mt5_trade_bridge as trade
    import mt5_native_ticket as native
    import mt5_chart_overlay as chart


@dataclass(frozen=True)
class Settings:
    market: market.Settings
    state_directory: Path
    account_mode: str
    volume: Decimal
    enable_manual_tickets: bool


def load_settings(args, environ=None):
    if not args.enable_manual_tickets or args.account_mode != "demo" or trade.positive_decimal(args.volume) != trade.DEMO_VOLUME:
        raise trade.GuardError("This helper requires manual Demo tickets at exactly 0.01 lots.")
    configured = market.load_settings(args, environ)
    if len(configured.key) < 32:
        raise trade.GuardError("The private manual bridge key must contain at least 32 characters.")
    return Settings(configured, Path(args.state_directory).expanduser().resolve(), "demo", trade.DEMO_VOLUME, True)


def check_manual_account(mt5, settings, ledger):
    """Retain the existing private account binding without requesting algo access."""
    if not settings.enable_manual_tickets or settings.account_mode != "demo" or settings.volume != trade.DEMO_VOLUME:
        raise trade.GuardError("Only manual Demo tickets at 0.01 lots are supported.")
    market.check_terminal(mt5, settings.market.terminal)
    account = mt5.account_info()
    if account is None or not getattr(account, "trade_allowed", False):
        raise trade.GuardError("The selected account must permit manual trading.")
    mode = getattr(account, "trade_mode", None)
    if type(mode) is not int or mode != getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0):
        raise trade.GuardError("The selected account is not Demo; ticket preparation is blocked.")
    login, server = getattr(account, "login", None), getattr(account, "server", None)
    if type(login) is not int or login <= 0 or type(server) is not str or not server or len(server) > 256:
        raise trade.GuardError("The local account identity could not be verified.")
    identity = json.dumps([server, login], separators=(",", ":")).encode("utf-8")
    digest = hmac.new(bytes.fromhex(ledger.value("binding_salt")), identity, hashlib.sha256).hexdigest()
    binding = json.dumps({
        "account_hash": digest, "terminal": str(settings.market.terminal).casefold(),
        "symbol": settings.market.symbol, "mode": settings.account_mode,
        "volume": str(settings.volume), "origin": urlsplit(settings.market.url).netloc,
    }, sort_keys=True)
    ledger.bind(binding)
    return account


def prepare_draft(mt5, settings, ledger, preparation, now):
    """Validate fixed protection, spread/drift and expiry without any trading API."""
    if not isinstance(preparation, dict) or preparation.get("workflow") != "manual_ticket":
        raise trade.GuardError("The preparation has an unsupported workflow.")
    payload, entry, stop, target = validate_manual_offer(preparation, settings, now)
    expires = trade.utc_date(preparation["expires_at"])
    if expires > now + timedelta(minutes=5):
        raise trade.GuardError("The manual ticket deadline exceeds five minutes.")
    account = check_manual_account(mt5, settings, ledger)
    symbol = mt5.symbol_info(settings.market.symbol)
    if symbol is None or getattr(symbol, "currency_base", None) != "XAU" or getattr(symbol, "currency_profit", None) != "USD":
        raise trade.GuardError("The broker symbol must identify XAU in USD.")
    direction = payload["direction"]
    symbol_mode = getattr(symbol, "trade_mode", None)
    if type(symbol_mode) is not int or (symbol_mode != 4 and symbol_mode != (1 if direction == "BUY" else 2)):
        raise trade.GuardError("The broker symbol does not permit this direction.")
    modes = getattr(symbol, "order_mode", None)
    if type(modes) is not int or (modes & 49) != 49:
        raise trade.GuardError("The broker must support market orders with SL and TP.")
    minimum, maximum, step = (trade.positive_decimal(getattr(symbol, key, None)) for key in ("volume_min", "volume_max", "volume_step"))
    if not minimum <= settings.volume <= maximum or settings.volume % step:
        raise trade.GuardError("The broker does not support exactly 0.01 lots.")
    metadata = trade.execution_metadata(symbol)
    tick_size, point = (trade.positive_decimal(metadata[key]) for key in ("tick_size", "point"))
    digits = metadata["digits"]
    quantum = Decimal(1).scaleb(-digits)
    if tick_size < quantum or tick_size % quantum or stop % tick_size or target % tick_size:
        raise trade.GuardError("The fixed SL and TP do not match the broker price grid.")
    if payload.get("execution") != metadata or payload.get("price_digits") != digits:
        raise trade.GuardError("The broker precision or stop restrictions changed since the proposal.")
    if trade.positive_decimal(payload.get("original_stop_distance")) != abs(entry - stop):
        raise trade.GuardError("The original proposal risk distance could not be verified.")
    margin_mode = getattr(account, "margin_mode", None)
    if type(margin_mode) is not int or margin_mode not in (0, 1, 2):
        raise trade.GuardError("The account position model could not be verified.")
    positions, orders = mt5.positions_get(), mt5.orders_get()
    if positions is None or orders is None or len(positions) or len(orders):
        raise trade.GuardError("The account must have no existing positions or pending orders.")
    tick = mt5.symbol_info_tick(settings.market.symbol)
    if tick is None:
        raise trade.GuardError("A fresh executable quote is required.")
    stamp = market.broker_timestamp_utc(getattr(tick, "time", None), settings.market)
    bid, ask = (trade.positive_decimal(getattr(tick, key, None)) for key in ("bid", "ask"))
    if ask <= bid or not -trade.EXECUTABLE_QUOTE_FUTURE_TOLERANCE_SECONDS <= now.timestamp() - stamp <= 10:
        raise trade.GuardError("The quote is stale, crossed or future-dated.")
    price = ask if direction == "BUY" else bid
    if ask - bid + abs(price - entry) > abs(entry - stop) * trade.MAX_DRIFT_R:
        raise trade.GuardError("Spread plus entry drift exceeds the original 0.1R guard.")
    distance = point * metadata["stops_level"]
    if direction == "BUY" and (not stop < price < target or bid - stop < distance or target - bid < distance):
        raise trade.GuardError("The fixed BUY stops no longer satisfy the broker quote.")
    if direction == "SELL" and (not target < price < stop or stop - ask < distance or ask - target < distance):
        raise trade.GuardError("The fixed SELL stops no longer satisfy the broker quote.")
    try:
        current = market.build_payload(mt5, settings.market, now)
        risk = current["risk_context"]
        if risk["open_positions"] or risk["pending_orders"]:
            raise trade.GuardError("An unexposed account is required.")
        if payload.get("broker_fingerprint") != risk["broker_fingerprint"]:
            raise trade.GuardError("The proposal broker model changed.")
        if payload.get("signal_mode") == "experimental_demo":
            costs = experimental_cost_context(payload, risk, metadata, ask - bid)
        else:
            if risk["costs_verified"] is not True:
                raise trade.GuardError("Verified costs and an unexposed account are required.")
            costs = {key: float(risk[key]) for key in ("commission_round_turn", "slippage_price", "loss_cash_per_price_unit", "profit_cash_per_price_unit")}
            if payload.get("cost_context") != costs:
                raise trade.GuardError("The qualified broker or cost model changed.")
        for key, frame, seconds in (("bar_time", "M1", 60), ("confirmation_bar_time", "M5", 300), ("direction_bar_time", "M15", 900)):
            reference = trade.utc_date(payload[key])
            if not any(trade.utc_date(row["time"]) == reference for row in current["timeframes"][frame]):
                raise trade.GuardError("The completed timeframe reference is unavailable.")
        try:
            local_context = chart.current_timeframe_context(current, observed_at=now,
                broker_offset_minutes=settings.market.broker_utc_offset_minutes)
            chart.validate_timeframe_context(local_context, direction)
            if local_context != payload["timeframe_context"]:
                raise chart.OverlayError("The local five-timeframe context changed")
            references = chart.validate_context_references(payload["context_bar_times"],
                trigger=trade.utc_date(payload["bar_time"]).timestamp(), boundary=now.timestamp(),
                broker_offset_minutes=settings.market.broker_utc_offset_minutes)
            for frame, reference in references.items():
                if trade.utc_date(current["timeframes"][frame][-1]["time"]) != reference:
                    raise chart.OverlayError("The local higher-timeframe reference changed")
        except chart.OverlayError:
            raise trade.GuardError("Current aligned five-timeframe context could not be verified.") from None
        slippage = Decimal(str(costs["slippage_price"]))
        commission = Decimal(str(costs["commission_round_turn"]))
        loss = (abs(price - stop) + 2 * slippage) * Decimal(str(risk["loss_cash_per_price_unit"])) + commission
        gain = (abs(target - price) - 2 * slippage) * Decimal(str(risk["profit_cash_per_price_unit"])) - commission
        if loss <= 0 or loss > Decimal(str(risk["equity"])) * Decimal("0.01") or gain / loss < Decimal("1.5") or risk["free_margin"] < 2 * risk["margin_required"]:
            raise trade.GuardError("The current cash risk, reward or margin guard failed.")
    except market.MarketDataError:
        raise trade.GuardError("Current five-timeframe risk data is unavailable.") from None
    return native.Draft(
        settings.market.symbol, direction, settings.volume, stop, target, digits, expires,
        **{key: payload[key] for key in ("display_timeframe", "strategy_id", "strategy_version", "policy_id", "horizon_seconds", "strategy_fingerprint")},
        qualification_id=payload.get("qualification_id", ""),
        signal_mode=payload.get("signal_mode", "qualified"), provisional=payload["provisional"],
        entry_window_seconds=payload.get("entry_window_seconds", 10),
        cost_assumptions=payload.get("cost_assumptions"),
        direction_bar_time=trade.utc_date(payload["direction_bar_time"]),
        confirmation_bar_time=trade.utc_date(payload["confirmation_bar_time"]),
        bar_time=trade.utc_date(payload["bar_time"]),
        context_bar_times=payload["context_bar_times"], timeframe_context=payload["timeframe_context"],
        broker_utc_offset_minutes=settings.market.broker_utc_offset_minutes,
    )


def experimental_cost_context(payload, risk, execution, current_spread):
    """Recheck Demo estimates; missing live costs never replace them with zero."""
    try:
        assumptions = chart.validate_cost_assumptions(payload.get("cost_assumptions"))
    except chart.OverlayError:
        raise trade.GuardError("Invalid experimental cost assumptions.") from None
    context = payload.get("cost_context")
    keys = {"commission_round_turn", "slippage_price", "loss_cash_per_price_unit", "profit_cash_per_price_unit"}
    if type(context) is not dict or set(context) != keys:
        raise trade.GuardError("The experimental cash-cost model is incomplete.")
    parsed = {key: trade.positive_decimal(context[key]) for key in keys}
    tick = trade.positive_decimal(execution["tick_size"])
    loss_unit = trade.positive_decimal(risk["loss_cash_per_price_unit"])
    profit_unit = trade.positive_decimal(risk["profit_cash_per_price_unit"])
    commission_floor = loss_unit * max(assumptions["spread_price"], 10 * tick)
    if (assumptions["tick_size"] != tick
        or parsed["loss_cash_per_price_unit"] != loss_unit or parsed["profit_cash_per_price_unit"] != profit_unit
        or parsed["commission_round_turn"] != assumptions["commission_round_turn"]
        or parsed["slippage_price"] != assumptions["slippage_price"]
        or assumptions["commission_round_turn"] < commission_floor):
        raise trade.GuardError("The experimental broker or cost assumptions changed.")
    def reported(key):
        amount = risk.get(key)
        if amount is None:
            if risk.get("costs_verified") is not False:
                raise trade.GuardError("The current verified cost model is incomplete.")
            return Decimal(0)
        if type(amount) in (int, float) and amount == 0:
            return Decimal(0)
        return trade.positive_decimal(amount)
    commission = max(parsed["commission_round_turn"], reported("commission_round_turn"), loss_unit * max(current_spread, 10 * tick))
    slippage = max(parsed["slippage_price"], reported("slippage_price"), current_spread / 2, 2 * tick)
    return {**context, "commission_round_turn": commission, "slippage_price": slippage}


def validate_manual_offer(offer, settings, now):
    """Allow complete, distinct Demo profiles without automatic execution."""
    if type(offer) is not dict or type(offer.get("payload")) is not dict:
        raise trade.GuardError("Invalid manual proposal.")
    trade.uuid_text(offer.get("id"))
    trade.uuid_text(offer.get("claim_id"))
    payload = offer["payload"]
    experimental = payload.get("signal_mode") == "experimental_demo"
    profile_valid = (
        payload.get("provisional") is True
        and payload.get("strategy_id") == "mtf-ema-pullback-60m-demo-v3"
        and type(payload.get("strategy_version")) is int and payload["strategy_version"] == 3
        and payload.get("policy_id") == "mtf-manual-demo-estimated-cost-risk-v3"
        and type(payload.get("entry_window_seconds")) is int and payload["entry_window_seconds"] == 30
        and payload.get("qualification_id", "") == ""
        and "evidence_metrics" not in payload
    ) if experimental else (
        payload.get("provisional") is False
        and payload.get("strategy_id") == "mtf-ema-pullback-60m-v2"
        and type(payload.get("strategy_version")) is int and payload["strategy_version"] == 2
        and payload.get("policy_id") == "mtf-manual-demo-cost-risk-v2"
        and payload.get("signal_mode", "qualified") == "qualified"
        and type(payload.get("entry_window_seconds", 10)) is int and payload.get("entry_window_seconds", 10) == 10
        and "cost_assumptions" not in payload
        and type(payload.get("qualification_id")) is str and re.fullmatch(r"[0-9a-f]{64}", payload["qualification_id"]) is not None
    )
    if (
        not profile_valid or not settings.enable_manual_tickets or settings.account_mode != "demo" or settings.volume != trade.DEMO_VOLUME
        or payload.get("symbol") != settings.market.symbol or payload.get("account_mode") != "demo"
        or trade.positive_decimal(payload.get("volume")) != settings.volume
        or payload.get("direction") not in ("BUY", "SELL")
        or payload.get("display_timeframe") != "M1"
        or type(payload.get("horizon_seconds")) is not int or payload["horizon_seconds"] != 3600
        or any(type(payload.get(key)) is not str or re.fullmatch(r"[0-9a-f]{64}", payload[key]) is None for key in ("strategy_fingerprint", "broker_fingerprint"))
    ):
        raise trade.GuardError("A complete five-timeframe manual Demo signal profile is required.")
    try:
        if chart.validate_broker_offset(payload.get("broker_utc_offset_minutes")) != settings.market.broker_utc_offset_minutes:
            raise chart.OverlayError("Changed broker offset")
        chart.validate_timeframe_context(payload.get("timeframe_context"), payload["direction"])
    except chart.OverlayError:
        raise trade.GuardError("A complete aligned five-timeframe manual Demo context is required.") from None
    if experimental:
        try:
            chart.validate_cost_assumptions(payload.get("cost_assumptions"))
        except chart.OverlayError:
            raise trade.GuardError("Invalid experimental cost assumptions.") from None
    expires, bar = trade.utc_date(offer.get("expires_at")), trade.utc_date(payload.get("bar_time"))
    decision = trade.utc_date(payload.get("decision_time"))
    window = 30 if experimental else 10
    deadline = bar + timedelta(seconds=60 + window) if experimental else bar + timedelta(minutes=6)
    if not bar + timedelta(minutes=1) <= decision <= now < expires <= min(now + timedelta(minutes=5), deadline) or now - (bar + timedelta(minutes=1)) > timedelta(seconds=window):
        raise trade.GuardError("The manual M1 entry window of " + str(window) + " seconds or its deadline expired.")
    for key, seconds in (("bar_time", 60), ("confirmation_bar_time", 300), ("direction_bar_time", 900)):
        stamp = trade.utc_date(payload[key])
        if stamp.microsecond or stamp.timestamp() != market.latest_closed_bar(bar.timestamp() + 60, seconds, settings.market.broker_utc_offset_minutes):
            raise trade.GuardError("The closed timeframe references are invalid.")
    try:
        chart.validate_context_references(payload.get("context_bar_times"), trigger=bar.timestamp(),
            boundary=now.timestamp(), broker_offset_minutes=settings.market.broker_utc_offset_minutes)
    except chart.OverlayError:
        raise trade.GuardError("The closed H1/H4 context references are invalid.") from None
    entry, stop, target = (trade.positive_decimal(payload.get(key)) for key in ("entry", "stop", "target"))
    if not (stop < entry < target if payload["direction"] == "BUY" else target < entry < stop) or trade.positive_decimal(payload.get("max_drift_r")) != trade.MAX_DRIFT_R:
        raise trade.GuardError("The frozen manual protection or drift limit is invalid.")
    return payload, entry, stop, target


class ManualJournal:
    """A distinct durable draft outbox in the existing device identity ledger."""

    def __init__(self, ledger):
        self.ledger = ledger
        self.db = ledger.db
        self.db.execute("""CREATE TABLE IF NOT EXISTS manual_tickets (
            id TEXT PRIMARY KEY, claim_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
            status TEXT NOT NULL, reason TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0)""")
        self.db.commit()

    def reserve(self, preparation):
        identity, claim = trade.uuid_text(preparation.get("id")), trade.uuid_text(preparation.get("claim_id"))
        encoded = json.dumps({key: preparation.get(key) for key in ("workflow", "payload", "expires_at")},
                             sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > trade.MAX_JSON_BYTES:
            raise trade.GuardError("The preparation is too large.")
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self.db:
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO manual_tickets VALUES (?,?,?,'failed','interrupted',0)",
                (identity, claim, digest),
            )
            row = self.db.execute("SELECT payload_hash,status FROM manual_tickets WHERE id=?", (identity,)).fetchone()
            if row[0] != digest:
                raise trade.GuardError("An existing draft identity changed; preparation is blocked.")
            self.db.execute("UPDATE manual_tickets SET claim_id=?,acknowledged=0 WHERE id=?", (claim, identity))
        return inserted.rowcount == 1, {"status": row[1]}

    def complete(self, preparation_id, status, reason=""):
        if status not in ("prepared", "failed"):
            raise trade.GuardError("Invalid manual preparation outcome.")
        with self.db:
            self.db.execute("UPDATE manual_tickets SET status=?,reason=?,acknowledged=0 WHERE id=?", (status, reason, preparation_id))

    def pending(self):
        rows = self.db.execute("SELECT id,claim_id,status FROM manual_tickets WHERE acknowledged=0 ORDER BY rowid LIMIT 100").fetchall()
        return [{"offer_id": row[0], "claim_id": row[1], "result": {"status": row[2]}} for row in rows]

    def acknowledge(self, offer_id, claim_id):
        with self.db:
            self.db.execute("UPDATE manual_tickets SET acknowledged=1 WHERE id=? AND claim_id=?", (offer_id, claim_id))


def prepare_ticket(mt5, settings, ledger, journal, adapter, preparation, *, clock=trade.now_utc):
    created, recorded = journal.reserve(preparation)
    if not created:
        return recorded
    result, reason = {"status": "failed"}, "guard_blocked"
    try:
        draft = prepare_draft(mt5, settings, ledger, preparation, clock())

        def recheck():
            fresh = prepare_draft(mt5, settings, ledger, preparation, clock())
            # SDK history/risk reads can consume the remaining entry window.
            # Re-read the clock after them, immediately before native work.
            validate_manual_offer(preparation, settings, clock())
            if fresh != draft:
                raise trade.GuardError("The fixed native draft changed during preparation.")

        result = adapter.prepare(draft, recheck_account=recheck)
        if result != {"status": "prepared"}:
            raise native.TicketError("readback_failed")
        reason = ""
    except native.TicketError as error:
        result, reason = {"status": "failed"}, error.reason
    except Exception:
        result, reason = {"status": "failed"}, "guard_blocked"
    journal.complete(trade.uuid_text(preparation["id"]), result["status"], reason)
    if reason:
        print("Native draft preparation blocked (" + reason + "); inspect MT5 if a window is open.", file=sys.stderr, flush=True)
    else:
        print("Native Demo ticket prepared. Review SL/TP, then choose Buy or Sell yourself in MT5.", flush=True)
    return result


def service_failure_reason(error):
    """Expose only authored transport codes, never URLs, bodies or error text."""
    if isinstance(error, HTTPError):
        code = error.code
        if type(code) is int:
            return {
                401: "bridge_authorization_rejected",
                404: "manual_api_unavailable",
                409: "older_snapshot",
                422: "feed_version_or_data_rejected",
                503: "service_not_ready",
            }.get(code, "service_unavailable")
        return "service_unavailable"
    if isinstance(error, (URLError, TimeoutError)):
        return "connection_unavailable"
    return "service_unavailable"


def api_post(settings, route, payload):
    routes = {"market": "/api/market/feed", "register": "/api/mt5/register",
              "poll": "/api/mt5/manual/poll", "result": "/api/mt5/manual/result",
              "chart": "/api/mt5/manual/chart"}
    if route not in routes:
        raise trade.GuardError("Invalid manual bridge route.")
    market.validate_feed_url(settings.market.url)
    parts = urlsplit(settings.market.url)
    body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(body) > trade.MAX_JSON_BYTES:
        raise trade.GuardError("The manual bridge request is too large.")
    req = request.Request(urlunsplit((parts.scheme, parts.netloc, routes[route], "", "")), data=body, method="POST",
                          headers={"Authorization": "Bearer " + settings.market.key, "Content-Type": "application/json"})
    with request.build_opener(market.NoRedirect()).open(req, timeout=15) as response:
        if response.status not in (200, 201, 202, 204):
            raise trade.GuardError("The manual bridge request was not accepted.")
        content = response.read(trade.MAX_JSON_BYTES + 1)
        if len(content) > trade.MAX_JSON_BYTES:
            raise trade.GuardError("The manual bridge response is too large.")
        result = json.loads(content) if content else {}
        if type(result) is not dict:
            raise trade.GuardError("Invalid manual bridge response.")
        return result


def flush_results(settings, ledger, journal, post=api_post):
    for pending in journal.pending():
        try:
            post(settings, "result", {"device_id": ledger.device_id, **pending})
        except HTTPError as error:
            if error.code != 409:
                raise
            print("The server draft claim is closed; the ticket is never prepared again.", file=sys.stderr, flush=True)
        journal.acknowledge(pending["offer_id"], pending["claim_id"])


def refresh_chart(settings, device_id, exporter, feed, *, post=api_post, clock=trade.now_utc):
    """Read display levels separately from claiming a ticket preparation."""
    reply = post(settings, "chart", {"device_id": device_id})
    if type(reply) is not dict or set(reply) != {"proposal"}:
        raise chart.OverlayError("Invalid chart response")
    exporter.publish(reply["proposal"], execution=feed.get("execution"), quote=feed.get("quote"), observed_at=clock())


def run_bridge(mt5, settings, *, post=api_post, clock=trade.now_utc, sleep=time.sleep, adapter_factory=None, exporter_factory=None):
    ledger = process_lock = None
    exporter = None
    initialized = False
    try:
        process_lock = trade.ProcessLock(settings.state_directory)
        ledger = trade.Ledger(settings.state_directory)
        journal = ManualJournal(ledger)
        if not trade.terminal_is_running(settings.market.terminal):
            raise trade.GuardError("Open and connect the intended Demo terminal first.")
        if not mt5.initialize(str(settings.market.terminal), timeout=15000):
            raise trade.GuardError("The existing MT5 terminal could not be connected.")
        initialized = True
        account = check_manual_account(mt5, settings, ledger)
        terminal_info = mt5.terminal_info()
        data_path = getattr(terminal_info, "data_path", None)
        if not data_path:
            raise trade.GuardError("The terminal data directory could not be verified.")
        try:
            if exporter_factory is not None:
                exporter = exporter_factory(terminal_info, settings.market, account)
            elif adapter_factory is None or getattr(terminal_info, "commondata_path", None) is not None:
                # A synthetic adapter without a common directory has no file
                # export. The real helper always requires the verified path.
                exporter = chart.ChartExporter.from_terminal_info(terminal_info, settings.market, account)
            if exporter is not None:
                exporter.clear(clock())
        except (chart.OverlayError, OSError):
            raise trade.GuardError("The MT5 common Files directory could not be verified for chart display.") from None
        factory = adapter_factory or (
            lambda: native.NativeTicketAdapter(native.Win32Terminal(
                settings.market.terminal, account.login, Path(data_path),
                account_guard=lambda: check_manual_account(mt5, settings, ledger),
            ), clock=clock)
        )
        adapter = factory()
        backend = getattr(adapter, "backend", None)
        if backend is not None:
            backend.verify_absolute_stop_mode()
            check_manual_account(mt5, settings, ledger)
            print("Native price mode verified.", flush=True)
        elif adapter_factory is None:
            raise native.TicketError("absolute_mode_unverified")
        code, registration = trade.pairing_registration(settings, ledger)
        registered = post(settings, "register", registration)
        print("Manual Demo helper ready. Telegram preparation only fills a native ticket; the final MT5 click is yours.", flush=True)
        if registered.get("paired") is not True:
            print("In your private bot chat, send: /connect_mt5 " + code, flush=True)
        next_feed = 0.0
        feed_available = False
        outage_reported = False
        chart_outage_reported = False
        latest_feed = None
        last_mtf_state = None

        def chart_unavailable():
            nonlocal chart_outage_reported
            if exporter is None:
                return
            try:
                exporter.clear(clock())
            except Exception:
                pass  # The previous row also has a bounded display deadline.
            if not chart_outage_reported:
                print("MT5 chart display unavailable.", file=sys.stderr, flush=True)
                chart_outage_reported = True

        def unavailable(reason):
            nonlocal feed_available, next_feed, outage_reported
            feed_available = False
            next_feed = time.monotonic() + 5
            chart_unavailable()
            if not outage_reported:
                print("MT5 feed unavailable (" + reason + ").", file=sys.stderr, flush=True)
                outage_reported = True

        while trade.terminal_is_running(settings.market.terminal):
            try:
                check_manual_account(mt5, settings, ledger)
                flush_results(settings, ledger, journal, post)
                if time.monotonic() >= next_feed:
                    payload = market.build_payload(mt5, settings.market, clock())
                    payload["device_id"] = ledger.device_id
                    payload["execution"] = trade.execution_metadata(mt5.symbol_info(settings.market.symbol))
                    post(settings, "market", payload)
                    latest_feed = payload
                    cadence = chart.FEED_REFRESH_SECONDS
                    next_feed = time.monotonic() + cadence
                    if not feed_available:
                        print("Fresh MT5 feed ready.", flush=True)
                    feed_available, outage_reported = True, False
                if feed_available:
                    if exporter is not None:
                        try:
                            refresh_chart(settings, ledger.device_id, exporter, latest_feed, post=post, clock=clock)
                            if chart_outage_reported:
                                print("MT5 chart display ready.", flush=True)
                            chart_outage_reported = False
                        except Exception:
                            # A display failure must never claim a draft or
                            # interfere with the existing preparation path.
                            chart_unavailable()
                    reply = post(settings, "poll", {"device_id": ledger.device_id})
                    preparation = reply.get("preparation")
                    if preparation is not None:
                        last_mtf_state = "experimental_preparation" if preparation.get("payload", {}).get("signal_mode") == "experimental_demo" else "qualified_preparation"
                        prepare_ticket(mt5, settings, ledger, journal, adapter, preparation, clock=clock)
                        flush_results(settings, ledger, journal, post)
                    else:
                        if reply.get("signal_mode") == "experimental_demo":
                            state = "waiting_experimental_signal"
                        else:
                            state = "unverified_costs" if latest_feed.get("risk_context", {}).get("costs_verified") is not True else "waiting_qualified_signal"
                        if state != last_mtf_state:
                            print("MTF status: " + state, flush=True)
                            last_mtf_state = state
            except market.MarketDataError as error:
                if error.reason_code == "terminal_installation_mismatch":
                    raise
                reason = error.reason_code or "market_data_unavailable"
                unavailable(reason)
            except (trade.GuardError, native.TicketError):
                raise
            except Exception as error:
                unavailable(service_failure_reason(error))
            sleep(5)
        print("MT5 closed; manual helper stopped.", flush=True)
        return 0
    except KeyboardInterrupt:
        print("Manual helper stopped. An open MT5 ticket remains for your review or cancellation.", flush=True)
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, (trade.GuardError, native.TicketError)) else type(error).__name__
        print("Manual helper stopped: " + message, file=sys.stderr, flush=True)
        return 1
    finally:
        if exporter is not None:
            try:
                exporter.clear(clock())
            except Exception:
                print("MT5 chart display unavailable.", file=sys.stderr, flush=True)
        if initialized:
            mt5.shutdown()
        if ledger is not None:
            ledger.close()
        if process_lock is not None:
            process_lock.close()


def main(argv=None):
    parser = market.PrivateArgumentParser(description=__doc__)
    parser.add_argument("--terminal", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--account-mode", required=True, choices=("demo",))
    parser.add_argument("--volume", required=True)
    parser.add_argument("--state-directory", required=True)
    parser.add_argument("--enable-manual-tickets", action="store_true")
    args = parser.parse_args(argv)
    try:
        if os.name != "nt":
            raise trade.GuardError("The native manual helper requires Windows.")
        settings = load_settings(args)
        mt5 = importlib.import_module("MetaTrader5")
    except Exception as error:
        message = str(error) if isinstance(error, trade.GuardError) else type(error).__name__
        print("Manual helper unavailable: " + message, file=sys.stderr)
        return 1
    return run_bridge(mt5, settings)


if __name__ == "__main__":
    raise SystemExit(main())
