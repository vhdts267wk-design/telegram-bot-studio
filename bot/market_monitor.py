"""Chart reports and explicitly subscribed 15-minute Telegram delivery."""

import asyncio
from datetime import datetime, timedelta, timezone
import hmac
import hashlib
import json
import logging
import math
import os
import re
from time import monotonic
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from telegram.error import Forbidden, RetryAfter
from telegram.ext import CommandHandler

from bot import chart_analysis, free_news, journal_store, market_news, market_store, mt5_api, mt5_notifications, paper_journal, paper_policy, paper_signals, reference_market, trade_store


logger = logging.getLogger(__name__)
SERVICE_KEY = "market_service"
WORKER_KEY = "market_worker"
REPORT_INTERVAL = timedelta(minutes=15)
NEWS_DAILY_LIMIT = 96
MAX_FEED_BYTES = 65536
QUOTE_FRESH_SECONDS = 180
QUOTE_CLOCK_SKEW_SECONDS = 5
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
    required = {"symbol", "timeframe", "source", "quote", "candles"}
    if not isinstance(payload, dict) or not required <= set(payload) or not set(payload) <= required | {"device_id", "execution"}:
        raise ValueError("Invalid feed")
    device_id = payload.get("device_id")
    if "device_id" in payload and (type(device_id) is not str or str(UUID(device_id)) != device_id):
        raise ValueError("Invalid market device")
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
    cleaned = {
        "symbol": symbol,
        "timeframe": "M15",
        "source": "MetaTrader 5",
        "quote": {"bid": bid, "ask": ask, "time": stamp.isoformat()},
        "candles": clean_bars,
    }
    if device_id is not None:
        cleaned["device_id"] = device_id
    if "execution" in payload:
        execution = payload["execution"]
        if type(execution) is not dict or set(execution) != {"tick_size", "point", "digits", "stops_level"}:
            raise ValueError("Invalid execution metadata")
        digits, stops = execution["digits"], execution["stops_level"]
        if type(digits) is not int or not 0 <= digits <= 10 or type(stops) is not int or not 0 <= stops <= 1000000:
            raise ValueError("Invalid execution metadata")
        cleaned["execution"] = {"tick_size": _number(execution["tick_size"]), "point": _number(execution["point"]), "digits": digits, "stops_level": stops}
    return cleaned


def _mt5_identity(payload):
    identity = "mt5:" + payload["symbol"]
    if payload.get("device_id") is not None:
        identity += ":" + str(UUID(payload["device_id"]))
    return identity


def _stamp(value):
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _pause_text(until):
    return (
        "الإشارات التجريبية موقوفة بعد 3 وقفات متتالية مرصودة ببيانات مكتملة. "
        f"إعادة التقييم بعد {_stamp(until)}. /reviews لعرض النتائج والملاحظات."
    )


def market_text(snapshot, now):
    """Explain the real broker chart; order delivery stays on its existing path."""
    if snapshot is None:
        return chart_analysis.format_chart_analysis(None, None, now, include_proposal=False)
    try:
        payload = validate_feed(snapshot["payload"], now)
        received_at = snapshot["updated_at"]
    except (KeyError, TypeError, ValueError):
        return "بيانات الأسعار المتاحة غير صالحة حالياً. يلزم تحديث مصدر الوسيط."
    result, _, _ = paper_result(snapshot, now, "mt5")
    return chart_analysis.format_chart_analysis(
        payload, result, now, received_at=received_at, include_proposal=False,
    )


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


