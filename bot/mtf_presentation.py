"""Arabic chart observations and empirically qualified manual proposals."""

from datetime import datetime, timedelta, timezone
import math

from bot import mtf_runtime, paper_signals

REASONS = {
    "evidence_unavailable": "لم تثبت الأدلة خارج العينة شرط 70% و200 صفقة بعد التكاليف.",
    "unverified_costs": "تكاليف العمولة والانزلاق غير موثّقة.",
    "costs_unverified": "تكاليف العمولة والانزلاق غير موثّقة.",
    "missing_timeframes": "بيانات أحد الأطر M15 أو M5 أو M1 غير متاحة.",
    "insufficient_history": "نحتاج 22 شمعة مكتملة ومتّصلة بعد آخر فجوة في كل إطار.",
    "stale_quote": "السعر أقدم من 10 ثوانٍ.",
    "stale_snapshot": "تحديث الربط قديم.",
    "stale_m1_timing": "شمعة توقيت الدخول M1 قديمة.",
    "entry_window_expired": "انتهت نافذة الدخول: 10 ثوانٍ فقط بعد إغلاق شمعة M1؛ انتظر إشارة جديدة.",
    "outside_full_horizon_session": "الوقت خارج نافذة السيولة المحددة أو لا يتسع لمدة 60 دقيقة.",
    "existing_exposure": "توجد صفقات أو أوامر معلّقة بالحساب.",
    "insufficient_free_margin": "الهامش المتاح لا يجتاز الحد المطلوب.",
    "flat_m15_trend": "الاتجاه العام M15 غير واضح.",
    "conflicting_m15_trend": "اتجاه M15 وزخمه غير متوافقين.",
    "m5_pullback_not_confirmed": "فرصة M5 لم تؤكد الاتجاه العام.",
    "m1_breakout_not_confirmed": "لم يتأكد توقيت الدخول على M1.",
    "extreme_true_range": "الحركة الأخيرة تتجاوز حد التقلب المسموح.",
    "low_tick_activity": "نشاط الأسعار أقل من حد السيولة التقريبي.",
    "excessive_or_unknown_spread": "السبريد مرتفع أو غير صالح للتحقق.",
    "equity_risk_limit": "المخاطرة المقدّرة تتجاوز 1% من رصيد الحساب المتاح للمخاطرة.",
    "insufficient_reward_after_costs": "العائد إلى المخاطرة بعد التكاليف أقل من 1.5:1.",
    "broker_protection_distance": "الوقف أو الهدف لا يطابق قيود الوسيط.",
    "risk_pause": "الإشارات موقوفة مؤقتاً بسبب فلتر المخاطر.",
}
HIGH_RISK = {"extreme_true_range", "low_tick_activity", "excessive_or_unknown_spread", "equity_risk_limit", "insufficient_reward_after_costs", "existing_exposure", "insufficient_free_margin"}


def _time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Aware timestamp required")
    return value.astimezone(timezone.utc)


def status_text(result):
    reason = result.get("reason", "") if type(result) is dict else ""
    title = "فرصة عالية المخاطر" if reason in HIGH_RISK else "لا توجد فرصة مؤكدة الشروط حاليًا"
    if reason in REASONS:
        detail = REASONS[reason]
    elif type(result) is dict and result.get("state") == "stale":
        detail = "بيانات MT5 غير حديثة؛ ننتظر تحديث السعر والشموع."
    elif type(result) is dict and result.get("state") == "warmup":
        detail = REASONS["insufficient_history"]
    else:
        detail = "بيانات السوق أو المخاطر غير مكتملة أو شروط الأطر الثلاثة غير متوافقة."
    return title + "\nالسبب: " + detail


