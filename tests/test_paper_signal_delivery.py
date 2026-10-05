"""Paper delivery safety with synthetic feeds and in-memory persistence only."""

import asyncio
import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import market_monitor as monitor
from bot.market_news import NewsBriefing


NOW = datetime(2026, 10, 4, 12, 30, tzinfo=timezone.utc)


def broker_snapshot(count=24):
    candles = []
    for index in range(count):
        price = 2700.0 + index
        candles.append({
            "time": (NOW - timedelta(minutes=15 * (count - index))).isoformat(),
            "open": price, "high": price + 2, "low": price - 1,
            "close": price + 1, "tick_volume": 100,
        })
    return {
        "payload": {
            "symbol": "XAUUSD", "timeframe": "M15", "source": "MetaTrader 5",
            "quote": {"bid": 2724.0, "ask": 2724.2, "time": NOW.isoformat()},
            "candles": candles,
        },
        "updated_at": NOW,
    }


def reference_snapshot(periods=24, partial=None):
    start = NOW - timedelta(minutes=15 * periods)
    samples = []
    for period in range(periods):
        minutes = range(9) if period == partial else range(15)
        for minute in minutes:
            samples.append({
                "time": (start + timedelta(minutes=period * 15 + minute)).isoformat(),
                "price": 2700.0 + period + minute / 100,
            })
    return {
        "payload": {"price": 2725.0, "as_of": NOW.isoformat(), "samples": samples},
        "updated_at": NOW,
    }


def setup_result(**changes):
    result = {
        "state": "signal", "strategy_id": "ema9-21-atr14-v1",
        "bar_time": "2026-10-04T12:15:00Z", "direction": "BUY",
        "entry": 2725.0, "stop": 2722.0, "target": 2731.0, "atr": 2.0,
        "candle_count": 24,
    }
    result.update(changes)
    return result


class PaperSourceGateTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(monitor.os.environ, {"MARKET_GOLD_SYMBOL": "XAUUSD"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_fresh_complete_broker_history_reaches_engine_with_source_identity(self):
        with patch.object(monitor.paper_signals, "analyze_paper_signal", return_value=setup_result()) as engine:
            result, label, identity = monitor.paper_result(broker_snapshot(), NOW, "mt5")
        self.assertEqual(result["state"], "signal")
        self.assertEqual(identity, "mt5:XAUUSD")
        self.assertIn("MetaTrader 5", label)
        bars = engine.call_args.args[0]
        self.assertEqual(len(bars), 24)
        self.assertEqual(bars[-1]["time"], (NOW - timedelta(minutes=15)).isoformat())
        self.assertEqual(engine.call_args.kwargs, {"now": NOW})

    def test_stale_or_excess_future_quote_and_receipt_gate_even_full_covered_history(self):
        for source, make_snapshot, quote_path in (
            ("mt5", broker_snapshot, ("quote", "time")),
            ("reference", reference_snapshot, ("as_of",)),
        ):
            for field in ("quote_old", "quote_future", "receipt_old", "receipt_future"):
                snapshot = make_snapshot()
                if field.startswith("quote"):
                    stamp = NOW - timedelta(minutes=4) if field == "quote_old" else NOW + timedelta(seconds=6 if source == "mt5" else 1)
                    target = snapshot["payload"]
                    for name in quote_path[:-1]:
                        target = target[name]
                    target[quote_path[-1]] = stamp.isoformat()
                else:
                    snapshot["updated_at"] = (
                        NOW - timedelta(minutes=4) if field == "receipt_old" else NOW + timedelta(seconds=1)
                    )
                with self.subTest(source=source, field=field), patch.object(
                    monitor.paper_signals, "analyze_paper_signal"
                ) as engine:
                    result, _, _ = monitor.paper_result(snapshot, NOW, source)
                    self.assertEqual(result["state"], "stale")
                    engine.assert_not_called()

    def test_only_mt5_quote_allows_five_seconds_clock_skew(self):
        for lead in (1, 3, 5):
            snapshot = broker_snapshot()
            snapshot["payload"]["quote"]["time"] = (NOW + timedelta(seconds=lead)).isoformat()
            with self.subTest(lead=lead), patch.object(
                monitor.paper_signals, "analyze_paper_signal", return_value=setup_result()
            ) as engine:
                result, _, _ = monitor.paper_result(snapshot, NOW, "mt5")
                self.assertEqual(result["state"], "signal")
                engine.assert_called_once()

    def test_fresh_quote_does_not_make_old_completed_bars_fresh(self):
        snapshot = broker_snapshot()
        for bar in snapshot["payload"]["candles"]:
            bar["time"] = (monitor._utc(bar["time"]) - timedelta(hours=1)).isoformat()
        result, _, _ = monitor.paper_result(snapshot, NOW, "mt5")
        self.assertEqual(result["state"], "stale")

    def test_latest_partial_reference_period_prevents_signal_from_older_full_bars(self):
        result, _, identity = monitor.paper_result(reference_snapshot(partial=23), NOW, "reference")
        self.assertEqual(result["state"], "warmup")
        self.assertEqual(result["candle_count"], 0)
        self.assertEqual(identity, "reference:XAUUSD")

    def test_reference_coverage_gap_limits_warmup_to_subsequent_full_periods(self):
        result, _, _ = monitor.paper_result(reference_snapshot(partial=4), NOW, "reference")
        self.assertEqual(result["state"], "warmup")
        self.assertEqual(result["candle_count"], 19)
        self.assertEqual(result["remaining_bars"], 3)

    def test_missing_broker_interval_cannot_be_bridged_by_old_history(self):
        snapshot = broker_snapshot()
        snapshot["payload"]["candles"].pop(-10)
        result, _, _ = monitor.paper_result(snapshot, NOW, "mt5")
        self.assertEqual(result["state"], "warmup")
        self.assertEqual(result["candle_count"], 9)


class PaperDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(monitor.os.environ, {"MARKET_SOURCE": "mt5"}, clear=True)
        self.clock = patch.object(monitor, "utc_now", return_value=NOW)
        self.env.start()
        self.clock.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.clock.stop)
        self.service = monitor.MarketService(object(), 9901)
        self.saved = {}
        self.cache_read = patch.object(monitor.market_store, "get_cache", side_effect=self.read_cache)
        self.cache_write = patch.object(monitor.market_store, "save_cache", side_effect=self.write_cache)
        self.read = self.cache_read.start()
        self.write = self.cache_write.start()
        self.addCleanup(self.cache_read.stop)
        self.addCleanup(self.cache_write.stop)

    async def read_cache(self, pool, bot_id, key):
        self.assertEqual(bot_id, 9901)
        return copy.deepcopy(self.saved.get(key))

    async def write_cache(self, pool, bot_id, key, payload, now):
        self.assertEqual(bot_id, 9901)
        self.saved[key] = {"payload": copy.deepcopy(payload), "updated_at": now}

    def reporting_service(self):
        service = monitor.MarketService(self.service.pool, 9901)
        service.market = AsyncMock(return_value="synthetic price report")
        service.signals = AsyncMock(return_value=(setup_result(), "synthetic BUY paper setup", "setup-id"))
        service.news = AsyncMock(return_value=NewsBriefing(NOW, ("synthetic cited news",)))
        return service

    async def test_per_chat_dedup_survives_service_recreation_without_suppressing_other_chat(self):
        first = self.reporting_service()
        initial_bot = SimpleNamespace(send_message=AsyncMock())
        await first.send_report(initial_bot, 4401)
        self.assertEqual(self.saved["paper_delivery:4401"]["payload"]["id"], "setup-id")
        restarted = self.reporting_service()
        repeated_bot = SimpleNamespace(send_message=AsyncMock())
        await restarted.send_report(repeated_bot, 4401)
        repeated_texts = [call.args[1] for call in repeated_bot.send_message.await_args_list]
        self.assertNotIn("synthetic BUY paper setup", repeated_texts)
        self.assertTrue(any("سبق إرسال" in text for text in repeated_texts))
        new_chat_bot = SimpleNamespace(send_message=AsyncMock())
        await restarted.send_report(new_chat_bot, 4402)
        self.assertIn("synthetic BUY paper setup", [call.args[1] for call in new_chat_bot.send_message.await_args_list])
        self.assertEqual(self.saved["paper_delivery:4402"]["payload"]["id"], "setup-id")
        delivery_writes = [call.args[2] for call in self.write.await_args_list]
        self.assertEqual(delivery_writes.count("paper_delivery:4401"), 1)
        self.assertEqual(delivery_writes.count("paper_delivery:4402"), 1)

    async def test_manual_inspection_freezes_setup_but_does_not_acknowledge_worker_delivery(self):
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=AsyncMock()),
            effective_chat=SimpleNamespace(type="private", id=4401),
        )
        context = SimpleNamespace(bot_data={monitor.SERVICE_KEY: self.service})
        with patch.object(monitor, "paper_result", return_value=(setup_result(), "synthetic source", "reference:XAUUSD")):
            await monitor.signals_command(update, context)
        update.effective_message.reply_text.assert_awaited_once()
        text = update.effective_message.reply_text.await_args.args[0]
        self.assertIn("BUY", text)
        self.assertEqual(update.effective_message.reply_text.await_args.kwargs, {"parse_mode": None})
        self.assertIn("paper_setup", self.saved)
        self.assertFalse(any(key.startswith("paper_delivery:") for key in self.saved))
        self.assertEqual(self.service.active_users, set())
        self.service.market = AsyncMock(return_value="synthetic price report")
        self.service.news = AsyncMock(side_effect=monitor.market_news.BriefingUnavailable())
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(monitor, "paper_result", return_value=(setup_result(), "synthetic source", "reference:XAUUSD")):
            await self.service.send_report(bot, 4401)
        self.assertTrue(any("BUY" in call.args[1] for call in bot.send_message.await_args_list))
        self.assertIn("paper_delivery:4401", self.saved)

    async def test_original_levels_remain_frozen_for_same_source_strategy_bar_and_side(self):
        initial = setup_result()
        recalculated = setup_result(entry=2726.0, stop=2720.0, target=2738.0, atr=4.0)
        with patch.object(monitor, "paper_result", return_value=(initial, "synthetic source", "reference:XAUUSD")):
            first_result, _, first_id = await self.service.signals()
        restarted = monitor.MarketService(self.service.pool, 9901)
        with patch.object(monitor, "paper_result", return_value=(recalculated, "synthetic source", "reference:XAUUSD")):
            frozen_result, text, second_id = await restarted.signals()
        self.assertEqual(first_id, second_id)
        self.assertEqual(frozen_result, first_result)
        self.assertIn("2725.00", text)
        self.assertNotIn("2738.00", text)
        self.assertEqual(self.write.await_count, 1)

    async def test_same_bar_from_different_provider_has_different_setup_identity(self):
        ids = []
        for identity in ("reference:XAUUSD", "mt5:XAUUSD.m"):
            with patch.object(monitor, "paper_result", return_value=(setup_result(), "synthetic source", identity)):
                _, _, signal_id = await self.service.signals()
                ids.append(signal_id)
        self.assertNotEqual(*ids)

    async def test_unwatch_during_signal_computation_stops_paper_and_news(self):
        entered, proceed = asyncio.Event(), asyncio.Event()
        consent = {"active": True}
        service = self.reporting_service()

        async def delayed_signals():
            entered.set()
            await proceed.wait()
            return setup_result(), "synthetic BUY paper setup", "setup-id"

        service.signals = delayed_signals
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(monitor.market_store, "delivery_active", new_callable=AsyncMock,
                          side_effect=lambda *args: consent["active"]):
            task = asyncio.create_task(service.send_report(bot, 4401, "synthetic-lease"))
            await asyncio.wait_for(entered.wait(), 1)
            consent["active"] = False
            proceed.set()
            await asyncio.wait_for(task, 1)
        bot.send_message.assert_awaited_once_with(4401, "synthetic price report", parse_mode=None)
        service.news.assert_not_awaited()
        self.assertNotIn("paper_delivery:4401", self.saved)

    async def test_unwatch_during_delivery_cache_lookup_stops_paper_and_news(self):
        entered, proceed = asyncio.Event(), asyncio.Event()
        consent = {"active": True}
        service = self.reporting_service()

        async def delayed_read(pool, bot_id, key):
            if key.startswith("paper_delivery:"):
                entered.set()
                await proceed.wait()
            return None

        self.read.side_effect = delayed_read
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(monitor.market_store, "delivery_active", new_callable=AsyncMock,
                          side_effect=lambda *args: consent["active"]):
            task = asyncio.create_task(service.send_report(bot, 4401, "synthetic-lease"))
            await asyncio.wait_for(entered.wait(), 1)
            consent["active"] = False
            proceed.set()
            await asyncio.wait_for(task, 1)
        bot.send_message.assert_awaited_once_with(4401, "synthetic price report", parse_mode=None)
        service.news.assert_not_awaited()
        self.assertNotIn("paper_delivery:4401", self.saved)

    async def test_paused_report_never_shows_proposal_or_opens_another_trade(self):
        service = self.reporting_service()
        service.journal_enabled = True
        service.risk_pause = AsyncMock(return_value=NOW + timedelta(minutes=15))
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(monitor.journal_store, "open_trade", new_callable=AsyncMock) as journal, patch.object(
            monitor.trade_store, "create_offer", new_callable=AsyncMock
        ) as offer:
            await service.send_report(bot, 4401)
        texts = [call.args[1] for call in bot.send_message.await_args_list]
        self.assertTrue(any("موقوفة" in text for text in texts))
        self.assertFalse(any("BUY" in text for text in texts))
        journal.assert_not_awaited()
        offer.assert_not_awaited()
        service.news.assert_not_awaited()
        self.assertNotIn("paper_delivery:4401", self.saved)


if __name__ == "__main__":
    unittest.main()
