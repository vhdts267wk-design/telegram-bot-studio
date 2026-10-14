"""User-started, owner-approved DEMO execution with frozen attached SL/TP.

Only explicit demo_execution claims are consumed. No terminal login, setting
changes, native UI automation, order retries or background installation.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_FLOOR
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import time
from urllib import request
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit

try:
    from bridge import mt5_market_bridge as market
    from bridge import mt5_trade_bridge as trade
    from bridge import mt5_manual_bridge as manual
    from bridge import mt5_chart_overlay as chart
    from bridge.mt5_history_contract import closed_history_sha256
except ModuleNotFoundError:
    import mt5_market_bridge as market
    import mt5_trade_bridge as trade
    import mt5_manual_bridge as manual
    import mt5_chart_overlay as chart
    from mt5_history_contract import closed_history_sha256

# Direct script starts put bridge/ on sys.path. The pure strategy modules are
# bundled alongside it; neither imports the MT5 SDK or accesses a terminal.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot import multi_timeframe


WORKFLOW = "demo_execution"
FEED_SECONDS = 5
POLL_SECONDS = 1


@dataclass(frozen=True)
class Settings:
    market: market.Settings
    state_directory: Path
    account_mode: str
    volume: Decimal
    enable_demo_orders: bool


def execution_settings(settings):
    if settings.enable_demo_orders is not True or settings.account_mode != "demo" or settings.volume != trade.DEMO_VOLUME:
        raise trade.GuardError("Only explicitly enabled Demo orders at exactly 0.01 lots are supported.")
    return trade.Settings(settings.market, settings.state_directory, "demo", trade.DEMO_VOLUME, True)


def load_settings(args, environ=None):
    if args.enable_demo_orders is not True or args.account_mode != "demo" or trade.positive_decimal(args.volume) != trade.DEMO_VOLUME:
        raise trade.GuardError("Explicit Demo order opt-in and exactly 0.01 lots are required.")
    configured = market.load_settings(args, environ)
    if len(configured.key) < 32:
        raise trade.GuardError("The private Demo bridge key must contain at least 32 characters.")
    return Settings(configured, Path(args.state_directory).expanduser().resolve(), "demo", trade.DEMO_VOLUME, True)


class DemoLedger(trade.Ledger):
    """Reuse identity and reservations; never deliver an old workflow's outbox."""

    def __init__(self, directory):
        super().__init__(directory)
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS demo_execution_attempts (id TEXT PRIMARY KEY)")

    def reserve(self, offer):
        created, recorded = super().reserve(offer)
        # The reservation is already durable. A crash before this marker cannot
        # cause execution: its surviving initial outcome is UNKNOWN, never new.
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO demo_execution_attempts VALUES (?)", (trade.uuid_text(offer["id"]),))
        return created, recorded

    def pending(self):
        rows = self.db.execute("""SELECT offers.id,claim_id,result FROM offers
            JOIN demo_execution_attempts AS demo ON demo.id = offers.id
            WHERE acknowledged=0 ORDER BY offers.rowid LIMIT 100""").fetchall()
        return [{"offer_id": row[0], "claim_id": row[1], "result": json.loads(row[2])} for row in rows]


def adapted_offer(offer, settings, now=None):
    """Validate the new workflow before adapting to the unchanged risk checker."""
    execution_settings(settings)
    if (type(offer) is not dict or set(offer) != {"id", "claim_id", "workflow", "payload", "expires_at"}
        or offer["workflow"] != WORKFLOW or type(offer["payload"]) is not dict
        or offer["payload"].get("workflow") != WORKFLOW or offer["payload"].get("state") != "signal"):
        raise trade.GuardError("Only a complete owner-approved Demo execution claim is supported.")
    trade.uuid_text(offer["id"])
    trade.uuid_text(offer["claim_id"])
    adapted = deepcopy(offer)
    adapted["workflow"] = adapted["payload"]["workflow"] = "manual_ticket"
    if now is not None:
        validation_settings = manual.Settings(settings.market, settings.state_directory, "demo", trade.DEMO_VOLUME, True)
        manual.validate_manual_offer(adapted, validation_settings, now)
        window = 30 if adapted["payload"].get("signal_mode") == "experimental_demo" else 10
        deadline = trade.utc_date(adapted["payload"]["bar_time"]) + timedelta(seconds=60 + window)
        if trade.utc_date(adapted["expires_at"]) > deadline:
            raise trade.GuardError("The frozen M1 execution deadline cannot be extended.")
    return adapted


