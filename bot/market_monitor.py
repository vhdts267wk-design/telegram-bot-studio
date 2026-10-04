"""Market reports and explicitly subscribed 15-minute Telegram delivery."""

import asyncio
from datetime import datetime, timedelta, timezone
import hmac
import json
import logging
import math
import os
import re
from time import monotonic

from fastapi import Request
from fastapi.responses import JSONResponse
from telegram.error import Forbidden, RetryAfter
from telegram.ext import CommandHandler

from bot import market_news, market_store, reference_market


logger = logging.getLogger(__name__)
SERVICE_KEY = "market_service"
WORKER_KEY = "market_worker"
REPORT_INTERVAL = timedelta(minutes=15)
NEWS_DAILY_LIMIT = 96
MAX_FEED_BYTES = 65536
QUOTE_FRESH_SECONDS = 180
NOTICE = "للتثقيف فقط. ليس نصيحة مالية أو توصية شراء أو بيع."


def utc_now():
    return datetime.now(timezone.utc)


def _utc(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Timezone required")
    return stamp.astimezone(timezone.utc)


def _number(value):
    if type(value) not in (float, int):
        raise ValueError("Invalid price")
    try:
        numeric = float(value)
    except OverflowError:
        raise ValueError("Invalid price") from None
    if not math.isfinite(numeric) or not 0 < numeric < 1e9:
        raise ValueError("Invalid price")
    return numeric


def validate_feed(payload, now, expected_symbol=None):
    """Accept completed M15 broker bars only; never fabricate or fill gaps."""
    if not isinstance(payload, dict) or set(payload) != {
        "symbol", "timeframe", "source", "quote", "candles"
    }:
        raise ValueError("Invalid feed")
    symbol = payload["symbol"]
    if (
        not isinstance(symbol, str)
        or not re.fullmatch(r"(?:XAUUSD|GOLD)[A-Za-z0-9._#-]{0,24}", symbol, re.IGNORECASE)
        or (expected_symbol is not None and symbol != expected_symbol)
    ):
        raise ValueError("Invalid symbol")
    if payload["timeframe"] != "M15" or payload["source"] != "MetaTrader 5":
        raise ValueError("Unsupported feed")
    quote = payload["quote"]
    if not isinstance(quote, dict) or set(quote) != {"bid", "ask", "time"}:
        raise ValueError("Invalid quote")
    bid, ask = _number(quote["bid"]), _number(quote["ask"])
    stamp = _utc(quote["time"])
    if bid > ask or stamp > now + timedelta(seconds=30) or stamp < now - timedelta(days=10):
        raise ValueError("Invalid quote")
    bars = payload["candles"]
    if not isinstance(bars, list) or not 4 <= len(bars) <= 64:
        raise ValueError("Insufficient history")
    clean_bars = []
    previous = None
    for bar in bars:
        if not isinstance(bar, dict) or set(bar) != {
            "time", "open", "high", "low", "close", "tick_volume"
        }:
            raise ValueError("Invalid candle")
        start = _utc(bar["time"])
        if (
            int(start.timestamp()) % 900 or start.microsecond
            or start + timedelta(minutes=15) > now
            or start < now - timedelta(days=30)
            or (previous is not None and start <= previous)
        ):
            raise ValueError("Invalid candle time")
        prices = {name: _number(bar[name]) for name in ("open", "high", "low", "close")}
        if prices["low"] > min(prices["open"], prices["close"]) or (
            prices["high"] < max(prices["open"], prices["close"])
        ) or prices["low"] > prices["high"]:
            raise ValueError("Invalid candle range")
        volume = bar["tick_volume"]
        if type(volume) is not int or not 0 <= volume <= 10**12:
            raise ValueError("Invalid tick count")
        clean_bars.append({"time": start.isoformat(), **prices, "tick_volume": volume})
        previous = start
    if _utc(clean_bars[-1]["time"]) > stamp:
        raise ValueError("Quote predates history")
    return {
        "symbol": symbol,
        "timeframe": "M15",
        "source": "MetaTrader 5",
        "quote": {"bid": bid, "ask": ask, "time": stamp.isoformat()},
        "candles": clean_bars,
    }


def _stamp(value):
    return value.strftime("%Y-%m-%d %H:%M UTC")


def market_text(snapshot, now):
    """Deterministic observations from broker data, with original timestamps."""
    if snapshot is None:
        return (
            "متابعة XAUUSD — M15\n\n"
            "مصدر أسعار الوسيط غير متصل بعد. ربط MetaTrader 5 أو مصدر أسعار مباشر "
            "مطلوب للمتابعة من دون صور. الأخبار متاحة عبر /news."
        )
    try:
        payload = validate_feed(snapshot["payload"], now)
    except (KeyError, TypeError, ValueError):
        return "بيانات الأسعار المتاحة غير صالحة حالياً. يلزم تحديث مصدر الوسيط."
    quote = payload["quote"]
    quote_time = _utc(quote["time"])
    received_at = snapshot["updated_at"]
    bars = payload["candles"]
    last = bars[-1]
    last_start = _utc(last["time"])
    fresh_quote = (now - quote_time).total_seconds() <= QUOTE_FRESH_SECONDS
    fresh_bridge = now - received_at <= timedelta(minutes=3)
    fresh_bar = now - (last_start + timedelta(minutes=15)) <= timedelta(minutes=20)
    lines = [
        f"متابعة {payload['symbol']} — M15",
        "المصدر: MetaTrader 5، بيانات وسيطك؛ الأسعار بالدولار.",
        f"آخر سعر لدى المصدر: Bid {quote['bid']:.2f} | Ask {quote['ask']:.2f}",
        f"وقت السعر: {_stamp(quote_time)}",
        f"آخر اتصال بالمصدر: {_stamp(received_at)}",
    ]
    if not (fresh_quote and fresh_bridge and fresh_bar):
        lines.extend([
            "",
            "البيانات قديمة حالياً؛ ليست سعراً مباشراً. لا يوجد تحليل جديد لحركة السوق.",
            NOTICE,
        ])
        return "\n".join(lines)
    lines.extend([
        "",
        f"آخر شمعة مكتملة بدأت {_stamp(last_start)}:",
        f"افتتاح {last['open']:.2f} | أعلى {last['high']:.2f} | "
        f"أدنى {last['low']:.2f} | إغلاق {last['close']:.2f}",
    ])
    # A gap (weekend, unavailable terminal or missing history) ends the window.
    consecutive = [last]
    for bar in reversed(bars[:-1]):
        if _utc(consecutive[0]["time"]) - _utc(bar["time"]) != timedelta(minutes=15):
            break
        consecutive.insert(0, bar)
    if len(consecutive) >= 5:
        old = consecutive[-5]["close"]
        change = (last["close"] / old - 1) * 100
        lines.append(f"تغير الإغلاق خلال ساعة: {change:+.2f}%")
    if len(consecutive) >= 16:
        recent = consecutive[-16:]
        lines.append(
            f"نطاق آخر 4 ساعات: {min(b['low'] for b in recent):.2f} — "
            f"{max(b['high'] for b in recent):.2f}"
        )
    if len(consecutive) >= 20:
        ema = sum(b["close"] for b in consecutive[:20]) / 20
        for bar in consecutive[20:]:
            ema += (bar["close"] - ema) * (2 / 21)
        relation = "فوق" if last["close"] > ema else "تحت" if last["close"] < ema else "عند"
        lines.append(f"EMA20 للشموع المكتملة: {ema:.2f}؛ آخر إغلاق {relation} المتوسط.")
    else:
        lines.append("السجل المتصل قصير؛ تفاصيل الاتجاه الأوسع غير متاحة بعد.")
    lines.extend(["", NOTICE])
    return "\n".join(lines)


def reference_text(snapshot, now):
    """Describe actual observed reference prices, including collection warmup."""
    if snapshot is None:
        return "تعذر جلب السعر المرجعي للذهب حالياً. لم أستخدم أسعاراً مفترضة."
    try:
        payload = snapshot["payload"]
        price = _number(payload["price"])
        as_of = _utc(payload["as_of"])
        bars = reference_market.aggregate_samples(payload["samples"], now)
    except (KeyError, TypeError, ValueError, OverflowError):
        return "البيانات المرجعية غير صالحة حالياً. يلزم تحديث المصدر."
    lines = [
        "متابعة XAU/USD — M15",
        "المصدر: GoldAPI؛ سعر مرجعي مستقل للذهب بالدولار، وليس سعر وسيط تداول.",
        f"آخر سعر لدى المصدر: {price:.2f} USD",
        f"وقت السعر: {_stamp(as_of)}",
        "المصدر: https://gold-api.com/",
    ]
    age = (now - as_of).total_seconds()
    if not 0 <= age <= QUOTE_FRESH_SECONDS or (
        now - snapshot["updated_at"] > timedelta(minutes=3)
    ):
        lines.extend([
            "",
            "السعر قديم حالياً؛ ليس تحديثاً مباشراً. لا يوجد تحليل جديد لحركة السوق.",
            NOTICE,
        ])
        return "\n".join(lines)
    consecutive = []
    for bar in reversed(bars):
        if not bar["coverage_ok"]:
            break
        if consecutive and _utc(consecutive[0]["time"]) - _utc(bar["time"]) != timedelta(minutes=15):
            break
        consecutive.insert(0, bar)
    if consecutive and now - (
        _utc(consecutive[-1]["time"]) + timedelta(minutes=15)
    ) <= timedelta(minutes=20):
        last = consecutive[-1]
        lines.extend([
            "",
            "الشموع التالية مُجمَّعة من عينات سعر كل دقيقة؛ ليست شموع وسيط أو سجل سوق كامل.",
            f"آخر فترة مكتملة: {_stamp(_utc(last['time']))}",
            f"افتتاح مرصود {last['open']:.2f} | أعلى مرصود {last['high']:.2f} | "
            f"أدنى مرصود {last['low']:.2f} | إغلاق مرصود {last['close']:.2f}",
            f"عدد العينات في الفترة: {last['sample_count']}",
        ])
        if len(consecutive) >= 5:
            change = (last["close"] / consecutive[-5]["close"] - 1) * 100
            lines.append(f"تغير الإغلاق المرصود خلال ساعة: {change:+.2f}%")
        if len(consecutive) >= 16:
            recent = consecutive[-16:]
            lines.append(
                f"نطاق السعر المرصود في آخر 4 ساعات: {min(b['low'] for b in recent):.2f} — "
                f"{max(b['high'] for b in recent):.2f}"
            )
    else:
        lines.extend([
            "",
            "جارٍ جمع سجل M15 تلقائياً. يلزم اكتمال فترة بتغطية كافية قبل وصف حركتها؛ "
            "لا أملأ الفترات المفقودة أو أخمّن شموعاً.",
        ])
    lines.extend(["", NOTICE])
    return "\n".join(lines)


class MarketService:
    def __init__(self, pool, bot_id):
        self.pool = pool
        self.bot_id = bot_id
        self.news_lock = asyncio.Lock()
        self.active_users = set()
        self.command_times = {}
        self.market_command_times = {}
        self.retry_news_at = 0.0
        self.source = os.getenv("MARKET_SOURCE", "reference").strip().lower()
        if self.source not in {"reference", "mt5"}:
            raise ValueError("Unsupported market source")
        self.reference_client = None
        self.reference_lock = asyncio.Lock()
        self.next_reference_at = 0.0

    async def refresh_reference(self):
        async with self.reference_lock:
            if monotonic() < self.next_reference_at:
                return
            self.next_reference_at = monotonic() + 60
            if self.reference_client is None:
                self.reference_client = reference_market.ReferenceClient()
            now = utc_now()
            try:
                quote = await self.reference_client.fetch_quote(now)
                cached = await market_store.get_cache(self.pool, self.bot_id, "reference_feed")
                samples = []
                if cached is not None:
                    try:
                        old = cached["payload"]
                        # Discard corrupt or excessive history rather than passing it on.
                        prior = old["samples"]
                        if not isinstance(prior, list) or len(prior) > 1600:
                            raise ValueError("Invalid sample history")
                        reference_market.aggregate_samples(prior, now)
                        samples = [s for s in prior if _utc(s["time"]) >= now - timedelta(days=1)]
                    except (KeyError, TypeError, ValueError, OverflowError):
                        samples = []
                # One real upstream timestamp per observation. Re-fetching an
                # unchanged/old timestamp does not fabricate another sample.
                if (
                    timedelta(0) <= now - quote.as_of <= timedelta(seconds=QUOTE_FRESH_SECONDS)
                    and (not samples or quote.as_of > _utc(samples[-1]["time"]))
                ):
                    samples.append({"time": quote.as_of.isoformat(), "price": quote.price})
                await market_store.save_feed_cache(
                    self.pool, self.bot_id, "reference_feed",
                    {"price": quote.price, "as_of": quote.as_of.isoformat(), "samples": samples},
                    utc_now(), quote.as_of,
                )
            except reference_market.ReferenceUnavailable:
                logger.warning("Reference price update unavailable.")

    async def market(self):
        if self.source == "mt5":
            snapshot = await market_store.get_cache(self.pool, self.bot_id, "broker_feed")
            return market_text(snapshot, utc_now())
        await self.refresh_reference()
        snapshot = await market_store.get_cache(self.pool, self.bot_id, "reference_feed")
        return reference_text(snapshot, utc_now())

    async def news(self):
        async with self.news_lock:
            now = utc_now()
            cached = await market_store.get_cache(self.pool, self.bot_id, "news")
            if cached is not None:
                payload = cached["payload"]
                try:
                    fetched = _utc(payload["fetched_at"])
                    chunks = payload["html_chunks"]
                    if (
                        timedelta(0) <= now - fetched < REPORT_INTERVAL
                        and isinstance(chunks, list) and 1 <= len(chunks) <= 8
                        and all(
                            isinstance(c, str) and 0 < len(c.encode("utf-16-le")) // 2 <= 4096
                            for c in chunks
                        )
                    ):
                        return market_news.NewsBriefing(fetched, tuple(chunks))
                except (KeyError, TypeError, ValueError):
                    pass
            if monotonic() < self.retry_news_at:
                raise market_news.BriefingUnavailable()
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            if not api_key:
                raise market_news.BriefingUnavailable()
            if not await market_store.claim_news_request(
                self.pool, self.bot_id, now, limit=NEWS_DAILY_LIMIT
            ):
                raise market_news.BriefingUnavailable()
            try:
                briefing = await market_news.generate_briefing(api_key, now)
                await market_store.save_cache(
                    self.pool, self.bot_id, "news",
                    {"fetched_at": briefing.fetched_at.isoformat(),
                     "html_chunks": list(briefing.html_chunks)}, utc_now(),
                )
                return briefing
            except market_news.BriefingUnavailable:
                self.retry_news_at = monotonic() + 300
                raise

    async def send_report(self, bot, chat_id, lease_id=None):
        async def may_send():
            return lease_id is None or await market_store.delivery_active(
                self.pool, self.bot_id, chat_id, lease_id, utc_now()
            )
        text = await self.market()
        if not await may_send():
            return
        await bot.send_message(chat_id, text, parse_mode=None)
        if not await may_send():
            return
        try:
            briefing = await self.news()
        except market_news.BriefingUnavailable:
            if await may_send():
                await bot.send_message(
                    chat_id, "تعذر تحديث الأخبار حالياً. لم أستنتج أخباراً جديدة أو تأثيراً على السعر.",
                    parse_mode=None,
                )
            return
        await send_news(bot, chat_id, briefing, may_send=may_send)


async def send_news(bot, chat_id, briefing, may_send=None):
    heading = (
        "أخبار السياسة والاقتصاد المرتبطة بالذهب\n"
        f"آخر بحث: {_stamp(briefing.fetched_at)}\n"
        "وقت البحث مختلف عن وقت نشر الخبر المذكور في التقرير."
    )
    if may_send is not None and not await may_send():
        return
    await bot.send_message(chat_id, heading, parse_mode=None)
    for chunk in briefing.html_chunks:
        if may_send is not None and not await may_send():
            return
        await bot.send_message(chat_id, chunk, parse_mode="HTML", disable_web_page_preview=True)


async def _worker(application, service):
    while True:
        await asyncio.sleep(60)
        if not application.running:
            continue
        try:
            if service.source == "reference" and await market_store.has_subscriptions(
                service.pool, service.bot_id
            ):
                await service.refresh_reference()
            backoff = await market_store.get_cache(service.pool, service.bot_id, "telegram_backoff")
            if backoff is not None:
                try:
                    if utc_now() < _utc(backoff["payload"]["until"]):
                        continue
                except (KeyError, ValueError, TypeError):
                    pass
            due = await market_store.claim_due(service.pool, service.bot_id, utc_now(), limit=5)
            for subscription in due:
                chat_id, lease_id = subscription["chat_id"], subscription["lease_id"]
                try:
                    # Re-check a current claim: /unwatch must stop queued reports.
                    if not await market_store.delivery_active(
                        service.pool, service.bot_id, chat_id, lease_id, utc_now()
                    ):
                        continue
                    await service.send_report(application.bot, chat_id, lease_id)
                    await market_store.mark_delivered(
                        service.pool, service.bot_id, chat_id, lease_id, utc_now()
                    )
                except Forbidden:
                    await market_store.disable_subscription(
                        service.pool, service.bot_id, chat_id
                    )
                except RetryAfter as exc:
                    delay = exc.retry_after
                    seconds = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
                    delay_until = utc_now() + timedelta(seconds=max(1, min(seconds, 86400)))
                    await market_store.save_cache(
                        service.pool, service.bot_id, "telegram_backoff",
                        {"until": delay_until.isoformat()}, utc_now(),
                    )
                    await market_store.release_delivery(
                        service.pool, service.bot_id, chat_id, lease_id, utc_now()
                    )
                    break
                except Exception as exc:
                    logger.warning("Market report delivery failed (%s).", type(exc).__name__)
                    await market_store.release_delivery(
                        service.pool, service.bot_id, chat_id, lease_id, utc_now()
                    )
        except Exception as exc:
            logger.warning("Market monitor cycle failed (%s).", type(exc).__name__)


async def setup(application):
    pool = application.bot_data.get("db")
    if pool is None:
        return
    try:
        await market_store.initialize_schema(pool)
        service = MarketService(pool, application.bot.id)
        application.bot_data[SERVICE_KEY] = service
        # Do not use Application.create_task for an endless worker: stop() waits
        # for those tasks. This task is explicitly canceled before client shutdown.
        application.bot_data[WORKER_KEY] = asyncio.create_task(_worker(application, service))
    except Exception as exc:
        logger.warning("Market features unavailable (%s).", type(exc).__name__)


async def stop(application):
    data = getattr(application, "bot_data", {})
    task = data.pop(WORKER_KEY, None)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def close(application):
    await stop(application)
    service = getattr(application, "bot_data", {}).get(SERVICE_KEY)
    if service is not None and service.reference_client is not None:
        await service.reference_client.aclose()
        service.reference_client = None


def _service(update, context):
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None or chat.type != "private":
        return None
    return context.bot_data.get(SERVICE_KEY)


async def market_command(update, context):
    if update.effective_message is None:
        return
    service = _service(update, context)
    if service is None:
        await update.effective_message.reply_text("المتابعة متاحة في المحادثة الخاصة بعد إعداد مصدر البيانات.")
        return
    actor = update.effective_chat.id
    now = monotonic()
    service.market_command_times = {
        a: t for a, t in service.market_command_times.items() if now - t < 5
    }
    if actor in service.active_users or actor in service.market_command_times or len(service.active_users) >= 2:
        await update.effective_message.reply_text("المتابعة مشغولة حالياً. انتظر قليلاً.")
        return
    service.active_users.add(actor)
    service.market_command_times[actor] = now
    try:
        await update.effective_message.reply_text(await service.market(), parse_mode=None)
    finally:
        service.active_users.discard(actor)


async def news_command(update, context):
    message = update.effective_message
    if message is None:
        return
    service = _service(update, context)
    if service is None:
        await message.reply_text("الأخبار متاحة في المحادثة الخاصة بعد تفعيل خدمة المتابعة.")
        return
    actor = update.effective_chat.id
    now = monotonic()
    service.command_times = {a: t for a, t in service.command_times.items() if now - t < 30}
    if actor in service.active_users or actor in service.command_times or len(service.active_users) >= 2:
        await message.reply_text("انتظر قليلاً قبل طلب تقرير أخبار آخر.")
        return
    service.active_users.add(actor)
    service.command_times[actor] = now
    try:
        try:
            briefing = await service.news()
        except market_news.BriefingUnavailable:
            await message.reply_text(
                "تعذر تحديث الأخبار الآن. قد تكون الخدمة غير متاحة أو وصل حد البحث اليومي. جرّب لاحقاً."
            )
            return
        await send_news(context.bot, actor, briefing)
    finally:
        service.active_users.discard(actor)


async def watch_command(update, context):
    message = update.effective_message
    if message is None:
        return
    service = _service(update, context)
    if service is None:
        await message.reply_text("التقارير التلقائية تتطلب محادثة خاصة وقاعدة بيانات متصلة.")
        return
    await market_store.enable_subscription(
        service.pool, service.bot_id, update.effective_chat.id, utc_now()
    )
    await message.reply_text(
        "تم تفعيل تقرير XAUUSD والأخبار كل 15 دقيقة. أول تقرير خلال 15 دقيقة.\n"
        "يعرض السعر المرجعي للذهب وسجل M15 الذي يُجمع تلقائياً، وأخباراً مع روابط وتواريخ.\n"
        "استخدم /market أو /news الآن، و/unwatch لإيقاف التقارير.\n"
        "البحث مشترك ومحدود بـ96 محاولة يومياً، ويستهلك رصيد OpenAI."
    )


async def unwatch_command(update, context):
    message = update.effective_message
    if message is None:
        return
    service = _service(update, context)
    if service is None:
        await message.reply_text("خدمة المتابعة غير متاحة حالياً.")
        return
    await market_store.disable_subscription(
        service.pool, service.bot_id, update.effective_chat.id
    )
    await message.reply_text("تم إيقاف التقارير التلقائية. /watch لإعادة التفعيل.")


def register_handlers(application):
    application.add_handler(CommandHandler("market", market_command, block=False))
    application.add_handler(CommandHandler("news", news_command, block=False))
    application.add_handler(CommandHandler("watch", watch_command))
    application.add_handler(CommandHandler("unwatch", unwatch_command))


def install_feed_route(app, application, settings):
    async def ingest(request: Request):
        key = getattr(settings, "market_bridge_key", "")
        if len(key) < 32:
            return JSONResponse({"detail": "Market feed is not configured"}, status_code=404)
        expected = ("Bearer " + key).encode("utf-8")
        supplied = request.headers.get("authorization", "").encode("utf-8")
        if not hmac.compare_digest(expected, supplied):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        service = application.bot_data.get(SERVICE_KEY)
        if service is None:
            return JSONResponse({"detail": "Market service unavailable"}, status_code=503)
        size, parts = 0, []
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_FEED_BYTES:
                return JSONResponse({"detail": "Payload too large"}, status_code=413)
            parts.append(chunk)
        try:
            payload = json.loads(b"".join(parts))
            cleaned = validate_feed(
                payload, utc_now(), os.getenv("MARKET_GOLD_SYMBOL", "XAUUSD").strip()
            )
        except (ValueError, TypeError, UnicodeDecodeError, OverflowError):
            return JSONResponse({"detail": "Invalid M15 broker data"}, status_code=422)
        accepted = await market_store.save_feed_cache(
            service.pool, service.bot_id, "broker_feed", cleaned, utc_now(),
            _utc(cleaned["quote"]["time"]),
        )
        if not accepted:
            return JSONResponse({"detail": "Older broker snapshot"}, status_code=409)
        return JSONResponse({"ok": True})

    app.add_api_route("/api/market/feed", ingest, methods=["POST"])
