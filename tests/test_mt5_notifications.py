"""Synthetic Telegram/store tests never contact MT5 or execute a trade."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from telegram import Chat, Message, User
from telegram.error import Forbidden, RetryAfter

from bot import mt5_notifications as notifications


NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
BOT_ID, OWNER_ID, MESSAGE_ID = 999, 101, 77
DEVICE_ID = UUID("12345678-1234-5678-1234-567812345678")
OFFER_ID = UUID("87654321-4321-8765-4321-876543218765")


def device():
    return {
        "bot_id": BOT_ID, "device_id": DEVICE_ID, "symbol": "XAUUSD",
        "account_mode": "demo", "volume": 0.01,
        "owner_chat_id": OWNER_ID, "owner_user_id": OWNER_ID, "last_seen_at": NOW,
    }


def signal():
    return {
        "state": "signal", "strategy_id": "ema9-21-atr14-v1",
        "bar_time": "2026-10-04T11:45:00Z", "direction": "BUY",
        "entry": 100.0, "stop": 97.0, "target": 106.0,
    }


def offer(status="offered"):
    payload = signal()
    payload.update(symbol="XAUUSD", account_mode="demo", volume=0.01, max_drift_r=0.1,
                   price_digits=3, original_stop_distance=3.0,
                   execution={"tick_size": 0.001, "point": 0.001, "digits": 3, "stops_level": 0})
    return {
        "id": OFFER_ID, "bot_id": BOT_ID, "device_id": DEVICE_ID,
        "chat_id": OWNER_ID, "user_id": OWNER_ID, "signal_id": "f" * 64,
        "payload": payload, "status": status, "message_id": MESSAGE_ID,
        "created_at": NOW - timedelta(minutes=1), "expires_at": NOW + timedelta(minutes=5),
        "result": None, "decided_at": None, "executing_at": None,
    }


def snapshot():
    return {
        "updated_at": NOW,
        "payload": {
            "symbol": "XAUUSD", "timeframe": "M15", "source": "MetaTrader 5", "device_id": str(DEVICE_ID),
            "quote": {"bid": 100.0, "ask": 100.2, "time": NOW.isoformat()},
            "execution": {"tick_size": 0.001, "point": 0.001, "digits": 3, "stops_level": 0},
            "candles": [
                {"time": (NOW - timedelta(minutes=15 * (22 - i))).isoformat(),
                 "open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0, "tick_volume": 100}
                for i in range(22)
            ],
        },
    }


def message(*, chat_id=OWNER_ID, user_id=BOT_ID, message_id=MESSAGE_ID, chat_type="private"):
    return Message(message_id, NOW, Chat(chat_id, chat_type), from_user=User(user_id, "synthetic", user_id == BOT_ID), text="Synthetic MT5 signal")


class MT5NotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.device, self.offer, self.snapshot = device(), offer(), snapshot()
        self.service = SimpleNamespace(
            pool=object(), bot_id=BOT_ID, source="mt5", trading_enabled=True,
            risk_pause=AsyncMock(return_value=None), telegram_backoff=AsyncMock(),
            signals=AsyncMock(return_value=(signal(), "MetaTrader 5", "f" * 64)),
            order_send=AsyncMock(),
        )
        self.context = SimpleNamespace(bot_data={notifications.market_monitor.SERVICE_KEY: self.service}, args=[])
        self.query = SimpleNamespace(
            data=f"mt5:a:{OFFER_ID.hex}", message=message(), from_user=User(OWNER_ID, "owner", False),
            inline_message_id=None, answer=AsyncMock(), edit_message_text=AsyncMock(),
        )
        self.update = SimpleNamespace(callback_query=self.query, effective_message=None, effective_user=self.query.from_user)
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=message()), edit_message_reply_markup=AsyncMock())
        self.patches = []
        self.store = {}
        defaults = {
            "get_offer": None, "get_device": None, "decide": None, "pair_device": None,
            "subscription_active": True, "list_paired_devices": [], "create_offer": None,
            "publish_offer": True, "expire_offers": 0, "list_notifications": [], "mark_notified": True,
        }
        for name, value in defaults.items():
            self.store[name] = self.start(patch.object(notifications.trade_store, name, new_callable=AsyncMock, return_value=value))
        self.store["get_offer"].side_effect = lambda *args: deepcopy(self.offer)
        self.store["get_device"].side_effect = lambda *args: deepcopy(self.device)
        self.feed = self.start(patch.object(notifications.market_store, "get_cache", new_callable=AsyncMock))
        self.feed.side_effect = lambda *args: deepcopy(self.snapshot)
        self.enabled = self.start(patch.object(notifications.market_store, "enable_subscription", new_callable=AsyncMock))
        self.disabled = self.start(patch.object(notifications.market_store, "disable_subscription", new_callable=AsyncMock))
        self.reply = self.start(patch.object(Message, "reply_text", new_callable=AsyncMock))
        self.start(patch.object(notifications.market_monitor, "utc_now", return_value=NOW))
        self.start(patch.dict(notifications.os.environ, {"MARKET_GOLD_SYMBOL": "XAUUSD"}, clear=True))
        self.store["decide"].side_effect = self.decide

    def start(self, patcher):
        self.patches.append(patcher)
        self.addCleanup(patcher.stop)
        return patcher.start()

    async def decide(self, pool, bot_id, offer_id, chat_id, user_id, message_id, decision, now):
        if self.offer["status"] != "offered" or now >= self.offer["expires_at"]:
            return None
        self.offer["status"] = decision
        return deepcopy(self.offer)

    async def run_callback(self, action="a"):
        self.query.data = f"mt5:{action}:{OFFER_ID.hex}"
        await notifications.decision_callback(self.update, self.context)

    async def test_authorised_accept_only_queues_and_replay_does_not_reapprove_or_claim_fill(self):
        await self.run_callback()
        self.assertEqual(self.offer["status"], "accepted")
        self.assertEqual(self.store["decide"].await_args.args[-2], "accepted")
        self.assertIn("لم يتأكد تنفيذ", self.query.answer.await_args.args[0])
        self.assertEqual(self.query.edit_message_text.await_args.kwargs["reply_markup"], None)
        await self.run_callback("r")
        self.assertEqual(self.offer["status"], "accepted")
        self.assertEqual(self.store["decide"].await_count, 1)
        self.service.order_send.assert_not_awaited()

    async def test_reject_remains_available_when_quote_stale_source_changes_and_risk_pause_active(self):
        self.snapshot["payload"]["quote"]["time"] = (NOW - timedelta(hours=1)).isoformat()
        self.service.source = "reference"
        self.service.risk_pause.return_value = NOW + timedelta(minutes=45)
        await self.run_callback("r")
        self.assertEqual(self.offer["status"], "rejected")
        self.feed.assert_not_awaited()
        self.service.risk_pause.assert_not_awaited()
        self.service.order_send.assert_not_awaited()

    async def test_concurrent_accept_reject_has_one_durable_winner(self):
        lock = asyncio.Lock()
        async def decide_once(*args):
            async with lock:
                return await self.decide(*args)
        self.store["decide"].side_effect = decide_once
        second = deepcopy(self.query)
        second.data = f"mt5:r:{OFFER_ID.hex}"
        update2 = SimpleNamespace(callback_query=second)
        await asyncio.gather(notifications.decision_callback(self.update, self.context), notifications.decision_callback(update2, self.context))
        self.assertIn(self.offer["status"], ("accepted", "rejected"))
        self.service.order_send.assert_not_awaited()

    async def test_wrong_owner_chat_bot_message_or_device_never_mutates_offer(self):
        for field, value in (("user_id", 202), ("chat_id", 202), ("bot_id", 202), ("message_id", 202)):
            with self.subTest(field=field):
                self.offer = offer()
                self.offer[field] = value
                await self.run_callback()
                self.assertEqual(self.offer["status"], "offered")
        self.offer = offer()
        self.device["owner_user_id"] = 202
        await self.run_callback()
        self.store["decide"].assert_not_awaited()
        self.assertTrue(self.query.answer.await_args.kwargs["show_alert"])

    async def test_groups_inline_inaccessible_wrong_sender_and_malformed_callbacks_are_answered(self):
        cases = (
            {"message": message(chat_type="group")}, {"message": None},
            {"message": SimpleNamespace(chat=Chat(OWNER_ID, "private"), message_id=MESSAGE_ID)},
            {"inline_message_id": "inline-id"}, {"message": message(user_id=202)},
            {"from_user": User(202, "other", False)},
            {"data": "mt5:a:" + "x" * 64}, {"data": "mt5:x:" + OFFER_ID.hex}, {"data": 42},
        )
        baseline = dict(vars(self.query))
        for changes in cases:
            with self.subTest(changes=list(changes)):
                self.query.__dict__.update(baseline)
                self.query.__dict__.update(changes)
                await notifications.decision_callback(self.update, self.context)
        self.store["get_offer"].assert_not_awaited()
        self.store["decide"].assert_not_awaited()
        self.assertEqual(self.query.answer.await_count, len(cases))

    async def test_exact_expiry_inactive_subscription_and_paused_risk_block_accept(self):
        self.offer["expires_at"] = NOW
        await self.run_callback()
        self.assertIn("انتهت", self.query.answer.await_args.args[0])
        self.offer = offer()
        self.store["subscription_active"].return_value = False
        await self.run_callback()
        self.store["subscription_active"].return_value = True
        self.service.risk_pause.return_value = NOW + timedelta(minutes=45)
        await self.run_callback()
        self.store["decide"].assert_not_awaited()

    async def test_original_quote_receipt_and_device_heartbeat_must_all_be_fresh(self):
        for kind in ("quote", "receipt", "device", "future"):
            with self.subTest(kind=kind):
                self.snapshot, self.device = snapshot(), device()
                if kind == "quote":
                    self.snapshot["payload"]["quote"]["time"] = (NOW - timedelta(seconds=181)).isoformat()
                elif kind == "receipt":
                    self.snapshot["updated_at"] = NOW - timedelta(seconds=181)
                elif kind == "device":
                    self.device["last_seen_at"] = NOW - timedelta(seconds=181)
                else:
                    self.snapshot["payload"]["quote"]["time"] = (NOW + timedelta(seconds=1)).isoformat()
                await self.run_callback()
        self.store["decide"].assert_not_awaited()

    async def test_source_symbol_device_and_changed_account_settings_block_accept(self):
        self.service.source = "reference"
        await self.run_callback()
        self.service.source = "mt5"
        self.snapshot["payload"]["device_id"] = str(UUID(int=1))
        await self.run_callback()
        self.snapshot = snapshot()
        self.snapshot["payload"]["symbol"] = "XAUUSD.m"
        await self.run_callback()
        self.snapshot = snapshot()
        self.device["volume"] = 0.02
        await self.run_callback()
        self.device = device()
        self.device["account_mode"] = "real"
        await self.run_callback()
        self.store["decide"].assert_not_awaited()

    async def test_slow_approval_work_cannot_use_snapshot_that_has_become_stale(self):
        with patch.object(notifications.market_monitor, "utc_now", side_effect=[NOW, NOW + timedelta(minutes=4)]):
            await self.run_callback()
        self.store["decide"].assert_not_awaited()

    async def test_store_failure_and_message_edit_failure_do_not_echo_private_errors_or_change_replay(self):
        self.store["decide"].side_effect = RuntimeError("PRIVATE credentials")
        with self.assertLogs(notifications.logger, level="WARNING") as logs:
            await self.run_callback()
        self.assertNotIn("PRIVATE", " ".join(logs.output))
        self.assertNotIn("PRIVATE", self.query.answer.await_args.args[0])
        self.store["decide"].side_effect = self.decide
        self.query.edit_message_text.side_effect = RuntimeError("PRIVATE url")
        with self.assertLogs(notifications.logger, level="WARNING"):
            await self.run_callback()
            await self.run_callback("r")
        self.assertEqual(self.offer["status"], "accepted")
        self.assertEqual(self.store["decide"].await_count, 2)

    async def test_pairing_hashes_normalised_short_code_and_enables_owner_subscription(self):
        self.update.effective_message = message(user_id=OWNER_ID)
        self.context.args = ["ab12-cd34", "ef56"]
        self.store["pair_device"].return_value = self.device
        await notifications.connect_mt5_command(self.update, self.context)
        self.assertEqual(self.store["pair_device"].await_args.args[2], hashlib.sha256(b"AB12CD34EF56").hexdigest())
        self.enabled.assert_awaited_once_with(self.service.pool, BOT_ID, OWNER_ID, NOW)
        self.assertIn("Demo", self.reply.await_args.args[0])
        self.assertIn("0.01", self.reply.await_args.args[0])
        self.assertNotIn("AB12", self.reply.await_args.args[0])

    async def test_bad_used_expired_codes_and_public_pairing_do_not_subscribe(self):
        self.update.effective_message = message(user_id=OWNER_ID)
        for args in ([], ["short"], ["x" * 65], ["invalid!"]):
            self.context.args = args
            await notifications.connect_mt5_command(self.update, self.context)
        self.store["pair_device"].assert_not_awaited()
        self.context.args = ["AB12CD34EF56"]
        await notifications.connect_mt5_command(self.update, self.context)
        self.update.effective_message = message(user_id=OWNER_ID, chat_type="group")
        await notifications.connect_mt5_command(self.update, self.context)
        self.enabled.assert_not_awaited()

    async def prepare_offer(self):
        self.store["list_paired_devices"].return_value = [self.device]
        async def create(*args):
            self.offer = offer("draft")
            self.offer.update(payload=deepcopy(args[6]), created_at=args[7], expires_at=args[8], message_id=None)
            return deepcopy(self.offer)
        self.store["create_offer"].side_effect = create

    async def test_offer_is_draft_before_send_binds_message_and_displays_demo_lot_drift_and_short_callbacks(self):
        await self.prepare_offer()
        async def send(*args, **kwargs):
            self.store["create_offer"].assert_awaited_once()
            self.store["publish_offer"].assert_not_awaited()
            return message()
        self.bot.send_message.side_effect = send
        self.assertTrue(await notifications.send_offers(self.service, self.bot))
        self.store["publish_offer"].assert_awaited_once_with(self.service.pool, BOT_ID, OFFER_ID, MESSAGE_ID, NOW)
        sent = self.bot.send_message.await_args
        for phrase in ("Demo", "0.01", "0.1R", "مرجعي", "لا يؤكد"):
            self.assertIn(phrase, sent.args[1])
        for button in sent.kwargs["reply_markup"].inline_keyboard[0]:
            self.assertLessEqual(len(button.callback_data.encode("utf-8")), 64)
            self.assertIsNotNone(notifications.CALLBACK_PATTERN.fullmatch(button.callback_data))
        self.assertEqual(self.store["create_offer"].await_args.args[-1], NOW + timedelta(minutes=5))

    async def test_broker_raw_close_and_outward_tick_grid_are_frozen_before_display(self):
        await self.prepare_offer()
        for direction, stop, target, rounded_stop, rounded_target in (
            ("BUY", 97.13, 106.13, 97.10, 106.15),
            ("SELL", 103.13, 94.13, 103.15, 94.10),
        ):
            with self.subTest(direction=direction):
                current = signal()
                # The paper signal uses two decimals; execution must preserve
                # the matching broker candle's actual three-decimal close.
                current.update(direction=direction, entry=100.12, stop=stop, target=target)
                self.service.signals.return_value = (current, "MetaTrader 5", "f" * 64)
                self.snapshot["payload"]["candles"][-1]["close"] = 100.125
                self.snapshot["payload"]["execution"]["tick_size"] = 0.05
                await notifications.send_offers(self.service, self.bot)
                payload = self.store["create_offer"].await_args.args[6]
                self.assertEqual(payload["entry"], 100.125)
                self.assertEqual(payload["stop"], rounded_stop)
                self.assertEqual(payload["target"], rounded_target)
                self.assertEqual(payload["price_digits"], 3)
                self.assertAlmostEqual(payload["original_stop_distance"], 3.025)
                self.assertTrue(notifications._offer_grid_matches(payload, self.snapshot["payload"]))
                text = self.bot.send_message.await_args.args[1]
                for value in ("100.125", f"{rounded_stop:.3f}", f"{rounded_target:.3f}"):
                    self.assertIn(value, text)
                frozen = deepcopy(payload)
                self.snapshot["payload"]["candles"][-1]["close"] = 101.0
                self.assertEqual(self.offer["payload"], frozen)

    def test_non_power_of_ten_tick_is_a_grid_and_not_just_display_precision(self):
        current, feed = signal(), snapshot()["payload"]
        current.update(stop=97.131, target=106.131)
        feed["execution"]["tick_size"] = 0.025
        payload = notifications._normalise_signal(current, feed)
        self.assertEqual((payload["stop"], payload["target"]), (97.125, 106.15))
        self.assertTrue(notifications._offer_grid_matches(payload, feed))

    async def test_missing_invalid_or_unrepresentable_broker_metadata_never_drafts_an_offer(self):
        await self.prepare_offer()
        cases = (
            None, {"tick_size": 0.001},
            {"tick_size": 0, "point": 0.001, "digits": 3, "stops_level": 0},
            {"tick_size": 0.0005, "point": 0.001, "digits": 3, "stops_level": 0},
            {"tick_size": 0.001, "point": 0.001, "digits": True, "stops_level": 0},
            {"tick_size": 0.001, "point": 0.001, "digits": 3, "stops_level": -1},
        )
        for metadata in cases:
            with self.subTest(metadata=metadata):
                self.snapshot = snapshot()
                if metadata is None:
                    del self.snapshot["payload"]["execution"]
                else:
                    self.snapshot["payload"]["execution"] = metadata
                await notifications.send_offers(self.service, self.bot)
        self.store["create_offer"].assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_missing_trigger_candle_unrepresentable_close_and_invalid_rounded_levels_suppress_offer(self):
        await self.prepare_offer()
        for condition in ("missing", "precision", "collapse", "nonpositive"):
            with self.subTest(condition=condition):
                self.snapshot = snapshot()
                current = signal()
                if condition == "missing":
                    self.snapshot["payload"]["candles"].pop()
                elif condition == "precision":
                    self.snapshot["payload"]["candles"][-1]["close"] = 100.1234
                elif condition == "collapse":
                    current["target"] = 100.0
                else:
                    current["stop"] = 0.0001
                self.service.signals.return_value = (current, "MetaTrader 5", "f" * 64)
                with self.assertLogs(notifications.logger, level="WARNING"):
                    await notifications.send_offers(self.service, self.bot)
        self.store["create_offer"].assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_accept_rechecks_frozen_grid_precision_and_stop_distance(self):
        for condition in ("digits", "tick", "distance", "unbounded"):
            with self.subTest(condition=condition):
                self.snapshot, self.offer = snapshot(), offer()
                if condition == "digits":
                    self.snapshot["payload"]["execution"]["digits"] = 2
                    self.snapshot["payload"]["execution"]["tick_size"] = 0.01
                elif condition == "tick":
                    self.snapshot["payload"]["execution"]["tick_size"] = 0.3
                elif condition == "distance":
                    self.offer["payload"]["original_stop_distance"] = 2.9
                else:
                    self.offer["payload"]["entry"] = 1e308
                await self.run_callback()
                self.assertEqual(self.offer["status"], "offered")
        self.store["decide"].assert_not_awaited()

    async def test_offer_expiry_is_anchored_to_trigger_bar_and_retries_use_frozen_draft(self):
        await self.prepare_offer()
        late = NOW + timedelta(minutes=14)
        self.device["last_seen_at"] = late
        self.snapshot["updated_at"] = late
        self.snapshot["payload"]["quote"]["time"] = late.isoformat()
        with patch.object(notifications.market_monitor, "utc_now", return_value=late):
            await notifications.send_offers(self.service, self.bot)
        self.assertEqual(self.store["create_offer"].await_args.args[-1], NOW + timedelta(minutes=15))
        original = deepcopy(self.offer)
        self.store["create_offer"].side_effect = None
        self.store["create_offer"].return_value = original
        with patch.object(notifications.market_monitor, "utc_now", return_value=late + timedelta(seconds=20)):
            await notifications.send_offers(self.service, self.bot)
        self.assertIn("12:15:00", self.bot.send_message.await_args.args[1])

    async def test_offline_inactive_wrong_device_no_signal_and_paused_owners_get_no_offer(self):
        await self.prepare_offer()
        self.device["last_seen_at"] = NOW - timedelta(minutes=4)
        await notifications.send_offers(self.service, self.bot)
        self.device = device()
        self.store["list_paired_devices"].return_value = [self.device]
        self.store["subscription_active"].return_value = False
        await notifications.send_offers(self.service, self.bot)
        self.store["subscription_active"].return_value = True
        self.service.risk_pause.return_value = NOW + timedelta(minutes=45)
        await notifications.send_offers(self.service, self.bot)
        self.service.risk_pause.return_value = None
        self.snapshot["payload"]["device_id"] = str(UUID(int=1))
        await notifications.send_offers(self.service, self.bot)
        self.snapshot = snapshot()
        self.service.signals.return_value = ({"state": "no_signal"}, "", None)
        await notifications.send_offers(self.service, self.bot)
        self.bot.send_message.assert_not_awaited()
        self.store["create_offer"].assert_not_awaited()

    async def test_persisted_real_or_different_volume_cannot_offer_or_accept_but_can_be_rejected(self):
        await self.prepare_offer()
        for account_mode, volume in (("real", 0.01), ("demo", 0.02)):
            with self.subTest(account_mode=account_mode, volume=volume):
                self.device = device()
                self.device.update(account_mode=account_mode, volume=volume)
                self.store["list_paired_devices"].return_value = [self.device]
                self.offer = offer()
                self.offer["payload"].update(account_mode=account_mode, volume=volume)
                await notifications.send_offers(self.service, self.bot)
                await self.run_callback()
                self.assertEqual(self.offer["status"], "offered")
                await self.run_callback("r")
                self.assertEqual(self.offer["status"], "rejected")
        self.bot.send_message.assert_not_awaited()
        self.store["create_offer"].assert_not_awaited()
        self.assertTrue(all(call.args[-2] == "rejected" for call in self.store["decide"].await_args_list))

    async def test_delivery_failure_never_publishes_and_throttle_or_forbidden_handle_subscription(self):
        await self.prepare_offer()
        self.bot.send_message.side_effect = RuntimeError("PRIVATE endpoint")
        with self.assertLogs(notifications.logger, level="WARNING") as logs:
            await notifications.send_offers(self.service, self.bot)
        self.assertNotIn("PRIVATE", " ".join(logs.output))
        self.store["publish_offer"].assert_not_awaited()
        self.bot.send_message.side_effect = RetryAfter(10)
        self.assertFalse(await notifications.send_offers(self.service, self.bot))
        self.service.telegram_backoff.assert_awaited_once()
        self.bot.send_message.side_effect = Forbidden("blocked")
        await notifications.send_offers(self.service, self.bot)
        self.disabled.assert_awaited_once_with(self.service.pool, BOT_ID, OWNER_ID)

    async def test_failed_publication_removes_buttons_and_existing_terminal_offer_is_not_resent(self):
        await self.prepare_offer()
        self.store["publish_offer"].return_value = False
        await notifications.send_offers(self.service, self.bot)
        self.bot.edit_message_reply_markup.assert_awaited_once_with(OWNER_ID, MESSAGE_ID, reply_markup=None)
        self.store["create_offer"].side_effect = None
        self.store["create_offer"].return_value = offer("accepted")
        await notifications.send_offers(self.service, self.bot)
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_results_use_reported_status_and_unknown_never_claims_fill_or_retries_order(self):
        for status in ("filled", "failed", "unknown"):
            current = offer(status)
            current["result"] = {"status": status}
            self.store["list_notifications"].return_value = [current]
            await notifications.send_results(self.service, self.bot)
            text = self.bot.send_message.await_args.args[1]
            self.assertEqual("أكد MT5 تنفيذ" in text, status == "filled")
            if status == "unknown":
                self.assertIn("تحقق من MT5", text)
                self.assertIn("لا تُجرى إعادة", text)
        self.assertEqual(self.store["mark_notified"].await_count, 3)
        inconsistent = offer("filled")
        inconsistent["result"] = {"status": "unknown"}
        self.store["list_notifications"].return_value = [inconsistent]
        await notifications.send_results(self.service, self.bot)
        self.assertNotIn("أكد MT5 تنفيذ", self.bot.send_message.await_args.args[1])
        self.service.order_send.assert_not_awaited()

    async def test_approved_unclaimed_expiry_or_cancellation_reports_that_no_order_was_sent(self):
        for status in ("expired", "cancelled"):
            current = offer(status)
            current["decided_at"] = NOW - timedelta(minutes=1)
            self.store["list_notifications"].return_value = [current]
            await notifications.send_results(self.service, self.bot)
            text = self.bot.send_message.await_args.args[1]
            self.assertIn("لم يُرسل أي أمر تداول", text)
            self.assertNotIn("غير مؤكدة", text)
            self.assertNotIn("أكد MT5 تنفيذ", text)
        self.assertEqual(self.store["mark_notified"].await_count, 2)
        self.bot.send_message.reset_mock()
        self.store["mark_notified"].reset_mock()
        for status in ("expired", "cancelled"):
            for clicked, executing in ((False, False), (True, True)):
                current = offer(status)
                current["decided_at"] = NOW if clicked else None
                current["executing_at"] = NOW if executing else None
                self.store["list_notifications"].return_value = [current]
                await notifications.send_results(self.service, self.bot)
        self.bot.send_message.assert_not_awaited()
        self.store["mark_notified"].assert_not_awaited()

    async def test_confirmed_fill_displays_actual_order_ticket_and_time_only_for_matching_status(self):
        current = offer("filled")
        current["result"] = {"status": "filled", "code": 10009,
                             "order_ticket": 123456789, "executed_at": NOW.isoformat()}
        self.store["list_notifications"].return_value = [current]
        await notifications.send_results(self.service, self.bot)
        text = self.bot.send_message.await_args.args[1]
        self.assertIn("أكد MT5 تنفيذ", text)
        self.assertIn("رقم الأمر: 123456789", text)
        self.assertIn("2026-10-04 12:00:00 UTC", text)
        self.assertNotIn("السعر المؤكد", text)
        current["status"] = "unknown"
        await notifications.send_results(self.service, self.bot)
        text = self.bot.send_message.await_args.args[1]
        self.assertIn("غير مؤكدة", text)
        self.assertNotIn("123456789", text)

    async def test_results_still_deliver_after_unwatch_but_owner_change_or_send_failure_is_not_acknowledged(self):
        current = offer("unknown")
        current["result"] = {"status": "unknown"}
        self.store["list_notifications"].return_value = [current]
        self.store["subscription_active"].return_value = False
        await notifications.send_results(self.service, self.bot)
        self.bot.send_message.assert_awaited_once()
        self.store["mark_notified"].assert_awaited_once()
        self.store["subscription_active"].assert_not_awaited()
        self.bot.send_message.reset_mock()
        self.store["mark_notified"].reset_mock()
        self.store["subscription_active"].return_value = True
        self.device["owner_user_id"] = 202
        await notifications.send_results(self.service, self.bot)
        self.device = device()
        self.bot.send_message.side_effect = RetryAfter(10)
        self.assertFalse(await notifications.send_results(self.service, self.bot))
        self.store["mark_notified"].assert_not_awaited()

    async def test_trading_switch_disabled_blocks_pairing_callbacks_offers_and_results(self):
        self.service.trading_enabled = False
        self.update.effective_message = message(user_id=OWNER_ID)
        self.context.args = ["AB12CD34EF56"]
        await notifications.connect_mt5_command(self.update, self.context)
        await self.run_callback()
        await notifications.send_offers(self.service, self.bot)
        await notifications.send_results(self.service, self.bot)
        self.store["pair_device"].assert_not_awaited()
        self.store["get_offer"].assert_not_awaited()
        self.store["create_offer"].assert_not_awaited()
        self.store["list_notifications"].assert_not_awaited()

    def test_registration_uses_separate_callback_namespace(self):
        application = SimpleNamespace(add_handler=Mock())
        notifications.register_handlers(application)
        command, callback = [call.args[0] for call in application.add_handler.call_args_list]
        self.assertIn("connect_mt5", command.commands)
        self.assertIsNotNone(callback.pattern.match(f"mt5:a:{OFFER_ID.hex}"))
        self.assertIsNotNone(callback.pattern.match("mt5:a:invalid"))
        self.assertIsNone(callback.pattern.match("command:help"))


if __name__ == "__main__":
    unittest.main()