def account_guard(mt5, settings, ledger, *, require_empty=True):
    account = trade.check_bound_account(mt5, execution_settings(settings), ledger)
    positions, orders = mt5.positions_get(), mt5.orders_get()
    if positions is None or orders is None or (require_empty and (len(positions) or len(orders))):
        raise trade.GuardError("Demo execution requires no account positions or pending orders.")
    return account


def quote_guard(mt5, settings, payload, metadata, now, *, atr_spread_cap=None):
    tick = mt5.symbol_info_tick(settings.market.symbol)
    if tick is None:
        raise trade.GuardError("A fresh executable quote is required.")
    stamp = market.broker_timestamp_utc(getattr(tick, "time", None), settings.market)
    bid, ask = (trade.positive_decimal(getattr(tick, key, None)) for key in ("bid", "ask"))
    if ask <= bid or not -trade.EXECUTABLE_QUOTE_FUTURE_TOLERANCE_SECONDS <= now.timestamp() - stamp <= 10:
        raise trade.GuardError("The executable quote is stale, crossed or future-dated.")
    entry, stop, target = (trade.positive_decimal(payload[key]) for key in ("entry", "stop", "target"))
    price = ask if payload["direction"] == "BUY" else bid
    used = ask - bid + abs(price - entry)
    allowance = abs(entry - stop) * trade.MAX_DRIFT_R
    distance = trade.positive_decimal(metadata["point"]) * metadata["stops_level"]
    if used > allowance:
        raise trade.GuardError("Spread plus entry drift exceeds the original 0.1R guard.")
    if atr_spread_cap is not None and ask - bid > atr_spread_cap:
        raise trade.GuardError("The current spread exceeds 0.15 of the M1 ATR.")
    if payload["direction"] == "BUY" and (not stop < price < target or bid - stop < distance or target - bid < distance):
        raise trade.GuardError("Frozen BUY protection is no longer valid.")
    if payload["direction"] == "SELL" and (not target < price < stop or stop - ask < distance or ask - target < distance):
        raise trade.GuardError("Frozen SELL protection is no longer valid.")
    return bid, ask, price, allowance - used


@dataclass(frozen=True)
class PreparedOrder:
    request: dict
    quote: tuple
    symbol_signature: tuple
    loss: Decimal
    margin_required: Decimal
    atr_spread_cap: Decimal


def symbol_signature(symbol):
    return tuple(getattr(symbol, key, None) for key in (
        "currency_base", "currency_profit", "trade_mode", "order_mode", "volume_min",
        "volume_max", "volume_step", "filling_mode", "trade_exemode"))


