"""Market monitoring contracts, using local ASGI and mocked persistence only."""

import asyncio
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
from fastapi import FastAPI

from bot import market_monitor as monitor
from bot import market_news
from bot import mtf_runtime
from tests import test_mtf_runtime as mtf_fixtures
from tests import test_multi_timeframe as candle_fixtures


NOW = candle_fixtures.MultiTimeframeTests.now
BRIDGE_KEY = "synthetic-feed-key-" + "x" * 32
MANUAL_BRIDGE_KEY = "synthetic-manual-feed-key-" + "y" * 32


def broker_feed(count=24):
    return candle_fixtures.MultiTimeframeTests().feed(count=count)


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
        self.assertEqual(result["candles"][0]["close"], 1995.4)
        self.assertIsInstance(result["candles"][0]["close"], float)
        self.assertEqual(result["quote"]["time"], original["quote"]["time"])
        self.assertEqual(original["candles"][0]["close"], 1995.4)
        self.assertEqual(set(result["timeframes"]), {"M15", "M5", "M1"})
        self.assertEqual(result["risk_context"], original["risk_context"])

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
        body["schema_version"] = 1
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
                self.assertNotRegex(text, r"(?i)\b(?:BUY|SELL)\b")
                self.assertNotIn("EMA9", text)
                self.assertNotIn("دعم محتمل", text)
                self.assertNotIn("اقتراح شراء", text)

    def test_gap_ends_derived_window_instead_of_filling_missing_history(self):
        feed = broker_feed()
        feed["timeframes"]["M5"].pop(-5)
        text = monitor.market_text({"payload": feed, "updated_at": NOW}, NOW)
        self.assertIn("تجهيز السجل (4/22", text)
        self.assertIn("22 شمعة", text)
        self.assertNotIn("M5 — تأكيد الفرصة: ميل", text)
        self.assertNotIn("دعم محتمل", text)
        text = monitor.market_text({"payload": broker_feed(), "updated_at": NOW}, NOW)
        self.assertIn("M15 — الاتجاه العام: ميل", text)
        self.assertIn("M5 — تأكيد الفرصة: ميل", text)
        self.assertIn("M1 — توقيت الدخول: ميل", text)
        self.assertIn("لا توجد فرصة مؤكدة الشروط", text)
        self.assertNotIn("/news", text)

    def test_quote_skew_is_bounded_and_never_applies_to_bridge_receipt(self):
        feed = broker_feed()
        feed["quote"]["time"] = (NOW + timedelta(seconds=5)).isoformat()
        text = monitor.market_text({"payload": feed, "updated_at": NOW}, NOW)
        self.assertIn("M15 — الاتجاه العام: ميل", text)
        for receipt, quote_lead in ((NOW + timedelta(seconds=1), 0), (NOW, 6)):
            feed["quote"]["time"] = (NOW + timedelta(seconds=quote_lead)).isoformat()
            with self.subTest(receipt=receipt, quote_lead=quote_lead):
                text = monitor.market_text({"payload": feed, "updated_at": receipt}, NOW)
                self.assertNotIn("M15 — الاتجاه العام: ميل", text)
                self.assertNotRegex(text, r"(?i)\b(?:BUY|SELL)\b")

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
            enable.assert_awaited_once_with(self.service.pool, 9901, 4401, NOW, interval=monitor.market_store.MTF_DELIVERY_INTERVAL)
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

    async def test_scheduled_mt5_report_sends_one_status_without_chart_or_news(self):
        self.service.market = AsyncMock(return_value="تحليل الشارت")
        self.service.signals = AsyncMock(return_value=({"state": "no_signal"}, "ننتظر تقاطعاً جديداً", None))
        self.service.news = AsyncMock(side_effect=AssertionError("Automatic reports must not request news"))
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(monitor.market_store, "get_cache", new_callable=AsyncMock, return_value=None), patch.object(
            monitor.market_store, "save_cache", new_callable=AsyncMock
        ):
            await self.service.send_report(bot, 4401)
        bot.send_message.assert_awaited_once()
        self.assertIn("لا توجد فرصة مؤكدة الشروط", bot.send_message.await_args.args[1])
        self.assertNotIn("BUY", bot.send_message.await_args.args[1])
        self.service.market.assert_not_awaited()
        self.service.news.assert_not_awaited()

    async def test_unwatch_while_signals_runs_prevents_queued_report(self):
        entered, proceed = asyncio.Event(), asyncio.Event()
        enabled = {"value": True}

        async def get_signals():
            entered.set()
            await proceed.wait()
            return {"state": "warmup"}, "جارٍ جمع بيانات الإشارة", None

        self.service.market = AsyncMock()
        self.service.signals = get_signals
        self.service.news = AsyncMock()
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(monitor.market_store, "delivery_active", new_callable=AsyncMock, side_effect=lambda *args: enabled["value"]), patch.object(monitor.market_store, "get_cache", new_callable=AsyncMock, return_value=None):
            task = asyncio.create_task(self.service.send_report(bot, 4401, "synthetic-lease"))
            await asyncio.wait_for(entered.wait(), 1)
            enabled["value"] = False
            proceed.set()
            await task
        bot.send_message.assert_not_awaited()
        self.service.market.assert_not_awaited()
        self.service.news.assert_not_awaited()

    async def test_market_command_shows_chart_and_frozen_proposal_without_order_or_journal(self):
        self.service.market = AsyncMock(return_value="الاتجاه: صاعد | دعم 2700 | مقاومة 2730")
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(type="private", id=4401))
        context = SimpleNamespace(bot_data={monitor.SERVICE_KEY: self.service})
        with mtf_fixtures.pinned_synthetic_evidence(broker_feed(), NOW), patch.object(
            monitor.market_store, "get_cache", new_callable=AsyncMock,
            return_value={"payload": broker_feed(), "updated_at": NOW},
        ), patch.object(monitor.journal_store, "open_trade", new_callable=AsyncMock) as journal, patch.object(
            monitor.trade_store, "create_offer", new_callable=AsyncMock
        ) as offer:
            result = mtf_runtime.evaluate_feed(broker_feed(), NOW)
            signal_text = monitor.chart_analysis.format_chart_proposal(result)
            self.service.signals = AsyncMock(return_value=(result, signal_text, "setup-id"))
            await monitor.market_command(update, context)
        message.reply_text.assert_awaited_once()
        text = message.reply_text.await_args.args[0]
        self.assertIn("M15 / M5 / M1", text)
        self.assertIn("M15 — الاتجاه العام: ميل", text)
        self.assertIn("M5 — تأكيد الفرصة: ميل", text)
        self.assertIn("M1 — توقيت الدخول: ميل", text)
        self.assertEqual(text.count("شراء BUY"), 1)
        for field in ("entry", "stop", "target", "target2"):
            self.assertIn(f"{result[field]:.3f}", text)
        self.assertEqual(message.reply_text.await_args.kwargs, {"parse_mode": None})
        journal.assert_not_awaited()
        offer.assert_not_awaited()
        self.assertEqual(self.service.active_users, set())

    async def test_reference_chart_and_signal_commands_suppress_proposal_during_risk_pause(self):
        self.service.source = "reference"
        self.service.market = AsyncMock(return_value="الاتجاه: صاعد")
        self.service.signals = AsyncMock(return_value=({"state": "signal"}, "BUY | دخول 2720 | وقف 2717 | هدف 2726", "setup-id"))
        self.service.risk_pause = AsyncMock(return_value=NOW + timedelta(minutes=15))
        for command in (monitor.market_command, monitor.signals_command):
            self.service.market_command_times.clear()
            message = SimpleNamespace(reply_text=AsyncMock())
            update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(type="private", id=4401))
            context = SimpleNamespace(bot_data={monitor.SERVICE_KEY: self.service})
            with patch.object(monitor.market_store, "get_cache", new_callable=AsyncMock, return_value=None):
                await command(update, context)
            text = message.reply_text.await_args.args[0]
            self.assertIn("موقوفة", text)
            self.assertNotIn("BUY", text)
            self.assertNotIn("دخول", text)

    async def test_mt5_market_command_shows_blocked_reason_once_with_real_chart(self):
        feed = broker_feed()
        feed["risk_context"]["costs_verified"] = False
        snapshot = {"payload": feed, "updated_at": NOW}
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(type="private", id=4401))
        context = SimpleNamespace(bot_data={monitor.SERVICE_KEY: self.service})

        async def read_cache(pool, bot_id, key):
            return snapshot if key == "broker_feed" else None

        with patch.object(monitor.market_store, "get_cache", side_effect=read_cache), patch.object(
            monitor.journal_store, "recent_trades", new_callable=AsyncMock,
            side_effect=AssertionError("MTF status must not consult legacy stop history"),
        ) as history:
            await monitor.market_command(update, context)
        message.reply_text.assert_awaited_once()
        text = message.reply_text.await_args.args[0]
        self.assertIn("M15 / M5 / M1", text)
        self.assertIn("M1 — توقيت الدخول", text)
        self.assertEqual(text.count("لا توجد فرصة مؤكدة الشروط"), 1)
        self.assertEqual(text.count("السبب:"), 1)
        self.assertEqual(text.count("تكاليف العمولة والانزلاق غير موثّقة"), 1)
        self.assertNotIn("BUY", text)
        self.assertNotIn("SELL", text)
        history.assert_not_awaited()

    async def test_market_command_reads_real_broker_candles_and_shows_one_current_proposal(self):
        feed = broker_feed(count=22)
        snapshot = {"payload": feed, "updated_at": NOW}
        self.service.manual_tickets_enabled = True
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(type="private", id=4401))
        context = SimpleNamespace(bot_data={monitor.SERVICE_KEY: self.service})

        async def read_cache(pool, bot_id, key):
            return snapshot if key == "broker_feed" else None

        with mtf_fixtures.pinned_synthetic_evidence(feed, NOW), patch.object(monitor.market_store, "get_cache", side_effect=read_cache), patch.object(
            monitor.market_store, "save_cache", new_callable=AsyncMock
        ) as save, patch.object(monitor.trade_store, "create_offer", new_callable=AsyncMock) as offer:
            await monitor.market_command(update, context)
        text = message.reply_text.await_args.args[0]
        self.assertIn("M15 / M5 / M1", text)
        self.assertIn("M15 — الاتجاه العام: ميل", text)
        self.assertIn("M5 — تأكيد الفرصة: ميل", text)
        self.assertIn("M1 — توقيت الدخول: ميل", text)
        self.assertEqual(text.count("شراء BUY"), 1)
        self.assertIn("2000.003", text)
        self.assertNotIn("https://", text)
        self.assertEqual(save.await_args.args[2], "paper_setup")
        offer.assert_not_awaited()
        self.assertFalse(self.service.trading_enabled)

    async def test_stop_cancels_owned_worker_without_waiting_forever(self):
        task = asyncio.create_task(asyncio.Event().wait())
        application = SimpleNamespace(bot_data={monitor.WORKER_KEY: task})
        await asyncio.wait_for(monitor.stop(application), 1)
        self.assertTrue(task.cancelled())
        self.assertNotIn(monitor.WORKER_KEY, application.bot_data)


class ManualTicketMonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(monitor.os.environ, {
            "MARKET_SOURCE": "mt5", "MT5_MANUAL_TICKETS_ENABLED": "true", "MT5_TRADING_ENABLED": "true",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.clock = patch.object(monitor, "utc_now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.service = monitor.MarketService(object(), 9901)

    async def test_setup_initializes_manual_store_after_devices_and_manual_flag_disables_automatic_trading(self):
        order = []

        def initialise(name):
            async def initialize(pool):
                order.append(name)
            return initialize

        application = SimpleNamespace(bot_data={"db": object()}, bot=SimpleNamespace(id=9901), running=False)
        with patch.object(monitor.market_store, "initialize_schema", side_effect=initialise("market")), patch.object(
            monitor.journal_store, "initialize_schema", side_effect=initialise("journal")
        ), patch.object(monitor.trade_store, "initialize_schema", side_effect=initialise("trade")), patch.object(
            monitor.manual_ticket_store, "initialize_schema", side_effect=initialise("manual")
        ):
            await monitor.setup(application)
            await monitor.stop(application)
        service = application.bot_data[monitor.SERVICE_KEY]
        self.assertTrue(service.manual_tickets_enabled)
        self.assertFalse(service.trading_enabled)
        self.assertEqual(order, ["market", "journal", "trade", "manual"])

    async def test_worker_dispatches_manual_preparation_offers_and_results_with_auto_disabled(self):
        application = SimpleNamespace(running=True, bot=object())
        with patch.object(monitor.asyncio, "sleep", new_callable=AsyncMock, side_effect=[None, asyncio.CancelledError]) as pause, patch.object(
            monitor.market_store, "get_cache", new_callable=AsyncMock, return_value=None
        ), patch.object(monitor.market_store, "claim_due", new_callable=AsyncMock, return_value=[]), patch.object(
            monitor.manual_ticket_store, "expire_offers", new_callable=AsyncMock
        ) as manual_expire, patch.object(monitor.trade_store, "expire_offers", new_callable=AsyncMock) as auto_expire, patch.object(
            monitor.mt5_notifications, "send_results", new_callable=AsyncMock, return_value=True
        ) as results, patch.object(monitor.mt5_notifications, "send_offers", new_callable=AsyncMock, return_value=True) as offers:
            with self.assertRaises(asyncio.CancelledError):
                await monitor._worker(application, self.service)
        self.assertFalse(self.service.trading_enabled)
        manual_expire.assert_awaited_once()
        auto_expire.assert_not_awaited()
        results.assert_awaited_once_with(self.service, application.bot)
        offers.assert_awaited_once_with(self.service, application.bot)
        self.assertEqual([call.args for call in pause.await_args_list], [(5,), (5,)])

    async def test_unwatch_cancels_manual_pending_and_explains_existing_native_window(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(type="private", id=4401))
        context = SimpleNamespace(bot_data={monitor.SERVICE_KEY: self.service})
        with patch.object(monitor.market_store, "disable_subscription", new_callable=AsyncMock), patch.object(
            monitor.manual_ticket_store, "cancel_offers", new_callable=AsyncMock
        ) as manual_cancel, patch.object(monitor.trade_store, "cancel_offers", new_callable=AsyncMock) as auto_cancel:
            await monitor.unwatch_command(update, context)
        manual_cancel.assert_awaited_once_with(self.service.pool, 9901, 4401, NOW)
        auto_cancel.assert_not_awaited()
        text = message.reply_text.await_args.args[0]
        self.assertIn("تجهيز MT5 المعلقة", text)
        self.assertIn("لا يغلقها", text)


class ManualReportDedupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(monitor.os.environ, {
            "MARKET_SOURCE": "mt5", "MT5_MANUAL_TICKETS_ENABLED": "true",
        }, clear=True))
        self.enterContext(patch.object(monitor, "utc_now", return_value=NOW))
        self.service = monitor.MarketService(object(), 9901)
        self.device_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        self.feed = broker_feed()
        self.feed["device_id"] = str(self.device_id)
        self.device = {
            "device_id": self.device_id, "owner_chat_id": 4401, "owner_user_id": 4401,
            "symbol": "XAUUSD", "account_mode": "demo", "volume": 0.01,
            "last_seen_at": NOW,
        }
        self.cache = {"broker_feed": {"payload": self.feed, "updated_at": NOW}}
        self.offer = None
        self.enterContext(mtf_fixtures.pinned_synthetic_evidence(self.feed, NOW))

        async def read_cache(pool, bot_id, key):
            return copy.deepcopy(self.cache.get(key))

        async def save_cache(pool, bot_id, key, payload, now):
            self.cache[key] = {"payload": copy.deepcopy(payload), "updated_at": now}

        self.enterContext(patch.object(monitor.market_store, "get_cache", side_effect=read_cache))
        self.saved = self.enterContext(patch.object(monitor.market_store, "save_cache", side_effect=save_cache))
        self.device_read = self.enterContext(patch.object(
            monitor.trade_store, "get_device", new_callable=AsyncMock, return_value=self.device,
        ))
        self.offer_read = self.enterContext(patch.object(
            monitor.manual_ticket_store, "get_chart_offer", new_callable=AsyncMock,
            side_effect=lambda *args: copy.deepcopy(self.offer),
        ))
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)))

    async def create_offer(self, pool, bot_id, device_id, chat_id, user_id, signal_id, payload, now, expiry):
        self.offer = {
            "id": UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"), "bot_id": bot_id,
            "device_id": device_id, "chat_id": chat_id, "user_id": user_id,
            "signal_id": signal_id, "payload": copy.deepcopy(payload), "status": "draft",
            "created_at": now, "updated_at": now, "expires_at": expiry,
            "published_at": None, "message_id": None,
        }
        return copy.deepcopy(self.offer)

    async def publish_offer(self, pool, bot_id, offer_id, message_id, now):
        self.offer.update(status="offered", message_id=message_id, published_at=now, updated_at=now)
        return True

    async def publish_current(self):
        result, _, signal_id = await self.service.signals()
        self.assertEqual(result["state"], "signal", result)
        payload = dict(result, workflow="manual_ticket", source_identity=f"mt5:XAUUSD:{self.device_id}")
        await self.create_offer(self.service.pool, self.service.bot_id, self.device_id, 4401, 4401,
                                signal_id, payload, NOW, NOW + timedelta(seconds=10))
        await self.publish_offer(self.service.pool, self.service.bot_id, self.offer["id"], 77, NOW)

    async def test_successful_manual_offer_skips_full_report_but_changed_blocked_status_arrives(self):
        self.cache["mtf_status:4401"] = {"payload": {"token": "blocked:costs_unverified"}, "updated_at": NOW}
        with patch.object(monitor.manual_ticket_store, "expire_offers", new_callable=AsyncMock), patch.object(
            monitor.trade_store, "list_paired_devices", new_callable=AsyncMock, return_value=[self.device],
        ), patch.object(monitor.trade_store, "subscription_active", new_callable=AsyncMock, return_value=True), patch.object(
            monitor.manual_ticket_store, "create_offer", side_effect=self.create_offer,
        ), patch.object(monitor.manual_ticket_store, "publish_offer", side_effect=self.publish_offer) as published:
            self.assertTrue(await monitor.mt5_notifications.send_offers(self.service, self.bot))
            published.assert_awaited_once()
            await self.service.send_report(self.bot, 4401)
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertIn("جهّز على اللابتوب", self.bot.send_message.await_args.args[1])
        self.assertEqual(self.cache["mtf_status:4401"]["payload"]["token"], self.offer["signal_id"])
        self.offer_read.assert_awaited_once_with(self.service.pool, 9901, self.device_id, NOW)

        self.feed["risk_context"]["costs_verified"] = False
        await self.service.send_report(self.bot, 4401)
        self.assertEqual(self.bot.send_message.await_count, 2)
        blocked_text = self.bot.send_message.await_args.args[1]
        self.assertIn("تكاليف العمولة والانزلاق غير موثّقة", blocked_text)
        self.assertNotIn("BUY", blocked_text)
        await self.service.send_report(self.bot, 4401)
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_unpublished_other_setup_or_wrong_owner_cannot_suppress_report(self):
        await self.publish_current()
        original = copy.deepcopy(self.offer)
        cases = (
            {"status": "draft", "published_at": None, "message_id": None},
            {"signal_id": "another-setup"},
            {"chat_id": 4402, "user_id": 4402},
            {"status": "rejected"},
        )
        for changed in cases:
            with self.subTest(changed=changed):
                self.offer = {**copy.deepcopy(original), **changed}
                self.cache.pop("mtf_status:4401", None)
                self.bot.send_message.reset_mock()
                await self.service.send_report(self.bot, 4401)
                self.bot.send_message.assert_awaited_once()
                self.assertIn("شراء BUY", self.bot.send_message.await_args.args[1])

    async def test_offer_lookup_failure_is_not_treated_as_successful_delivery(self):
        await self.publish_current()
        self.offer_read.side_effect = RuntimeError("Synthetic storage failure")
        with self.assertRaises(RuntimeError):
            await self.service.send_report(self.bot, 4401)
        self.bot.send_message.assert_not_awaited()
        self.assertNotIn("mtf_status:4401", self.cache)

    async def test_feed_blocked_during_offer_lookup_sends_current_blocked_status(self):
        await self.publish_current()

        async def lookup(*args):
            self.feed["risk_context"]["costs_verified"] = False
            return copy.deepcopy(self.offer)

        self.offer_read.side_effect = lookup
        await self.service.send_report(self.bot, 4401)
        self.bot.send_message.assert_awaited_once()
        self.assertNotIn("BUY", self.bot.send_message.await_args.args[1])

    async def test_unwatch_during_offer_lookup_prevents_monitoring_delivery(self):
        await self.publish_current()
        active = {"value": True}

        async def lookup(*args):
            active["value"] = False
            return None

        self.offer_read.side_effect = lookup
        with patch.object(monitor.market_store, "delivery_active", new_callable=AsyncMock,
                          side_effect=lambda *args: active["value"]):
            await self.service.send_report(self.bot, 4401, "synthetic-lease")
        self.bot.send_message.assert_not_awaited()
        self.assertNotIn("mtf_status:4401", self.cache)


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
        self.service.manual_tickets_enabled = False
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

    async def test_manual_feed_accepts_only_dedicated_key_and_never_legacy_key(self):
        self.service.manual_tickets_enabled = True
        with patch.dict(monitor.os.environ, {"MT5_MANUAL_BRIDGE_KEY": MANUAL_BRIDGE_KEY}), patch.object(
            monitor.market_store, "save_feed_cache", new_callable=AsyncMock, return_value=True
        ) as save:
            self.assertEqual((await self.post(auth=BRIDGE_KEY)).status_code, 401)
            save.assert_not_awaited()
            self.assertEqual((await self.post(auth=MANUAL_BRIDGE_KEY)).status_code, 200)
            save.assert_awaited_once()

    async def test_missing_or_short_manual_key_fails_closed_even_with_valid_legacy_key(self):
        self.service.manual_tickets_enabled = True
        for manual_key in ("", "short"):
            with self.subTest(manual_key=manual_key), patch.dict(
                monitor.os.environ, {"MT5_MANUAL_BRIDGE_KEY": manual_key}
            ), patch.object(monitor.market_store, "save_feed_cache", new_callable=AsyncMock) as save:
                self.assertEqual((await self.post(auth=BRIDGE_KEY)).status_code, 404)
                self.assertEqual((await self.post(auth=MANUAL_BRIDGE_KEY)).status_code, 404)
                save.assert_not_awaited()

    async def test_automatic_feed_does_not_accept_manual_key(self):
        with patch.dict(monitor.os.environ, {"MT5_MANUAL_BRIDGE_KEY": MANUAL_BRIDGE_KEY}), patch.object(
            monitor.market_store, "save_feed_cache", new_callable=AsyncMock, return_value=True
        ) as save:
            self.assertEqual((await self.post(auth=MANUAL_BRIDGE_KEY)).status_code, 401)
            save.assert_not_awaited()
            self.assertEqual((await self.post(auth=BRIDGE_KEY)).status_code, 200)
            save.assert_awaited_once()

    async def test_configured_manual_mode_isolates_key_before_service_ready_and_during_switch(self):
        with patch.dict(monitor.os.environ, {
            "MT5_MANUAL_TICKETS_ENABLED": "true", "MT5_MANUAL_BRIDGE_KEY": MANUAL_BRIDGE_KEY,
        }), patch.object(monitor.market_store, "save_feed_cache", new_callable=AsyncMock, return_value=True) as save:
            # Configuration already says manual even before service restart.
            self.assertEqual((await self.post(auth=BRIDGE_KEY)).status_code, 401)
            self.assertEqual((await self.post(auth=MANUAL_BRIDGE_KEY)).status_code, 200)
            save.reset_mock()
            self.application.bot_data.clear()
            self.assertEqual((await self.post(auth=BRIDGE_KEY)).status_code, 401)
            self.assertEqual((await self.post(auth=MANUAL_BRIDGE_KEY)).status_code, 503)
            save.assert_not_awaited()

    async def test_invalid_runtime_mode_flag_fails_closed_before_any_feed_write(self):
        for flag in ("true", 1):
            self.service.manual_tickets_enabled = flag
            with self.subTest(flag=flag), patch.object(monitor.market_store, "save_feed_cache", new_callable=AsyncMock) as save:
                self.assertEqual((await self.post(auth=BRIDGE_KEY)).status_code, 404)
                save.assert_not_awaited()

    async def test_invalid_payloads_are_rejected_without_echoing_the_body(self):
        bad_feed = broker_feed()
        bad_feed["quote"]["bid"] = False
        for raw in (b"not-json-private-payload", b"\xff", json.dumps(bad_feed).encode(), b'{"private-payload": true}'):
            with self.subTest(body=raw[:20]), patch.object(monitor.market_store, "save_feed_cache", new_callable=AsyncMock) as save:
                response = await self.post(body=raw)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json(), {"detail": "Invalid broker data"})
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
        older["quote"]["time"] = (NOW - timedelta(seconds=2)).isoformat()
        with patch.object(monitor.market_store, "save_feed_cache", new_callable=AsyncMock, return_value=False) as save:
            response = await self.post(body=json.dumps(older).encode())
            self.assertEqual(response.status_code, 409)
            save.assert_awaited_once()

    async def test_missing_multi_timeframe_clock_or_execution_metadata_does_not_write(self):
        for field in ("as_of", "broker_utc_offset_minutes", "execution"):
            body = broker_feed()
            del body[field]
            with self.subTest(field=field), patch.object(
                monitor.market_store, "save_feed_cache", new_callable=AsyncMock,
            ) as save:
                response = await self.post(body=json.dumps(body).encode())
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json(), {"detail": "Invalid broker data"})
                save.assert_not_awaited()

    async def test_registered_device_lookup_cannot_refresh_an_aged_snapshot(self):
        body = broker_feed()
        body["device_id"] = "11111111-1111-4111-8111-111111111111"
        elapsed = {"now": NOW}
        async def delayed_device(*args):
            await asyncio.sleep(0)
            elapsed["now"] = NOW + timedelta(seconds=31)
            return {"symbol": "XAUUSD"}
        with patch.object(monitor.trade_store, "get_device", side_effect=delayed_device), patch.object(
            monitor.market_store, "save_feed_cache", new_callable=AsyncMock,
        ) as save, patch.object(monitor, "utc_now", side_effect=lambda: elapsed["now"]):
            response = await self.post(body=json.dumps(body).encode())
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {"detail": "Invalid broker data"})
        save.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
