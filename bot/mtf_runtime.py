"""Server-owned eligibility for every manual multi-timeframe output.

Incoming market data cannot supply certification. Only the pinned local
evaluator artifact can authorize a proposal; old queued offers fail closed.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math
import os

from bot import multi_timeframe, strategy_evidence


ENTRY_WINDOW_SECONDS = 10


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
        return closed <= decision <= clock and timedelta(0) <= clock - closed <= timedelta(seconds=ENTRY_WINDOW_SECONDS)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False


def evaluate_feed(feed, now=None):
    clock = _clock(now)
    if type(feed) is dict and type(feed.get("quote")) is dict:
        bid, ask = (feed["quote"].get(key) for key in ("bid", "ask"))
        if type(bid) in (int, float) and type(ask) in (int, float) and bid == ask:
            return blocked("excessive_or_unknown_spread")
    result = multi_timeframe.analyze_multi_timeframe(feed, clock)
    if result.get("state") != "signal":
        return result
    if not entry_window_open(result, clock):
        return blocked("entry_window_expired")
    evidence = configured_evidence(clock)
    if not strategy_evidence.qualification_gate(result, evidence, now=clock):
        return blocked()
    qualified = dict(result)
    qualified["qualification_id"] = evidence.artifact_sha256
    qualified["evidence_metrics"] = list(evidence.scenario_metrics)
    return qualified


def eligible_result(result, now=None):
    """Revalidate a frozen result, including already published legacy offers."""
    try:
        clock = _clock(now)
        if type(result) is not dict or result.get("state") != "signal" or result.get("provisional") is True:
            return False
        if type(result.get("strategy_version")) is not int or result["strategy_version"] != 1 or result.get("display_timeframe") != "M1" or result.get("account_mode") != "demo" or result.get("volume") != 0.01:
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
            if stamp.microsecond or stamp.timestamp() % seconds:
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
        evidence = configured_evidence(clock)
        return (
            result.get("qualification_id") == evidence.artifact_sha256
            and strategy_evidence.qualification_gate(result, evidence, now=clock)
        )
    except (ValueError, TypeError, KeyError, OSError, ImportError, OverflowError, ArithmeticError, AttributeError):
        return False


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
        current = multi_timeframe.analyze_multi_timeframe(feed, clock)
        if current.get("state") != "signal" or current.get("direction") != payload.get("direction"):
            return False
        if any(current.get(key) != payload.get(key) for key in (
            "bar_time", "direction_bar_time", "confirmation_bar_time", "strategy_fingerprint", "cost_context", "broker_fingerprint", "execution",
        )):
            return False
        for key, seconds in (("bar_time", 60), ("confirmation_bar_time", 300), ("direction_bar_time", 900)):
            stamp = datetime.fromisoformat(payload[key].replace("Z", "+00:00")).astimezone(timezone.utc)
            bars = feed["timeframes"][{60: "M1", 300: "M5", 900: "M15"}[seconds]]
            if stamp.microsecond or stamp.timestamp() % seconds or stamp + timedelta(seconds=seconds) > clock:
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
        risk = feed["risk_context"]
        loss = (abs(price - stop) + 2 * decimal(risk["slippage_price"])) * decimal(risk["loss_cash_per_price_unit"]) + decimal(risk["commission_round_turn"])
        gain = (abs(target - price) - 2 * decimal(risk["slippage_price"])) * decimal(risk["profit_cash_per_price_unit"]) - decimal(risk["commission_round_turn"])
        return loss > 0 and loss <= decimal(risk["equity"]) * Decimal("0.01") and gain / loss >= Decimal("1.5")
    except (ValueError, TypeError, KeyError, OSError, ImportError, OverflowError, ArithmeticError):
        return False