def format_proposal(result, *, symbol="XAUUSD", execution_enabled=False, manual_ticket_enabled=False):
    if not mtf_runtime.eligible_result(result):
        if type(result) is dict and result.get("state") == "signal":
            result = mtf_runtime.blocked("entry_window_expired") if not mtf_runtime.entry_window_open(result) else mtf_runtime.blocked()
        return status_text(result)
    try:
        digits = result["price_digits"]
        if type(digits) is not int or not 0 <= digits <= 8:
            raise ValueError("Invalid precision")
        entry, stop, target, target2, low, high = (result[key] for key in ("entry", "stop", "target", "target2", "entry_zone_low", "entry_zone_high"))
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in (entry, stop, target, target2, low, high)):
            raise ValueError("Invalid levels")
        direction = result["direction"]
        if not (stop < low <= entry <= high < target <= target2 if direction == "BUY" else target2 <= target < low <= entry <= high < stop if direction == "SELL" else False):
            raise ValueError("Invalid ordered protection")
        text = "شراء BUY" if direction == "BUY" else "بيع SELL"
        lines = [
            f"إشارة مؤهلة للاختبار التجريبي — {symbol} | {text}",
            "M15 اتجاه عام → M5 تأكيد → M1 توقيت؛ شموع مكتملة.",
            f"الدخول المقترح: {entry:.{digits}f}",
            f"منطقة الدخول: {low:.{digits}f} – {high:.{digits}f}",
            f"SL: {stop:.{digits}f} | TP1: {target:.{digits}f} | TP2: {target2:.{digits}f}",
            f"العائد إلى المخاطرة: {result['nominal_reward_risk']:.2f}:1؛ بعد التكاليف المقدّرة {result['effective_reward_risk']:.2f}:1.",
            "السبب: اتجاه M15 واضح، ارتداد M5 مؤكّد، وكسر M1 بإغلاق متوافق، مع اجتياز فلاتر المخاطر والسبريد والنشاط؛ نافذة الدخول 10 ثوانٍ من إغلاق M1.",
            "الإلغاء: مرور 10 ثوانٍ بعد إغلاق M1، انعكاس الاتجاه أو التأكيد، تدهور السبريد أو حداثة البيانات أو المخاطر، أو تجاوز انحراف السعر والسبريد معاً 0.1R. يبقى الوقف والهدف ثابتين.",
            "مدة تقييم النجاح: 60 دقيقة من الدخول؛ الهدف الأول قبل الوقف، مع احتساب التكاليف. انتهاء المدة دون الهدف يُحسب غير ناجح.",
        ]
        for metric in result.get("evidence_metrics", []):
            lines.append(f"اختبار خارج العينة ({metric['scenario']}): {metric['wins']}/{metric['trades']} نجاح؛ النسبة الملاحظة {metric['win_rate']:.1%}، الحد الأدنى لفاصل الثقة 95%: {metric['lower_95']:.1%}.")
        lines.extend([
            "التنفيذ بموافقتك اليدوية لكل صفقة: وافق على التجهيز خلال 10 ثوانٍ من إغلاق M1؛ تراجع نافذة MT5 وتضغط Buy أو Sell بنفسك. إن فاتت النافذة انتظر إشارة جديدة.",
            "النتيجة تقدير تاريخي مشروط بنموذج التنفيذ والتكاليف؛ ليست احتمالاً مثبتاً لهذه الصفقة أو ضماناً للربح.",
        ])
        return "\n".join(lines)
    except (KeyError, TypeError, ValueError, OverflowError):
        return status_text(mtf_runtime.blocked("invalid_levels"))


