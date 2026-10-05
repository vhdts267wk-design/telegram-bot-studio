"""Readable chart observations and existing-strategy proposals from MT5 bars.

This presentation layer does not send orders or introduce a trading rule. It
uses the paper engine's EMA/ATR calculations and the caller's accepted setup.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from numbers import Real

from bot import paper_signals


QUOTE_FRESH_SECONDS = 180
FUTURE_QUOTE_TOLERANCE_SECONDS = 5
LEVEL_BARS = 16


def _utc(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An aware timestamp is required.")
    return value.astimezone(timezone.utc)


def _positive(value) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("A finite positive price is required.")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("A finite positive price is required.")
    return number


def _contiguous_tail(bars: list[dict]) -> list[dict]:
    """Use only the most recent unbroken completed-bar sequence."""
    tail = []
    for bar in reversed(bars):
        if tail and (_utc(tail[0]["time"]) - _utc(bar["time"])).total_seconds() != paper_signals.BAR_SECONDS:
            break
        tail.insert(0, bar)
    return tail


def _waiting_reason(result: dict) -> str:
    reason = result.get("reason")
    if reason == "zero_atr":
        return "ننتظر: الحركة شبه معدومة وATR14 لا يعطي وقفاً وهدفاً صالحين."
    if reason in {"invalid_levels", "rounded_levels_collapsed"}:
        return "ننتظر: مستويات الوقف والهدف غير صالحة بعد التقريب؛ لا يوجد اقتراح صفقة."
    return "ننتظر: لم يتكوّن تقاطع جديد بين EMA9 وEMA21 على آخر شمعة مكتملة؛ لا يوجد اقتراح دخول جديد."


def _valid_proposal(result: dict, last_bar: dict) -> bool:
    try:
        if (
            result.get("state") != "signal"
            or result.get("strategy_id") != paper_signals.STRATEGY_ID
            or _utc(result["bar_time"]) != _utc(last_bar["time"])
        ):
            return False
        entry, stop, target = (_positive(result[key]) for key in ("entry", "stop", "target"))
        if entry != paper_signals._money(_positive(last_bar["close"])):
            return False
        direction = result.get("direction")
        return (
            stop < entry < target if direction == "BUY"
            else target < entry < stop if direction == "SELL"
            else False
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def format_chart_proposal(
    result: dict,
    *,
    symbol: str = "XAUUSD",
    execution_enabled: bool = False,
) -> str:
    """Describe the caller's trusted, freshness-gated MT5 strategy result.

    This shorter companion to the full chart report adds no trading rule and
    makes no execution claim. Enabled execution still requires an offered,
    accepted request and the separate Demo bridge's checks.
    """
    if not isinstance(symbol, str) or not symbol or len(symbol) > 32 or any(char in symbol for char in "\n\r"):
        symbol = "XAUUSD"
    prefix = f"🎯 اقتراح صفقة تجريبي — {symbol} M15"
    if not isinstance(result, dict):
        return prefix + "\nبيانات الإشارة غير صالحة؛ ننتظر تحديثاً صحيحاً ولا يوجد اقتراح دخول جديد."
    state = result.get("state")
    if state == "warmup":
        count = result.get("candle_count", 0)
        if type(count) is not int or not 0 <= count < paper_signals.REQUIRED_BARS:
            count = 0
        return (
            prefix + f"\nننتظر: متاح {count}/{paper_signals.REQUIRED_BARS} شمعة M15 مكتملة ومتّصلة؛ "
            f"نحتاج {paper_signals.REQUIRED_BARS - count} شمعة إضافية قبل اقتراح صفقة."
        )
    if state == "stale":
        return prefix + "\nالسعر أو الشموع قديمة؛ ننتظر تحديث MT5 حديثاً ولا يوجد اقتراح دخول جديد."
    if state == "no_signal":
        return prefix + "\n" + _waiting_reason(result)
    if state != "signal":
        return prefix + "\nبيانات الإشارة غير صالحة أو الشموع غير متّصلة؛ ننتظر تحديثاً صحيحاً ولا يوجد اقتراح دخول جديد."
    try:
        if result.get("strategy_id") != paper_signals.STRATEGY_ID:
            raise ValueError("An existing strategy result is required.")
        entry, stop, target, atr = (_positive(result[key]) for key in ("entry", "stop", "target", "atr"))
        direction = result.get("direction")
        if not (
            stop < entry < target if direction == "BUY"
            else target < entry < stop if direction == "SELL"
            else False
        ):
            raise ValueError("An ordered setup is required.")
        bar_time = _utc(result["bar_time"])
        if bar_time.microsecond or bar_time.timestamp() % paper_signals.BAR_SECONDS:
            raise ValueError("A completed M15 trigger timestamp is required.")
        direction_text = "شراء BUY" if direction == "BUY" else "بيع SELL"
        lines = [
            prefix,
            "المصدر: MT5، شموع وسيطك المكتملة.",
            f"{direction_text} — تقاطع EMA9/21 جديد مؤكّد بإغلاق الشمعة.",
            f"افتتاح الشمعة المرجعية: {bar_time:%Y-%m-%d %H:%M} UTC",
            f"الدخول المرجعي: {entry:.2f} | الوقف: {stop:.2f} | الهدف: {target:.2f}",
            f"ATR14: {atr:.2f}؛ الوقف 1.5×ATR والهدف 3×ATR (نحو 2:1).",
            "الدخول من إغلاق الشمعة؛ قد يختلف عن السعر الحالي بسبب الحركة والسبريد.",
            (
                "تنفيذ Demo فقط بعد عرض طلب قابل للتنفيذ وقبولك؛ الإشارة وحدها لا تؤكّد تنفيذ صفقة."
                if execution_enabled is True
                else "اختبار ورقي؛ لا يُرسل أمر تداول من هذه الإشارة."
            ),
            "اقتراح احتمالي؛ قد يصل السعر إلى الوقف قبل الهدف.",
        ]
        return "\n".join(lines)
    except (KeyError, TypeError, ValueError, OverflowError):
        return prefix + "\nمستويات الإشارة أو مرجع الشمعة غير صالحة؛ لا يوجد اقتراح دخول جديد."


def format_chart_analysis(
    feed: dict | None,
    result: dict,
    now: datetime,
    *,
    received_at: datetime | None = None,
    include_proposal: bool = True,
) -> str:
    """Render validated MT5 data and an existing rule result as plain Arabic.

    Quote and optional bridge receipt freshness are checked independently.
    EMA/ATR are reused from the engine on the latest contiguous completed
    candles. Only the accepted setup may supply entry, stop or target, and its
    trigger must match the latest completed candle. No setup is forced during
    warmup, stale data or a continuing trend without a new cross.
    """
    prefix = "📊 تحليل الشارت — الذهب M15 (تجريبي)"
    if feed is None:
        return prefix + "\nمصدر MT5 غير متصل؛ ننتظر سعر الوسيط والشموع المكتملة قبل التحليل واقتراح صفقة."
    try:
        clock = _utc(now)
        if feed.get("source") != "MetaTrader 5" or feed.get("timeframe") != "M15":
            raise ValueError("Actual MT5 M15 data is required.")
        symbol = feed["symbol"]
        if not isinstance(symbol, str) or not symbol or len(symbol) > 32 or any(char in symbol for char in "\n\r"):
            raise ValueError("A broker symbol is required.")
        prefix = f"📊 تحليل الشارت — {symbol} M15 (تجريبي)"
        quote = feed["quote"]
        bid, ask = _positive(quote["bid"]), _positive(quote["ask"])
        if bid > ask:
            raise ValueError("An ordered quote is required.")
        quote_time = _utc(quote["time"])
        quote_age = (clock - quote_time).total_seconds()
        receipt_age = 0 if received_at is None else (clock - _utc(received_at)).total_seconds()
        if not (
            -FUTURE_QUOTE_TOLERANCE_SECONDS <= quote_age <= QUOTE_FRESH_SECONDS
            and 0 <= receipt_age <= QUOTE_FRESH_SECONDS
        ):
            return prefix + "\nبيانات MT5 قديمة أو توقيتها غير صالح؛ ننتظر تحديثاً حديثاً قبل التحليل واقتراح صفقة."
        bars = feed["candles"]
        if not isinstance(bars, list) or not bars:
            raise ValueError("Completed candles are required.")
        tail = _contiguous_tail(bars)
        # Reuse the same strategy validation, EMA seeds and Wilder ATR rather
        # than creating a second indicator implementation.
        computed = paper_signals.analyze_paper_signal(tail, clock)
        if computed["state"] == "stale" or (isinstance(result, dict) and result.get("state") == "stale"):
            return prefix + "\nالشموع أو السعر قديمة؛ ننتظر بيانات حديثة قبل التحليل واقتراح صفقة."
        if (
            computed["state"] == "invalid"
            or not isinstance(result, dict)
            or result.get("state") not in {"signal", "no_signal", "warmup", "stale"}
        ):
            raise ValueError("Valid completed candles and a validated strategy result are required.")
        last = tail[-1]
        last_time = _utc(last["time"])
        lines = [
            prefix,
            "المصدر: MT5، شموع وسيطك المكتملة؛ الأسعار بالدولار.",
            f"السعر الحالي: Bid {bid:.2f} | Ask {ask:.2f} | السبريد {ask - bid:.2f}",
            f"وقت السعر: {quote_time:%Y-%m-%d %H:%M:%S} UTC",
            f"آخر شمعة مكتملة: {last_time:%Y-%m-%d %H:%M} UTC؛ الإغلاق {last['close']:.2f}",
        ]
        if computed["state"] == "warmup" or result.get("state") == "warmup":
            lines.append(
                f"ننتظر: متاح {len(tail)}/{paper_signals.REQUIRED_BARS} شمعة M15 متصلة؛ "
                "السجل غير كافٍ بعد لحساب الاتجاه واقتراح صفقة."
            )
            return "\n".join(lines)
        fast, slow = computed["ema_fast"], computed["ema_slow"]
        trend = "صاعد" if fast > slow else "هابط" if fast < slow else "محايد"
        relation = "فوق" if last["close"] > slow else "تحت" if last["close"] < slow else "عند"
        lines.append(
            f"الاتجاه حسب EMA9/21: {trend}؛ EMA9 {fast:.2f} | EMA21 {slow:.2f}؛ "
            f"الإغلاق {relation} EMA21."
        )
        previous = tail[-2]["close"]
        movement = last["close"] - previous
        momentum = "ارتفع" if movement > 0 else "انخفض" if movement < 0 else "لم يتغيّر"
        lines.append(f"الحركة الأخيرة: الإغلاق {momentum} بمقدار {abs(movement):.2f} عن الشمعة السابقة.")
        lines.append(f"ATR14: {computed['atr']:.2f}؛ مقياس حركة السعر لحساب الوقف والهدف.")
        recent = tail[-LEVEL_BARS:]
        support = min(bar["low"] for bar in recent)
        resistance = max(bar["high"] for bar in recent)
        lines.append(
            f"دعم محتمل {support:.2f} | مقاومة محتملة {resistance:.2f} "
            "(أدنى وأعلى آخر 4 ساعات مكتملة)."
        )
        lines.append("")
        if result.get("state") == "signal":
            if not _valid_proposal(result, last):
                lines.append("ننتظر: إعداد الصفقة لا يطابق آخر شمعة أو مستوياته غير صالحة؛ لا يوجد اقتراح دخول جديد.")
            else:
                if include_proposal:
                    direction = "شراء BUY" if result["direction"] == "BUY" else "بيع SELL"
                    lines.extend([
                        f"اقتراح {direction} تجريبي: تقاطع جديد EMA9/21 مؤكّد بإغلاق الشمعة.",
                        f"دخول مرجعي {result['entry']:.2f} | وقف {result['stop']:.2f} | هدف {result['target']:.2f}",
                        "الوقف 1.5×ATR والهدف 3×ATR؛ العائد إلى المخاطرة نحو 2:1.",
                        "شرط التنفيذ: مراجعة السعر الحالي والسبريد ثم موافقتك؛ الدخول المرجعي من إغلاق الشمعة.",
                    ])
                else:
                    lines.append("تكوّن تقاطع جديد EMA9/21 على آخر شمعة مكتملة.")
        else:
            lines.append(_waiting_reason(computed if result.get("reason") is None else result))
        lines.append("قراءة احتمالية؛ الدعم والمقاومة والاتجاه قد يتغيّرون.")
        return "\n".join(lines)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return prefix + "\nبيانات MT5 أو الشموع غير صالحة للتحليل؛ ننتظر تحديثاً صحيحاً قبل اقتراح صفقة."
