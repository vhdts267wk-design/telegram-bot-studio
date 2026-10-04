"""Market monitoring contracts, using local ASGI and mocked persistence only."""

import asyncio
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from bot import market_monitor as monitor
from bot import market_news


NOW = datetime(2026, 10, 4, 12, 30, tzinfo=timezone.utc)
BRIDGE_KEY = "synthetic-feed-key-" + "x" * 32


def broker_feed(count=24):
    bars = []
    for index in range(count):
        price = 2700 + index
        bars.append({
            "time": (NOW - timedelta(minutes=15 * (count - index))).isoformat(),
            "open": price,
            "high": price + 2,
            "low": price - 1,
            "close": price + 1,
            "tick_volume": 100 + index,
        })
    return {
        "symbol": "XAUUSD",
        "timeframe": "M15",
        "source": "MetaTrader 5",
        "quote": {"bid": 2724.0, "ask": 2724.2, "time": (NOW - timedelta(seconds=20)).isoformat()},
        "candles": bars,
    }


def briefing(stamp=NOW):
    return market_news.NewsBriefing(
        stamp, ('خبر اقتصادي موثق <a href="https://www.federalreserve.gov/test">المصدر</a>',)
    )


def cached_news(stamp=NOW, chunks=None):
    return {
        "payload": {
            "source": "openai",
            "fetched_at": stamp.isoformat(),
            "html_chunks": list(briefing(stamp).html_chunks) if chunks is None else chunks,
        },
        "updated_at": stamp,
    }


def reference_samples(periods=20, *, sparse_last=False):
    start = NOW - timedelta(minutes=15 * periods)
    samples = []
    for period in range(periods):
        minutes = range(9) if sparse_last and period == periods - 1 else range(15)
        for minute in minutes:
            samples.append({
                "time": (start + timedelta(minutes=period * 15 + minute)).isoformat(),
                "price": 2700.0 + (period * 15 + minute) * 0.05,
            })
    return samples


class BrokerFeedTests(unittest.TestCase):
    def test_valid_completed_broker_history_is_preserved_and_normalized(self):
        original = broker_feed()
        result = monitor.validate_feed(original, NOW)
        self.assertEqual(result["symbol"], "XAUUSD")
        self.assertEqual(result["timeframe"], "M15")
        self.assertEqual(len(result["candles"]), 24)
        self.assertEqual(result["candles"][0]["close"], 2701.0)
        self.assertIsInstance(result["candles"][0]["close"], float)
        self.assertEqual(result["quote"]["time"], original["quote"]["time"])
        self.assertEqual(original["candles"][0]["close"], 2701)

    def test_rejects_bad_shapes_values_times_ranges_and_tick_counts(self):
        cases = []
        for key, value in (("timeframe", "M5"), ("source", "Unverified"), ("symbol", "<XAUUSD>")):
            body = broker_feed()
            body[key] = value
            cases.append(body)
        body = broker_feed()
        body["unexpected"] = True
        cases.append(body)
        for value in (True, 0, -1, float("nan"), float("inf"), "2700"):
            body = broker_feed()
            body["quote"]["bid"] = value
            cases.append(body)
        body = broker_feed()
        body["quote"]["bid"] = body["quote"]["ask"] + 1
        cases.append(body)
        for stamp in (NOW + timedelta(minutes=1), NOW - timedelta(days=11)):
            body = broker_feed()
            body["quote"]["time"] = stamp.isoformat()
            cases.append(body)
        body = broker_feed()
        body["quote"]["time"] = "2026-10-04T12:30:00"
        cases.append(body)
        body = broker_feed()
        body["candles"] = body["candles"][:3]
        cases.append(body)
        body = broker_feed()
        body["candles"][1]["time"] = body["candles"][0]["time"]
        cases.append(body)
        body = broker_feed()
        body["candles"][-1]["time"] = (NOW - timedelta(minutes=14)).isoformat()
        cases.append(body)
        for field, value in (("high", 1), ("low", 9999), ("tick_volume", True), ("tick_volume", -1)):
            body = broker_feed()
            body["candles"][0][field] = value
            cases.append(body)
        for index, body in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ValueError):
                monitor.validate_feed(body, NOW)

    def test_candle_must_already_be_complete_without_quote_clock_skew_grace(self):
        # The last M15 bar closes in ten seconds, so it is still forming.
        with self.assertRaises(ValueError):
            monitor.validate_feed(broker_feed(), NOW - timedelta(seconds=10))

    def test_broker_symbol_must_match_configured_gold_instrument(self):
        feed = broker_feed()
        feed["symbol"] = "EURUSD"
        with self.assertRaises(ValueError):
            monitor.validate_feed(feed, NOW, "XAUUSD")
        feed["symbol"] = "XAUUSD.m"
        self.assertEqual(monitor.validate_feed(feed, NOW, "XAUUSD.m")["symbol"], "XAUUSD.m")
        with self.assertRaises(ValueError):
            monitor.validate_feed(feed, NOW, "XAUUSD")

    def test_stale_quote_bridge_or_history_cannot_generate_fresh_trend_claims(self):
        snapshots = []
        body = broker_feed()
        body["quote"]["time"] = (NOW - timedelta(minutes=4)).isoformat()
        snapshots.append({"payload": body, "updated_at": NOW})
        snapshots.append({"payload": broker_feed(), "updated_at": NOW - timedelta(minutes=4)})
        body = broker_feed()
        for bar in body["candles"]:
            bar["time"] = (monitor._utc(bar["time"]) - timedelta(hours=1)).isoformat()
        snapshots.append({"payload": body, "updated_at": NOW})
        for snapshot in snapshots:
            with self.subTest(snapshot=snapshot):
                text = monitor.market_text(snapshot, NOW)
                self.assertIn("قديمة", text)
                self.assertIn("ليست سعراً مباشراً", text)
                self.assertNotIn("EMA20", text)
                self.assertNotIn("تغير الإغلاق", text)
                self.assertNotIn("نطاق آخر", text)

    def test_gap_ends_derived_window_instead_of_filling_missing_history(self):
        feed = broker_feed()
        feed["candles"].pop(-5)
        text = monitor.market_text({"payload": feed, "updated_at": NOW}, NOW)
        self.assertIn("السجل المتصل قصير", text)
        self.assertNotIn("EMA20", text)
        self.assertNotIn("تغير الإغلاق", text)
        self.assertNotIn("نطاق آخر", text)
        text = monitor.market_text({"payload": broker_feed(), "updated_at": NOW}, NOW)
        self.assertIn("EMA20", text)
        self.assertIn("تغير الإغلاق", text)
        self.assertIn("نطاق آخر", text)

    def test_corrupt_persisted_huge_number_yields_unavailable_text(self):
        feed = broker_feed()
        feed["quote"]["bid"] = 10**400
        text = monitor.market_text({"payload": feed, "updated_at": NOW}, NOW)
        self.assertIn("غير صالحة", text)
        self.assertNotIn("EMA20", text)


class MarketServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock_patch = patch.object(monitor, "utc_now", return_value=NOW)
        self.env_patch = patch.dict(
            monitor.os.environ,
            {"OPENAI_API_KEY": "synthetic-test-key", "MARKET_SOURCE": "mt5", "NEWS_SOURCE": "openai", "OPENAI_ENABLED": "true"},
            clear=True,
        )
        self.clock_patch.start()
        self.env_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.addCleanup(self.env_patch.stop)
        self.service = monitor.MarketService(object(), 9901)

    async def test_fresh_persistent_cache_survives_service_recreation_without_provider_cost(self):
        stored = {"news": None}

        async def read_cache(pool, bot_id, key):
            return copy.deepcopy(stored[key])

        async def write_cache(pool, bot_id, key, payload, updated_at):
            stored[key] = {"payload": payload, "updated_at": updated_at}

        generate = AsyncMock(return_value=briefing())
        claim = AsyncMock(return_value=True)
        with patch.object(monitor.market_store, "get_cache", side_effect=read_cache), patch.object(
            monitor.market_store, "save_cache", side_effect=write_cache
        ), patch.object(monitor.market_store, "claim_news_request", claim), patch.object(
            monitor.market_news, "generate_briefing", generate
        ):
            first = await self.service.news()
            self.clock_patch.return_value = NOW + timedelta(minutes=5)
            recreated = monitor.MarketService(self.service.pool, 9901)
            second = await recreated.news()
        self.assertEqual(first, second)
        generate.assert_awaited_once_with("synthetic-test-key", NOW)
        claim.assert_awaited_once_with(self.service.pool, 9901, NOW, limit=96)

    async def test_simultaneous_news_requests_share_one_refresh_and_claim(self):
        started, proceed = asyncio.Event(), asyncio.Event()
        saved = None

        async def read_cache(*args):
            return saved

        async def write_cache(pool, bot_id, key, payload, stamp):
            nonlocal saved
            saved = {"payload": payload, "updated_at": stamp}

        async def generate(*args):
            started.set()
            await proceed.wait()
            return briefing()

        with patch.object(monitor.market_store, "get_cache", side_effect=read_cache), patch.object(
            monitor.market_store, "save_cache", side_effect=write_cache
        ), patch.object(monitor.market_store, "claim_news_request", new_callable=AsyncMock, return_value=True) as claim, patch.object(
            monitor.market_news, "generate_briefing", side_effect=generate
        ) as provider:
            first = asyncio.create_task(self.service.news())
            await asyncio.wait_for(started.wait(), 1)
            second = asyncio.create_task(self.service.news())
            proceed.set()
            results = await asyncio.gather(first, second)
        self.assertEqual(results, [briefing(), briefing()])
        self.assertEqual(claim.await_count, 1)
        self.assertEqual(provider.call_count, 1)

    async def test_daily_limit_is_claimed_before_each_uncached_call_and_denial_stops_provider(self):
        remaining = 96

        async def claim(pool, bot_id, now, *, limit):
            nonlocal remaining
            self.assertEqual(limit, 96)
            if remaining:
                remaining -= 1
                return True
            return False

        with patch.object(monitor.market_store, "get_cache", new_callable=AsyncMock, return_value=None), patch.object(
            monitor.market_store, "save_cache", new_callable=AsyncMock
        ), patch.object(monitor.market_store, "claim_news_request", side_effect=claim), patch.object(
            monitor.market_news, "generate_briefing", new_callable=AsyncMock, return_value=briefing()
        ) as provider:
            for _ in range(96):
                await self.service.news()
            with self.assertRaises(market_news.BriefingUnavailable):
                await self.service.news()
        self.assertEqual(provider.await_count, 96)

    async def test_expired_future_and_oversized_unicode_cache_are_refreshed(self):
        cases = (
            cached_news(NOW - timedelta(minutes=16)),
            cached_news(NOW + timedelta(seconds=1)),
            cached_news(chunks=["🟡" * 2100]),
        )
        for cached in cases:
            with self.subTest(cached=cached), patch.object(
                monitor.market_store, "get_cache", new_callable=AsyncMock, return_value=cached
            ), patch.object(monitor.market_store, "save_cache", new_callable=AsyncMock), patch.object(
                monitor.market_store, "claim_news_request", new_callable=AsyncMock, return_value=True
            ), patch.object(monitor.market_news, "generate_briefing", new_callable=AsyncMock, return_value=briefing()) as provider:
                result = await self.service.news()
                self.assertEqual(result, briefing())
                provider.assert_awaited_once()

    async def test_provider_failure_uses_backoff_and_does_not_cache_false_freshness(self):
        clock = {"value": 100.0}
        with patch.object(monitor, "monotonic", side_effect=lambda: clock["value"]), patch.object(
            monitor.market_store, "get_cache", new_callable=AsyncMock, return_value=None
        ), patch.object(monitor.market_store, "save_cache", new_callable=AsyncMock) as save, patch.object(
            monitor.market_store, "claim_news_request", new_callable=AsyncMock, return_value=True
        ) as claim, patch.object(monitor.market_news, "generate_briefing", new_callable=AsyncMock) as provider:
            provider.side_effect = [market_news.BriefingUnavailable(), briefing()]
            with self.assertRaises(market_news.BriefingUnavailable):
                await self.service.news()
            save.assert_not_awaited()
            clock["value"] = 399.0
            with self.assertRaises(market_news.BriefingUnavailable):
                await self.service.news()
            self.assertEqual(claim.await_count, 1)
            self.assertEqual(provider.await_count, 1)
            clock["value"] = 400.0
            self.assertEqual(await self.service.news(), briefing())
            self.assertEqual(claim.await_count, 2)
            save.assert_awaited_once()

    async def test_missing_key_does_not_claim_budget_or_request_provider(self):
        with patch.dict(monitor.os.environ, {}, clear=True), patch.object(
            monitor.market_store, "get_cache", new_callable=AsyncMock, return_value=None
        ), patch.object(monitor.market_store, "claim_news_request", new_callable=AsyncMock) as claim, patch.object(
            monitor.market_news, "generate_briefing", new_callable=AsyncMock
        ) as provider:
            with self.assertRaises(market_news.BriefingUnavailable):
                await self.service.news()
            claim.assert_not_awaited()
            provider.assert_not_awaited()

    async def test_cancelled_news_command_releases_admission(self):
        entered = asyncio.Event()

        async def pending_news():
            entered.set()
            await asyncio.Event().wait()

        self.service.news = pending_news
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(type="private", id=4401))
        context = SimpleNamespace(bot_data={monitor.SERVICE_KEY: self.service}, bot=SimpleNamespace(send_message=AsyncMock()))
        task = asyncio.create_task(monitor.news_command(update, context))
        await asyncio.wait_for(entered.wait(), 1)
        self.assertIn(4401, self.service.active_users)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.service.active_users, set())
        context.bot.send_message.assert_not_awaited()

    async def test_subscription_changes_only_apply_in_private_chat(self):
        context = SimpleNamespace(bot_data={monitor.SERVICE_KEY: self.service})
        with patch.object(monitor.market_store, "enable_subscription", new_callable=AsyncMock) as enable, patch.object(
            monitor.market_store, "disable_subscription", new_callable=AsyncMock
        ) as disable:
            for kind in ("group", "supergroup", "channel"):
                update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()), effective_chat=SimpleNamespace(type=kind, id=-100))
                await monitor.watch_command(update, context)
                await monitor.unwatch_command(update, context)
            enable.assert_not_awaited()
            disable.assert_not_awaited()
            private = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()), effective_chat=SimpleNamespace(type="private", id=4401))
            await monitor.watch_command(private, context)
            await monitor.unwatch_command(private, context)
            enable.assert_awaited_once_with(self.service.pool, 9901, 4401, NOW)
            disable.assert_awaited_once_with(self.service.pool, 9901, 4401)

    async def test_worker_rechecks_subscription_before_sending_queued_report(self):
        application = SimpleNamespace(running=True, bot=object())
        self.service.send_report = AsyncMock()
        subscription = {"chat_id": 4401, "lease_id": "synthetic-lease"}
        with patch.object(monitor.asyncio, "sleep", new_callable=AsyncMock, side_effect=[None, asyncio.CancelledError]), patch.object(
            monitor.market_store, "get_cache", new_callable=AsyncMock, return_value=None
        ), patch.object(
            monitor.market_store, "claim_due", new_callable=AsyncMock, return_value=[subscription]
        ) as due, patch.object(monitor.market_store, "delivery_active", new_callable=AsyncMock, return_value=False) as active, patch.object(
            monitor.market_store, "mark_delivered", new_callable=AsyncMock
        ) as mark:
            with self.assertRaises(asyncio.CancelledError):
                await monitor._worker(application, self.service)
            self.service.send_report.assert_not_awaited()
            mark.assert_not_awaited()
            due.assert_awaited_once_with(self.service.pool, 9901, NOW, limit=5)
            active.assert_awaited_once_with(self.service.pool, 9901, 4401, "synthetic-lease", NOW)

    async def test_unwatch_while_provider_runs_prevents_remaining_news_messages(self):
        entered, proceed = asyncio.Event(), asyncio.Event()
        enabled = {"value": True}

        async def get_news():
            entered.set()
            await proceed.wait()
            return briefing()

        self.service.market = AsyncMock(return_value="بيانات الأسعار التجريبية")
        self.service.signals = AsyncMock(return_value=({"state": "warmup"}, "جارٍ جمع بيانات الإشارة", None))
        self.service.news = get_news
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(monitor.market_store, "delivery_active", new_callable=AsyncMock, side_effect=lambda *args: enabled["value"]), patch.object(monitor.market_store, "get_cache", new_callable=AsyncMock, return_value=None):
            task = asyncio.create_task(self.service.send_report(bot, 4401, "synthetic-lease"))
            await asyncio.wait_for(entered.wait(), 1)
            enabled["value"] = False
            proceed.set()
            await task
        self.assertEqual(bot.send_message.await_count, 2)
        bot.send_message.assert_any_await(4401, "بيانات الأسعار التجريبية", parse_mode=None)
        bot.send_message.assert_any_await(4401, "جارٍ جمع بيانات الإشارة", parse_mode=None)

    async def test_stop_cancels_owned_worker_without_waiting_forever(self):
        task = asyncio.create_task(asyncio.Event().wait())
        application = SimpleNamespace(bot_data={monitor.WORKER_KEY: task})
        await asyncio.wait_for(monitor.stop(application), 1)
        self.assertTrue(task.cancelled())
        self.assertNotIn(monitor.WORKER_KEY, application.bot_data)


class ReferenceCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = {"now": NOW, "monotonic": 100.0}
        self.env_patch = patch.dict(monitor.os.environ, {"MARKET_SOURCE": "reference"}, clear=True)
        self.utc_patch = patch.object(monitor, "utc_now", side_effect=lambda: self.clock["now"])
        self.monotonic_patch = patch.object(monitor, "monotonic", side_effect=lambda: self.clock["monotonic"])
        for replacement in (self.env_patch, self.utc_patch, self.monotonic_patch):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.service = monitor.MarketService(object(), 9901)

    async def test_minute_cadence_does_not_turn_repeated_or_stale_quote_into_new_samples(self):
        quote = monitor.reference_market.Quote(price=2720.0, as_of=NOW - timedelta(seconds=20))
        upstream = SimpleNamespace(fetch_quote=AsyncMock(return_value=quote), aclose=AsyncMock())
        self.service.reference_client = upstream
        saved = {"value": None}

        async def read_cache(pool, bot_id, key):
            self.assertEqual(key, "reference_feed")
            return copy.deepcopy(saved["value"])

        async def save_feed(pool, bot_id, key, payload, received, source_time):
            self.assertEqual(key, "reference_feed")
            self.assertEqual(source_time, quote.as_of)
            saved["value"] = {"payload": copy.deepcopy(payload), "updated_at": received}
            return True

        with patch.object(monitor.market_store, "get_cache", side_effect=read_cache), patch.object(
            monitor.market_store, "save_feed_cache", side_effect=save_feed
        ):
            await self.service.refresh_reference()
            self.assertEqual(upstream.fetch_quote.await_count, 1)
            self.clock.update(now=NOW + timedelta(seconds=59), monotonic=159.0)
            await self.service.refresh_reference()
            self.assertEqual(upstream.fetch_quote.await_count, 1)
            for minutes in (1, 2, 3):
                self.clock.update(now=NOW + timedelta(minutes=minutes), monotonic=100.0 + minutes * 60)
                await self.service.refresh_reference()
            self.assertEqual(upstream.fetch_quote.await_count, 4)
        payload = saved["value"]["payload"]
        self.assertEqual(payload["as_of"], quote.as_of.isoformat())
        self.assertEqual(payload["samples"], [{"time": quote.as_of.isoformat(), "price": quote.price}])
        text = monitor.reference_text(saved["value"], self.clock["now"])
        self.assertIn("قديم", text)
        self.assertNotIn("آخر فترة مكتملة", text)
        self.assertNotIn("تغير الإغلاق", text)

    async def test_worker_collects_reference_every_minute_even_without_a_due_report(self):
        application = SimpleNamespace(running=True, bot=object())
        self.service.refresh_reference = AsyncMock()
        self.service.send_report = AsyncMock()
        with patch.object(monitor.asyncio, "sleep", new_callable=AsyncMock, side_effect=[None, asyncio.CancelledError]) as pause, patch.object(
            monitor.market_store, "has_subscriptions", new_callable=AsyncMock, return_value=True
        ) as subscribed, patch.object(monitor.market_store, "get_cache", new_callable=AsyncMock, return_value=None), patch.object(
            monitor.market_store, "claim_due", new_callable=AsyncMock, return_value=[]
        ) as due:
            with self.assertRaises(asyncio.CancelledError):
                await monitor._worker(application, self.service)
        self.assertEqual([call.args for call in pause.await_args_list], [(60,), (60,)])
        subscribed.assert_awaited_once_with(self.service.pool, 9901)
        self.service.refresh_reference.assert_awaited_once()
        due.assert_awaited_once_with(self.service.pool, 9901, NOW, limit=5)
        self.service.send_report.assert_not_awaited()