def prepare_order(mt5, settings, ledger, offer, clock=trade.now_utc):
    adapted = adapted_offer(offer, settings, clock())
    account_guard(mt5, settings, ledger)
    validation_settings = manual.Settings(settings.market, settings.state_directory, "demo", trade.DEMO_VOLUME, True)
    # The modern validator reads all four closed histories and checks
    # provenance, fixed levels, costs, cash risk, reward/risk and free margin.
    manual.prepare_draft(mt5, validation_settings, ledger, adapted, clock())
    account_guard(mt5, settings, ledger)  # Recheck after slow history/risk reads.
    payload = offer["payload"]
    current = market.build_payload(mt5, settings.market, clock())
    current["execution"] = trade.execution_metadata(mt5.symbol_info(settings.market.symbol))
    if payload.get("closed_history_sha256") != closed_history_sha256(current):
        raise trade.GuardError("The accepted closed OHLCV histories changed.")
    analysis = multi_timeframe.analyze_multi_timeframe(current, clock(), experimental_demo=payload.get("signal_mode") == "experimental_demo")
    if analysis.get("state") != "signal":
        raise trade.GuardError("The current local technical or risk conditions no longer authorize a signal.")
    for key in ("strategy_id", "strategy_version", "policy_id", "strategy_fingerprint", "direction", "timeframe_context", "context_bar_times"):
        if analysis.get(key) != payload.get(key):
            raise trade.GuardError("The current local strategy or provenance changed.")
    for key in ("bar_time", "direction_bar_time", "confirmation_bar_time"):
        if trade.utc_date(analysis[key]) != trade.utc_date(payload[key]):
            raise trade.GuardError("The current local closed signal references changed.")
    m1_bars = multi_timeframe._bars(current["timeframes"]["M1"], "M1", clock(), settings.market.broker_utc_offset_minutes)
    m1_suffix = multi_timeframe._contiguous_suffix(m1_bars, "M1")
    atr_spread_cap = Decimal(str(multi_timeframe._indicators(m1_suffix)["atr"])) * Decimal("0.15")
    account_guard(mt5, settings, ledger)
    symbol = mt5.symbol_info(settings.market.symbol)
    metadata = trade.execution_metadata(symbol)
    if metadata != payload["execution"]:
        raise trade.GuardError("The frozen broker execution grid changed.")
    bid, ask, price, remaining = quote_guard(mt5, settings, payload, metadata, clock(), atr_spread_cap=atr_spread_cap)
    # Reprice cash risk at this executable quote after the history read. These
    # are read-only broker arithmetic calls, never order_check/order_send.
    risk = market.build_risk_context(mt5, settings.market, float(bid), float(ask), clock())
    if risk["open_positions"] or risk["pending_orders"] or risk["broker_fingerprint"] != payload["broker_fingerprint"]:
        raise trade.GuardError("The unexposed broker risk model changed.")
    if payload.get("signal_mode") == "experimental_demo":
        costs = manual.experimental_cost_context(payload, risk, metadata, ask - bid)
    else:
        costs = {key: risk[key] for key in ("commission_round_turn", "slippage_price", "loss_cash_per_price_unit", "profit_cash_per_price_unit")}
        if risk["costs_verified"] is not True or costs != payload["cost_context"]:
            raise trade.GuardError("The verified cost model changed.")
    stop, target = (trade.positive_decimal(payload[key]) for key in ("stop", "target"))
    slippage, commission = (Decimal(str(costs[key])) for key in ("slippage_price", "commission_round_turn"))
    loss = (abs(price - stop) + 2 * slippage) * Decimal(str(risk["loss_cash_per_price_unit"])) + commission
    gain = (abs(target - price) - 2 * slippage) * Decimal(str(risk["profit_cash_per_price_unit"])) - commission
    if loss <= 0 or loss > Decimal(str(risk["equity"])) * Decimal("0.01") or gain / loss < Decimal("1.5") or risk["free_margin"] < 2 * risk["margin_required"]:
        raise trade.GuardError("Current cash risk, reward or free margin is insufficient.")
    filling, execution = getattr(symbol, "filling_mode", None), getattr(symbol, "trade_exemode", None)
    if type(filling) is not int or type(execution) is not int or execution not in (0, 1, 2, 3):
        raise trade.GuardError("The broker execution policy is unavailable.")
    if execution in (0, 1) or filling & 1:
        policy = mt5.ORDER_FILLING_FOK
    elif filling & 2:
        policy = mt5.ORDER_FILLING_IOC
    else:
        raise trade.GuardError("No supported cancellable filling policy is available.")
    digest = hashlib.sha256(offer["id"].encode("ascii")).hexdigest()
    order = {"action": mt5.TRADE_ACTION_DEAL, "symbol": settings.market.symbol,
             "volume": float(trade.DEMO_VOLUME), "type": mt5.ORDER_TYPE_BUY if payload["direction"] == "BUY" else mt5.ORDER_TYPE_SELL,
             "sl": float(stop), "tp": float(target),
             "deviation": int((remaining / trade.positive_decimal(metadata["point"])).to_integral_value(rounding=ROUND_FLOOR)),
             "magic": int(digest[:8], 16), "comment": "tgd:" + digest[:20],
             "type_time": mt5.ORDER_TIME_GTC, "type_filling": policy}
    if execution != 2:
        order["price"] = float(price)
    return PreparedOrder(order, (bid, ask), symbol_signature(symbol), loss, Decimal(str(risk["margin_required"])), atr_spread_cap)


