"""MarketService integration with real review rules and synthetic persistence.

These checks run without credentials or network. The fake storage survives
service recreation, but the service and paper-review engine run unchanged.
"""

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bot import market_monitor as monitor
from bot.market_news import NewsBriefing


OPENED = datetime(2026, 10, 4, 12, 30, tzinfo=timezone.utc)
BOT_ID, CHAT_ID, SIGNAL_ID = 9901, 4401, "synthetic-setup"


def setup_result():
    return {
        "state": "signal", "strategy_id": "ema9-21-atr14-v1",
        "bar_time": "2026-10-04T12:15:00Z", "direction": "BUY",
        "entry": 2700.0, "stop": 2697.0, "target": 2706.0, "atr": 2.0,
    }


def observation(minute, price=2701.0):
    return {"time": (OPENED + timedelta(minutes=minute)).isoformat(), "price": price}


def broker_snapshot(now, bid=2707.0):
    last_closed = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
    candles = []
    for index in range(4):
        candles.append({
            "time": (last_closed - timedelta(minutes=15 * (4 - index))).isoformat(),
            "open": 2700.0, "high": 2708.0, "low": 2698.0,
            "close": 2701.0, "tick_volume": 100,
        })
    return {
        "payload": {
            "symbol": "XAUUSD", "timeframe": "M15", "source": "MetaTrader 5",
            "quote": {"bid": bid, "ask": bid + 0.2, "time": now.isoformat()},
            "candles": candles,
        },
        "updated_at": now,
    }


class MonitorJournalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pool = object()
        self.now = OPENED
        self.cached = {}
        self.rows = {}
        self.active = {CHAT_ID: True}
        self.env = patch.dict(monitor.os.environ, {
            "MARKET_SOURCE": "reference", "MARKET_GOLD_SYMBOL": "XAUUSD",
            "NEWS_SOURCE": "rss", "OPENAI_ENABLED": "false", "PTB_TIMEDELTA": "true",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.clock = patch.object(monitor, "utc_now", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.cache_read = self.mock(monitor.market_store, "get_cache", self.read_cache)
        self.cache_write = self.mock(monitor.market_store, "save_cache", self.write_cache)
        self.disable = self.mock(monitor.market_store, "disable_subscription", self.disable_subscription)
        self.open_trade = self.mock(monitor.journal_store, "open_trade", self.insert_trade)
        self.list_open = self.mock(monitor.journal_store, "list_open", self.read_open)
        self.update_trade = self.mock(monitor.journal_store, "update_trade", self.update_record)
        self.list_reviews = self.mock(monitor.journal_store, "list_reviews", self.read_reviews)
        self.review_active = self.mock(monitor.journal_store, "review_active", self.review_is_active)
        self.mark_sent = self.mock(monitor.journal_store, "mark_review_sent", self.acknowledge_review)
        self.recent = self.mock(monitor.journal_store, "recent_trades", self.recent_records)
        self.service = self.make_service()

    def mock(self, target, name, side_effect):
        patcher = patch.object(target, name, new_callable=AsyncMock, side_effect=side_effect)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def scope(self, pool, bot_id):
        self.assertIs(pool, self.pool)
        self.assertEqual(bot_id, BOT_ID)

    def make_service(self):
        service = monitor.MarketService(self.pool, BOT_ID)
        service.journal_enabled = True
        service.market = AsyncMock(return_value="synthetic market report")
        service.signals = AsyncMock(return_value=(setup_result(), "synthetic BUY setup", SIGNAL_ID))
        service.news = AsyncMock(return_value=NewsBriefing(OPENED, ()))
        return service

    def add_trade(self, source_identity="reference:XAUUSD", signal_id=SIGNAL_ID):
        trade = monitor.paper_journal.create_trade(signal_id, setup_result(), source_identity, OPENED, 15)
        self.rows[(CHAT_ID, signal_id)] = {
            "chat_id": CHAT_ID, "signal_id": signal_id, "payload": trade,
            "updated_at": OPENED, "reviewed_sent": False,
        }
        return trade

    def feed(self, samples):
        self.cached["reference_feed"] = {
            "payload": {
                "price": samples[-1]["price"] if samples else 2700.0,
                "as_of": self.now.isoformat(), "samples": copy.deepcopy(samples),
            },
            "updated_at": self.now,
        }

    async def read_cache(self, pool, bot_id, key):
        self.scope(pool, bot_id)
        return copy.deepcopy(self.cached.get(key))

    async def write_cache(self, pool, bot_id, key, payload, now):
        self.scope(pool, bot_id)
        self.cached[key] = {"payload": copy.deepcopy(payload), "updated_at": now}

    async def disable_subscription(self, pool, bot_id, chat_id):
        self.scope(pool, bot_id)
        changed = self.active.get(chat_id, False)
        self.active[chat_id] = False
        return changed

    async def insert_trade(self, pool, bot_id, chat_id, trade, now):
        self.scope(pool, bot_id)
        key = (chat_id, trade["id"])
        if key in self.rows:
            return False
        self.rows[key] = {
            "chat_id": chat_id, "signal_id": trade["id"], "payload": copy.deepcopy(trade),
            "updated_at": now, "reviewed_sent": False,
        }
        return True

    async def read_open(self, pool, bot_id, limit=100):
        self.scope(pool, bot_id)
        return copy.deepcopy([row for row in self.rows.values() if row["payload"]["status"] == "open"][:limit])

    async def update_record(self, pool, bot_id, chat_id, signal_id, trade, now, *, expected_updated_at=None):
        self.scope(pool, bot_id)
        row = self.rows.get((chat_id, signal_id))
        if row is None or row["payload"]["status"] != "open" or row["updated_at"] != expected_updated_at:
            return False
        row["payload"] = copy.deepcopy(trade)
        row["updated_at"] = max(now, row["updated_at"] + timedelta(microseconds=1))
        return True

    async def read_reviews(self, pool, bot_id, limit=100):
        self.scope(pool, bot_id)
        return copy.deepcopy([
            row for row in self.rows.values()
            if row["payload"]["status"] != "open" and not row["reviewed_sent"]
            and self.active.get(row["chat_id"], False)
        ][:limit])

    async def review_is_active(self, pool, bot_id, chat_id, signal_id):
        self.scope(pool, bot_id)
        row = self.rows.get((chat_id, signal_id))
        return bool(row and row["payload"]["status"] != "open" and not row["reviewed_sent"] and self.active.get(chat_id, False))

    async def acknowledge_review(self, pool, bot_id, chat_id, signal_id, now):
        self.scope(pool, bot_id)
        row = self.rows[(chat_id, signal_id)]
        if row["reviewed_sent"]:
            return False
        row["reviewed_sent"] = True
        return True

    async def recent_records(self, pool, bot_id, chat_id, limit=5):
        self.scope(pool, bot_id)
        return copy.deepcopy([row["payload"] for row in reversed(list(self.rows.values())) if row["chat_id"] == chat_id][:limit])

    def close_at_target(self):
        trade = self.add_trade()
        self.now = OPENED + timedelta(minutes=1)
        self.rows[(CHAT_ID, SIGNAL_ID)]["payload"] = monitor.paper_journal.advance_trade(
            trade, [observation(1, 2706.0)], self.now,
        )

    def add_complete_stop_history(self):
        for index, minutes_before in enumerate((10, 7, 4)):
            opened = OPENED - timedelta(minutes=minutes_before)
            result = setup_result()
            result["strategy_id"] = monitor.paper_signals.STRATEGY_ID
            result["bar_time"] = "2026-10-04T12:00:00Z"
            signal_id = f"completed-stop-{index}"
            trade = monitor.paper_journal.create_trade(signal_id, result, "reference:XAUUSD", opened, 15)
            closed_at = opened + timedelta(minutes=1)
            closed = monitor.paper_journal.advance_trade(
                trade, [{"time": closed_at.isoformat(), "price": 2697.0}], closed_at,
            )
            self.assertEqual(closed["status"], "stop_observed")
            self.assertTrue(closed["coverage_complete"])
            self.rows[(CHAT_ID, signal_id)] = {
                "chat_id": CHAT_ID, "signal_id": signal_id, "payload": closed,
                "updated_at": closed_at, "reviewed_sent": True,
            }

    async def test_successful_signal_delivery_records_declared_fifteen_minute_trade(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        await self.service.send_report(bot, CHAT_ID)
        self.open_trade.assert_awaited_once()
        trade = self.rows[(CHAT_ID, SIGNAL_ID)]["payload"]
        self.assertEqual(trade["status"], "open")
        self.assertEqual(trade["duration_minutes"], 15)
        self.assertEqual(trade["opened_at"], "2026-10-04T12:30:00Z")
        self.assertEqual(trade["deadline"], "2026-10-04T12:45:00Z")
        self.assertEqual(trade["source_identity"], "reference:XAUUSD")
        self.assertEqual((trade["entry"], trade["stop"], trade["target"]), (2700.0, 2697.0, 2706.0))
        signal_message = bot.send_message.await_args_list[1]
        self.assertIn("synthetic BUY setup", signal_message.args[1])
        self.assertIn("15", signal_message.args[1])
        self.assertEqual(signal_message.kwargs, {"parse_mode": None})
        self.assertEqual(self.cached[f"paper_delivery:{CHAT_ID}"]["payload"]["id"], SIGNAL_ID)

    async def test_failed_signal_send_never_opens_or_acknowledges_trade(self):
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=[None, RuntimeError("synthetic send failure")]))
        with self.assertRaises(RuntimeError):
            await self.service.send_report(bot, CHAT_ID)
        self.open_trade.assert_not_awaited()
        self.cache_write.assert_not_awaited()
        self.assertEqual(self.rows, {})
        self.service.news.assert_not_awaited()

    async def test_delayed_signal_ack_starts_window_after_success_and_ignores_earlier_prices(self):
        accepted_at = OPENED + timedelta(minutes=4)

        async def accept_message(chat_id, text, **kwargs):
            if "synthetic BUY setup" in text:
                self.now = accepted_at

        bot = SimpleNamespace(send_message=AsyncMock(side_effect=accept_message))
        await self.service.send_report(bot, CHAT_ID)
        trade = self.rows[(CHAT_ID, SIGNAL_ID)]["payload"]
        self.assertEqual(trade["opened_at"], "2026-10-04T12:34:00Z")
        self.assertEqual(trade["deadline"], "2026-10-04T12:49:00Z")
        self.assertEqual(self.cached[f"paper_delivery:{CHAT_ID}"]["payload"]["sent_at"], accepted_at.isoformat())
        self.assertEqual(self.open_trade.await_args.args[-1], accepted_at)
        # A target-price sample while Telegram was still sending must not be
        # counted as an outcome of a setup that was not yet available.
        self.now = OPENED + timedelta(minutes=5)
        self.feed([observation(1, 2707.0), observation(5, 2701.0)])
        await self.service.review_trades()
        observed = self.rows[(CHAT_ID, SIGNAL_ID)]["payload"]
        self.assertEqual(observed["status"], "open")
        self.assertEqual(observed["last_observation_at"], "2026-10-04T12:35:00Z")
        self.assertEqual(observed["last_observation_price"], 2701.0)
        self.assertIsNone(observed["gross_r"])

    async def test_reference_history_replay_uses_cas_and_closes_exactly_at_deadline(self):
        original = copy.deepcopy(self.add_trade())
        self.now = OPENED + timedelta(minutes=14, seconds=59)
        self.feed([observation(minute, 2700.0 + minute / 10) for minute in range(15)])
        await self.service.review_trades()
        first = self.rows[(CHAT_ID, SIGNAL_ID)]
        self.assertEqual(first["payload"]["status"], "open")
        self.assertEqual(first["payload"]["last_observation_at"], "2026-10-04T12:44:00Z")
        self.assertEqual(self.update_trade.await_args.kwargs, {"expected_updated_at": OPENED})
        previous_version = first["updated_at"]
        self.now = OPENED + timedelta(minutes=15)
        await self.service.review_trades()
        closed = self.rows[(CHAT_ID, SIGNAL_ID)]["payload"]
        self.assertEqual(closed["status"], "expired")
        self.assertEqual(closed["closed_at"], original["deadline"])
        self.assertEqual(closed["deadline"], original["deadline"])
        self.assertEqual(closed["exit_price"], 2701.4)
        self.assertAlmostEqual(closed["gross_r"], 1.4 / 3, places=6)
        self.assertEqual(self.update_trade.await_args.kwargs, {"expected_updated_at": previous_version})
        self.assertEqual((closed["entry"], closed["stop"], closed["target"]), (original["entry"], original["stop"], original["target"]))

    async def test_first_observed_target_closes_before_deadline_and_persists_once(self):
        self.add_trade()
        self.now = OPENED + timedelta(minutes=2)
        self.feed([observation(1, 2707.0), observation(2, 2696.0)])
        await self.service.review_trades()
        closed = self.rows[(CHAT_ID, SIGNAL_ID)]["payload"]
        self.assertEqual(closed["status"], "target_observed")
        self.assertEqual(closed["closed_at"], "2026-10-04T12:31:00Z")
        self.assertEqual(closed["exit_price"], 2707.0)
        self.assertAlmostEqual(closed["gross_r"], 7 / 3, places=6)
        await self.service.review_trades()
        self.update_trade.assert_awaited_once()
        self.assertEqual(self.rows[(CHAT_ID, SIGNAL_ID)]["payload"], closed)

    async def test_missing_samples_produce_inconclusive_review_without_price_result(self):
        self.add_trade()
        self.now = OPENED + timedelta(minutes=15)
        self.feed([observation(minute) for minute in (1, 5, 9, 13, 15)])
        await self.service.review_trades()
        closed = self.rows[(CHAT_ID, SIGNAL_ID)]["payload"]
        self.assertEqual(closed["status"], "inconclusive")
        self.assertFalse(closed["coverage_complete"])
        self.assertEqual(closed["closed_at"], "2026-10-04T12:45:00Z")
        self.assertIsNone(closed["gross_r"])
        self.assertIsNone(closed["exit_price"])
        self.assertGreater(closed["max_gap_seconds"], 180)
        self.assertTrue(closed["review_notes"])

    async def test_malformed_open_row_does_not_block_healthy_trade_review(self):
        malformed = {
            "chat_id": CHAT_ID, "signal_id": "broken-record", "payload": {"status": "open"},
            "updated_at": OPENED, "reviewed_sent": False,
        }
        self.rows[(CHAT_ID, "broken-record")] = copy.deepcopy(malformed)
        self.add_trade()
        self.now = OPENED + timedelta(minutes=1)
        self.feed([observation(1, 2707.0)])
        with self.assertLogs(monitor.logger, level="WARNING"):
            await self.service.review_trades()
        self.assertEqual(self.rows[(CHAT_ID, "broken-record")], malformed)
        self.assertEqual(self.rows[(CHAT_ID, SIGNAL_ID)]["payload"]["status"], "target_observed")
        self.update_trade.assert_awaited_once()
        self.assertEqual(self.update_trade.await_args.args[3], SIGNAL_ID)

    async def test_malformed_pending_review_does_not_block_healthy_review_delivery(self):
        malformed = {
            "chat_id": CHAT_ID, "signal_id": "broken-record", "payload": {"status": "target_observed"},
            "updated_at": OPENED, "reviewed_sent": False,
        }
        self.rows[(CHAT_ID, "broken-record")] = copy.deepcopy(malformed)
        self.close_at_target()
        bot = SimpleNamespace(send_message=AsyncMock())
        with self.assertLogs(monitor.logger, level="WARNING"):
            finished = await self.service.send_reviews(bot)
        self.assertTrue(finished)
        bot.send_message.assert_awaited_once()
        self.mark_sent.assert_awaited_once_with(self.pool, BOT_ID, CHAT_ID, SIGNAL_ID, self.now)
        self.assertEqual(self.rows[(CHAT_ID, "broken-record")], malformed)
        self.assertTrue(self.rows[(CHAT_ID, SIGNAL_ID)]["reviewed_sent"])

    async def test_unwatch_after_review_selection_prevents_send_and_acknowledgment(self):
        self.close_at_target()
        selected = await self.read_reviews(self.pool, BOT_ID)
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(id=CHAT_ID, type="private"))
        context = SimpleNamespace(bot_data={monitor.SERVICE_KEY: self.service})

        async def selected_before_unwatch(pool, bot_id, limit=100):
            self.scope(pool, bot_id)
            await monitor.unwatch_command(update, context)
            return selected

        self.list_reviews.side_effect = selected_before_unwatch
        bot = SimpleNamespace(send_message=AsyncMock())
        await self.service.send_reviews(bot)
        self.disable.assert_awaited_once_with(self.pool, BOT_ID, CHAT_ID)
        self.review_active.assert_awaited_once_with(self.pool, BOT_ID, CHAT_ID, SIGNAL_ID)
        bot.send_message.assert_not_awaited()
        self.mark_sent.assert_not_awaited()
        self.assertFalse(self.rows[(CHAT_ID, SIGNAL_ID)]["reviewed_sent"])

    async def test_failed_review_send_stays_pending_and_can_be_retried_after_restart(self):
        self.close_at_target()
        failed_bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("synthetic send failure")))
        with self.assertLogs(monitor.logger, level="WARNING"):
            await self.service.send_reviews(failed_bot)
        self.mark_sent.assert_not_awaited()
        self.assertFalse(self.rows[(CHAT_ID, SIGNAL_ID)]["reviewed_sent"])
        restarted = self.make_service()
        successful_bot = SimpleNamespace(send_message=AsyncMock())
        await restarted.send_reviews(successful_bot)
        successful_bot.send_message.assert_awaited_once()
        self.mark_sent.assert_awaited_once_with(self.pool, BOT_ID, CHAT_ID, SIGNAL_ID, self.now)
        self.assertTrue(self.rows[(CHAT_ID, SIGNAL_ID)]["reviewed_sent"])
        await restarted.send_reviews(successful_bot)
        successful_bot.send_message.assert_awaited_once()

    async def test_review_retry_after_saves_backoff_stops_batch_and_leaves_reviews_pending(self):
        self.close_at_target()
        second = copy.deepcopy(self.rows[(CHAT_ID, SIGNAL_ID)])
        second["signal_id"] = "second-setup"
        second["payload"]["id"] = "second-setup"
        self.rows[(CHAT_ID, "second-setup")] = second
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=monitor.RetryAfter(120)))
        finished = await self.service.send_reviews(bot)
        self.assertFalse(finished)
        bot.send_message.assert_awaited_once()
        self.mark_sent.assert_not_awaited()
        self.assertFalse(any(row["reviewed_sent"] for row in self.rows.values()))
        expected_until = self.now + timedelta(seconds=120)
        self.cache_write.assert_awaited_once_with(
            self.pool, BOT_ID, "telegram_backoff", {"until": expected_until.isoformat()}, self.now,
        )
        self.assertEqual(self.cached["telegram_backoff"]["payload"]["until"], expected_until.isoformat())

    async def test_three_complete_stops_pause_new_signals_for_that_chat_and_keep_other_chat_active(self):
        self.add_complete_stop_history()
        before = copy.deepcopy(self.rows)
        paused_bot = SimpleNamespace(send_message=AsyncMock())
        await self.service.send_report(paused_bot, CHAT_ID)
        self.open_trade.assert_not_awaited()
        self.assertEqual(self.rows, before)
        self.assertNotIn(f"paper_delivery:{CHAT_ID}", self.cached)
        self.assertFalse(any("synthetic BUY setup" in call.args[1] for call in paused_bot.send_message.await_args_list))
        self.assertTrue(any("موقوفة" in call.args[1] and "3" in call.args[1] and "13:12 UTC" in call.args[1]
                            for call in paused_bot.send_message.await_args_list))
        self.recent.assert_awaited_with(self.pool, BOT_ID, CHAT_ID, limit=20)
        self.service.news.assert_not_awaited()
        other_chat = CHAT_ID + 1
        active_bot = SimpleNamespace(send_message=AsyncMock())
        await self.service.send_report(active_bot, other_chat)
        self.open_trade.assert_awaited_once()
        self.assertEqual(self.open_trade.await_args.args[2], other_chat)
        self.assertEqual(self.rows[(other_chat, SIGNAL_ID)]["payload"]["status"], "open")
        self.assertTrue(any("synthetic BUY setup" in call.args[1] for call in active_bot.send_message.await_args_list))
        self.recent.assert_awaited_with(self.pool, BOT_ID, other_chat, limit=20)

    async def test_completed_stops_from_another_strategy_do_not_pause_current_strategy(self):
        self.add_complete_stop_history()
        for row in self.rows.values():
            row["payload"]["strategy_id"] = "another-strategy"
        bot = SimpleNamespace(send_message=AsyncMock())
        await self.service.send_report(bot, CHAT_ID)
        self.open_trade.assert_awaited_once()
        self.assertEqual(self.rows[(CHAT_ID, SIGNAL_ID)]["payload"]["strategy_id"], monitor.paper_signals.STRATEGY_ID)
        self.assertTrue(any("synthetic BUY setup" in call.args[1] for call in bot.send_message.await_args_list))

    async def test_provider_switch_cannot_use_broker_quote_to_resolve_reference_trade(self):
        original = copy.deepcopy(self.add_trade())
        switched = self.make_service()
        switched.source = "mt5"
        self.now = OPENED + timedelta(minutes=1)
        self.cached["broker_feed"] = broker_snapshot(self.now, bid=2707.0)
        await switched.review_trades()
        self.assertEqual(self.rows[(CHAT_ID, SIGNAL_ID)]["payload"], original)
        self.update_trade.assert_not_awaited()
        self.now = OPENED + timedelta(minutes=15)
        self.cached["broker_feed"] = broker_snapshot(self.now, bid=2707.0)
        await switched.review_trades()
        self.assertEqual(self.rows[(CHAT_ID, SIGNAL_ID)]["payload"], original)
        self.list_open.assert_not_awaited()
        self.update_trade.assert_not_awaited()

    async def test_mt5_legacy_journal_is_never_advanced_delivered_or_used_for_pause(self):
        self.add_complete_stop_history()
        for row in self.rows.values():
            row["payload"]["source_identity"] = "mt5:XAUUSD"
        self.add_trade("mt5:XAUUSD", "legacy-open")
        original = copy.deepcopy(self.rows)
        self.service.source = "mt5"
        self.now = OPENED + timedelta(minutes=30)
        self.cached["broker_feed"] = broker_snapshot(self.now, bid=2696.0)
        bot = SimpleNamespace(send_message=AsyncMock())
        await self.service.review_trades()
        self.assertTrue(await self.service.send_reviews(bot))
        self.assertIsNone(await self.service.risk_pause(CHAT_ID))
        self.assertEqual(self.rows, original)
        for store in (self.cache_read, self.list_open, self.update_trade, self.list_reviews, self.recent, self.mark_sent):
            store.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    async def test_requested_mt5_reviews_are_historical_read_only_archive(self):
        self.add_trade("mt5:XAUUSD")
        original = copy.deepcopy(self.rows)
        self.service.source = "mt5"
        self.service.review_trades = AsyncMock(side_effect=AssertionError("Archive must not advance records"))
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(id=CHAT_ID, type="private"))
        context = SimpleNamespace(bot_data={monitor.SERVICE_KEY: self.service})
        await monitor.reviews_command(update, context)
        self.service.review_trades.assert_not_awaited()
        self.recent.assert_awaited_once_with(self.pool, BOT_ID, CHAT_ID, limit=3)
        self.update_trade.assert_not_awaited()
        self.mark_sent.assert_not_awaited()
        self.assertEqual(self.rows, original)
        header, stored = [call.args[0] for call in message.reply_text.await_args_list]
        for marker in ("أرشيف", "عرض عند الطلب", "لا تُحدّث", "60 دقيقة", "ولا تُستخدم كأدلة", "/signals"):
            self.assertIn(marker, header)
        self.assertIn("سجل تاريخي كما حُفظ", stored)
        self.assertIn("لا توجد متابعة حالية", stored)
        self.assertIn("الحالة المحفوظة: مفتوح", stored)
        self.assertNotIn("المتابعة مستمرة", stored)
        self.assertIn("15 دقيقة", stored)
        self.assertEqual(self.service.active_users, set())

    async def test_empty_mt5_archive_does_not_promote_new_legacy_tracking(self):
        self.service.source = "mt5"
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(id=CHAT_ID, type="private"))
        context = SimpleNamespace(bot_data={monitor.SERVICE_KEY: self.service})
        await monitor.reviews_command(update, context)
        text = "\n".join(call.args[0] for call in message.reply_text.await_args_list)
        self.assertIn("لا توجد سجلات ورقية قديمة", text)
        self.assertNotIn("/watch", text)
        self.list_open.assert_not_awaited()

    async def test_requested_reference_review_still_advances_live_paper_record(self):
        self.add_trade()
        self.now = OPENED + timedelta(minutes=1)
        self.feed([observation(1, 2707.0)])
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(id=CHAT_ID, type="private"))
        context = SimpleNamespace(bot_data={monitor.SERVICE_KEY: self.service})
        await monitor.reviews_command(update, context)
        self.update_trade.assert_awaited_once()
        self.assertEqual(self.rows[(CHAT_ID, SIGNAL_ID)]["payload"]["status"], "target_observed")
        message.reply_text.assert_awaited_once()
        self.assertNotIn("أرشيف", message.reply_text.await_args.args[0])

    async def test_restart_reads_stored_trade_observations_and_deduplication(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        await self.service.send_report(bot, CHAT_ID)
        self.now = OPENED + timedelta(minutes=2)
        self.feed([observation(1), observation(2)])
        await self.service.review_trades()
        version_before_restart = self.rows[(CHAT_ID, SIGNAL_ID)]["updated_at"]
        restarted = self.make_service()
        repeated_bot = SimpleNamespace(send_message=AsyncMock())
        await restarted.send_report(repeated_bot, CHAT_ID)
        self.open_trade.assert_awaited_once()
        self.assertFalse(any("synthetic BUY setup" in call.args[1] for call in repeated_bot.send_message.await_args_list))
        self.now = OPENED + timedelta(minutes=15)
        self.feed([observation(minute) for minute in range(1, 16)])
        await restarted.review_trades()
        self.assertEqual(self.update_trade.await_args.kwargs, {"expected_updated_at": version_before_restart})
        closed = self.rows[(CHAT_ID, SIGNAL_ID)]["payload"]
        self.assertEqual(closed["status"], "expired")
        self.assertTrue(closed["coverage_complete"])
        self.assertEqual(closed["opened_at"], "2026-10-04T12:30:00Z")


if __name__ == "__main__":
    unittest.main()