def paper_result(snapshot, now, source):
    """Gate a paper strategy on fresh quotes and sufficiently covered real bars."""
    label = "GoldAPI — عينات سعر مرجعي مستقل"
    identity = "reference:XAUUSD"
    if source == "mt5":
        symbol = os.getenv("MARKET_GOLD_SYMBOL", "XAUUSD").strip()
        label, identity = f"MetaTrader 5 — {symbol}", "mt5:" + symbol
    if snapshot is None:
        return {"state": "warmup", "candle_count": 0, "remaining_bars": 22}, label, identity
    try:
        received = snapshot["updated_at"]
        if not isinstance(received, datetime) or received.tzinfo is None:
            raise ValueError("Invalid receipt time")
        if source == "mt5":
            payload = validate_feed(snapshot["payload"], now)
            stamp = _utc(payload["quote"]["time"])
            bars = payload["candles"]
            label = f"MetaTrader 5 — {payload['symbol']}"
            identity = _mt5_identity(payload)
        else:
            payload = snapshot["payload"]
            _number(payload["price"])
            stamp = _utc(payload["as_of"])
            observed = reference_market.aggregate_samples(payload["samples"], now)
            bars = []
            for bar in reversed(observed):
                if not bar["coverage_ok"]:
                    break
                if bars and _utc(bars[0]["time"]) - _utc(bar["time"]) != timedelta(minutes=15):
                    break
                bars.insert(0, {key: bar[key] for key in ("time", "open", "high", "low", "close")})
        # Broker tick time can lead the API clock by a few seconds. Only the
        # quote gets this bound; cache/device clocks and completed bars do not.
        quote_min_age = timedelta(seconds=-QUOTE_CLOCK_SKEW_SECONDS) if source == "mt5" else timedelta(0)
        if not (
            quote_min_age <= now - stamp <= timedelta(seconds=QUOTE_FRESH_SECONDS)
            and timedelta(0) <= now - received <= timedelta(minutes=3)
        ):
            return {"state": "stale", "candle_count": len(bars)}, label, identity
        # A missing interval ends the usable history; never fill a strategy gap.
        contiguous = []
        for bar in reversed(bars):
            if contiguous and _utc(contiguous[0]["time"]) - _utc(bar["time"]) != timedelta(minutes=15):
                break
            contiguous.insert(0, bar)
        return paper_signals.analyze_paper_signal(contiguous, now=now), label, identity
    except (KeyError, TypeError, ValueError, OverflowError):
        return {"state": "invalid", "candle_count": 0}, label, identity


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
        self.journal_enabled = False
        self.trading_enabled = False
        self.review_lock = asyncio.Lock()
        self.news_source = os.getenv("NEWS_SOURCE", "rss").strip().lower()
        if self.news_source not in {"rss", "openai"}:
            raise ValueError("Unsupported news source")
        self.openai_enabled = os.getenv("OPENAI_ENABLED", "false").strip().lower() == "true"
        self.trade_minutes = 15

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

    async def signals(self):
        if self.source == "reference":
            await self.refresh_reference()
        key = "broker_feed" if self.source == "mt5" else "reference_feed"
        snapshot = await market_store.get_cache(self.pool, self.bot_id, key)
        result, source, identity = paper_result(snapshot, utc_now(), self.source)
        signal_id = None
        if result.get("state") == "signal":
            token = "|".join((identity, result["strategy_id"], result["bar_time"], result["direction"]))
            signal_id = hashlib.sha256(token.encode()).hexdigest()
            # Preserve levels once a setup is first computed. A rolling window
            # must not silently change a previously shown paper setup.
            previous = await market_store.get_cache(self.pool, self.bot_id, "paper_setup")
            if previous is not None and previous["payload"].get("id") == signal_id:
                frozen = previous["payload"].get("result")
                if isinstance(frozen, dict) and frozen.get("state") == "signal":
                    result = frozen
            else:
                await market_store.save_cache(
                    self.pool, self.bot_id, "paper_setup",
                    {"id": signal_id, "result": result}, utc_now(),
                )
        if self.source == "mt5":
            text = chart_analysis.format_chart_proposal(
                result, symbol=source.removeprefix("MetaTrader 5 — "),
                execution_enabled=self.trading_enabled,
            )
        else:
            text = paper_signals.format_paper_signal(result, source=source)
        return result, text, signal_id

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
                        payload.get("source") == self.news_source
                        and timedelta(0) <= now - fetched < REPORT_INTERVAL
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
            try:
                if self.news_source == "rss":
                    briefing = await free_news.generate_briefing(now)
                else:
                    api_key = os.getenv("OPENAI_API_KEY", "").strip()
                    if not self.openai_enabled or not api_key:
                        raise market_news.BriefingUnavailable()
                    if not await market_store.claim_news_request(
                        self.pool, self.bot_id, now, limit=NEWS_DAILY_LIMIT
                    ):
                        raise market_news.BriefingUnavailable()
                    briefing = await market_news.generate_briefing(api_key, now)
                await market_store.save_cache(
                    self.pool, self.bot_id, "news",
                    {"source": self.news_source, "fetched_at": briefing.fetched_at.isoformat(),
                     "html_chunks": list(briefing.html_chunks)}, utc_now(),
                )
                return briefing
            except market_news.BriefingUnavailable:
                self.retry_news_at = monotonic() + 300
                raise

    async def review_trades(self):
        """Advance durable paper setups from actual post-send quote samples."""
        if not self.journal_enabled:
            return
        async with self.review_lock:
            now = utc_now()
            key = "broker_feed" if self.source == "mt5" else "reference_feed"
            snapshot = await market_store.get_cache(self.pool, self.bot_id, key)
            observations = []
            identity = "reference:XAUUSD"
            if snapshot is not None:
                try:
                    if self.source == "reference":
                        payload = snapshot["payload"]
                        reference_market.aggregate_samples(payload["samples"], now)
                        observations = payload["samples"]
                    else:
                        payload = validate_feed(snapshot["payload"], now)
                        identity = _mt5_identity(payload)
                        quote = payload["quote"]
                        observations = [{"time": quote["time"], "price": quote["bid"]}]
                except (KeyError, ValueError, TypeError, OverflowError):
                    observations = []
            for row in await journal_store.list_open(self.pool, self.bot_id):
                try:
                    trade = row["payload"]
                    points = observations if trade["source_identity"] == identity else []
                    advanced = paper_journal.advance_trade(trade, points, now)
                    if advanced != trade:
                        await journal_store.update_trade(
                            self.pool, self.bot_id, row["chat_id"], row["signal_id"],
                            advanced, now, expected_updated_at=row["updated_at"],
                        )
                except (KeyError, ValueError, TypeError, OverflowError):
                    logger.warning("Paper review skipped invalid observations or journal row.")

    async def send_reviews(self, bot):
        if not self.journal_enabled:
            return True
        for row in await journal_store.list_reviews(self.pool, self.bot_id):
            try:
                text = paper_journal.format_review(row["payload"])
                until = await self.risk_pause(row["chat_id"])
                if until is not None:
                    text += f"\n\nتم إيقاف الإشارات الجديدة مؤقتاً بعد 3 وقفات مرصودة ببيانات مكتملة؛ إعادة التقييم بعد {_stamp(until)}."
                if not await journal_store.review_active(
                    self.pool, self.bot_id, row["chat_id"], row["signal_id"]
                ):
                    continue
                await bot.send_message(row["chat_id"], text, parse_mode=None)
                await journal_store.mark_review_sent(
                    self.pool, self.bot_id, row["chat_id"], row["signal_id"], utc_now()
                )
            except Forbidden:
                await market_store.disable_subscription(self.pool, self.bot_id, row["chat_id"])
            except RetryAfter as exc:
                await self.telegram_backoff(exc)
                return False
            except Exception as exc:
                logger.warning("Paper review delivery failed (%s).", type(exc).__name__)
        return True

    async def risk_pause(self, chat_id):
        if not self.journal_enabled:
            return None
        identity = await self.source_identity()
        history = await journal_store.recent_trades(self.pool, self.bot_id, chat_id, limit=20)
        return paper_policy.pause_until(history, identity, paper_signals.STRATEGY_ID, utc_now())

    async def source_identity(self):
        if self.source != "mt5":
            return "reference:XAUUSD"
        snapshot = await market_store.get_cache(self.pool, self.bot_id, "broker_feed")
        if snapshot is not None:
            try:
                return _mt5_identity(validate_feed(snapshot["payload"], utc_now()))
            except (KeyError, ValueError, TypeError, OverflowError):
                pass
        return "mt5:" + os.getenv("MARKET_GOLD_SYMBOL", "XAUUSD").strip()

    async def telegram_backoff(self, error):
        delay = error.retry_after
        seconds = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
        delay_until = utc_now() + timedelta(seconds=max(1, min(seconds, 86400)))
        await market_store.save_cache(
            self.pool, self.bot_id, "telegram_backoff",
            {"until": delay_until.isoformat()}, utc_now(),
        )

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
        result, signal_text, signal_id = await self.signals()
        if not await may_send():
            return
        until = await self.risk_pause(chat_id)
        if until is not None:
            signal_id = None
            signal_text = _pause_text(until)
        delivery_key = f"paper_delivery:{chat_id}"
        delivered = await market_store.get_cache(self.pool, self.bot_id, delivery_key)
        if not await may_send():
            return
        repeated = signal_id is not None and delivered is not None and delivered["payload"].get("id") == signal_id
        if repeated:
            signal_text = "الإشارات التجريبية: سبق إرسال إعداد هذه الشمعة؛ لا توجد إشارة جديدة."
        trade = None
        if signal_id is not None and not repeated and self.journal_enabled:
            identity = await self.source_identity()
            trade = paper_journal.create_trade(signal_id, result, identity, utc_now(), self.trade_minutes)
            signal_text += "\n\nمدة الاختبار: 15 دقيقة من إرسال الإشارة؛ تُرسل مراجعة عند رصد الوقف أو الهدف أو انتهاء المدة."
        await bot.send_message(chat_id, signal_text, parse_mode=None)
        if trade is not None:
            # The evaluation window begins only after Telegram accepted the
            # signal. Slow sending must not include earlier price samples.
            trade = paper_journal.create_trade(signal_id, result, identity, utc_now(), self.trade_minutes)
            await journal_store.open_trade(self.pool, self.bot_id, chat_id, trade, utc_now())
        if signal_id is not None and not repeated:
            await market_store.save_cache(
                self.pool, self.bot_id, delivery_key,
                {"id": signal_id, "result": result, "sent_at": utc_now().isoformat()}, utc_now(),
            )


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
            await service.review_trades()
            backoff = await market_store.get_cache(service.pool, service.bot_id, "telegram_backoff")
            if backoff is not None:
                try:
                    if utc_now() < _utc(backoff["payload"]["until"]):
                        continue
                except (KeyError, ValueError, TypeError):
                    pass
            if not await service.send_reviews(application.bot):
                continue
            if service.trading_enabled:
                await trade_store.expire_offers(service.pool, service.bot_id, utc_now())
                if not await mt5_notifications.send_results(service, application.bot):
                    continue
                if not await mt5_notifications.send_offers(service, application.bot):
                    continue
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
                    await service.telegram_backoff(exc)
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
        await journal_store.initialize_schema(pool)
        await trade_store.initialize_schema(pool)
        service = MarketService(pool, application.bot.id)
        service.journal_enabled = True
        service.trading_enabled = (
            service.source == "mt5"
            and os.getenv("MT5_TRADING_ENABLED", "false").strip().lower() == "true"
        )
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
        chart_text = await service.market()
        _, signal_text, _ = await service.signals()
        until = await service.risk_pause(actor)
        if until is not None:
            signal_text = _pause_text(until)
        await update.effective_message.reply_text(chart_text + "\n\n" + signal_text, parse_mode=None)
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