def final_guard(mt5, settings, ledger, offer, prepared, clock):
    account_guard(mt5, settings, ledger)
    adapted_offer(offer, settings, clock())
    symbol = mt5.symbol_info(settings.market.symbol)
    metadata = trade.execution_metadata(symbol)
    if metadata != offer["payload"]["execution"] or symbol_signature(symbol) != prepared.symbol_signature:
        raise trade.GuardError("Broker protection requirements changed before submission.")
    bid, ask, _, _ = quote_guard(mt5, settings, offer["payload"], metadata, clock(), atr_spread_cap=prepared.atr_spread_cap)
    # Do not refresh price after validating cash risk. Changed prices require a
    # new offer; this attempt fails without a send or a retry.
    if (bid, ask) != prepared.quote:
        raise trade.GuardError("The quote changed after cash-risk validation.")
    account = account_guard(mt5, settings, ledger)
    if prepared.loss > trade.positive_decimal(getattr(account, "equity", None)) * Decimal("0.01") or trade.positive_decimal(getattr(account, "margin_free", None)) < 2 * prepared.margin_required:
        raise trade.GuardError("The account cash-risk or margin capacity changed before submission.")
    adapted_offer(offer, settings, clock())


def execute_offer(mt5, settings, ledger, offer, clock=trade.now_utc):
    adapted_offer(offer, settings)  # Reject foreign workflows before reserving.
    created, recorded = ledger.reserve(offer)
    if not created:
        return recorded
    sent = False
    result = {"status": "failed", "code": 0}
    try:
        prepared = prepare_order(mt5, settings, ledger, offer, clock)
        checked = mt5.order_check(prepared.request)
        if checked is None or type(getattr(checked, "retcode", None)) is not int or checked.retcode != 0:
            raise trade.GuardError("MT5 rejected the Demo preflight.", getattr(checked, "retcode", 0))
        final = prepare_order(mt5, settings, ledger, offer, clock)
        if {k: v for k, v in prepared.request.items() if k not in ("price", "deviation")} != {k: v for k, v in final.request.items() if k not in ("price", "deviation")}:
            raise trade.GuardError("A fixed checked order field changed.")
        final_guard(mt5, settings, ledger, offer, final, clock)
        sent = True
        response = mt5.order_send(final.request)  # One durable attempt; NEVER retry.
        code, ticket = trade.safe_integer(getattr(response, "retcode", None)), trade.safe_integer(getattr(response, "order", None))
        result = {"status": "unknown", "code": code}
        if code == getattr(mt5, "TRADE_RETCODE_DONE", 10009):
            if ticket and trade.positive_decimal(getattr(response, "volume", None)) == trade.DEMO_VOLUME:
                result = {"status": "filled", "code": code, "executed_at": trade.iso_date(clock())}
        elif code in trade.REJECTION_CODES:
            result = {"status": "failed", "code": code}
        if ticket:
            result["order_ticket"] = ticket
    except Exception as error:
        code = result.get("code", 0) if sent else (error.code if isinstance(error, trade.GuardError) else 0)
        result = {"status": "unknown" if sent else "failed", "code": trade.safe_integer(code)}
    ledger.complete(trade.uuid_text(offer["id"]), result)
    print("Demo execution result: " + result["status"] + ".", flush=True)
    return result


