"""MTF delivery and preserved reference coverage with synthetic local data."""

import asyncio
import copy
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import market_monitor as monitor, mtf_runtime
from tests import test_mtf_runtime as mtf_fixtures
from tests import test_multi_timeframe as candle_fixtures

NOW = candle_fixtures.MultiTimeframeTests.now


def broker_snapshot(count=24):
    return {"payload": candle_fixtures.MultiTimeframeTests().feed(count=count), "updated_at": NOW}


def reference_snapshot(periods=24, partial=None):
    start = NOW - timedelta(minutes=15 * periods)
    samples = []
    for period in range(periods):
        minutes = range(9) if period == partial else range(15)
        for minute in minutes:
            samples.append({"time": (start + timedelta(minutes=period * 15 + minute)).isoformat(),
                            "price": 2700.0 + period + minute / 100})
    return {"payload": {"price": 2725.0, "as_of": NOW.isoformat(), "samples": samples}, "updated_at": NOW}


class PinnedSyntheticEvidenceMixin:
    def evidence(self):
        return mtf_fixtures.pinned_synthetic_evidence(broker_snapshot()["payload"], NOW)

    def assert_no_trade(self, text):
        self.assertNotRegex(text, r"(?i)\b(?:BUY|SELL)\b")
        self.assertNotIn("SL:", text)
        self.assertNotIn("الدخول المقترح:", text)


