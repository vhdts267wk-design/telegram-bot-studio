"""Server-owned eligibility for every manual multi-timeframe output.

Incoming market data cannot supply certification or enable experimental mode.
Qualified proposals need pinned evidence; explicitly configured experimental
Demo proposals use separate identities and estimated costs. Old offers fail closed.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math
import os

from bot import multi_timeframe, strategy_evidence


ENTRY_WINDOW_SECONDS = 10
EXPERIMENTAL_ENTRY_WINDOW_SECONDS = 30


def signal_mode():
    """Experimental Demo is enabled only by the explicit operator setting."""
    return (multi_timeframe.EXPERIMENTAL_SIGNAL_MODE
            if os.getenv("MT5_SIGNAL_MODE", "qualified").strip().lower() == multi_timeframe.EXPERIMENTAL_SIGNAL_MODE
            else "qualified")


def is_experimental_result(result):
    return (type(result) is dict
            and result.get("signal_mode") == multi_timeframe.EXPERIMENTAL_SIGNAL_MODE
            and result.get("strategy_id") == multi_timeframe.EXPERIMENTAL_STRATEGY_ID
            and type(result.get("strategy_version")) is int
            and result["strategy_version"] == multi_timeframe.EXPERIMENTAL_STRATEGY_VERSION
            and result.get("policy_id") == multi_timeframe.EXPERIMENTAL_POLICY_ID)


def entry_window_seconds(result):
    if is_experimental_result(result):
        return EXPERIMENTAL_ENTRY_WINDOW_SECONDS if result.get("entry_window_seconds") == 30 else 0
    if type(result) is dict and result.get("signal_mode", "qualified") == "qualified":
        return ENTRY_WINDOW_SECONDS
    return 0


def prepare_analysis_feed(feed):
    return multi_timeframe.prepare_analysis_feed(
        feed, experimental_demo=signal_mode() == multi_timeframe.EXPERIMENTAL_SIGNAL_MODE,
    )


def _clock(now=None):
    now = datetime.now(timezone.utc) if now is None else now
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("An aware clock is required")
    return now.astimezone(timezone.utc)


def configured_evidence(now=None):
    return strategy_evidence.load_strategy_evidence(
        os.getenv("MT5_EVIDENCE_PATH", ""),
        expected_sha256=os.getenv("MT5_EVIDENCE_SHA256", ""),
        identity=multi_timeframe.strategy_identity(), now=_clock(now),
    )


def blocked(reason="evidence_unavailable", details=None):
    if signal_mode() == multi_timeframe.EXPERIMENTAL_SIGNAL_MODE:
        return multi_timeframe._result(
            "blocked", reason, experimental_demo=True,
            details=details or "اقتراح تجريبي غير مؤهل إحصائيًا؛ لم تجتز شروط البيانات أو المخاطر الحالية.",
        )
    return {
        "state": "blocked", "reason": reason,
        "details": details or "لم تثبت الاختبارات خارج العينة معيار النجاح المطلوب بعد التكاليف.",
        "strategy_id": multi_timeframe.STRATEGY_ID, "horizon_seconds": 3600,
    }


def entry_window_open(result, now=None):
    """Admit actions only within the evaluator's completed-M1 entry window."""
    try:
        clock = _clock(now)
        bar = datetime.fromisoformat(result["bar_time"].replace("Z", "+00:00"))
        decision = datetime.fromisoformat(result["decision_time"].replace("Z", "+00:00"))
        if bar.tzinfo is None or decision.tzinfo is None or bar.microsecond or bar.timestamp() % 60:
            return False
        closed = bar.astimezone(timezone.utc) + timedelta(minutes=1)
        seconds = entry_window_seconds(result)
        return seconds > 0 and closed <= decision <= clock and timedelta(0) <= clock - closed <= timedelta(seconds=seconds)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False