def api_post(settings, route, payload):
    routes = {"market": "/api/market/feed", "register": "/api/mt5/register",
              "poll": "/api/mt5/demo/poll", "result": "/api/mt5/demo/result", "chart": "/api/mt5/demo/chart"}
    if route not in routes:
        raise trade.GuardError("Invalid Demo bridge route.")
    market.validate_feed_url(settings.market.url)
    parts = urlsplit(settings.market.url)
    body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(body) > trade.MAX_JSON_BYTES:
        raise trade.GuardError("Demo request exceeds its size limit.")
    req = request.Request(urlunsplit((parts.scheme, parts.netloc, routes[route], "", "")), data=body, method="POST",
                          headers={"Authorization": "Bearer " + settings.market.key, "Content-Type": "application/json"})
    with request.build_opener(market.NoRedirect()).open(req, timeout=15) as response:
        if response.status not in (200, 201, 202, 204):
            raise trade.GuardError("The Demo bridge request was not accepted.")
        content = response.read(trade.MAX_JSON_BYTES + 1)
        if len(content) > trade.MAX_JSON_BYTES:
            raise trade.GuardError("Demo response exceeds its size limit.")
        result = json.loads(content) if content else {}
        if type(result) is not dict:
            raise trade.GuardError("Invalid Demo bridge response.")
        return result


def flush_results(settings, ledger, post=api_post):
    trade.flush_results(settings, ledger, post)


def refresh_chart(settings, device_id, exporter, feed, *, post=api_post, clock=trade.now_utc, fresh_feed=None):
    manual.refresh_chart(settings, device_id, exporter, feed, post=post, clock=clock, fresh_feed=fresh_feed)


def capture_feed(mt5, settings, ledger, clock):
    """Read one current local snapshot, bound to the verified Demo account."""
    account_guard(mt5, settings, ledger, require_empty=False)
    payload = market.build_payload(mt5, settings.market, clock())
    payload.update(device_id=ledger.device_id, execution=trade.execution_metadata(mt5.symbol_info(settings.market.symbol)))
    account_guard(mt5, settings, ledger, require_empty=False)
    return payload


def market_failure_reason(error):
    """Return authored diagnostic codes only, never SDK exception details."""
    if isinstance(error, market.MarketDataError) and error.reason_code in market.TERMINAL_FAILURE_MESSAGES:
        return error.reason_code
    return {
        "A fresh broker quote is required.": "missing_quote",
        "A fresh, valid broker quote is required.": "stale_quote",
        "Load completed bars in every required timeframe.": "incomplete_history",
        "Incomplete broker candle.": "incomplete_history",
        "Invalid or unfinished broker candle.": "invalid_history",
        "Required closed histories must be current.": "history_not_current",
        "Current Demo risk data is required.": "risk_data_unavailable",
        "Broker risk identity unavailable.": "risk_data_unavailable",
        "Complete account exposure data is required.": "risk_data_unavailable",
        "Broker cash-risk conversion unavailable.": "risk_data_unavailable",
        "Account risk snapshot changed during collection.": "risk_data_unavailable",
        "Complete broker risk data is required.": "risk_data_unavailable",
    }.get(str(error), "invalid_local_data")


