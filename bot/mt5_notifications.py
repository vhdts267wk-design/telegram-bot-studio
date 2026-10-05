"""Private human approvals and execution reports; this module never sends orders.

An accepted button only commits an approved request to the durable queue. The
separate, user-started local bridge applies account, volume and price guards.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException, ROUND_CEILING, ROUND_FLOOR
import hashlib
import logging
import math
from numbers import Real
import os
import re
from uuid import UUID

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.error import Forbidden, RetryAfter
from telegram.ext import CallbackQueryHandler, CommandHandler

from bot import market_monitor, market_store, trade_store


logger = logging.getLogger(__name__)
CALLBACK_PATTERN = re.compile(r"^mt5:([ar]):([0-9a-f]{32})$")
FRESH_SECONDS = 180
QUOTE_FUTURE_TOLERANCE_SECONDS = 5
OFFER_MINUTES = 5
MAX_DRIFT_R = 0.1


def _utc(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An aware timestamp is required.")
    return value.astimezone(timezone.utc)


def _id(value) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _enabled(service) -> bool:
    return service is not None and getattr(service, "trading_enabled", False) is True


def _service(context):
    return getattr(context, "bot_data", {}).get(market_monitor.SERVICE_KEY)


def _positive(value) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("A positive finite value is required.")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("A positive finite value is required.")
    return number


def _owner(device, chat_id, user_id) -> bool:
    return (
        device is not None and device.get("active", True) is True
        and device.get("owner_chat_id") == chat_id
        and device.get("owner_user_id") == user_id
    )


def _demo_policy(device) -> bool:
    """This release is explicitly authorized only for Demo at 0.01 lot."""
    try:
        return device["account_mode"] == "demo" and _positive(device["volume"]) == 0.01
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _settings_match(payload, device) -> bool:
    try:
        return (
            _demo_policy(device)
            and payload["account_mode"] == "demo"
            and _positive(payload["volume"]) == 0.01
            and payload["symbol"] == device["symbol"]
            and payload["account_mode"] == device["account_mode"]
            and _positive(payload["volume"]) == _positive(device["volume"])
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _execution_metadata(feed):
    execution = feed.get("execution")
    if not isinstance(execution, dict) or set(execution) != {"tick_size", "point", "digits", "stops_level"}:
        raise ValueError("Broker execution metadata is required.")
    digits, stops = execution["digits"], execution["stops_level"]
    if type(digits) is not int or not 0 <= digits <= 10 or type(stops) is not int or not 0 <= stops <= 1_000_000:
        raise ValueError("Invalid broker precision or minimum stops.")
    tick = Decimal(str(_positive(execution["tick_size"])))
    _positive(execution["point"])
    quantum = Decimal(1).scaleb(-digits)
    if tick < quantum or tick % quantum:
        raise ValueError("The broker tick size cannot be represented at its displayed precision.")
    return tick, digits, quantum


def _normalise_signal(result, feed):
    """Keep the actual trigger close and round protective levels outwards."""
    tick, digits, quantum = _execution_metadata(feed)
    direction = result.get("direction")
    if direction not in ("BUY", "SELL"):
        raise ValueError("An ordered BUY or SELL setup is required.")
    trigger = _utc(result["bar_time"])
    original = next((bar for bar in feed["candles"] if _utc(bar["time"]) == trigger), None)
    if original is None:
        raise ValueError("The triggering broker candle is unavailable.")
    entry = Decimal(str(_positive(original["close"])))
    if entry.quantize(quantum) != entry:
        raise ValueError("The triggering close cannot be represented at broker precision.")
    stop, target = (Decimal(str(_positive(result[key]))) for key in ("stop", "target"))
    stop = (stop / tick).to_integral_value(rounding=ROUND_FLOOR if direction == "BUY" else ROUND_CEILING) * tick
    target = (target / tick).to_integral_value(rounding=ROUND_CEILING if direction == "BUY" else ROUND_FLOOR) * tick
    if stop <= 0 or target <= 0 or not ((stop < entry < target) if direction == "BUY" else (target < entry < stop)):
        raise ValueError("Normalized broker levels must remain positive and strictly ordered.")
    payload = deepcopy(result)
    payload.update(
        entry=float(entry), stop=float(stop), target=float(target), price_digits=digits,
        original_stop_distance=float(abs(entry - stop)), execution=deepcopy(feed["execution"]),
    )
    return payload


def _offer_grid_matches(payload, feed) -> bool:
    try:
        tick, digits, quantum = _execution_metadata(feed)
        if type(payload.get("price_digits")) is not int or payload["price_digits"] != digits:
            return False
        entry, stop, target = (Decimal(str(_positive(payload[key]))) for key in ("entry", "stop", "target"))
        if entry.quantize(quantum) != entry or stop % tick or target % tick:
            return False
        ordered = stop < entry < target if payload["direction"] == "BUY" else target < entry < stop if payload["direction"] == "SELL" else False
        return ordered and Decimal(str(_positive(payload["original_stop_distance"]))) == abs(entry - stop)
    except (KeyError, TypeError, ValueError, OverflowError, DecimalException):
        return False


def _digits(payload) -> int:
    value = payload.get("price_digits", 2)
    if type(value) is not int or not 0 <= value <= 10:
        raise ValueError("Valid displayed price digits are required.")
    return value


def _fresh_feed(snapshot, device, now) -> dict | None:
    """Check source timestamps independently of repeated receipt/heartbeats."""
    try:
        if snapshot is None or device is None or device.get("active", True) is not True:
            return None
        payload = snapshot["payload"]
        if _id(payload["device_id"]) != _id(device["device_id"]):
            return None
        symbol = os.getenv("MARKET_GOLD_SYMBOL", "XAUUSD").strip()
        if device["symbol"] != symbol or payload["symbol"] != symbol:
            return None
        if not _demo_policy(device):
            return None
        _positive(device["volume"])
        # Legacy validation accepts its original schema; the device binding is
        # checked separately against the raw authenticated cached envelope.
        clean = {key: value for key, value in payload.items() if key != "device_id"}
        validated = market_monitor.validate_feed(clean, now, symbol)
        _execution_metadata(validated)
        for stamp in (snapshot["updated_at"], device["last_seen_at"]):
            age = (now - _utc(stamp)).total_seconds()
            if not 0 <= age <= FRESH_SECONDS:
                return None
        quote_age = (now - _utc(validated["quote"]["time"])).total_seconds()
        if not -QUOTE_FUTURE_TOLERANCE_SECONDS <= quote_age <= FRESH_SECONDS:
            return None
        return validated
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError, DecimalException):
        return None


def _pair_hash(args) -> str | None:
    if not isinstance(args, (list, tuple)) or not all(isinstance(arg, str) for arg in args):
        return None
    code = re.sub(r"[\s-]", "", "".join(args)).upper()
    if not re.fullmatch(r"[A-Z0-9]{8,64}", code):
        return None
    return hashlib.sha256(code.encode("ascii")).hexdigest()


async def _answer(query, text, *, alert=False):
    try:
        await query.answer(text, show_alert=alert)
    except Exception as error:
        logger.warning("MT5 callback acknowledgment failed (%s).", type(error).__name__)


def _status_text(status: str) -> str:
    return {
        "draft": "الإشارة قيد التحضير؛ حاول بعد قليل.",
        "offered": "الإشارة تنتظر اختيارك.",
        "accepted": "تمت الموافقة ووضع الطلب في قائمة التنفيذ؛ لم يتأكد تنفيذ الصفقة بعد.",
        "rejected": "تم رفض الطلب؛ لم يُرسل أمر تداول لهذا الاختيار.",
        "expired": "انتهت صلاحية الإشارة؛ لا يمكن الموافقة عليها.",
        "cancelled": "أُلغي الطلب؛ لا يمكن الموافقة عليه.",
        "executing": "الجهاز يعالج الطلب؛ انتظر تأكيد MT5 ولا تكرر الإرسال.",
        "filled": "ورد تأكيد التنفيذ من MT5؛ راجع إشعار النتيجة.",
        "failed": "فشل التنفيذ؛ راجع إشعار النتيجة.",
        "unknown": "حالة التنفيذ غير مؤكدة؛ تحقق من MT5. لا تُجرى إعادة تلقائية.",
    }.get(status, "هذا الطلب غير متاح.")


def _effective_status(offer) -> str:
    status = offer["status"]
    if status in ("filled", "failed", "unknown"):
        result = offer.get("result")
        if not isinstance(result, dict) or result.get("status") != status:
            return "unknown"
    return status


async def _show_decision(query, status: str):
    text = _status_text(status)
    await _answer(query, text)
    if status in ("draft", "offered"):
        return
    try:
        original = getattr(query.message, "text", "") or "إشارة MT5"
        await query.edit_message_text(original[:3600] + "\n\n" + text, parse_mode=None, reply_markup=None)
    except Exception as error:
        logger.warning("MT5 decision message update failed (%s).", type(error).__name__)


async def connect_mt5_command(update, context):
    """Pair a one-use short code without displaying or logging the code."""
    message, user = update.effective_message, update.effective_user
    if message is None:
        return
    if not isinstance(message, Message) or message.chat.type != "private" or user is None or user.id != message.chat.id:
        await message.reply_text("اقتران MT5 متاح لصاحب الجهاز في المحادثة الخاصة فقط.", parse_mode=None)
        return
    service = _service(context)
    if not _enabled(service):
        await message.reply_text("خدمة اقتران MT5 غير مفعلة حالياً.", parse_mode=None)
        return
    code_hash = _pair_hash(getattr(context, "args", None))
    if code_hash is None:
        await message.reply_text("استخدم /connect_mt5 ثم رمز الاقتران المؤقت الظاهر على جهازك.", parse_mode=None)
        return
    try:
        now = market_monitor.utc_now()
        device = await trade_store.pair_device(service.pool, service.bot_id, code_hash, message.chat.id, user.id, now)
        if not _owner(device, message.chat.id, user.id):
            await message.reply_text("رمز الاقتران غير صالح أو انتهت مدته أو استُخدم سابقاً.", parse_mode=None)
            return
        if not _demo_policy(device):
            await message.reply_text("هذه النسخة تسمح بحساب Demo وحجم 0.01 lot فقط؛ لم تُفعّل تنبيهات هذا الجهاز.", parse_mode=None)
            return
        await market_store.enable_subscription(service.pool, service.bot_id, message.chat.id, now)
        mode = "Demo" if device["account_mode"] == "demo" else "Real"
        volume = _positive(device["volume"])
        await message.reply_text(
            f"تم اقتران الجهاز وتفعيل التنبيهات. الحساب: {mode} | الرمز: {device['symbol']} | الحجم: {volume:g} lot.\n"
            "Accept يوافق على طلب تنفيذ عبر جهازك؛ لا يُعد الطلب منفذاً حتى يصل تأكيد MT5. Reject يرفضه. /unwatch يوقف التنبيهات ويلغي الطلبات المعلقة.",
            parse_mode=None,
        )
    except Exception as error:
        logger.warning("MT5 pairing failed (%s).", type(error).__name__)
        await message.reply_text("تعذر إكمال الاقتران حالياً؛ حاول لاحقاً دون إرسال أي كلمات مرور.", parse_mode=None)


async def decision_callback(update, context):
    """Authenticate an exact offer and commit one human decision atomically."""
    query = update.callback_query
    if query is None:
        return
    service = _service(context)
    match = CALLBACK_PATTERN.fullmatch(query.data) if isinstance(query.data, str) else None
    message, user = query.message, query.from_user
    if (
        not _enabled(service) or match is None or not isinstance(message, Message)
        or message.chat.type != "private" or user is None or user.id != message.chat.id
        or message.from_user is None or message.from_user.id != service.bot_id
        or getattr(query, "inline_message_id", None) is not None
    ):
        await _answer(query, "هذا الطلب غير متاح في هذه المحادثة.", alert=True)
        return
    try:
        offer_id = UUID(hex=match.group(2))
        offer = await trade_store.get_offer(service.pool, service.bot_id, offer_id)
        if (
            offer is None or offer.get("bot_id") != service.bot_id
            or offer.get("chat_id") != message.chat.id or offer.get("user_id") != user.id
            or offer.get("message_id") != message.message_id
        ):
            await _answer(query, "هذا الطلب غير متاح لك.", alert=True)
            return
        device = await trade_store.get_device(service.pool, service.bot_id, offer["device_id"])
        if not _owner(device, message.chat.id, user.id):
            await _answer(query, "هذا الجهاز غير مقترن بك حالياً.", alert=True)
            return
        if offer["status"] != "offered":
            await _show_decision(query, _effective_status(offer))
            return
        if not await trade_store.subscription_active(service.pool, service.bot_id, message.chat.id):
            await _show_decision(query, "cancelled")
            return
        now = market_monitor.utc_now()
        if now >= _utc(offer["expires_at"]):
            await _show_decision(query, "expired")
            return
        decision = "accepted" if match.group(1) == "a" else "rejected"
        if decision == "accepted":
            if service.source != "mt5" or not _settings_match(offer["payload"], device):
                await _answer(query, "تغير مصدر البيانات أو إعداد الجهاز؛ لا يمكن الموافقة على هذه الإشارة.", alert=True)
                return
            snapshot = await market_store.get_cache(service.pool, service.bot_id, "broker_feed")
            feed = _fresh_feed(snapshot, device, now)
            if feed is None or not _offer_grid_matches(offer["payload"], feed):
                await _answer(query, "بيانات MT5 قديمة أو الجهاز غير متصل؛ لم تتم الموافقة.", alert=True)
                return
            if await service.risk_pause(message.chat.id) is not None:
                await _answer(query, "الإشارات موقوفة مؤقتاً؛ لم تتم الموافقة.", alert=True)
                return
            # Slow storage/network work must not approve an already stale
            # snapshot. The store separately rechecks expiry/owner/consent.
            snapshot = await market_store.get_cache(service.pool, service.bot_id, "broker_feed")
            feed = _fresh_feed(snapshot, device, market_monitor.utc_now())
            if feed is None or not _offer_grid_matches(offer["payload"], feed):
                await _answer(query, "تغيرت البيانات أو أصبحت قديمة؛ لم تتم الموافقة.", alert=True)
                return
        updated = await trade_store.decide(
            service.pool, service.bot_id, offer_id, message.chat.id, user.id,
            message.message_id, decision, market_monitor.utc_now(),
        )
        if updated is None:
            latest = await trade_store.get_offer(service.pool, service.bot_id, offer_id)
            await _show_decision(query, _effective_status(latest) if latest is not None else "unavailable")
        else:
            await _show_decision(query, _effective_status(updated))
    except Exception as error:
        logger.warning("MT5 decision failed (%s).", type(error).__name__)
        await _answer(query, "تعذر حفظ القرار؛ تحقق من الحالة قبل إعادة المحاولة.", alert=True)


def _offer_text(payload, expires_at) -> str:
    mode = "Demo" if payload["account_mode"] == "demo" else "Real"
    digits = _digits(payload)
    return (
        f"إشارة MT5 تحتاج موافقتك — {payload['symbol']}\n"
        f"{payload['direction']} | الحساب: {mode} | الحجم: {payload['volume']:g} lot\n"
        f"دخول مرجعي: {payload['entry']:.{digits}f}\nوقف: {payload['stop']:.{digits}f}\nهدف: {payload['target']:.{digits}f}\n"
        f"صلاحية الموافقة حتى: {_utc(expires_at):%Y-%m-%d %H:%M:%S} UTC\n"
        "Accept يضع طلباً موافقاً عليه في قائمة التنفيذ على جهازك؛ لا يؤكد تعبئة الصفقة. Reject يرفض الطلب.\n"
        "سعر الدخول المعروض مرجعي. إذا تغير السعر بأكثر من 0.1R يُرفض التنفيذ؛ تُراجع إعدادات الحساب والحجم محلياً."
    )


async def send_offers(service, bot):
    """Create a durable draft before sending; bind buttons only on publication."""
    if not _enabled(service) or service.source != "mt5":
        return True
    try:
        now = market_monitor.utc_now()
        await trade_store.expire_offers(service.pool, service.bot_id, now)
        result, _, signal_id = await service.signals()
        if result.get("state") != "signal" or not signal_id:
            return True
        expiry = min(_utc(result["bar_time"]) + timedelta(minutes=30), now + timedelta(minutes=OFFER_MINUTES))
        if expiry <= now:
            return True
        snapshot = await market_store.get_cache(service.pool, service.bot_id, "broker_feed")
        for device in await trade_store.list_paired_devices(service.pool, service.bot_id):
            try:
                chat_id, user_id = device["owner_chat_id"], device["owner_user_id"]
                if chat_id != user_id or not _owner(device, chat_id, user_id):
                    continue
                feed = _fresh_feed(snapshot, device, now)
                if feed is None or not await trade_store.subscription_active(service.pool, service.bot_id, chat_id):
                    continue
                if await service.risk_pause(chat_id) is not None:
                    continue
                payload = _normalise_signal(result, feed)
                payload.update(
                    symbol=device["symbol"], volume=_positive(device["volume"]), account_mode=device["account_mode"],
                    max_drift_r=MAX_DRIFT_R, source_identity=f"mt5:{device['symbol']}:{_id(device['device_id'])}",
                    source_time=feed["quote"]["time"],
                )
                offer = await trade_store.create_offer(
                    service.pool, service.bot_id, device["device_id"], chat_id, user_id,
                    signal_id, payload, now, expiry,
                )
                if offer is None or offer["status"] != "draft" or _utc(offer["expires_at"]) <= market_monitor.utc_now():
                    continue
                if not await trade_store.subscription_active(service.pool, service.bot_id, chat_id):
                    continue
                offer_id = _id(offer["id"])
                markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Accept", callback_data=f"mt5:a:{offer_id.hex}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"mt5:r:{offer_id.hex}"),
                ]])
                message = await bot.send_message(chat_id, _offer_text(offer["payload"], offer["expires_at"]), parse_mode=None, reply_markup=markup)
                published = await trade_store.publish_offer(service.pool, service.bot_id, offer_id, message.message_id, market_monitor.utc_now())
                if not published:
                    try:
                        await bot.edit_message_reply_markup(chat_id, message.message_id, reply_markup=None)
                    except Exception as error:
                        logger.warning("MT5 expired offer button cleanup failed (%s).", type(error).__name__)
            except Forbidden:
                await market_store.disable_subscription(service.pool, service.bot_id, device["owner_chat_id"])
            except RetryAfter as error:
                await service.telegram_backoff(error)
                return False
            except Exception as error:
                logger.warning("MT5 offer delivery failed (%s).", type(error).__name__)
        return True
    except RetryAfter as error:
        await service.telegram_backoff(error)
        return False
    except Exception as error:
        logger.warning("MT5 offers unavailable (%s).", type(error).__name__)
        return True


def _result_text(offer) -> str:
    result = offer.get("result")
    status = result.get("status") if isinstance(result, dict) else "unknown"
    no_claim = offer.get("decided_at") is not None and offer.get("executing_at") is None
    if offer.get("status") == "expired" and no_claim:
        heading = "انتهت صلاحية الطلب بعد الموافقة وقبل أن يستلمه الجهاز؛ لم يُرسل أي أمر تداول."
    elif offer.get("status") == "cancelled" and no_claim:
        heading = "أُلغي الطلب بعد الموافقة وقبل التنفيذ؛ لم يُرسل أي أمر تداول."
    elif status == "filled" and offer.get("status") == "filled":
        heading = "✅ أكد MT5 تنفيذ الصفقة."
    elif status == "failed" and offer.get("status") == "failed":
        heading = "لم يُنفذ الطلب: أبلغ الجهاز عن فشل معالجة الطلب."
    else:
        heading = "حالة التنفيذ غير مؤكدة؛ تحقق من MT5 قبل أي إجراء. لا تُجرى إعادة تنفيذ تلقائية."
    payload = offer["payload"]
    mode = "Demo" if payload["account_mode"] == "demo" else "Real"
    text = heading + f"\n{payload['direction']} — {payload['symbol']} | {mode} | الحجم المطلوب: {_positive(payload['volume']):g} lot"
    if status == "filled" and offer.get("status") == "filled":
        for key, label in (("order_ticket", "رقم الأمر"), ("deal_ticket", "رقم العملية")):
            value = result.get(key)
            if type(value) is int and value > 0:
                text += f"\n{label}: {value}"
        if result.get("executed_at") is not None:
            try:
                text += f"\nوقت التنفيذ: {_utc(result['executed_at']):%Y-%m-%d %H:%M:%S} UTC"
            except (TypeError, ValueError, OverflowError):
                pass
    return text


async def send_results(service, bot):
    """Notify only the bound owner and acknowledge only successful delivery."""
    if not _enabled(service):
        return True
    for offer in await trade_store.list_notifications(service.pool, service.bot_id):
        try:
            if offer.get("status") in ("expired", "cancelled") and (
                offer.get("decided_at") is None or offer.get("executing_at") is not None
            ):
                continue
            device = await trade_store.get_device(service.pool, service.bot_id, offer["device_id"])
            if (
                offer.get("bot_id") != service.bot_id
                or not _owner(device, offer["chat_id"], offer["user_id"])
            ):
                continue
            await bot.send_message(offer["chat_id"], _result_text(offer), parse_mode=None)
            await trade_store.mark_notified(service.pool, service.bot_id, offer["id"], market_monitor.utc_now())
        except Forbidden:
            await market_store.disable_subscription(service.pool, service.bot_id, offer["chat_id"])
        except RetryAfter as error:
            await service.telegram_backoff(error)
            return False
        except Exception as error:
            logger.warning("MT5 execution notification failed (%s).", type(error).__name__)
    return True


def register_handlers(application):
    application.add_handler(CommandHandler("connect_mt5", connect_mt5_command))
    # Route malformed data in our namespace here too, so it receives an answer;
    # the callback itself still validates the exact action/UUID syntax.
    application.add_handler(CallbackQueryHandler(decision_callback, pattern=r"^mt5:"))
