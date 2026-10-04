"""Bounded, deterministic reviews of observed reference-price paper setups.

This module observes prices after a setup became available. It never models an
order or a fill, infers a price between samples, or changes the strategy rules.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
from numbers import Real


MAX_GAP_SECONDS = 180
MAX_OBSERVATIONS = 1600
MAX_PRICE = 100_000_000
TERMINAL_STATUSES = frozenset({"target_observed", "stop_observed", "expired", "inconclusive"})


def _time(value) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, OverflowError):
            raise ValueError("A timezone-aware timestamp is required.") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required.")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _price(value) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("A positive bounded reference price is required.")
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= MAX_PRICE:
        raise ValueError("A positive bounded reference price is required.")
    return value


def _label(value, maximum=128) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or any(c in value for c in "\r\n"):
        raise ValueError("A nonempty bounded identifier is required.")
    return value


def _levels(trade: dict) -> tuple[float, float, float]:
    if trade.get("direction") not in ("BUY", "SELL"):
        raise ValueError("The paper direction must be BUY or SELL.")
    entry, stop, target = (_price(trade.get(key)) for key in ("entry", "stop", "target"))
    ordered = stop < entry < target if trade["direction"] == "BUY" else target < entry < stop
    if not ordered:
        raise ValueError("Paper levels must be strictly ordered for their direction.")
    return entry, stop, target


def create_trade(signal_id, result, source_identity, opened_at, duration_minutes=15) -> dict:
    """Freeze a setup for a declared observation duration; return JSON-safe data.

    opened_at is when this setup became available, not a retrospective fill
    time. The reference entry remains the strategy's immutable candle close.
    """
    if not isinstance(result, dict) or result.get("state") != "signal":
        raise ValueError("A valid paper signal is required.")
    if type(duration_minutes) is not int or not 1 <= duration_minutes <= 1440:
        raise ValueError("The paper duration must be between 1 and 1440 whole minutes.")
    opened = _time(opened_at)
    bar = _time(result.get("bar_time"))
    if bar.microsecond or bar.timestamp() % 900 or bar + timedelta(minutes=15) > opened:
        raise ValueError("The signal must refer to a fully closed aligned M15 bar.")
    entry, stop, target = _levels(result)
    return {
        "id": _label(signal_id),
        "source_identity": _label(source_identity),
        "strategy_id": _label(result.get("strategy_id")),
        "signal_bar_time": _iso(bar),
        "direction": result["direction"],
        "entry": entry,
        "stop": stop,
        "target": target,
        "opened_at": _iso(opened),
        "deadline": _iso(opened + timedelta(minutes=duration_minutes)),
        "duration_minutes": duration_minutes,
        "status": "open",
        "last_observation_at": None,
        "last_observation_price": None,
        "coverage_complete": True,
        "max_gap_seconds": 0.0,
        "closed_at": None,
        "exit_price": None,
        "gross_r": None,
        "review_notes": [],
    }


def _validate_trade(trade: dict) -> tuple[datetime, datetime, datetime | None]:
    if not isinstance(trade, dict) or trade.get("status") not in ("open", *TERMINAL_STATUSES):
        raise ValueError("A valid paper journal record is required.")
    _levels(trade)
    for key in ("id", "source_identity", "strategy_id"):
        _label(trade.get(key))
    opened, deadline = _time(trade.get("opened_at")), _time(trade.get("deadline"))
    bar = _time(trade.get("signal_bar_time"))
    if bar.microsecond or bar.timestamp() % 900 or bar + timedelta(minutes=15) > opened:
        raise ValueError("The stored signal bar must be aligned and fully closed.")
    duration = trade.get("duration_minutes")
    if type(duration) is not int or not 1 <= duration <= 1440 or deadline != opened + timedelta(minutes=duration):
        raise ValueError("The immutable paper deadline and duration must agree.")
    if type(trade.get("coverage_complete")) is not bool:
        raise ValueError("A coverage flag is required.")
    gap = trade.get("max_gap_seconds")
    if isinstance(gap, bool) or not isinstance(gap, Real) or not math.isfinite(gap) or gap < 0:
        raise ValueError("A finite observation gap is required.")
    last = trade.get("last_observation_at")
    if last is not None:
        last = _time(last)
        if not opened < last <= deadline:
            raise ValueError("The last observation must be inside the paper window.")
        _price(trade.get("last_observation_price"))
    elif trade.get("last_observation_price") is not None:
        raise ValueError("An observation price requires a timestamp.")
    status = trade["status"]
    if status == "open":
        if any(trade.get(key) is not None for key in ("closed_at", "exit_price", "gross_r")):
            raise ValueError("An open paper review cannot have a final outcome.")
    else:
        closed = _time(trade.get("closed_at"))
        if not opened < closed <= deadline:
            raise ValueError("The paper outcome must be inside its observation window.")
        if status == "inconclusive":
            if trade["coverage_complete"] or any(trade.get(key) is not None for key in ("exit_price", "gross_r")):
                raise ValueError("An inconclusive review cannot claim a price result.")
        else:
            _price(trade.get("exit_price"))
            gross_r = trade.get("gross_r")
            if isinstance(gross_r, bool) or not isinstance(gross_r, Real) or not math.isfinite(gross_r):
                raise ValueError("A finite reference R result is required.")
            if last is None or (status in ("target_observed", "stop_observed") and closed != last):
                raise ValueError("A resolved paper outcome requires an observed price timestamp.")
            if status == "expired" and (closed != deadline or not trade["coverage_complete"]):
                raise ValueError("Expiry requires complete window coverage.")
    return opened, deadline, last


def _gross_r(price: float, entry: float, risk: float, sign: int) -> float:
    result = sign * (price - entry) / risk
    if not math.isfinite(result):
        raise ValueError("The observed reference movement must produce a finite R result.")
    return round(result, 6)


def _notes(status: str, coverage_complete: bool) -> list[str]:
    notes = []
    if not coverage_complete:
        notes.append("توجد فجوة في العينات تتجاوز 3 دقائق أو لا توجد عينة حديثة قرب نهاية المدة؛ التغطية غير مكتملة.")
    if status in {"target_observed", "stop_observed"}:
        notes.append("هذه أول عينة مرصودة تتجاوز المستوى؛ العينات لا تستبعد عبوراً سابقاً غير مرصود، ولا تؤكد تنفيذاً.")
    proposals = {
        "target_observed": "اقتراح للمراجعة فقط: قارن ثبات النتيجة عبر عدد أكبر من الإشارات قبل أي تغيير في الهدف.",
        "stop_observed": "اقتراح للمراجعة فقط: راجع تأخر نشر الإشارة والتقلب وقتها على سجل أكبر قبل تعديل الوقف.",
        "expired": "اقتراح للمراجعة فقط: قارن مدد احتفاظ محددة مسبقاً على سجل أكبر؛ لا تستنتج تحسناً من نتيجة واحدة.",
        "inconclusive": "اقتراح للمراجعة فقط: حسّن استمرارية البيانات أولاً ثم أعد التقييم؛ لا تحتسب هذه الحالة نجاحاً أو فشلاً.",
    }
    if status in proposals:
        notes.append(proposals[status])
    notes.append("تبقى مستويات الصفقة ومعاملات EMA/ATR ثابتة؛ الملاحظات لا تعيد كتابة النتائج السابقة.")
    return notes


def advance_trade(trade, observations, now) -> dict:
    """Advance a copy with ordered observations; replaying history is harmless.

    Entire batches are validated before processing. Samples outside the declared
    window or already processed are ignored; future samples are rejected. A
    threshold result uses its first observed crossing price, not a modeled fill.
    """
    opened, deadline, last = _validate_trade(trade)
    clock = _time(now)
    if clock < opened:
        raise ValueError("The review clock cannot predate the paper setup.")
    if last is not None and last > clock:
        raise ValueError("The review clock cannot predate an already recorded observation.")
    if not isinstance(observations, list) or len(observations) > MAX_OBSERVATIONS:
        raise ValueError("A bounded list of price observations is required.")
    validated = []
    previous = None
    for sample in observations:
        if not isinstance(sample, dict):
            raise ValueError("A timestamped price observation is required.")
        stamp, price = _time(sample.get("time")), _price(sample.get("price"))
        if previous is not None and stamp <= previous:
            raise ValueError("Observation timestamps must be strictly increasing and unique.")
        if stamp > clock:
            raise ValueError("Future price observations cannot be reviewed.")
        previous = stamp
        validated.append((stamp, price))
    updated = deepcopy(trade)
    if trade["status"] in TERMINAL_STATUSES:
        return updated
    entry, stop, target = _levels(trade)
    sign = 1 if trade["direction"] == "BUY" else -1
    risk = abs(entry - stop)
    for stamp, price in validated:
        if stamp <= opened or stamp > deadline or (last is not None and stamp <= last):
            continue
        gap = (stamp - (last or opened)).total_seconds()
        updated["max_gap_seconds"] = max(float(updated["max_gap_seconds"]), gap)
        updated["coverage_complete"] = updated["coverage_complete"] and gap <= MAX_GAP_SECONDS
        last = stamp
        updated["last_observation_at"] = _iso(stamp)
        updated["last_observation_price"] = price
        hit_target = price >= target if sign == 1 else price <= target
        hit_stop = price <= stop if sign == 1 else price >= stop
        if hit_target or hit_stop:
            updated.update(
                status="target_observed" if hit_target else "stop_observed",
                closed_at=_iso(stamp), exit_price=price,
                gross_r=_gross_r(price, entry, risk, sign),
            )
            updated["review_notes"] = _notes(updated["status"], updated["coverage_complete"])
            return updated
    if clock >= deadline:
        trailing_gap = (deadline - (last or opened)).total_seconds()
        updated["max_gap_seconds"] = max(float(updated["max_gap_seconds"]), trailing_gap)
        updated["coverage_complete"] = updated["coverage_complete"] and last is not None and trailing_gap <= MAX_GAP_SECONDS
        if updated["coverage_complete"]:
            price = updated["last_observation_price"]
            updated.update(status="expired", closed_at=_iso(deadline), exit_price=price,
                           gross_r=_gross_r(price, entry, risk, sign))
        else:
            updated.update(status="inconclusive", closed_at=_iso(deadline), exit_price=None, gross_r=None)
        updated["review_notes"] = _notes(updated["status"], updated["coverage_complete"])
    return updated


def format_review(trade) -> str:
    """Render an Arabic review with reference observations and bounded proposals."""
    _validate_trade(trade)
    labels = {
        "open": "المتابعة مستمرة",
        "target_observed": "رُصد تجاوز الهدف الورقي",
        "stop_observed": "رُصد تجاوز الوقف الورقي",
        "expired": "انتهت مدة المتابعة دون تجاوز مرصود للوقف أو الهدف",
        "inconclusive": "النتيجة غير محسومة بسبب نقص التغطية أو قدم العينات",
    }
    lines = [
        f"🧪 مراجعة ورقية — XAU/USD — {trade['duration_minutes']} دقيقة",
        f"{trade['direction']} — {labels[trade['status']]}",
        f"المصدر: {trade['source_identity']}",
        f"بدء توفر الإعداد (UTC): {trade['opened_at']}",
        f"نهاية المدة (UTC): {trade['deadline']}",
        f"دخول مرجعي ثابت: {trade['entry']:.2f} | وقف: {trade['stop']:.2f} | هدف: {trade['target']:.2f}",
    ]
    if trade["last_observation_at"] is not None:
        lines.append(f"آخر عينة (UTC): {trade['last_observation_at']} — {trade['last_observation_price']:.2f}")
    if trade.get("exit_price") is not None and trade.get("gross_r") is not None:
        lines.append(f"السعر المرصود للمراجعة: {trade['exit_price']:.2f} | الحركة المرجعية الإجمالية: {trade['gross_r']:+.3f}R")
    elif trade["status"] == "inconclusive":
        lines.append("لا تُحتسب حركة R أو نتيجة ربح/خسارة لهذه الحالة.")
    if trade["status"] in TERMINAL_STATUSES:
        lines.extend(_notes(trade["status"], trade["coverage_complete"]))
    lines.append("R = مقدار الحركة المرجعية مقسوماً على مسافة الوقف الأصلية. السبريد والانزلاق والعمولات غير محسوبة؛ لا أوامر أو تنفيذ حقيقي.")
    return "\n".join(lines)