class ReferenceReportTests(unittest.TestCase):
    def snapshot(self, *, samples=None, as_of=None, updated_at=NOW):
        return {
            "payload": {
                "price": 2720.0,
                "as_of": (NOW - timedelta(seconds=20) if as_of is None else as_of).isoformat(),
                "samples": reference_samples() if samples is None else samples,
            },
            "updated_at": updated_at,
        }

    def test_partial_latest_period_suppresses_m15_and_derived_movement_claims(self):
        text = monitor.reference_text(self.snapshot(samples=reference_samples(sparse_last=True)), NOW)
        self.assertIn("جارٍ جمع سجل M15", text)
        self.assertNotIn("آخر فترة مكتملة", text)
        self.assertNotIn("افتتاح مرصود", text)
        self.assertNotIn("تغير الإغلاق", text)
        self.assertNotIn("نطاق السعر المرصود", text)
        complete = monitor.reference_text(self.snapshot(), NOW)
        self.assertIn("آخر فترة مكتملة", complete)
        self.assertIn("تغير الإغلاق المرصود", complete)
        self.assertIn("نطاق السعر المرصود", complete)
        self.assertIn("ليست شموع وسيط", complete)

    def test_old_quote_or_old_received_cache_cannot_enable_indicators_despite_full_history(self):
        for snapshot in (
            self.snapshot(as_of=NOW - timedelta(days=2)),
            self.snapshot(updated_at=NOW - timedelta(minutes=4)),
        ):
            with self.subTest(snapshot=snapshot):
                text = monitor.reference_text(snapshot, NOW)
                self.assertIn("قديم", text)
                self.assertIn("ليس تحديثاً مباشراً", text)
                self.assertNotIn("آخر فترة مكتملة", text)
                self.assertNotIn("تغير الإغلاق", text)
                self.assertNotIn("نطاق السعر المرصود", text)


class FeedRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = monitor.MarketService(object(), 9901)
        self.application = SimpleNamespace(bot_data={monitor.SERVICE_KEY: self.service})
        self.settings = SimpleNamespace(market_bridge_key=BRIDGE_KEY)
        self.app = FastAPI()
        monitor.install_feed_route(self.app, self.application, self.settings)
        self.clock_patch = patch.object(monitor, "utc_now", return_value=NOW)
        self.env_patch = patch.dict(monitor.os.environ, {"MARKET_GOLD_SYMBOL": "XAUUSD"}, clear=True)
        self.clock_patch.start()
        self.env_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.addCleanup(self.env_patch.stop)

    async def post(self, *, body=None, auth=BRIDGE_KEY):
        headers = {} if auth is None else {"authorization": "Bearer " + auth}
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://local.test") as client:
            if body is None:
                return await client.post("/api/market/feed", json=broker_feed(), headers=headers)
            return await client.post("/api/market/feed", content=body, headers=headers)

    async def test_unconfigured_unauthorized_and_unavailable_routes_do_not_write(self):
        with patch.object(monitor.market_store, "save_feed_cache", new_callable=AsyncMock) as save:
            self.settings.market_bridge_key = "short"
            self.assertEqual((await self.post()).status_code, 404)
            self.settings.market_bridge_key = BRIDGE_KEY
            self.assertEqual((await self.post(auth=None)).status_code, 401)
            self.assertEqual((await self.post(auth="wrong-key")).status_code, 401)
            self.application.bot_data.clear()
            self.assertEqual((await self.post()).status_code, 503)
            save.assert_not_awaited()

    async def test_body_is_bounded_even_when_streamed_without_content_length(self):
        async def oversized_stream():
            yield b"{"
            yield b"x" * monitor.MAX_FEED_BYTES
            raise AssertionError("Read after the body limit")

        with patch.object(monitor.market_store, "save_feed_cache", new_callable=AsyncMock) as save:
            response = await self.post(body=oversized_stream())
            self.assertEqual(response.status_code, 413)
            save.assert_not_awaited()

    async def test_invalid_payloads_are_rejected_without_echoing_the_body(self):
        bad_feed = broker_feed()
        bad_feed["quote"]["bid"] = False
        for raw in (b"not-json-private-payload", b"\xff", json.dumps(bad_feed).encode(), b'{"private-payload": true}'):
            with self.subTest(body=raw[:20]), patch.object(monitor.market_store, "save_feed_cache", new_callable=AsyncMock) as save:
                response = await self.post(body=raw)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json(), {"detail": "Invalid M15 broker data"})
                self.assertNotIn("private-payload", response.text)
                save.assert_not_awaited()

    async def test_valid_feed_is_persisted_and_older_quote_is_rejected(self):
        with patch.object(monitor.market_store, "save_feed_cache", new_callable=AsyncMock, return_value=True) as save:
            response = await self.post()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"ok": True})
            args = save.await_args.args
            self.assertEqual(args[:3], (self.service.pool, 9901, "broker_feed"))
            self.assertEqual(args[3], monitor.validate_feed(broker_feed(), NOW))
            self.assertEqual(args[4], NOW)
            self.assertEqual(args[5], monitor._utc(broker_feed()["quote"]["time"]))
        older = broker_feed()
        older["quote"]["time"] = (NOW - timedelta(seconds=60)).isoformat()
        with patch.object(monitor.market_store, "save_feed_cache", new_callable=AsyncMock, return_value=False) as save:
            response = await self.post(body=json.dumps(older).encode())
            self.assertEqual(response.status_code, 409)
            save.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
