"""Arabic chart observations and clearly labelled manual Demo proposals."""

from datetime import datetime, timedelta, timezone
import math

from bot import mtf_runtime, multi_timeframe

REASONS = {
    "evidence_unavailable": "لم تثبت الأدلة خارج العينة شرط 70% و200 صفقة بعد التكاليف.",
    "unverified_costs": "تكاليف العمولة والانزلاق غير موثّقة.",
    "costs_unverified": "تكاليف العمولة والانزلاق غير موثّقة.",
    "missing_timeframes": "بيانات أحد الأطر M1 أو M5 أو M15 أو H1 أو H4 غير متاحة.",
    "insufficient_history": "نحتاج 22 شمعة مكتملة ومتّصلة بعد آخر فجوة في كل إطار.",
    "stale_quote": "السعر أقدم من 10 ثوانٍ.",
    "stale_snapshot": "تحديث الربط قديم.",
    "stale_m1_timing": "شمعة توقيت الدخول M1 قديمة.",
    "entry_window_expired": "انتهت نافذة الدخول: 10 ثوانٍ فقط بعد إغلاق شمعة M1؛ انتظر إشارة جديدة.",
    "outside_full_horizon_session": "الوقت خارج نافذة السيولة المحددة أو لا يتسع لمدة 60 دقيقة.",
    "existing_exposure": "توجد صفقات أو أوامر معلّقة بالحساب.",
    "insufficient_free_margin": "الهامش المتاح لا يجتاز الحد المطلوب.",
    "flat_m15_trend": "تأكيد الاتجاه على M15 غير واضح.",
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
    "experimental_profile_invalid": "بيانات الإشارة التجريبية أو افتراضات تكاليفها غير صالحة للتحقق.",
    "higher_timeframe_conflict": "إشارة الدخول تعاكس اتجاه H1 أو H4؛ ننتظر توافق الأطر. الثقة النوعية منخفضة، وليست نسبة نجاح أو إشارة شراء قوية.",
    "higher_timeframe_neutral": "اتجاه H1 أو H4 محايد؛ ننتظر اتجاهاً أوضح قبل اقتراح الدخول.",
}
HIGH_RISK = {"extreme_true_range", "low_tick_activity", "excessive_or_unknown_spread", "equity_risk_limit", "insufficient_reward_after_costs", "existing_exposure", "insufficient_free_margin"}
FRAME_ROLES = (("H4", 14400, "الاتجاه العام"), ("H1", 3600, "الاتجاه القريب"),
               ("M15", 900, "تأكيد الاتجاه"), ("M5", 300, "إشارة الدخول"),
               ("M1", 60, "توقيت الدخول"))
TREND_LABELS = {"BUY": "صاعد", "SELL": "هابط", "NEUTRAL": "محايد"}


def context_lines(result, digits=2):
    """Describe structural alignment without assigning a win probability."""
    context = result.get("timeframe_context") if type(result) is dict else None
    if type(context) is not dict:
        return []
    try:
        trends = context["trends"]
        if type(trends) is not dict or set(trends) != {frame for frame, _, _ in FRAME_ROLES}:
            raise ValueError("Incomplete trend context")
        labels = {frame: TREND_LABELS[value] for frame, value in trends.items()}
        alignment = {"aligned": "متوافقة مع الدخول", "counter_trend": "معاكسة للدخول", "unconfirmed": "غير مؤكّدة"}[context["alignment"]]
        confidence = {"aligned": "مدعومة بتوافق الأطر", "reduced": "منخفضة بسبب التعارض", "unconfirmed": "غير مؤكّدة"}[context["confidence"]]
        if type(context.get("counter_trend")) is not bool:
            raise ValueError("Invalid alignment context")
        levels = []
        for key, name in (("support", "دعم"), ("resistance", "مقاومة")):
            value = context[key]
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
                raise ValueError("Invalid observed level")
            levels.append(f"{name} {value:.{digits}f}" if value is not None else f"{name} غير متاح")
        return [
            f"الأطر الكبيرة: H1 {labels['H1']} | H4 {labels['H4']} — {alignment}.",
            f"الثقة النوعية: {confidence}؛ وصف لتوافق الأطر، وليست نسبة نجاح.",
            "H4 من شموع مكتملة: " + " | ".join(levels) + ".",
        ]
    except (KeyError, TypeError, ValueError, OverflowError):
        return []


def _time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Aware timestamp required")
    return value.astimezone(timezone.utc)


def _experimental(result):
    return type(result) is dict and result.get("signal_mode") == "experimental_demo"


def _window_text(result):
    return "30 ثانية" if _experimental(result) else "10 ثوانٍ"


def _status_context(result, blocked):
    if _experimental(result):
        return {**blocked, "signal_mode": "experimental_demo", "entry_window_seconds": 30, "provisional": True}
    return blocked


def _blocked_proposal(result, now=None):
    reason = "entry_window_expired" if not mtf_runtime.entry_window_open(result, now) else "experimental_profile_invalid" if _experimental(result) else "evidence_unavailable"
    return _status_context(result, mtf_runtime.blocked(reason))