def run_bridge(mt5, settings, *, post=api_post, clock=trade.now_utc, sleep=time.sleep, exporter_factory=None):
    ledger = process_lock = exporter = None
    initialized = False
    try:
        execution_settings(settings)
        process_lock = trade.ProcessLock(settings.state_directory)
        ledger = DemoLedger(settings.state_directory)
        if not trade.terminal_is_running(settings.market.terminal):
            raise trade.GuardError("Open and connect the intended Demo terminal first.")
        if not mt5.initialize(str(settings.market.terminal), timeout=15000):
            raise trade.GuardError("The existing Demo terminal could not be connected.")
        initialized = True
        account = account_guard(mt5, settings, ledger, require_empty=False)
        info = mt5.terminal_info()
        factory = exporter_factory or chart.ChartExporter.from_terminal_info
        exporter = factory(info, settings.market, account)
        exporter.clear(clock())
        code, registration = trade.pairing_registration(settings, ledger)
        registered = post(settings, "register", registration)
        print("Demo approval helper ready. Only an explicit Telegram Accept can submit one Demo 0.01 order with fixed SL/TP.", flush=True)
        if registered.get("paired") is not True:
            print("In your private bot chat, send: /connect_mt5 " + code, flush=True)
        next_feed, latest_feed = 0.0, None
        network_retry_after = 0.0
        outage_reported = False
        chart_outage_reported = False
        while trade.terminal_is_running(settings.market.terminal):
            try:
                account_guard(mt5, settings, ledger, require_empty=False)
                flush_results(settings, ledger, post)
                cycle_started = time.monotonic()
                payload = capture_feed(mt5, settings, ledger, clock)
                if latest_feed is not None:
                    # Keep local counts current before blocking HTTP, without
                    # extending an existing proposal or waiting-row deadline.
                    exporter.refresh_readiness(payload, clock())
                if cycle_started >= max(next_feed, network_retry_after):
                    post(settings, "market", payload)
                    newly_available = latest_feed is None or outage_reported
                    latest_feed, next_feed = payload, cycle_started + FEED_SECONDS
                    network_retry_after = 0.0
                    if newly_available:
                        print("Fresh MT5 feed ready.", flush=True)
                    outage_reported = False
                if latest_feed is not None:
                    try:
                        refresh_chart(settings, ledger.device_id, exporter, latest_feed, post=post, clock=clock,
                                      fresh_feed=lambda: capture_feed(mt5, settings, ledger, clock))
                        if chart_outage_reported:
                            print("MT5 chart display ready.", flush=True)
                        chart_outage_reported = False
                    except (trade.GuardError, market.MarketDataError):
                        raise  # A failed fresh account/feed check must not reach the claim poll.
                    except Exception:
                        exporter.clear(clock())
                        if not chart_outage_reported:
                            print("MT5 chart display unavailable.", file=sys.stderr, flush=True)
                        chart_outage_reported = True
                    reply = post(settings, "poll", {"device_id": ledger.device_id})
                    if type(reply) is not dict or set(reply) != {"trade"}:
                        raise trade.GuardError("Invalid Demo execution queue response.")
                    if reply["trade"] is not None:
                        execute_offer(mt5, settings, ledger, reply["trade"], clock)
                        flush_results(settings, ledger, post)
            except market.MarketDataError as error:
                if error.reason_code == "terminal_installation_mismatch":
                    raise
                # The local SDK is already checked each loop. A fresh quote or
                # completed bar may return on the next tick: do not add a five
                # second upload wait after that local guard has recovered.
                # Transport failures retain their separate backoff below.
                latest_feed, next_feed = None, 0.0
                exporter.clear(clock())
                if not outage_reported:
                    print("MT5 feed unavailable; Demo execution waits for fresh data. Reason: " +
                          market_failure_reason(error) + "; UTC: " + trade.iso_date(clock()), file=sys.stderr, flush=True)
                outage_reported = True
            except trade.GuardError:
                raise
            except Exception:
                latest_feed, next_feed = None, time.monotonic() + FEED_SECONDS
                network_retry_after = next_feed
                try:
                    exporter.clear(clock())
                except Exception:
                    pass
                if not outage_reported:
                    print("Demo connection unavailable; no new execution claim is processed.", file=sys.stderr, flush=True)
                outage_reported = True
            sleep(POLL_SECONDS)
        return 0
    except KeyboardInterrupt:
        print("Demo helper stopped. Unknown outcomes must be checked in MT5; orders are never retried.", flush=True)
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, trade.GuardError) else type(error).__name__
        print("Demo helper stopped: " + message, file=sys.stderr, flush=True)
        return 1
    finally:
        if exporter is not None:
            try:
                exporter.clear(clock())
            except Exception:
                pass
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
    parser.add_argument("--enable-demo-orders", action="store_true")
    args = parser.parse_args(argv)
    try:
        if os.name != "nt":
            raise trade.GuardError("The Demo MT5 helper requires Windows.")
        settings = load_settings(args)
        mt5 = importlib.import_module("MetaTrader5")
    except Exception as error:
        message = str(error) if isinstance(error, trade.GuardError) else type(error).__name__
        print("Demo helper unavailable: " + message, file=sys.stderr)
        return 1
    return run_bridge(mt5, settings)


if __name__ == "__main__":
    raise SystemExit(main())