def format_analysis(feed, result, now, *, received_at=None, include_proposal=True):
    lines = ["تحليل شارت MT5 — M15 / M5 / M1"]
    try:
        clock = _time(now)
        if type(feed) is not dict or feed.get("source") != "MetaTrader 5":
            return "\n".join(lines + ["لا توجد فرصة مؤكدة الشروط حاليًا", "السبب: ربط MT5 لا يزوّد بيانات حديثة."])
        quote = feed["quote"]
        if any(type(quote.get(key)) not in (int, float) or not math.isfinite(quote[key]) or quote[key] <= 0 for key in ("bid", "ask")) or quote["ask"] <= quote["bid"]:
            raise ValueError("Invalid executable quote")
        if not -5 <= (clock - _time(quote["time"])).total_seconds() <= 10 or (received_at is not None and not 0 <= (clock - _time(received_at)).total_seconds() <= 30):
            return "\n".join(lines + [status_text({"state": "stale"})])
        digits = feed.get("execution", {}).get("digits", 2)
        if type(digits) is not int or not 0 <= digits <= 8:
            raise ValueError("Invalid display precision")
        lines.append(f"Bid {quote['bid']:.{digits}f} | Ask {quote['ask']:.{digits}f} | السبريد {quote['ask'] - quote['bid']:.{digits}f}")
        frames = feed.get("timeframes", {})
        validated_frames = {}
        for frame, seconds in (("M15", 900), ("M5", 300), ("M1", 60)):
            rows = frames.get(frame, [])
            if type(rows) is not list or len(rows) > 64:
                raise ValueError("Invalid timeframe history")
            previous = None
            for row in rows:
                stamp = _time(row["time"])
                values = [row[key] for key in ("open", "high", "low", "close")]
                if stamp.microsecond or stamp.timestamp() % seconds or stamp + timedelta(seconds=seconds) > clock or (previous is not None and stamp <= previous):
                    raise ValueError("Invalid closed history")
                if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in values) or row["high"] < max(row["open"], row["close"], row["low"]) or row["low"] > min(row["open"], row["close"], row["high"]):
                    raise ValueError("Invalid candle prices")
                previous = stamp
            validated_frames[frame] = rows
        if validated_frames["M1"]:
            close1 = _time(validated_frames["M1"][-1]["time"]) + timedelta(seconds=60)
            if clock - close1 > timedelta(seconds=75):
                raise ValueError("Stale timing history")
            for frame, seconds in (("M15", 900), ("M5", 300)):
                if validated_frames[frame]:
                    closed = _time(validated_frames[frame][-1]["time"]) + timedelta(seconds=seconds)
                    if not timedelta(0) <= close1 - closed < timedelta(seconds=seconds):
                        raise ValueError("Noncausal timeframe history")
        for frame, seconds, role in (("M15", 900, "الاتجاه العام"), ("M5", 300, "تأكيد الفرصة"), ("M1", 60, "توقيت الدخول")):
            rows = frames.get(frame, [])
            suffix = []
            for row in reversed(rows):
                if suffix and (_time(suffix[0]["time"]) - _time(row["time"])).total_seconds() != seconds:
                    break
                suffix.insert(0, row)
            if not suffix or _time(suffix[-1]["time"]) + timedelta(seconds=seconds) > clock:
                lines.append(f"{frame} — {role}: بيانات مكتملة غير متاحة.")
            elif len(suffix) < 22:
                lines.append(f"{frame} — {role}: تجهيز السجل ({len(suffix)}/22 شمعة متصلة).")
            else:
                closes = [row["close"] for row in suffix]
                fast, slow = paper_signals._ema(closes, 9)[-1], paper_signals._ema(closes, 21)[-1]
                trend = "صاعد" if fast > slow else "هابط" if fast < slow else "محايد"
                lines.append(f"{frame} — {role}: ميل {trend}؛ آخر إغلاق {_time(suffix[-1]['time']) + timedelta(seconds=seconds):%H:%M} UTC.")
        lines.append("السيولة تُقدّر من نشاط الأسعار والسبريد ووقت الجلسة؛ حجم تداول فعلي وعمق السوق غير متاحين في هذه البيانات.")
        proposal = {**result, "workflow": "manual_ticket"} if type(result) is dict else {}
        qualified = mtf_runtime.eligible_payload(proposal, feed, clock)
        displayed = result
        if not qualified and result.get("state") == "signal":
            displayed = mtf_runtime.blocked("entry_window_expired") if not mtf_runtime.entry_window_open(result, clock) else mtf_runtime.blocked()
        lines.append(format_proposal(displayed, symbol=feed.get("symbol", "XAUUSD")) if include_proposal else "توجد إشارة اجتازت بوابة الأدلة؛ تفاصيلها عبر /signals." if qualified else status_text(displayed))
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        lines.append(status_text({"state": "invalid"}))
    return "\n".join(lines)