def evaluate_feed(feed, now=None):
    clock = _clock(now)
    if type(feed) is dict and type(feed.get("quote")) is dict:
        bid, ask = (feed["quote"].get(key) for key in ("bid", "ask"))
        if type(bid) in (int, float) and type(ask) in (int, float) and bid == ask:
            return blocked("excessive_or_unknown_spread")
    experimental = signal_mode() == multi_timeframe.EXPERIMENTAL_SIGNAL_MODE
    result = multi_timeframe.analyze_multi_timeframe(feed, clock, experimental_demo=True) if experimental else multi_timeframe.analyze_multi_timeframe(feed, clock)
    if result.get("state") != "signal":
        return result
    def blocked_with_context(reason="evidence_unavailable"):
        # Keep causal observations available to an on-demand report, while
        # withholding the actionable direction, entry and protection levels.
        return {**blocked(reason), **{key: result[key] for key in (
            "context_bar_times", "timeframe_context", "broker_utc_offset_minutes",
        )}}
    if not entry_window_open(result, clock):
        return blocked_with_context("entry_window_expired")
    if experimental:
        return result
    evidence = configured_evidence(clock)
    if not strategy_evidence.qualification_gate(result, evidence, now=clock):
        return blocked_with_context()
    qualified = dict(result)
    qualified["qualification_id"] = evidence.artifact_sha256
    qualified["evidence_metrics"] = list(evidence.scenario_metrics)
    return qualified


def timeframe_context_valid(result, *, require_aligned=True):
    """Validate qualitative five-frame context and causal higher references.

    This is a shape/provenance guard, never empirical qualification. A frozen
    payload must additionally be compared with freshly recomputed market data.
    """
    try:
        offset = multi_timeframe.broker_utc_offset_minutes(result["broker_utc_offset_minutes"])
        context, references = result["timeframe_context"], result["context_bar_times"]
        if (type(context) is not dict or set(context) != {
                "trends", "alignment", "confidence", "counter_trend", "support", "resistance"}
                or type(references) is not dict or set(references) != {"H1", "H4"}):
            return False
        trends = context["trends"]
        if (type(trends) is not dict or set(trends) != set(multi_timeframe.TIMEFRAME_SECONDS)
                or any(type(value) is not str or value not in {"BUY", "SELL", "NEUTRAL"} for value in trends.values())
                or type(context["counter_trend"]) is not bool):
            return False
        direction = trends["M15"]
        counter = direction != "NEUTRAL" and any(
            trends[key] not in {direction, "NEUTRAL"} for key in ("H1", "H4")
        )
        aligned = direction != "NEUTRAL" and all(value == direction for value in trends.values())
        if (context["counter_trend"] != counter
                or context["alignment"] != ("counter_trend" if counter else "aligned" if aligned else "unconfirmed")
                or context["confidence"] != ("reduced" if counter else "aligned" if aligned else "unconfirmed")):
            return False
        if require_aligned and (not aligned or result.get("direction") != direction):
            return False
        for key in ("support", "resistance"):
            value = context[key]
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 < value < 1e9):
                return False
        if context["support"] is not None and context["resistance"] is not None and context["support"] > context["resistance"]:
            return False
        closed = multi_timeframe._utc(result["bar_time"]) + timedelta(seconds=60)
        for key in ("H1", "H4"):
            stamp = multi_timeframe._utc(references[key])
            seconds = multi_timeframe.TIMEFRAME_SECONDS[key]
            if (type(references[key]) is not str or stamp.microsecond
                    or (stamp.timestamp() + offset * 60) % seconds
                    or not timedelta(0) <= closed - stamp - timedelta(seconds=seconds) < timedelta(seconds=seconds)):
                return False
        return True
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False


