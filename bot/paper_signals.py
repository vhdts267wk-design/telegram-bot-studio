"""Deterministic paper setups from completed, contiguous M15 candles.

EMA9/21 use SMA seeds at indices 8/20. ATR14 starts with the mean true
range of bars 1..14, then uses Wilder's (previous ATR * 13 + TR) / 14.
No order is sent, and historical profitability is not implied.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import math
from numbers import Real
from typing import Any


STRATEGY_ID = "ema9-21-atr14-v1"
REQUIRED_BARS = 22
BAR_SECONDS = 15 * 60
MAX_BAR_AGE_SECONDS = 20 * 60
MAX_PRICE = 100_000_000


def _result(state: str, count: int, reason: str, details: str, **values) -> dict:
    return {
        "state": state,
        "strategy_id": STRATEGY_ID,
        "candle_count": count,
        "required_bars": REQUIRED_BARS,
        "remaining_bars": max(0, REQUIRED_BARS - count),
        "reason": reason,
        "details": details,
        **values,
    }


def _price(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("Invalid candle price.")
    price = float(value)
    if not math.isfinite(price) or not 0 < price <= MAX_PRICE:
        raise ValueError("Invalid candle price.")
    return price


def _bar_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("A UTC M15 bar timestamp is required.")
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if (
        timestamp.tzinfo is None
        or timestamp.utcoffset() != timedelta(0)
        or timestamp.timestamp() <= 0
        or timestamp.microsecond
        or timestamp.timestamp() % BAR_SECONDS
    ):
        raise ValueError("A UTC M15 bar timestamp is required.")
    return timestamp.astimezone(timezone.utc)


def _ema(closes: list[float], period: int) -> list[float | None]:
    values = [None] * (period - 1)
    current = math.fsum(closes[:period]) / period
    values.append(current)
    alpha = 2 / (period + 1)
    for close in closes[period:]:
        current = current + alpha * (close - current)
        values.append(current)
    return values


def _atr(candles: list[dict], period: int = 14) -> float:
    ranges = [
        max(current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]))
        for previous, current in zip(candles, candles[1:])
    ]
    atr = math.fsum(ranges[:period]) / period
    for true_range in ranges[period:]:
        atr = (atr * (period - 1) + true_range) / period
    return atr


def _money(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def analyze_paper_signal(candles: list[dict], now: datetime | None = None) -> dict:
    """Return signal/no_signal/warmup/stale/invalid with safe serializable details.

    The caller additionally gates quote freshness and source suitability. The
    engine requires every supplied bar to be closed and contiguous, rejects
    explicitly insufficient sampled coverage, and checks last-bar freshness.
    """
    if not isinstance(candles, list):
        return _result("invalid", 0, "invalid_candles", "Candles must be a list.")
    count = len(candles)
    clock = datetime.now(timezone.utc) if now is None else now
    if not isinstance(clock, datetime) or clock.tzinfo is None or clock.utcoffset() is None:
        return _result("invalid", count, "invalid_clock", "A timezone-aware clock is required.")
    clock = clock.astimezone(timezone.utc)
    validated = []
    previous_time = None
    try:
        for candle in candles:
            if not isinstance(candle, dict):
                raise ValueError("Invalid candle.")
            timestamp = _bar_time(candle.get("time"))
            prices = {name: _price(candle.get(name)) for name in ("open", "high", "low", "close")}
            if (
                prices["high"] < max(prices["open"], prices["close"], prices["low"])
                or prices["low"] > min(prices["open"], prices["close"], prices["high"])
            ):
                raise ValueError("Invalid OHLC range.")
            if timestamp + timedelta(seconds=BAR_SECONDS) > clock:
                return _result("invalid", count, "forming_bar", "Only fully closed M15 bars may be used.")
            if previous_time is not None and (timestamp - previous_time).total_seconds() != BAR_SECONDS:
                return _result("invalid", count, "non_contiguous", "Bars must be ordered, unique and contiguous.")
            if ("coverage_ok" in candle and candle["coverage_ok"] is not True) or (
                candle.get("sampled") is True and candle.get("coverage_ok") is not True
            ):
                return _result("invalid", count, "insufficient_coverage", "Sampled bars require adequate coverage.")
            previous_time = timestamp
            validated.append({"time": timestamp, **prices})
    except (ValueError, TypeError, OverflowError):
        return _result("invalid", count, "invalid_candle", "Candle prices, OHLC or UTC timestamps are invalid.")
    if not validated:
        return _result("warmup", count, "insufficient_history", "At least 22 completed M15 bars are required.")
    last_time = validated[-1]["time"]
    bar_time = last_time.isoformat().replace("+00:00", "Z")
    if (clock - last_time - timedelta(seconds=BAR_SECONDS)).total_seconds() > MAX_BAR_AGE_SECONDS:
        return _result("stale", count, "stale_bars", "The last completed candle is more than 20 minutes old.",
                       bar_time=bar_time)
    if count < REQUIRED_BARS:
        return _result("warmup", count, "insufficient_history", "At least 22 completed M15 bars are required.",
                       bar_time=bar_time)
    closes = [candle["close"] for candle in validated]
    fast, slow = _ema(closes, 9), _ema(closes, 21)
    atr = _atr(validated)
    indicators = {
        "bar_time": bar_time,
        "ema_fast": round(fast[-1], 8),
        "ema_slow": round(slow[-1], 8),
        "previous_ema_fast": round(fast[-2], 8),
        "previous_ema_slow": round(slow[-2], 8),
        "atr": round(atr, 8),
    }
    if atr <= 0:
        return _result("no_signal", count, "zero_atr", "Flat prices provide no valid ATR-based paper levels.",
                       **indicators)
    if fast[-2] <= slow[-2] and fast[-1] > slow[-1]:
        direction, sign = "BUY", 1
    elif fast[-2] >= slow[-2] and fast[-1] < slow[-1]:
        direction, sign = "SELL", -1
    else:
        return _result("no_signal", count, "no_crossover", "There is no new EMA9/21 crossing on the last bar.",
                       **indicators)
    entry = _money(closes[-1])
    stop = _money(closes[-1] - sign * 1.5 * atr)
    target = _money(closes[-1] + sign * 3 * atr)
    if not all(0 < value <= MAX_PRICE for value in (entry, stop, target)):
        return _result("no_signal", count, "invalid_levels", "Positive, bounded paper levels cannot be formed.",
                       **indicators)
    if not ((stop < entry < target) if direction == "BUY" else (target < entry < stop)):
        return _result("no_signal", count, "rounded_levels_collapsed", "ATR levels collapse after 0.01 rounding.",
                       **indicators)
    return _result(
        "signal", count, "ema_crossover", "A new closed-bar EMA9/21 crossing formed a paper setup.",
        direction=direction, entry=entry, stop=stop, target=target,
        reward_risk=2.0,
        rounded_reward_risk=round(abs(target - entry) / abs(entry - stop), 4),
        stop_atr_multiple=1.5, target_atr_multiple=3.0,
        **indicators,
    )


def format_paper_signal(result: dict, source: str = "GoldAPI reference prices") -> str:
    """Format transparent Arabic paper-only output; never claim real execution."""
    prefix = "🧪 اختبار ورقي فقط — XAU/USD\n"
    state = result.get("state") if isinstance(result, dict) else "invalid"
    if state == "warmup":
        count = result.get("candle_count", 0)
        remaining = result.get("remaining_bars", REQUIRED_BARS)
        return (prefix + f"تجميع البيانات: {count}/{REQUIRED_BARS} شمعة M15 مكتملة؛ المتبقي {remaining}.\n"
                "يلزم نحو 5.5 ساعات من الشموع المتصلة ذات التغطية الكافية. لا توجد إشارة بعد.")
    if state == "stale":
        return prefix + "الشموع قديمة؛ لا توجد إشارة جديدة. ننتظر بيانات حديثة من المصدر."
    if state == "invalid":
        return prefix + "البيانات غير كافية الجودة أو غير متصلة؛ لا توجد إشارة جديدة."
    if state == "no_signal":
        return prefix + "لا توجد إشارة ورقية جديدة؛ لم تتكوّن شروط تقاطع EMA9/21 مع مستويات ATR صالحة."
    if state != "signal" or any(key not in result for key in ("direction", "bar_time", "entry", "stop", "target", "atr")):
        return prefix + "لا توجد إشارة صالحة للعرض."
    label = str(source).replace("\n", " ").replace("\r", " ")[:100]
    return (
        prefix
        + f"{result['direction']} — تقاطع EMA9/21 على شمعة مكتملة\n"
        + f"المصدر: {label}\n"
        + f"وقت افتتاح الشمعة (UTC): {result['bar_time']}\n"
        + f"دخول مرجعي: {result['entry']:.2f}\n"
        + f"وقف ورقي: {result['stop']:.2f}\n"
        + f"هدف ورقي: {result['target']:.2f}\n"
        + f"ATR14: {result['atr']:.4f}\n"
        + "الوقف 1.5×ATR والهدف 3×ATR؛ النسبة الاسمية 1:2 قبل التقريب.\n"
        + "الدخول مرجعي من إغلاق الشمعة، وليس سعر تنفيذ مؤكد. الأسعار المرجعية أو المأخوذة بالعينات قد تختلف عن وسيطك.\n"
        + "السبريد والانزلاق والعمولات والتنفيذ غير محسوبة. هذا اختبار محاكاة، بلا أوامر حقيقية وبلا ادعاء نتائج تاريخية أو ربح مضمون."
    )