def status_text(result, *, include_context=True):
    reason = result.get("reason", "") if type(result) is dict else ""
    experimental = _experimental(result)
    title = "فرصة عالية المخاطر" if reason in HIGH_RISK else "لا توجد إشارة Demo تجريبية مستوفية الشروط حاليًا" if experimental else "لا توجد فرصة مؤكدة الشروط حاليًا"
    if reason == "entry_window_expired" and experimental:
        detail = "انتهت نافذة الدخول: 30 ثانية فقط بعد إغلاق شمعة M1؛ انتظر إشارة جديدة."
    elif reason in REASONS:
        detail = REASONS[reason]
    elif type(result) is dict and result.get("state") == "stale":
        detail = "بيانات MT5 غير حديثة؛ ننتظر تحديث السعر والشموع."
    elif type(result) is dict and result.get("state") == "warmup":
        detail = REASONS["insufficient_history"]
    else:
        detail = "بيانات السوق أو المخاطر غير مكتملة أو شروط الأطر الخمسة غير متوافقة."
    text = title + "\nالسبب: " + detail
    summary = context_lines(result) if include_context else []
    if summary:
        text += "\n" + "\n".join(summary)
    if experimental:
        text += "\nوضع Demo تجريبي — الأداء غير مثبت؛ التكاليف افتراضات تقديرية غير موثّقة."
    return text


def format_proposal(result, *, symbol="XAUUSD", execution_enabled=False, manual_ticket_enabled=False, include_context=True):
    if not mtf_runtime.eligible_result(result):
        if type(result) is dict and result.get("state") == "signal":
            result = _blocked_proposal(result)
        return status_text(result, include_context=include_context)
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
        experimental = _experimental(result)
        window = _window_text(result)
        lines = [
            f"إشارة Demo تجريبية — الأداء غير مثبت — {symbol} | {text}" if experimental else f"إشارة مؤهلة للاختبار التجريبي — {symbol} | {text}",
            "H4 اتجاه عام → H1 اتجاه قريب → M15 تأكيد → M5/M1 دخول؛ شموع مكتملة.",
            *(context_lines(result, digits) if include_context else []),
            f"الدخول المقترح: {entry:.{digits}f}",
            f"منطقة الدخول: {low:.{digits}f} – {high:.{digits}f}",
            f"SL: {stop:.{digits}f} | TP1: {target:.{digits}f} | TP2: {target2:.{digits}f}",
            f"العائد إلى المخاطرة: {result['nominal_reward_risk']:.2f}:1؛ بعد التكاليف المقدّرة {result['effective_reward_risk']:.2f}:1.",
            f"السبب: توافق H4 وH1 مع تأكيد M15، ارتداد M5 مؤكّد{' خلال آخر 3 شموع قبل شمعة التأكيد' if experimental else ''}، وكسر M1 بإغلاق متوافق، مع اجتياز فلاتر المخاطر والسبريد والنشاط؛ نافذة الدخول {window} من إغلاق M1.",
            f"الإلغاء: مرور {window} بعد إغلاق M1، انعكاس الاتجاه أو التأكيد، تدهور السبريد أو حداثة البيانات أو المخاطر، أو تجاوز انحراف السعر والسبريد معاً 0.1R. يبقى الوقف والهدف ثابتين.",
            "مدة تقييم النجاح: 60 دقيقة من الدخول؛ الهدف الأول قبل الوقف، مع احتساب التكاليف. انتهاء المدة دون الهدف يُحسب غير ناجح.",
        ]
        if experimental:
            assumptions = result["cost_assumptions"]
            commission, slippage = (assumptions[key] for key in ("commission_round_turn", "slippage_price"))
            if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in (commission, slippage)):
                raise ValueError("Invalid cost assumptions")
            lines.extend([
                "Demo فقط | الحجم 0.01 | المخاطرة المقدّرة حتى 1% من حقوق الحساب (Equity).",
                f"التكاليف افتراضات تقديرية غير موثّقة: عمولة ذهاب وإياب {commission:.8g} بعملة الحساب للحجم 0.01؛ انزلاق {slippage:.8g} بوحدات السعر لكل جهة.",
                "تقدير الانزلاق يراعي السبريد وحجم أصغر حركة سعر؛ قد تختلف التكاليف الفعلية.",
            ])
        else:
            for metric in result.get("evidence_metrics", []):
                lines.append(f"اختبار خارج العينة ({metric['scenario']}): {metric['wins']}/{metric['trades']} نجاح؛ النسبة الملاحظة {metric['win_rate']:.1%}، الحد الأدنى لفاصل الثقة 95%: {metric['lower_95']:.1%}.")
        lines.extend([
            f"التنفيذ بموافقتك اليدوية لكل صفقة: وافق على التجهيز خلال {window} من إغلاق M1؛ تراجع نافذة MT5 وتضغط Buy أو Sell بنفسك. إن فاتت النافذة انتظر إشارة جديدة.",
            "الأداء غير مثبت؛ اجتياز الشروط لا يثبت احتمال نجاح هذه الصفقة ولا يضمن الربح." if experimental else "النتيجة تقدير تاريخي مشروط بنموذج التنفيذ والتكاليف؛ ليست احتمالاً مثبتاً لهذه الصفقة أو ضماناً للربح.",
        ])
        return "\n".join(lines)
    except (KeyError, TypeError, ValueError, OverflowError):
        blocked = mtf_runtime.blocked("experimental_profile_invalid" if _experimental(result) else "invalid_levels")
        return status_text(_status_context(result, blocked), include_context=include_context)