async def signals_command(update, context):
    message = update.effective_message
    if message is None:
        return
    service = _service(update, context)
    if service is None:
        await message.reply_text("الإشارات التجريبية متاحة في المحادثة الخاصة بعد اتصال مصدر البيانات.")
        return
    actor = update.effective_chat.id
    now = monotonic()
    service.market_command_times = {a: t for a, t in service.market_command_times.items() if now - t < 5}
    if actor in service.active_users or actor in service.market_command_times or len(service.active_users) >= 2:
        await message.reply_text("انتظر قليلاً قبل طلب الإشارات التجريبية مجدداً.")
        return
    service.active_users.add(actor)
    service.market_command_times[actor] = now
    try:
        _, text, _ = await service.signals()
        until = await service.risk_pause(actor)
        if until is not None:
            text = _pause_text(until)
        await message.reply_text(text, parse_mode=None)
    finally:
        service.active_users.discard(actor)


async def reviews_command(update, context):
    message = update.effective_message
    if message is None:
        return
    service = _service(update, context)
    if service is None or not service.journal_enabled:
        await message.reply_text("سجل الصفقات متاح في المحادثة الخاصة بعد تفعيل المتابعة.")
        return
    actor = update.effective_chat.id
    if actor in service.active_users or len(service.active_users) >= 2:
        await message.reply_text("انتظر قليلاً قبل طلب السجل.")
        return
    service.active_users.add(actor)
    try:
        await service.review_trades()
        trades = await journal_store.recent_trades(service.pool, service.bot_id, actor, limit=3)
        if not trades:
            await message.reply_text("لا توجد صفقات ورقية مسجلة بعد. /watch لتفعيل الإشارات ومراجعتها.", parse_mode=None)
        for trade in trades:
            await message.reply_text(paper_journal.format_review(trade), parse_mode=None)
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
        "تم تفعيل تحليل شارت XAUUSD كل 15 دقيقة. أول تقرير خلال 15 دقيقة.\n"
        "يشمل اتجاه الشارت، الدعوم والمقاومات المرصودة، وEMA9/21 وATR14.\n"
        "اقتراح BUY أو SELL مع دخول ووقف وهدف يظهر عند تحقق الشروط فقط، "
        "بعد 22 شمعة M15 مكتملة ومتتابعة؛ وإلا يوضح سبب الانتظار.\n"
        "مدة الصفقة الورقية 15 دقيقة، مع مراجعة النتيجة وملاحظات محفوظة.\n"
        "استخدم /market للتحليل والاقتراح الآن، و/signals للاقتراح، و/reviews للنتائج، "
        "و/unwatch لإيقاف التقارير.\n"
        "الأخبار عند طلب /news فقط. التنفيذ على MT5 يتطلب موافقتك على الطلب."
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
    if service.trading_enabled:
        await trade_store.cancel_offers(service.pool, service.bot_id, update.effective_chat.id, utc_now())
    text = "تم إيقاف التقارير التلقائية. /watch لإعادة التفعيل."
    if service.trading_enabled:
        text += "\nأُلغيت طلبات MT5 المعلقة. الطلب الذي بدأ تنفيذه والصفقات المفتوحة يجب متابعتها داخل MT5؛ سيصلك إشعار نتيجة التنفيذ."
    await message.reply_text(text)


def register_handlers(application):
    mt5_notifications.register_handlers(application)
    application.add_handler(CommandHandler("market", market_command, block=False))
    application.add_handler(CommandHandler("news", news_command, block=False))
    application.add_handler(CommandHandler("signals", signals_command, block=False))
    application.add_handler(CommandHandler("reviews", reviews_command, block=False))
    application.add_handler(CommandHandler("watch", watch_command))
    application.add_handler(CommandHandler("unwatch", unwatch_command))


def install_feed_route(app, application, settings):
    mt5_api.install_routes(app, application, settings)
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
            if cleaned.get("device_id") is not None:
                device = await trade_store.get_device(
                    service.pool, service.bot_id, UUID(cleaned["device_id"])
                )
                if device is None or device["symbol"] != cleaned["symbol"]:
                    raise ValueError("Unregistered market device")
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