def eligible_result(result, now=None):
    """Revalidate a frozen result, including already published legacy offers."""
    try:
        clock = _clock(now)
        if type(result) is not dict or result.get("state") != "signal":
            return False
        if not timeframe_context_valid(result):
            return False
        experimental = is_experimental_result(result)
        if experimental != (signal_mode() == multi_timeframe.EXPERIMENTAL_SIGNAL_MODE):
            return False
        if experimental:
            identity = multi_timeframe.strategy_identity(experimental_demo=True)
            if (result.get("provisional") is not True or result.get("qualification_id") not in (None, "")
                    or "evidence_metrics" in result or result.get("entry_window_seconds") != 30
                    or result.get("strategy_fingerprint") != identity["fingerprint"]
                    or result.get("horizon_seconds") != 3600):
                return False
        elif result.get("provisional") is True or result.get("signal_mode", "qualified") != "qualified":
            return False
        expected_version = (multi_timeframe.EXPERIMENTAL_STRATEGY_VERSION if experimental
                            else multi_timeframe.STRATEGY_VERSION)
        if type(result.get("strategy_version")) is not int or result["strategy_version"] != expected_version or result.get("display_timeframe") != "M1" or result.get("account_mode") != "demo" or result.get("volume") != 0.01:
            return False
        if type(result.get("symbol")) is not str or not result["symbol"] or type(result.get("price_digits")) is not int or not 0 <= result["price_digits"] <= 8:
            return False
        if any(type(result.get(key)) not in (int, float) or not math.isfinite(result[key]) or result[key] <= 0 for key in ("nominal_reward_risk", "effective_reward_risk")) or result["effective_reward_risk"] < 1.5:
            return False
        stamps = {}
        for key, seconds in (("bar_time", 60), ("confirmation_bar_time", 300), ("direction_bar_time", 900)):
            stamp = datetime.fromisoformat(result[key].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                return False
            stamp = stamp.astimezone(timezone.utc)
            if stamp.microsecond or (stamp.timestamp() + result["broker_utc_offset_minutes"] * 60) % seconds:
                return False
            stamps[key] = stamp + timedelta(seconds=seconds)
        decision = datetime.fromisoformat(result["decision_time"].replace("Z", "+00:00"))
        quote = datetime.fromisoformat(result["quote_time"].replace("Z", "+00:00"))
        if decision.tzinfo is None or quote.tzinfo is None:
            return False
        if not entry_window_open(result, clock) or not timedelta(seconds=-5) <= decision - quote <= timedelta(seconds=10):
            return False
        if any(not timedelta(0) <= stamps["bar_time"] - stamps[key] < timedelta(seconds=seconds) for key, seconds in (("confirmation_bar_time", 300), ("direction_bar_time", 900))):
            return False
        prices = [result[key] for key in ("entry", "stop", "target", "target2", "entry_zone_low", "entry_zone_high", "original_stop_distance")]
        if any(type(value) not in (int, float) or not math.isfinite(value) or not 0 < value < 1e9 for value in prices):
            return False
        entry, stop, target, target2, low, high, risk = map(lambda value: Decimal(str(value)), prices)
        tick = Decimal(str(result["execution"]["tick_size"]))
        if not tick.is_finite() or tick <= 0 or any(value % tick for value in (entry, stop, target, target2, low, high)) or abs(entry - stop) != risk:
            return False
        if result["price_digits"] != result["execution"]["digits"] or Decimal(str(result["nominal_reward_risk"])) != abs(target - entry) / risk:
            return False
        if low >= high or abs(target - entry) != 2 * risk or abs(target2 - entry) != 3 * risk:
            return False
        expected_low = ((entry - risk * Decimal("0.1")) / tick).to_integral_value(rounding=ROUND_CEILING) * tick
        expected_high = ((entry + risk * Decimal("0.1")) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        if low != expected_low or high != expected_high:
            return False
        if not (stop < low <= entry <= high < target <= target2 if result.get("direction") == "BUY" else target2 <= target < low <= entry <= high < stop if result.get("direction") == "SELL" else False):
            return False
        if experimental:
            return _experimental_costs_valid(result, risk, tick)
        evidence = configured_evidence(clock)
        return (
            result.get("qualification_id") == evidence.artifact_sha256
            and strategy_evidence.qualification_gate(result, evidence, now=clock)
        )
    except (ValueError, TypeError, KeyError, OSError, ImportError, OverflowError, ArithmeticError, AttributeError):
        return False


def _experimental_costs_valid(result, distance, tick):
    """Validate estimates and risk metrics without implying certification."""
    cost = result["cost_context"]
    assumptions = result["cost_assumptions"]
    keys = {"commission_round_turn", "slippage_price", "loss_cash_per_price_unit", "profit_cash_per_price_unit"}
    if type(cost) is not dict or set(cost) != keys or type(assumptions) is not dict:
        return False
    if assumptions.get("verified") is not False or assumptions.get("method") != "spread_tick_floor_v1":
        return False
    if any(type(cost[key]) not in (int, float) or not math.isfinite(cost[key]) or cost[key] <= 0 for key in keys):
        return False
    values = {key: Decimal(str(value)) for key, value in cost.items()}
    spread = Decimal(str(result["spread_price"]))
    if not spread.is_finite() or spread <= 0:
        return False
    if (assumptions.get("commission_round_turn") != cost["commission_round_turn"]
            or assumptions.get("slippage_price") != cost["slippage_price"]
            or assumptions.get("spread_price") != result["spread_price"]
            or Decimal(str(assumptions["tick_size"])) != tick
            or values["commission_round_turn"] < values["loss_cash_per_price_unit"] * max(spread, 10 * tick)
            or values["slippage_price"] < max(spread / 2, 2 * tick)):
        return False
    cost_cash = values["commission_round_turn"] + 2 * values["slippage_price"] * values["loss_cash_per_price_unit"]
    loss = distance * values["loss_cash_per_price_unit"] + cost_cash
    reward = 2 * distance * values["profit_cash_per_price_unit"] - cost_cash
    if reward <= 0 or reward / loss < Decimal("1.5"):
        return False
    if result.get("estimated_loss_cash") != round(float(loss), 6) or result.get("estimated_cost_cash") != round(float(cost_cash), 6):
        return False
    if result["effective_reward_risk"] != round(float(reward / loss), 6):
        return False
    fraction = result.get("estimated_risk_fraction")
    return type(fraction) in (int, float) and math.isfinite(fraction) and 0 < fraction <= .01


def eligible_payload(payload, feed, now=None):
    """Check current risk, prices and closed-bar provenance without moving SL/TP."""
    try:
        clock = _clock(now)
        if not eligible_result(payload, clock) or type(feed) is not dict:
            return False
        if payload.get("workflow") != "manual_ticket" or payload.get("account_mode") != "demo" or payload.get("volume") != 0.01:
            return False
        # Recompute current technical/risk eligibility; the old result's
        # protection remains frozen even if a rolling window changes.
        experimental = is_experimental_result(payload)
        prepared = prepare_analysis_feed(feed) if experimental else feed
        if experimental:
            risk = prepared["risk_context"]
            for key in ("commission_round_turn", "slippage_price"):
                risk[key] = max(risk[key], payload["cost_context"][key])
        current = (multi_timeframe.analyze_multi_timeframe(prepared, clock, experimental_demo=True)
                   if experimental else multi_timeframe.analyze_multi_timeframe(prepared, clock))
        if current.get("state") != "signal" or current.get("direction") != payload.get("direction"):
            return False
        if any(current.get(key) != payload.get(key) for key in (
            "bar_time", "direction_bar_time", "confirmation_bar_time", "strategy_fingerprint", "broker_fingerprint", "execution",
            "context_bar_times", "timeframe_context", "broker_utc_offset_minutes",
        )):
            return False
        if experimental:
            if any(current["cost_context"][key] != payload["cost_context"][key]
                   for key in ("loss_cash_per_price_unit", "profit_cash_per_price_unit")):
                return False
        elif current.get("cost_context") != payload.get("cost_context"):
            return False
        references = {"M1": payload["bar_time"], "M5": payload["confirmation_bar_time"],
                      "M15": payload["direction_bar_time"], **payload["context_bar_times"]}
        for frame, reference in references.items():
            seconds = multi_timeframe.TIMEFRAME_SECONDS[frame]
            stamp = datetime.fromisoformat(reference.replace("Z", "+00:00")).astimezone(timezone.utc)
            bars = feed["timeframes"][frame]
            if (stamp.microsecond or (stamp.timestamp() + payload["broker_utc_offset_minutes"] * 60) % seconds
                    or stamp + timedelta(seconds=seconds) > clock):
                return False
            if not any(datetime.fromisoformat(bar["time"].replace("Z", "+00:00")).astimezone(timezone.utc) == stamp for bar in bars):
                return False
        decimal = lambda value: Decimal(str(value))
        entry, stop, target = (decimal(payload[key]) for key in ("entry", "stop", "target"))
        bid, ask = (decimal(feed["quote"][key]) for key in ("bid", "ask"))
        if not all(value.is_finite() and value > 0 for value in (entry, stop, target, bid, ask)):
            return False
        buy = payload.get("direction") == "BUY"
        price = ask if buy else bid
        ordered = stop < price < target if buy else target < price < stop
        distance = abs(entry - stop)
        if not ordered or ask <= bid or decimal(payload["original_stop_distance"]) != distance:
            return False
        if ask - bid + abs(price - entry) > distance * Decimal("0.1"):
            return False
        risk = prepared["risk_context"]
        loss = (abs(price - stop) + 2 * decimal(risk["slippage_price"])) * decimal(risk["loss_cash_per_price_unit"]) + decimal(risk["commission_round_turn"])
        gain = (abs(target - price) - 2 * decimal(risk["slippage_price"])) * decimal(risk["profit_cash_per_price_unit"]) - decimal(risk["commission_round_turn"])
        return loss > 0 and loss <= decimal(risk["equity"]) * Decimal("0.01") and gain / loss >= Decimal("1.5")
    except (ValueError, TypeError, KeyError, OSError, ImportError, OverflowError, ArithmeticError):
        return False
