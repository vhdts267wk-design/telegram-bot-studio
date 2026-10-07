"""Read-only chart prices from an immutable, published manual proposal.

The entry band is a static reference before spread, not a new execution rule.
Native preparation keeps its existing quote, spread and drift checks.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException, ROUND_CEILING, ROUND_FLOOR
import math
from uuid import UUID

from bot.manual_ticket_store import MAX_OFFER_TTL, PREPARATION_TIMEOUT
from bot import mtf_runtime

VISIBLE_STATUSES = frozenset({"offered", "requested", "preparing", "prepared"})
REFERENCE_RADIUS_R = Decimal("0.1")


def _utc(value):
    if type(value) is str and len(value) <= 40:
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An aware timestamp is required.")
    return value.astimezone(timezone.utc)


def _positive(value):
    if type(value) not in (int, float):
        raise ValueError("A bounded finite price is required.")
    number = Decimal(str(value))
    if not number.is_finite() or not 0 < number < Decimal("1e9"):
        raise ValueError("A bounded finite price is required.")
    return number


def _number(value):
    number = float(value)
    if not math.isfinite(number) or Decimal(str(number)) != value:
        raise ValueError("Chart prices must survive JSON numeric transport exactly.")
    return number


def _execution(value):
    if type(value) is not dict or set(value) != {"tick_size", "point", "digits", "stops_level"}:
        raise ValueError("Exact broker execution metadata is required.")
    digits, stops = value["digits"], value["stops_level"]
    if type(digits) is not int or not 0 <= digits <= 10 or type(stops) is not int or not 0 <= stops <= 1_000_000:
        raise ValueError("Invalid broker precision or minimum stops.")
    tick, point = _positive(value["tick_size"]), _positive(value["point"])
    quantum = Decimal(1).scaleb(-digits)
    if tick < quantum or tick % quantum or point != quantum:
        raise ValueError("Broker tick and point must match displayed precision.")
    return tick, point, digits, stops


def reference_zone(payload):
    """Validate frozen levels and derive inward-rounded entry ± 0.1R prices."""
    if type(payload) is not dict or payload.get("direction") not in {"BUY", "SELL"}:
        raise ValueError("A frozen BUY or SELL proposal is required.")
    tick, _, digits, _ = _execution(payload["execution"])
    if type(payload.get("price_digits")) is not int or payload["price_digits"] != digits:
        raise ValueError("The frozen precision must match execution metadata.")
    entry, stop, target = (_positive(payload[key]) for key in ("entry", "stop", "target"))
    if any(price % tick for price in (entry, stop, target)):
        raise ValueError("All chart prices must lie on the frozen broker tick grid.")
    ordered = stop < entry < target if payload["direction"] == "BUY" else target < entry < stop
    risk = abs(entry - stop)
    if not ordered or _positive(payload["original_stop_distance"]) != risk:
        raise ValueError("Frozen protective prices and original risk must agree.")
    if _positive(payload["max_drift_r"]) != REFERENCE_RADIUS_R:
        raise ValueError("The proposal must retain its original 0.1R drift limit.")
    radius = risk * REFERENCE_RADIUS_R
    low = ((entry - radius) / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    high = ((entry + radius) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    if low >= high or not min(stop, target) < low <= entry <= high < max(stop, target):
        raise ValueError("The reference band must remain strictly inside SL and TP.")
    return {"entry_zone_low": _number(low), "entry_zone_high": _number(high)}


def build_chart_overlay(offer, device, feed, now):
    """Return only public chart fields, or None when the snapshot is unsafe.

    Subscription and risk checks belong to the read-only API/store. Owner,
    status and timestamps are checked again here after those awaited reads.
    """
    try:
        now = _utc(now)
        if type(offer) is not dict or type(device) is not dict or type(feed) is not dict:
            return None
        owner = device.get("owner_user_id")
        if (
            type(owner) is not int or owner <= 0 or device.get("owner_chat_id") != owner
            or offer.get("chat_id") != owner or offer.get("user_id") != owner
            or device.get("active", True) is not True
        ):
            return None
        device_id = str(UUID(str(device["device_id"])))
        if str(UUID(str(offer["device_id"]))) != device_id or feed.get("device_id") != device_id:
            return None
        status = offer.get("status")
        if type(status) is not str or status not in VISIBLE_STATUSES:
            return None
        created, published, expiry = (_utc(offer[key]) for key in ("created_at", "published_at", "expires_at"))
        if (
            type(offer.get("message_id")) is not int or offer["message_id"] <= 0
            or not created <= published <= now < expiry <= created + MAX_OFFER_TTL
            or not created <= _utc(offer["updated_at"]) <= now
            or not timedelta(0) <= now - _utc(device["last_seen_at"]) <= timedelta(seconds=180)
        ):
            return None
        if status in {"requested", "preparing", "prepared"} and not published <= _utc(offer["decided_at"]) <= now:
            return None
        if status == "preparing" and not timedelta(0) <= now - _utc(offer["preparing_at"]) <= PREPARATION_TIMEOUT:
            return None
        if status in {"preparing", "prepared"} and offer.get("claim_id") is None:
            return None
        if status == "prepared" and (
            offer.get("result") != {"status": "prepared"}
            or not _utc(offer["preparing_at"]) <= _utc(offer["completed_at"]) <= now
        ):
            return None
        payload = offer["payload"]
        if not mtf_runtime.eligible_payload(payload, feed, now):
            return None
        symbol = device["symbol"]
        if (
            payload.get("workflow") != "manual_ticket" or payload.get("state") != "signal"
            or type(symbol) is not str or not symbol or payload.get("symbol") != symbol or feed.get("symbol") != symbol
            or device["account_mode"] != "demo" or payload.get("account_mode") != "demo"
            or _positive(device["volume"]) != Decimal("0.01") or _positive(payload["volume"]) != Decimal("0.01")
            or payload.get("source_identity") != f"mt5:{symbol}:{device_id}"
            or feed.get("timeframe") != "M15" or feed.get("source") != "MetaTrader 5"
            or _execution(payload["execution"]) != _execution(feed["execution"])
            or not timedelta(seconds=-5) <= now - _utc(feed["quote"]["time"]) <= timedelta(seconds=10)
        ):
            return None
        zone = reference_zone(payload)
        if payload["price_digits"] > 8:
            return None
        bar_time = _utc(payload["bar_time"])
        if (
            int(bar_time.timestamp()) % 60 or bar_time.microsecond
            or bar_time + timedelta(minutes=1) > created
            or expiry > bar_time + timedelta(minutes=6)
        ):
            return None
        trigger = next((bar for bar in feed["timeframes"]["M1"] if _utc(bar["time"]) == bar_time), None)
        if trigger is None:
            return None
        expiry = min(expiry, bar_time + timedelta(seconds=60 + mtf_runtime.entry_window_seconds(payload)))
        dto = {
            "version": 2, "workflow": "chart_overlay", "offer_id": str(UUID(str(offer["id"]))),
            "status": status, "symbol": symbol, "timeframe": "M1", "direction": payload["direction"],
            "entry": payload["entry"], **zone, "stop": payload["stop"], "target": payload["target"],
            "price_digits": payload["price_digits"], "execution": dict(payload["execution"]),
            "bar_time": bar_time.isoformat(), "expires_at": expiry.isoformat(),
            **{key: payload[key] for key in ("strategy_id", "strategy_version", "policy_id", "horizon_seconds", "strategy_fingerprint", "direction_bar_time", "confirmation_bar_time")},
        }
        if mtf_runtime.is_experimental_result(payload):
            dto.update(signal_mode="experimental_demo", provisional=True, entry_window_seconds=30,
                       cost_assumptions=dict(payload["cost_assumptions"]))
        else:
            dto["qualification_id"] = payload["qualification_id"]
        return dto
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError, DecimalException):
        return None