def format_analysis(feed, result, now, *, received_at=None, include_proposal=True):
    lines = ["تحليل شارت MT5 — M1 / M5 / M15 / H1 / H4"]
    try:
        clock = _time(now)
        if type(feed) is not dict or feed.get("source") != "MetaTrader 5":
            return "\n".join(lines + [status_text(_status_context(result, {"state": "stale"}))])
        quote = feed["quote"]
        if any(type(quote.get(key)) not in (int, float) or not math.isfinite(quote[key]) or quote[key] <= 0 for key in ("bid", "ask")) or quote["ask"] <= quote["bid"]:
            raise ValueError("Invalid executable quote")
        if not -5 <= (clock - _time(quote["time"])).total_seconds() <= 10 or (received_at is not None and not 0 <= (clock - _time(received_at)).total_seconds() <= 30):
            return "\n".join(lines + [status_text(_status_context(result, {"state": "stale"}))])
        digits = feed.get("execution", {}).get("digits", 2)
        if type(digits) is not int or not 0 <= digits <= 8:
            raise ValueError("Invalid display precision")
        lines.append(f"Bid {quote['bid']:.{digits}f} | Ask {quote['ask']:.{digits}f} | السبريد {quote['ask'] - quote['bid']:.{digits}f}")
        frames = feed.get("timeframes", {})
        validated_frames = {}
        offset = multi_timeframe.broker_utc_offset_minutes(feed.get("broker_utc_offset_minutes"))
        for frame, seconds, _ in FRAME_ROLES:
            rows = frames.get(frame, [])
            if type(rows) is not list or len(rows) > 64:
                raise ValueError("Invalid timeframe history")
            validated_frames[frame] = multi_timeframe._contiguous_suffix(
                multi_timeframe._bars(rows, frame, clock, offset), frame,
            )
        if validated_frames["M1"]:
            close1 = _time(validated_frames["M1"][-1]["time"]) + timedelta(seconds=60)
            if clock - close1 > timedelta(seconds=75):
                raise ValueError("Stale timing history")
            for frame, seconds, _ in FRAME_ROLES:
                if frame == "M1":
                    continue
                if validated_frames[frame]:
                    closed = _time(validated_frames[frame][-1]["time"]) + timedelta(seconds=seconds)
                    if not timedelta(0) <= close1 - closed < timedelta(seconds=seconds):
                        raise ValueError("Noncausal timeframe history")
        indicators = {}
        for frame, seconds, role in FRAME_ROLES:
            suffix = validated_frames[frame]
            if not suffix or _time(suffix[-1]["time"]) + timedelta(seconds=seconds) > clock:
                lines.append(f"{frame} — {role}: بيانات مكتملة غير متاحة.")
            elif len(suffix) < 22:
                lines.append(f"{frame} — {role}: تجهيز السجل ({len(suffix)}/22 شمعة متصلة).")
            else:
                indicators[frame] = multi_timeframe._indicators(suffix)
                trend = TREND_LABELS[multi_timeframe._trend(suffix, indicators[frame], strict=frame not in {"M1", "M5"})]
                lines.append(f"{frame} — {role}: ميل {trend}؛ آخر إغلاق {_time(suffix[-1]['time']) + timedelta(seconds=seconds):%H:%M} UTC.")
        has_context = len(indicators) == len(FRAME_ROLES)
        if has_context:
            observed_context = multi_timeframe._timeframe_context(validated_frames, indicators, quote["bid"], quote["ask"])
            lines.extend(context_lines({"timeframe_context": observed_context}, digits))
        lines.append("السيولة تُقدّر من نشاط الأسعار والسبريد ووقت الجلسة؛ حجم تداول فعلي وعمق السوق غير متاحين في هذه البيانات.")
        proposal = {**result, "workflow": "manual_ticket"} if type(result) is dict else {}
        qualified = mtf_runtime.eligible_payload(proposal, feed, clock)
        displayed = result
        if not qualified and result.get("state") == "signal":
            displayed = _blocked_proposal(result, clock)
        reference = "توجد إشارة Demo تجريبية — الأداء غير مثبت؛ التكاليف تقديرية، وتفاصيلها عبر /signals." if _experimental(result) else "توجد إشارة اجتازت بوابة الأدلة؛ تفاصيلها عبر /signals."
        lines.append(format_proposal(displayed, symbol=feed.get("symbol", "XAUUSD"), include_context=not has_context) if include_proposal else reference if qualified else status_text(displayed, include_context=not has_context))
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        lines.append(status_text(_status_context(result, {"state": "invalid"})))
    return "\n".join(lines)