class PaperSourceGateTests(PinnedSyntheticEvidenceMixin, unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(monitor.os.environ, {"MARKET_GOLD_SYMBOL": "XAUUSD"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_fresh_complete_broker_history_uses_actual_mtf_engine_and_local_artifact(self):
        with self.evidence(), patch.object(monitor.paper_signals, "analyze_paper_signal") as retired_engine:
            result, label, identity = monitor.paper_result(broker_snapshot(), NOW, "mt5")
        self.assertEqual(result["state"], "signal", result)
        self.assertEqual(result["display_timeframe"], "M1")
        self.assertEqual(identity, "mt5:XAUUSD")
        self.assertIn("MetaTrader 5", label)
        self.assertIn("qualification_id", result)
        retired_engine.assert_not_called()

    def test_stale_or_future_quote_and_receipt_gate_full_covered_history(self):
        for source, make_snapshot, quote_path in (("mt5", broker_snapshot, ("quote", "time")),
                                                   ("reference", reference_snapshot, ("as_of",))):
            for field in ("quote_old", "quote_future", "receipt_old", "receipt_future"):
                snapshot = make_snapshot()
                if field.startswith("quote"):
                    stamp = NOW - timedelta(minutes=4) if field == "quote_old" else NOW + timedelta(seconds=6 if source == "mt5" else 1)
                    target = snapshot["payload"]
                    for name in quote_path[:-1]: target = target[name]
                    target[quote_path[-1]] = stamp.isoformat()
                else:
                    snapshot["updated_at"] = NOW - timedelta(minutes=4) if field == "receipt_old" else NOW + timedelta(seconds=1)
                with self.subTest(source=source, field=field), patch.object(monitor.paper_signals, "analyze_paper_signal") as engine:
                    result, _, _ = monitor.paper_result(snapshot, NOW, source)
                    self.assertIn(result["state"], ("stale", "invalid"))
                    engine.assert_not_called()

    def test_only_mt5_quote_allows_five_seconds_clock_skew(self):
        with self.evidence():
            for lead in (1, 3, 5):
                snapshot = broker_snapshot()
                snapshot["payload"]["quote"]["time"] = (NOW + timedelta(seconds=lead)).isoformat()
                with self.subTest(lead=lead):
                    result, _, _ = monitor.paper_result(snapshot, NOW, "mt5")
                    self.assertEqual(result["state"], "signal", result)

    def test_mt5_receipt30_and_quote10_second_bounds_are_independent(self):
        with self.evidence():
            snapshot = broker_snapshot()
            snapshot["updated_at"] = NOW - timedelta(seconds=31)
            self.assertEqual(monitor.paper_result(snapshot, NOW, "mt5")[0]["state"], "stale")
        clock = NOW + timedelta(seconds=10)
        feed = candle_fixtures.MultiTimeframeTests().feed(now=clock, count=24)
        with mtf_fixtures.pinned_synthetic_evidence(feed, clock):
            for age, state in ((10, "signal"), (11, "stale")):
                snapshot = {"payload": copy.deepcopy(feed), "updated_at": clock}
                snapshot["payload"]["quote"]["time"] = (clock - timedelta(seconds=age)).isoformat()
                self.assertEqual(monitor.paper_result(snapshot, clock, "mt5")[0]["state"], state)

    def test_fresh_receipt_and_quote_cannot_extend_the_absolute_m1_entry_window(self):
        clock = NOW + timedelta(seconds=11)
        feed = candle_fixtures.MultiTimeframeTests().feed(now=clock, count=24)
        with mtf_fixtures.pinned_synthetic_evidence(feed, clock):
            result, _, _ = monitor.paper_result({"payload": feed, "updated_at": clock}, clock, "mt5")
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason"], "entry_window_expired")
        self.assertNotIn("direction", result)

    def test_fresh_quote_does_not_make_old_completed_bars_fresh(self):
        snapshot = broker_snapshot()
        for rows in snapshot["payload"]["timeframes"].values():
            for bar in rows: bar["time"] = (monitor._utc(bar["time"]) - timedelta(hours=1)).isoformat()
        snapshot["payload"]["candles"] = copy.deepcopy(snapshot["payload"]["timeframes"]["M15"])
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

    def test_missing_broker_interval_resets_only_actual_contiguous_suffix(self):
        snapshot = broker_snapshot()
        snapshot["payload"]["timeframes"]["M5"].pop(-10)
        result, _, _ = monitor.paper_result(snapshot, NOW, "mt5")
        self.assertEqual(result["state"], "warmup")
        self.assertEqual(result["contiguous_counts"]["M5"], 9)

    def test_legacy_m15_alone_never_reaches_retired_buy_sell_engine(self):
        snapshot = broker_snapshot()
        for key in ("schema_version", "timeframes", "risk_context", "as_of", "broker_utc_offset_minutes"):
            snapshot["payload"].pop(key)
        with self.evidence(), patch.object(monitor.paper_signals, "analyze_paper_signal") as engine:
            result, _, _ = monitor.paper_result(snapshot, NOW, "mt5")
        self.assertNotEqual(result["state"], "signal")
        self.assertNotIn("direction", result)
        engine.assert_not_called()


class PaperDeliveryTests(PinnedSyntheticEvidenceMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(monitor.os.environ, {"MARKET_SOURCE": "mt5", "MT5_MANUAL_TICKETS_ENABLED": "true"}, clear=True)
        self.clock = patch.object(monitor, "utc_now", return_value=NOW)
        self.env.start(); self.clock_mock = self.clock.start()
        self.addCleanup(self.env.stop); self.addCleanup(self.clock.stop)
        self.service = monitor.MarketService(object(), 9901)
        self.saved = {"broker_feed": broker_snapshot()}
        self.cache_read = patch.object(monitor.market_store, "get_cache", side_effect=self.read_cache)
        self.cache_write = patch.object(monitor.market_store, "save_cache", side_effect=self.write_cache)
        self.read = self.cache_read.start(); self.write = self.cache_write.start()
        self.addCleanup(self.cache_read.stop); self.addCleanup(self.cache_write.stop)

    async def read_cache(self, pool, bot_id, key):
        self.assertEqual(bot_id, 9901)
        return copy.deepcopy(self.saved.get(key))

    async def write_cache(self, pool, bot_id, key, payload, now):
        self.assertEqual(bot_id, 9901)
        self.saved[key] = {"payload": copy.deepcopy(payload), "updated_at": now}

    def reporting_service(self):
        service = monitor.MarketService(self.service.pool, 9901)
        service.market = AsyncMock(side_effect=AssertionError("MTF status delivery does not fetch a chart"))
        service.news = AsyncMock(side_effect=AssertionError("MTF status delivery does not fetch news"))
        return service

    async def test_per_chat_status_dedup_survives_recreation_and_other_chat_is_independent(self):
        with self.evidence():
            first = self.reporting_service()
            bot = SimpleNamespace(send_message=AsyncMock())
            await first.send_report(bot, 4401)
            bot.send_message.assert_awaited_once()
            self.assertIn("شراء BUY", bot.send_message.await_args.args[1])
            token = self.saved["mtf_status:4401"]["payload"]["token"]
            self.assertEqual(token, self.saved["paper_setup"]["payload"]["id"])
            restarted = self.reporting_service()
            repeated = SimpleNamespace(send_message=AsyncMock())
            await restarted.send_report(repeated, 4401)
            repeated.send_message.assert_not_awaited()
            other = SimpleNamespace(send_message=AsyncMock())
            await restarted.send_report(other, 4402)
            other.send_message.assert_awaited_once()
            self.assertEqual(self.saved["mtf_status:4402"]["payload"]["token"], token)
            restarted.market.assert_not_awaited(); restarted.news.assert_not_awaited()
        writes = [call.args[2] for call in self.write.await_args_list]
        self.assertEqual(writes.count("mtf_status:4401"), 1)
        self.assertEqual(writes.count("mtf_status:4402"), 1)

    async def test_status_only_notifies_when_waiting_state_or_reason_changes(self):
        self.saved["broker_feed"]["payload"]["risk_context"]["costs_verified"] = False
        service = self.reporting_service()
        bot = SimpleNamespace(send_message=AsyncMock())
        with self.evidence():
            await service.send_report(bot, 4401)
            await service.send_report(bot, 4401)
            self.assertEqual(bot.send_message.await_count, 1)
            self.assert_no_trade(bot.send_message.await_args.args[1])
            self.saved["broker_feed"]["payload"]["risk_context"].update(costs_verified=True, open_positions=1)
            await service.send_report(bot, 4401)
            self.assertEqual(bot.send_message.await_count, 2)
            self.assertIn("فرصة عالية المخاطر", bot.send_message.await_args.args[1])
        self.assertNotIn("paper_setup", self.saved)

    async def test_manual_inspection_freezes_qualified_setup_without_acknowledging_delivery(self):
        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()), effective_chat=SimpleNamespace(type="private", id=4401))
        context = SimpleNamespace(bot_data={monitor.SERVICE_KEY: self.service})
        with self.evidence():
            await monitor.signals_command(update, context)
            text = update.effective_message.reply_text.await_args.args[0]
            self.assertIn("شراء BUY", text)
            self.assertEqual(update.effective_message.reply_text.await_args.kwargs, {"parse_mode": None})
            self.assertIn("paper_setup", self.saved)
            self.assertNotIn("mtf_status:4401", self.saved)
            bot = SimpleNamespace(send_message=AsyncMock())
            await self.service.send_report(bot, 4401)
        self.assertIn("شراء BUY", bot.send_message.await_args.args[1])
        self.assertIn("mtf_status:4401", self.saved)
        self.assertEqual(self.service.active_users, set())

    async def test_original_levels_remain_frozen_for_same_source_strategy_bar_and_direction(self):
        with self.evidence():
            first, _, first_id = await self.service.signals()
            self.saved["broker_feed"]["payload"]["quote"].update(bid=2000.011, ask=2000.013)
            restarted = self.reporting_service()
            frozen, text, second_id = await restarted.signals()
        self.assertEqual(first_id, second_id)
        self.assertEqual(frozen, first)
        self.assertIn("2000.003", text)
        self.assertEqual([call.args[2] for call in self.write.await_args_list].count("paper_setup"), 1)

    async def test_original_frozen_result_is_rechecked_against_current_quote_and_risk(self):
        with self.evidence():
            initial, _, _ = await self.service.signals()
            self.saved["broker_feed"]["payload"]["quote"].update(bid=initial["entry"] + .2, ask=initial["entry"] + .202)
            result, text, signal_id = await self.service.signals()
            self.assertNotEqual(result["state"], "signal")
            self.assertIsNone(signal_id)
            self.assert_no_trade(text)

    async def test_same_bar_from_different_paired_devices_has_distinct_setup_identity(self):
        ids = []
        with self.evidence():
            for device_id in ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"):
                self.saved["broker_feed"]["payload"]["device_id"] = device_id
                _, _, signal_id = await self.service.signals()
                ids.append(signal_id)
        self.assertNotEqual(*ids)

    async def test_unqualified_or_legacy_mt5_data_never_freezes_a_setup(self):
        self.saved["broker_feed"]["payload"]["risk_context"]["costs_verified"] = False
        result, text, signal_id = await self.service.signals()
        self.assertEqual(result["reason"], "unverified_costs")
        self.assertIsNone(signal_id); self.assert_no_trade(text)
        self.assertNotIn("paper_setup", self.saved)
        self.saved["broker_feed"] = broker_snapshot()
        for key in ("schema_version", "timeframes", "risk_context", "as_of", "broker_utc_offset_minutes"):
            self.saved["broker_feed"]["payload"].pop(key)
        result, text, signal_id = await self.service.signals()
        self.assertNotEqual(result["state"], "signal")
        self.assertIsNone(signal_id); self.assert_no_trade(text)
        self.assertNotIn("paper_setup", self.saved)

    async def test_invalid_legacy_cached_result_is_not_reused_for_current_qualified_setup(self):
        with self.evidence():
            current, _, signal_id = await self.service.signals()
            self.saved["paper_setup"]["payload"]["result"] = {"state": "signal", "strategy_id": "ema9-21-atr14-v1", "direction": "BUY", "entry": 9000, "stop": 8990, "target": 9020}
            returned, text, returned_id = await self.service.signals()
        self.assertEqual(returned_id, signal_id)
        self.assertEqual(returned["entry"], current["entry"])
        self.assertNotIn("9000", text)

    async def test_unwatch_during_signal_computation_stops_entire_mt5_report(self):
        entered, proceed = asyncio.Event(), asyncio.Event()
        consent = {"active": True}
        service = self.reporting_service()
        async def delayed_signals():
            entered.set(); await proceed.wait()
            return {"state": "warmup", "reason": "insufficient_contiguous_history"}, "جارٍ جمع البيانات", None
        service.signals = delayed_signals
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(monitor.market_store, "delivery_active", new_callable=AsyncMock, side_effect=lambda *args: consent["active"]):
            task = asyncio.create_task(service.send_report(bot, 4401, "synthetic-lease"))
            await asyncio.wait_for(entered.wait(), 1)
            consent["active"] = False; proceed.set()
            await asyncio.wait_for(task, 1)
        bot.send_message.assert_not_awaited(); service.news.assert_not_awaited()
        self.assertNotIn("mtf_status:4401", self.saved)

    async def test_unwatch_during_status_cache_lookup_stops_qualified_delivery(self):
        entered, proceed = asyncio.Event(), asyncio.Event()
        consent = {"active": True}
        service = self.reporting_service()
        async def delayed_read(pool, bot_id, key):
            if key.startswith("mtf_status:"):
                entered.set(); await proceed.wait()
            return await self.read_cache(pool, bot_id, key)
        self.read.side_effect = delayed_read
        bot = SimpleNamespace(send_message=AsyncMock())
        with self.evidence(), patch.object(monitor.market_store, "delivery_active", new_callable=AsyncMock, side_effect=lambda *args: consent["active"]):
            task = asyncio.create_task(service.send_report(bot, 4401, "synthetic-lease"))
            await asyncio.wait_for(entered.wait(), 1)
            consent["active"] = False; proceed.set()
            await asyncio.wait_for(task, 1)
        bot.send_message.assert_not_awaited(); service.news.assert_not_awaited()
        self.assertNotIn("mtf_status:4401", self.saved)

    async def test_expiry_during_status_lookup_cannot_send_old_buy_levels(self):
        service = self.reporting_service()
        async def delayed_read(pool, bot_id, key):
            value = await self.read_cache(pool, bot_id, key)
            if key.startswith("mtf_status:"):
                self.clock_mock.return_value = NOW + timedelta(seconds=76)
            return value
        self.read.side_effect = delayed_read
        bot = SimpleNamespace(send_message=AsyncMock())
        with self.evidence(), patch.object(mtf_runtime, "_clock", side_effect=lambda *args: self.clock_mock.return_value):
            await service.send_report(bot, 4401)
        for call in bot.send_message.await_args_list: self.assert_no_trade(call.args[1])

    async def test_signals_command_rechecks_timing_after_async_risk_lookup(self):
        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()), effective_chat=SimpleNamespace(type="private", id=4401))
        context = SimpleNamespace(bot_data={monitor.SERVICE_KEY: self.service})
        async def delayed_risk(chat_id):
            await asyncio.sleep(0)
            self.clock_mock.return_value = NOW + timedelta(seconds=76)
            return None
        self.service.risk_pause = delayed_risk
        with self.evidence(), patch.object(mtf_runtime, "_clock", side_effect=lambda *args: self.clock_mock.return_value):
            await monitor.signals_command(update, context)
        update.effective_message.reply_text.assert_awaited_once()
        self.assert_no_trade(update.effective_message.reply_text.await_args.args[0])
        self.assertEqual(self.service.active_users, set())

    async def test_signals_command_rechecks_current_exposure_after_async_risk_lookup(self):
        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()), effective_chat=SimpleNamespace(type="private", id=4401))
        context = SimpleNamespace(bot_data={monitor.SERVICE_KEY: self.service})
        async def delayed_risk(chat_id):
            await asyncio.sleep(0)
            self.saved["broker_feed"]["payload"]["risk_context"]["open_positions"] = 1
            return None
        self.service.risk_pause = delayed_risk
        with self.evidence():
            await monitor.signals_command(update, context)
        self.assert_no_trade(update.effective_message.reply_text.await_args.args[0])

    async def test_paused_report_never_shows_proposal_or_creates_automatic_trade(self):
        service = self.reporting_service(); service.journal_enabled = True
        service.risk_pause = AsyncMock(return_value=NOW + timedelta(minutes=15))
        bot = SimpleNamespace(send_message=AsyncMock())
        with self.evidence(), patch.object(monitor.journal_store, "open_trade", new_callable=AsyncMock) as journal, patch.object(
            monitor.trade_store, "create_offer", new_callable=AsyncMock
        ) as offer:
            await service.send_report(bot, 4401)
        self.assertIn("موقوفة", bot.send_message.await_args.args[1]); self.assert_no_trade(bot.send_message.await_args.args[1])
        journal.assert_not_awaited(); offer.assert_not_awaited(); service.news.assert_not_awaited()
        self.assertFalse(service.trading_enabled)


if __name__ == "__main__": unittest.main()
