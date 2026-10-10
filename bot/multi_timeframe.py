"""Causal H4/H1 context, M15 direction and M5/M1 timing for manual Demo.

Candidates do not authorize orders or ticket preparation. Qualified signals
require operator-pinned empirical evidence. An explicitly selected experimental
Demo profile retains risk guards and labels its costs and outcomes unverified.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException, ROUND_CEILING, ROUND_FLOOR
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re
from statistics import median
from types import MappingProxyType

from bot import paper_signals

STRATEGY_ID = "mtf-ema-pullback-60m-v2"
STRATEGY_VERSION = 2
POLICY_ID = "mtf-manual-demo-cost-risk-v2"
EXPERIMENTAL_STRATEGY_ID = "mtf-ema-pullback-60m-demo-v3"
EXPERIMENTAL_STRATEGY_VERSION = 3
EXPERIMENTAL_POLICY_ID = "mtf-manual-demo-estimated-cost-risk-v3"
EXPERIMENTAL_SIGNAL_MODE = "experimental_demo"
HORIZON_SECONDS = 3600
LOOKBACK_BARS = 64
MIN_CONTIGUOUS_BARS = 22
TIMEFRAME_SECONDS = MappingProxyType({"M15": 900, "M5": 300, "M1": 60, "H1": 3600, "H4": 14400})
RISK_FIELDS = frozenset({
    "account_mode", "volume", "equity", "open_positions", "pending_orders",
    "loss_cash_per_price_unit", "profit_cash_per_price_unit", "commission_round_turn",
    "slippage_price", "costs_verified", "as_of", "free_margin", "margin_required",
    "broker_fingerprint",
})
POLICY = MappingProxyType({
    "schema_version": 3, "lookback_bars": LOOKBACK_BARS,
    "min_contiguous_bars": MIN_CONTIGUOUS_BARS,
    "gap_policy": "reset_indicators_at_contiguous_suffix_without_filling",
    "horizon_seconds": HORIZON_SECONDS,
    "ema_fast": 9, "ema_slow": 21, "atr_period": 14,
    "quote_max_age_seconds": 10, "quote_future_skew_seconds": 5,
    "snapshot_max_age_seconds": 30, "risk_max_age_seconds": 30,
    "m1_max_close_age_seconds": 75,
    "m15_min_ema_separation_atr": "0.05", "max_latest_true_range_atr": "3",
    "higher_context": "closed_H1_H4_must_agree_with_M15_ema_slope_close",
    "higher_context_min_ema_separation_atr": "0.05",
    "h4_level_bars": 20, "h4_levels": "completed_contiguous_range_extrema",
    "candle_grid": "verified_broker_offset_minutes",
    "swing_m5_bars": 5, "stop_buffer_m5_atr": "0.2",
    "target_r": "2", "target2_r": "3", "min_effective_reward_risk": "1.5",
    "min_m1_tick_volume_median_fraction": "0.5", "tick_volume_reference_bars": 20,
    "max_spread_r": "0.1", "max_spread_m1_atr": "0.15",
    "max_equity_risk_fraction": "0.01", "minimum_free_margin_multiple": "2",
    "volume": "0.01", "maximum_open_positions": 0, "maximum_pending_orders": 0,
    "session_start_utc_hour": 6, "session_end_utc_hour": 19, "weekdays_only": True,
    "max_entry_delay_seconds": 10,
    "entry_rounding": "adverse_tick", "protection_rounding": "outward_tick",
    "cost_policy": "verified_round_trip_commission_and_per_side_slippage_v1",
    "success_target": "target", "evidence_min_nonoverlapping_oos_trades": 200,
    "evidence_lower_95_wilson_bound": "0.70",
})
EXPERIMENTAL_POLICY = MappingProxyType({
    **POLICY, "max_entry_delay_seconds": 30, "m5_pullback_preceding_bars": 3,
    "cost_policy": "estimated_spread_tick_floor_v1", "empirical_qualification": False,
})


class _Invalid(ValueError):
    def __init__(self, reason):
        self.reason = reason


def _utc(value):
    if type(value) is str and len(value) <= 40:
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _Invalid("invalid_timestamp")
    return value.astimezone(timezone.utc)


def _decimal(value, *, zero=False, cash=False):
    if type(value) not in (int, float):
        raise _Invalid("invalid_number")
    result = Decimal(str(value))
    maximum = Decimal("1e12") if cash else Decimal("1e9")
    if not result.is_finite() or result < 0 or (not zero and result == 0) or result >= maximum:
        raise _Invalid("invalid_number")
    return result


@lru_cache(maxsize=2)
def _fingerprint(experimental_demo=False):
    # No dataset, API key, account identity or mutable runtime setting enters
    # the fingerprint. Runtime must separately match the evaluated cost model.
    components = {
        "strategy_id": EXPERIMENTAL_STRATEGY_ID if experimental_demo else STRATEGY_ID,
        "strategy_version": EXPERIMENTAL_STRATEGY_VERSION if experimental_demo else STRATEGY_VERSION,
        "policy_id": EXPERIMENTAL_POLICY_ID if experimental_demo else POLICY_ID,
        "policy": dict(EXPERIMENTAL_POLICY if experimental_demo else POLICY),
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
        "indicator_dependency_sha256": hashlib.sha256(Path(paper_signals.__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
    }
    encoded = json.dumps(components, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def strategy_identity(*, experimental_demo=False):
    """Return a detached exact identity for the operator-pinned evidence gate."""
    return {"strategy_id": EXPERIMENTAL_STRATEGY_ID if experimental_demo else STRATEGY_ID,
            "policy_id": EXPERIMENTAL_POLICY_ID if experimental_demo else POLICY_ID,
            "horizon_seconds": HORIZON_SECONDS, "fingerprint": _fingerprint(experimental_demo)}


def qualification_allowed(evidence, *, now=None):
    """A raw feed/report/AI confidence cannot replace the trusted loader token."""
    try:
        from bot.strategy_evidence import evidence_allows_alerts
        return evidence_allows_alerts(evidence, identity=strategy_identity(), now=now) is True
    except (ImportError, ValueError, TypeError, AttributeError):
        return False


def _result(state, reason, *, counts=None, experimental_demo=False, **values):
    return {
        "state": state, "reason": reason,
        "strategy_id": EXPERIMENTAL_STRATEGY_ID if experimental_demo else STRATEGY_ID,
        "strategy_version": EXPERIMENTAL_STRATEGY_VERSION if experimental_demo else STRATEGY_VERSION,
        "policy_id": EXPERIMENTAL_POLICY_ID if experimental_demo else POLICY_ID,
        "strategy_fingerprint": _fingerprint(experimental_demo), "horizon_seconds": HORIZON_SECONDS,
        "required_bars": MIN_CONTIGUOUS_BARS, "candle_counts": counts or {},
        **({"signal_mode": EXPERIMENTAL_SIGNAL_MODE, "entry_window_seconds": 30,
            "provisional": True} if experimental_demo else {}),
        **values,
    }


def prepare_analysis_feed(feed, *, experimental_demo=False):
    """Copy the input and derive explicit, unverified Demo cost estimates."""
    prepared = deepcopy(feed)
    if not experimental_demo:
        return prepared
    if type(prepared) is not dict or type(prepared.get("risk_context")) is not dict:
        raise _Invalid("missing_risk_context")
    risk = prepared["risk_context"]
    # Retain malformed/missing risk fields for the normal strict validator.
    if set(risk) != RISK_FIELDS:
        raise _Invalid("missing_risk_context")
    if type(risk["costs_verified"]) is not bool:
        raise _Invalid("unverified_costs")
    tick, _, _, _ = _execution(prepared["execution"])
    bid, ask = (_decimal(prepared["quote"][key]) for key in ("bid", "ask"))
    spread = ask - bid
    if spread <= 0:
        raise _Invalid("excessive_or_unknown_spread")
    loss_unit = _decimal(risk["loss_cash_per_price_unit"], cash=True)
    def reported_cost(key):
        value = risk[key]
        # The live bridge represents an explicitly unknown cost as None.
        # Only an unverified context may replace that absence with a floor.
        if value is None and risk["costs_verified"] is False:
            return Decimal(0)
        return _decimal(value, zero=True, cash=True)

    commission = reported_cost("commission_round_turn")
    slippage = reported_cost("slippage_price")
    def conservative_number(value):
        number = float(value)
        # A binary conversion must never round an explicit cost floor down.
        return math.nextafter(number, math.inf) if Decimal(str(number)) < value else number

    risk["commission_round_turn"] = conservative_number(max(commission, loss_unit * max(spread, 10 * tick)))
    risk["slippage_price"] = conservative_number(max(slippage, spread / 2, 2 * tick))
    risk["costs_verified"] = False
    return prepared


def broker_utc_offset_minutes(value):
    """Require the same explicit, bounded broker clock convention as the feed."""
    if type(value) is not int or not -720 <= value <= 840 or value % 15:
        raise _Invalid("invalid_broker_utc_offset")
    return value


def _bars(values, timeframe, now, broker_utc_offset_minutes=0):
    seconds = TIMEFRAME_SECONDS[timeframe]
    clean, previous = [], None
    for value in values:
        if type(value) is not dict or not {"time", "open", "high", "low", "close", "tick_volume"} <= set(value):
            raise _Invalid("invalid_candles_" + timeframe)
        stamp = _utc(value["time"])
        if stamp.microsecond or (int(stamp.timestamp()) + broker_utc_offset_minutes * 60) % seconds:
            raise _Invalid("unaligned_candles_" + timeframe)
        if stamp + timedelta(seconds=seconds) > now:
            raise _Invalid("forming_bar_" + timeframe)
        if previous is not None and stamp <= previous:
            raise _Invalid("non_monotonic_" + timeframe)
        prices = {key: float(_decimal(value[key])) for key in ("open", "high", "low", "close")}
        if prices["low"] > min(prices["open"], prices["close"]) or prices["high"] < max(prices["open"], prices["close"]) or prices["low"] > prices["high"]:
            raise _Invalid("invalid_ohlc_" + timeframe)
        volume = value["tick_volume"]
        if type(volume) is not int or not 0 <= volume <= 10**12:
            raise _Invalid("invalid_tick_volume_" + timeframe)
        clean.append({"time": stamp, **prices, "tick_volume": volume})
        previous = stamp
    return clean


def _contiguous_suffix(bars, timeframe):
    seconds = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    start = len(bars) - 1
    while start > 0 and bars[start]["time"] - bars[start - 1]["time"] == seconds:
        start -= 1
    return bars[max(0, start):]


def _indicators(bars):
    closes = [bar["close"] for bar in bars]
    fast, slow = paper_signals._ema(closes, 9), paper_signals._ema(closes, 21)
    atr = paper_signals._atr(bars, 14)
    last, previous = bars[-1], bars[-2]
    true_range = max(last["high"] - last["low"], abs(last["high"] - previous["close"]), abs(last["low"] - previous["close"]))
    return {"fast": fast[-1], "slow": slow[-1], "previous_fast": fast[-2],
            "previous_slow": slow[-2], "atr": atr, "latest_true_range": true_range}


def _trend(bars, indicators, *, strict=True):
    """Qualitative EMA agreement; the short timing gates remain separate."""
    separation = indicators["fast"] - indicators["slow"]
    if not strict:
        return "BUY" if separation > 0 else "SELL" if separation < 0 else "NEUTRAL"
    slope = indicators["fast"] - indicators["previous_fast"]
    if (not math.isfinite(indicators["atr"]) or indicators["atr"] <= 0
            or abs(separation) < .05 * indicators["atr"]):
        return "NEUTRAL"
    if separation > 0 and slope > 0 and bars[-1]["close"] > indicators["fast"]:
        return "BUY"
    if separation < 0 and slope < 0 and bars[-1]["close"] < indicators["fast"]:
        return "SELL"
    return "NEUTRAL"


def _timeframe_context(bars, indicators, bid, ask):
    """Describe closed history without claiming an empirical success probability."""
    trends = {key: _trend(bars[key], indicators[key], strict=key not in {"M1", "M5"})
              for key in TIMEFRAME_SECONDS}
    direction = trends["M15"]
    counter = direction != "NEUTRAL" and any(
        trends[key] not in {direction, "NEUTRAL"} for key in ("H1", "H4")
    )
    aligned = direction != "NEUTRAL" and all(value == direction for value in trends.values())
    midpoint = (Decimal(str(bid)) + Decimal(str(ask))) / 2
    closed_range = bars["H4"][-20:]
    support = min(bar["low"] for bar in closed_range)
    resistance = max(bar["high"] for bar in closed_range)
    return {"trends": trends,
            "alignment": "counter_trend" if counter else "aligned" if aligned else "unconfirmed",
            "confidence": "reduced" if counter else "aligned" if aligned else "unconfirmed",
            "counter_trend": counter,
            "support": support if Decimal(str(support)) <= midpoint else None,
            "resistance": resistance if Decimal(str(resistance)) >= midpoint else None}


def _execution(value):
    if type(value) is not dict or set(value) != {"tick_size", "point", "digits", "stops_level"}:
        raise _Invalid("invalid_execution_metadata")
    digits, stops = value["digits"], value["stops_level"]
    if type(digits) is not int or not 0 <= digits <= 8 or type(stops) is not int or not 0 <= stops <= 1_000_000:
        raise _Invalid("invalid_execution_metadata")
    tick, point = _decimal(value["tick_size"]), _decimal(value["point"])
    quantum = Decimal(1).scaleb(-digits)
    if point != quantum or tick < quantum or tick % quantum:
        raise _Invalid("invalid_execution_grid")
    return tick, point, digits, stops


def _risk(value, now, *, research_only=False):
    if type(value) is not dict or set(value) != RISK_FIELDS:
        raise _Invalid("missing_risk_context")
    if value["account_mode"] != "demo" or _decimal(value["volume"]) != Decimal("0.01"):
        raise _Invalid("unsupported_demo_configuration")
    if value["costs_verified"] is not True and not (research_only and value["costs_verified"] is False):
        raise _Invalid("unverified_costs")
    if type(value["broker_fingerprint"]) is not str or re.fullmatch(r"[0-9a-f]{64}", value["broker_fingerprint"]) is None:
        raise _Invalid("invalid_broker_fingerprint")
    for key in ("open_positions", "pending_orders"):
        if type(value[key]) is not int or value[key] < 0:
            raise _Invalid("invalid_exposure_context")
    if not timedelta(0) <= now - _utc(value["as_of"]) <= timedelta(seconds=30):
        raise _Invalid("stale_risk_context")
    clean = {key: _decimal(value[key], zero=key in {"commission_round_turn", "slippage_price"}, cash=True)
             for key in ("equity", "loss_cash_per_price_unit", "profit_cash_per_price_unit",
                         "commission_round_turn", "slippage_price", "free_margin", "margin_required")}
    clean.update(open_positions=value["open_positions"], pending_orders=value["pending_orders"])
    clean["broker_fingerprint"] = value["broker_fingerprint"]
    return clean


def _grid(price, tick, *, up):
    return (price / tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * tick


def _price_number(value):
    number = float(value)
    if not math.isfinite(number) or Decimal(str(number)) != value or not 0 < value < Decimal("1e9"):
        raise _Invalid("unrepresentable_price_grid")
    return number


def analyze_multi_timeframe(feed, now=None, *, research_only=False, experimental_demo=False):
    """Return a deterministic candidate or an explicit waiting/block reason.

    All references are closed at the M1 decision time. Entry is the current
    executable side rounded adversely to tick; SL/TP are frozen once published
    by the caller. No historical candle-close equality is implied for entry.
    Explicit research mode allows unverified declared costs but marks all
    outputs provisional. Such output cannot authorize live alerts or display.
    """
    if type(research_only) is not bool:
        return {**_result("invalid", "invalid_research_mode"), "provisional": True}
    if type(experimental_demo) is not bool or (research_only and experimental_demo):
        return {**_result("invalid", "invalid_signal_profile"), "provisional": True}
    try:
        prepared = prepare_analysis_feed(feed, experimental_demo=experimental_demo) if experimental_demo else feed
    except _Invalid as error:
        return _result("blocked", error.reason, experimental_demo=experimental_demo)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError, DecimalException):
        return _result("invalid", "invalid_market_or_risk_data", experimental_demo=experimental_demo)
    result = _analyze_multi_timeframe(prepared, now, research_only=research_only,
                                    experimental_demo=experimental_demo)
    if experimental_demo:
        risk = prepared["risk_context"]
        result["cost_assumptions"] = {
            "verified": False, "method": "spread_tick_floor_v1",
            "commission_round_turn": risk["commission_round_turn"],
            "slippage_price": risk["slippage_price"],
            "spread_price": float(Decimal(str(prepared["quote"]["ask"])) - Decimal(str(prepared["quote"]["bid"]))),
            "tick_size": prepared["execution"]["tick_size"],
        }
    return {**result, "provisional": research_only or experimental_demo}


def _analyze_multi_timeframe(feed, now=None, *, research_only=False, experimental_demo=False):
    counts = {}
    context = {}
    def result(state, reason, **values):
        return _result(state, reason, experimental_demo=experimental_demo, **context, **values)

    try:
        clock = _utc(datetime.now(timezone.utc) if now is None else now)
        if type(feed) is not dict or feed.get("source") != "MetaTrader 5" or type(feed.get("schema_version")) is not int or feed["schema_version"] != 2:
            return result("invalid", "unsupported_feed", counts=counts)
        offset = broker_utc_offset_minutes(feed.get("broker_utc_offset_minutes"))
        symbol = feed.get("symbol")
        if type(symbol) is not str or re.fullmatch(r"(?:XAUUSD|GOLD)[A-Za-z0-9._#-]{0,24}", symbol, re.I) is None:
            return result("invalid", "invalid_symbol", counts=counts)
        if not timedelta(0) <= clock - _utc(feed["as_of"]) <= timedelta(seconds=30):
            return result("stale", "stale_snapshot", counts=counts)
        quote = feed["quote"]
        if type(quote) is not dict or set(quote) != {"bid", "ask", "time"}:
            return result("invalid", "invalid_quote", counts=counts)
        bid, ask = _decimal(quote["bid"]), _decimal(quote["ask"])
        if bid > ask:
            return result("invalid", "crossed_quote", counts=counts)
        if not timedelta(seconds=-5) <= clock - _utc(quote["time"]) <= timedelta(seconds=10):
            return result("stale", "stale_quote", counts=counts)
        tick, point, digits, stops = _execution(feed["execution"])
        streams = feed.get("timeframes")
        if type(streams) is not dict or set(streams) != set(TIMEFRAME_SECONDS):
            return result("warmup", "missing_timeframes", counts=counts)
        if any(type(streams[key]) is not list for key in TIMEFRAME_SECONDS):
            return result("invalid", "invalid_timeframes", counts=counts)
        counts = {key: len(streams[key]) for key in TIMEFRAME_SECONDS}
        if any(count > LOOKBACK_BARS for count in counts.values()):
            return result("invalid", "excess_history", counts=counts)
        supplied = {key: _bars(streams[key], key, clock, offset) for key in TIMEFRAME_SECONDS}
        bars = {key: _contiguous_suffix(values, key) for key, values in supplied.items()}
        contiguous_counts = {key: len(values) for key, values in bars.items()}
        if any(count < MIN_CONTIGUOUS_BARS for count in contiguous_counts.values()):
            return result("warmup", "insufficient_contiguous_history", counts=counts,
                           contiguous_counts=contiguous_counts)
        trigger_close = bars["M1"][-1]["time"] + timedelta(seconds=60)
        if not timedelta(0) <= clock - trigger_close <= timedelta(seconds=75):
            return result("stale", "stale_m1_timing", counts=counts)
        for key in ("M15", "M5", "H1", "H4"):
            end = bars[key][-1]["time"] + timedelta(seconds=TIMEFRAME_SECONDS[key])
            if end > trigger_close:
                return result("invalid", "lookahead_" + key, counts=counts)
            if trigger_close - end >= timedelta(seconds=TIMEFRAME_SECONDS[key]):
                return result("stale", "stale_" + key, counts=counts)
        # Every supplied bar ends by the snapshot capture, never a later receipt.
        captured = _utc(feed["as_of"])
        if any(values[-1]["time"] + timedelta(seconds=TIMEFRAME_SECONDS[key]) > captured for key, values in bars.items()):
            return result("invalid", "snapshot_precedes_bar_close", counts=counts)
        indicators = {key: _indicators(values) for key, values in bars.items()}
        context.update(
            broker_utc_offset_minutes=offset,
            context_bar_times={key: bars[key][-1]["time"].isoformat() for key in ("H1", "H4")},
            timeframe_context=_timeframe_context(bars, indicators, bid, ask),
        )
        try:
            risk = _risk(feed.get("risk_context"), clock, research_only=research_only or experimental_demo)
        except _Invalid as error:
            return result("blocked", error.reason, counts=counts)
        if risk["open_positions"] or risk["pending_orders"]:
            return result("blocked", "existing_exposure", counts=counts)
        if risk["free_margin"] < Decimal("2") * risk["margin_required"]:
            return result("blocked", "insufficient_free_margin", counts=counts)
        session_start = clock.replace(hour=6, minute=0, second=0, microsecond=0)
        session_end = clock.replace(hour=19, minute=0, second=0, microsecond=0)
        if clock.weekday() >= 5 or clock < session_start or clock + timedelta(seconds=HORIZON_SECONDS + (30 if experimental_demo else 10)) > session_end:
            return result("blocked", "outside_full_horizon_session", counts=counts)
        if any(not math.isfinite(item["atr"]) or item["atr"] <= 0 for item in indicators.values()):
            return result("no_signal", "flat_atr", counts=counts)
        if any(item["latest_true_range"] > 3 * item["atr"] for item in indicators.values()):
            return result("blocked", "extreme_true_range", counts=counts)
        trend = indicators["M15"]
        separation = trend["fast"] - trend["slow"]
        slope = trend["fast"] - trend["previous_fast"]
        if abs(separation) < 0.05 * trend["atr"] or separation == 0 or slope == 0:
            return result("no_signal", "flat_m15_trend", counts=counts)
        buy = separation > 0 and slope > 0 and bars["M15"][-1]["close"] > trend["fast"]
        sell = separation < 0 and slope < 0 and bars["M15"][-1]["close"] < trend["fast"]
        if not (buy or sell):
            return result("no_signal", "conflicting_m15_trend", counts=counts)
        direction = "BUY" if buy else "SELL"
        higher_trends = context["timeframe_context"]["trends"]
        if context["timeframe_context"]["counter_trend"]:
            return result("no_signal", "higher_timeframe_conflict", counts=counts)
        if any(higher_trends[key] == "NEUTRAL" for key in ("H1", "H4")):
            return result("no_signal", "higher_timeframe_neutral", counts=counts)
        sign = 1 if buy else -1
        confirmation = indicators["M5"]
        previous5, current5 = bars["M5"][-2:]
        aligned5 = sign * (confirmation["fast"] - confirmation["slow"]) > 0
        pulled_back = (
            previous5["low"] <= confirmation["previous_fast"] and previous5["close"] >= confirmation["previous_slow"]
            if buy else previous5["high"] >= confirmation["previous_fast"] and previous5["close"] <= confirmation["previous_slow"]
        )
        if experimental_demo:
            closes5 = [bar["close"] for bar in bars["M5"]]
            fast5, slow5 = paper_signals._ema(closes5, 9), paper_signals._ema(closes5, 21)
            pulled_back = any(
                fast5[index] is not None and slow5[index] is not None and (
                    bar["low"] <= fast5[index] and bar["close"] >= slow5[index]
                    if buy else bar["high"] >= fast5[index] and bar["close"] <= slow5[index]
                )
                for index in range(max(0, len(bars["M5"]) - 4), len(bars["M5"]) - 1)
                for bar in (bars["M5"][index],)
            )
        recovered = sign * (current5["close"] - confirmation["fast"]) > 0 and sign * (current5["close"] - previous5["close"]) > 0 and sign * (current5["close"] - current5["open"]) > 0
        if not aligned5 or not pulled_back or not recovered:
            return result("no_signal", "m5_pullback_not_confirmed", counts=counts)
        timing = indicators["M1"]
        previous1, current1 = bars["M1"][-2:]
        if (
            sign * (timing["fast"] - timing["slow"]) <= 0
            or sign * (current1["close"] - current1["open"]) <= 0
            or (current1["close"] <= previous1["high"] if buy else current1["close"] >= previous1["low"])
        ):
            return result("no_signal", "m1_breakout_not_confirmed", counts=counts)
        volume_reference = median(bar["tick_volume"] for bar in bars["M1"][-21:-1])
        if volume_reference <= 0 or current1["tick_volume"] < 0.5 * volume_reference:
            return result("blocked", "low_tick_activity", counts=counts)
        entry = _grid(ask if buy else bid, tick, up=buy)
        swing = min(bar["low"] for bar in bars["M5"][-5:]) if buy else max(bar["high"] for bar in bars["M5"][-5:])
        buffer = Decimal(str(confirmation["atr"])) * Decimal("0.2")
        stop = _grid(Decimal(str(swing)) - sign * buffer, tick, up=not buy)
        risk_distance = sign * (entry - stop)
        if risk_distance <= 0 or stop <= 0:
            return result("no_signal", "invalid_swing_protection", counts=counts)
        spread = ask - bid
        if spread <= 0 or spread > min(risk_distance * Decimal("0.1"), Decimal(str(timing["atr"])) * Decimal("0.15")):
            return result("blocked", "excessive_or_unknown_spread", counts=counts)
        target = _grid(entry + sign * Decimal("2") * risk_distance, tick, up=buy)
        target2 = _grid(entry + sign * Decimal("3") * risk_distance, tick, up=buy)
        distance = point * stops
        if (
            not (stop < bid <= ask < target if buy else target < bid <= ask < stop)
            or (bid - stop < distance or target - bid < distance if buy else stop - ask < distance or ask - target < distance)
        ):
            return result("blocked", "broker_protection_distance", counts=counts)
        # Spread is represented by executable-side entry and checked above.
        # Do not subtract it again from bid/ask fills in the offline verifier.
        cost_cash = risk["commission_round_turn"] + Decimal("2") * risk["slippage_price"] * risk["loss_cash_per_price_unit"]
        stop_cash = risk_distance * risk["loss_cash_per_price_unit"] + cost_cash
        reward_cash = abs(target - entry) * risk["profit_cash_per_price_unit"] - cost_cash
        if stop_cash > risk["equity"] * Decimal("0.01"):
            return result("blocked", "equity_risk_limit", counts=counts)
        effective_rr = reward_cash / stop_cash
        if reward_cash <= 0 or effective_rr < Decimal("1.5"):
            return result("blocked", "insufficient_reward_after_costs", counts=counts)
        low = _grid(entry - risk_distance * Decimal("0.1"), tick, up=True)
        high = _grid(entry + risk_distance * Decimal("0.1"), tick, up=False)
        if low >= high or not min(stop, target) < low <= entry <= high < max(stop, target):
            return result("no_signal", "collapsed_entry_zone", counts=counts)
        prices = {"entry": _price_number(entry), "stop": _price_number(stop), "target": _price_number(target),
                  "target2": _price_number(target2), "entry_zone_low": _price_number(low),
                  "entry_zone_high": _price_number(high), "original_stop_distance": _price_number(risk_distance)}
        return result(
            "signal", "closed_five_timeframe_alignment", counts=counts,
            symbol=symbol, direction=direction, display_timeframe="M1", **prices,
            contiguous_counts=contiguous_counts,
            broker_fingerprint=risk["broker_fingerprint"],
            account_mode="demo", volume=0.01, price_digits=digits, execution=dict(feed["execution"]),
            max_drift_r=0.1, bar_time=current1["time"].isoformat(),
            direction_bar_time=bars["M15"][-1]["time"].isoformat(),
            confirmation_bar_time=current5["time"].isoformat(),
            decision_time=clock.isoformat(), quote_time=_utc(quote["time"]).isoformat(),
            deadline=(clock + timedelta(seconds=HORIZON_SECONDS)).isoformat(),
            nominal_reward_risk=float(abs(target - entry) / risk_distance),
            effective_reward_risk=round(float(effective_rr), 6),
            estimated_loss_cash=round(float(stop_cash), 6), estimated_cost_cash=round(float(cost_cash), 6),
            estimated_risk_fraction=round(float(stop_cash / risk["equity"]), 8), spread_price=float(spread),
            cost_context={key: float(risk[key]) for key in ("commission_round_turn", "slippage_price", "loss_cash_per_price_unit", "profit_cash_per_price_unit")},
            indicators={key: {name: round(number, 8) for name, number in values.items()} for key, values in indicators.items()},
            explanation=("Experimental Demo candidate with estimated costs; no empirical success claim. Human execution only."
                         if experimental_demo else "Closed H4/H1 context, M15 direction, M5 recovery and M1 breakout agree. Qualitative alignment is not a success probability. Candidate awaits empirical qualification and human review."),
            invalidation="Reject if closed higher context/direction/confirmation reverses, quote/spread/risk guards fail or the proposal expires. Keep published SL/TP fixed.",
        )
    except _Invalid as error:
        return result("invalid", error.reason, counts=counts)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError, DecimalException):
        return result("invalid", "invalid_market_or_risk_data", counts=counts)
