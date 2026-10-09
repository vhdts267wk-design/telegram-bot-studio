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
from bot import mtf_runtime
from tests.test_mtf_runtime import pinned_synthetic_evidence, synthetic_case


NOW = synthetic_case()[2]
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
    return mtf_runtime.evaluate_feed(synthetic_case()[0], NOW)


def offer(status="offered"):
    payload = notifications._normalise_signal(signal(), synthetic_case()[0])
    payload.update(symbol="XAUUSD", account_mode="demo", volume=0.01, max_drift_r=0.1,
                   workflow="manual_ticket")
    return {
        "id": OFFER_ID, "bot_id": BOT_ID, "device_id": DEVICE_ID,
        "chat_id": OWNER_ID, "user_id": OWNER_ID, "signal_id": "f" * 64,
        "payload": payload, "status": status, "message_id": MESSAGE_ID,
        "created_at": NOW, "expires_at": NOW + timedelta(minutes=5),
        "result": None, "decided_at": None, "executing_at": None,
    }


def snapshot():
    payload = synthetic_case()[0]
    payload["device_id"] = str(DEVICE_ID)
    return {"updated_at": NOW, "payload": payload}


def message(*, chat_id=OWNER_ID, user_id=BOT_ID, message_id=MESSAGE_ID, chat_type="private"):
    return Message(message_id, NOW, Chat(chat_id, chat_type), from_user=User(user_id, "synthetic", user_id == BOT_ID), text="Synthetic MT5 signal")


class MT5NotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = []
        self.start(patch.dict(notifications.os.environ, {"MARKET_GOLD_SYMBOL": "XAUUSD"}, clear=True))
        self.enterContext(pinned_synthetic_evidence(synthetic_case()[0], NOW))
        # The shared evidence fixture freezes the default clock; explicit
        # callback/result clocks must still exercise real temporal admission.
        self.start(patch.object(mtf_runtime, "_clock", side_effect=lambda now=None: NOW if now is None else now))
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



class ManualMT5NotificationTests(unittest.IsolatedAsyncioTestCase):
    start = MT5NotificationTests.start
    decide = MT5NotificationTests.decide

    def setUp(self):
        MT5NotificationTests.setUp(self)
        self.service.manual_tickets_enabled = True
        self.service.trading_enabled = False
        self.offer["payload"]["workflow"] = "manual_ticket"
        self.offer["preparing_at"] = None
        self.manual_store = {}
        for name, value in {
            "get_offer": None, "decide": None, "create_offer": None,
            "publish_offer": True, "expire_offers": 0,
            "list_notifications": [], "mark_notified": True,
        }.items():
            self.manual_store[name] = self.start(patch.object(
                notifications.manual_ticket_store, name, new_callable=AsyncMock, return_value=value,
            ))
        self.manual_store["get_offer"].side_effect = lambda *args: deepcopy(self.offer)
        self.manual_store["decide"].side_effect = self.decide

    async def run_manual_callback(self, action="p"):
        self.query.data = f"mt5manual:{action}:{OFFER_ID.hex}"
        await notifications.manual_decision_callback(self.update, self.context)

    async def prepare_manual_offer(self):
        self.store["list_paired_devices"].return_value = [self.device]

        async def create(*args):
            self.offer = offer("draft")
            self.offer.update(payload=deepcopy(args[6]), created_at=args[7], expires_at=args[8], message_id=None, preparing_at=None)
            return deepcopy(self.offer)

        self.manual_store["create_offer"].side_effect = create

    async def test_prepare_button_only_requests_a_manual_ticket_and_replay_cannot_queue_again(self):
        await self.run_manual_callback()
        self.assertEqual(self.offer["status"], "requested")
        self.assertEqual(self.manual_store["decide"].await_args.args[-2], "requested")
        self.assertIn("TP وSL فقط", self.query.answer.await_args.args[0])
        self.assertIn("بنفسك", self.query.answer.await_args.args[0])
        self.assertIn("Buy أو Sell", self.query.answer.await_args.args[0])
        await self.run_manual_callback("r")
        self.assertEqual(self.manual_store["decide"].await_count, 1)
        self.assertEqual(self.offer["status"], "requested")
        self.store["decide"].assert_not_awaited()
        self.service.order_send.assert_not_awaited()

    async def test_manual_mode_rejects_old_automatic_accept_even_if_both_flags_are_true(self):
        self.service.trading_enabled = True
        self.query.data = f"mt5:a:{OFFER_ID.hex}"
        await notifications.decision_callback(self.update, self.context)
        self.assertIn("التنفيذ التلقائي متوقف", self.query.answer.await_args.args[0])
        self.store["get_offer"].assert_not_awaited()
        self.store["decide"].assert_not_awaited()
        self.manual_store["decide"].assert_not_awaited()
        self.service.order_send.assert_not_awaited()

    async def test_manual_reject_stays_available_when_source_quote_or_risk_changes(self):
        self.service.source = "reference"
        self.snapshot["payload"]["quote"]["time"] = (NOW - timedelta(hours=1)).isoformat()
        self.service.risk_pause.return_value = NOW + timedelta(minutes=45)
        await self.run_manual_callback("r")
        self.assertEqual(self.offer["status"], "rejected")
        self.feed.assert_not_awaited()
        self.service.risk_pause.assert_not_awaited()
        self.store["decide"].assert_not_awaited()

    async def test_prepare_is_bound_to_owner_bot_message_device_workflow_and_active_subscription(self):
        for field in ("chat_id", "user_id", "bot_id", "message_id"):
            self.offer[field] = 202
            await self.run_manual_callback()
            self.offer[field] = {"chat_id": OWNER_ID, "user_id": OWNER_ID, "bot_id": BOT_ID, "message_id": MESSAGE_ID}[field]
        self.device["owner_user_id"] = 202
        await self.run_manual_callback()
        self.device = device()
        self.offer["payload"]["workflow"] = "automatic"
        await self.run_manual_callback()
        self.offer["payload"]["workflow"] = "manual_ticket"
        self.store["subscription_active"].return_value = False
        await self.run_manual_callback()
        self.manual_store["decide"].assert_not_awaited()
        self.assertEqual(self.offer["status"], "offered")

    async def test_prepare_obeys_strict_cache_device_freshness_quote_skew_and_risk_pause(self):
        for condition in ("old_quote", "future_quote", "future_receipt", "old_device", "risk_pause"):
            self.snapshot, self.device = snapshot(), device()
            self.service.risk_pause.return_value = None
            if condition == "old_quote":
                self.snapshot["payload"]["quote"]["time"] = (NOW - timedelta(minutes=4)).isoformat()
            elif condition == "future_quote":
                self.snapshot["payload"]["quote"]["time"] = (NOW + timedelta(seconds=6)).isoformat()
            elif condition == "future_receipt":
                self.snapshot["updated_at"] = NOW + timedelta(seconds=1)
            elif condition == "old_device":
                self.device["last_seen_at"] = NOW - timedelta(minutes=4)
            else:
                self.service.risk_pause.return_value = NOW + timedelta(minutes=15)
            with self.subTest(condition=condition):
                await self.run_manual_callback()
                self.assertEqual(self.offer["status"], "offered")
        self.manual_store["decide"].assert_not_awaited()
        self.snapshot, self.device = snapshot(), device()
        self.snapshot["payload"]["quote"]["time"] = (NOW + timedelta(seconds=3)).isoformat()
        self.service.risk_pause.return_value = None
        await self.run_manual_callback()
        self.assertEqual(self.offer["status"], "requested")

    async def test_manual_offer_uses_separate_store_and_buttons_and_does_not_duplicate_requested_ticket(self):
        await self.prepare_manual_offer()
        await notifications.send_offers(self.service, self.bot)
        self.manual_store["publish_offer"].assert_awaited_once_with(self.service.pool, BOT_ID, OFFER_ID, MESSAGE_ID, NOW)
        payload = self.manual_store["create_offer"].await_args.args[6]
        self.assertEqual(self.manual_store["create_offer"].await_args.args[8], NOW + timedelta(seconds=10))
        self.assertEqual(payload["workflow"], "manual_ticket")
        self.assertEqual((payload["stop"], payload["target"]), (signal()["stop"], signal()["target"]))
        sent = self.bot.send_message.await_args
        for text in ("Demo", "0.01", "TP", "SL", "ما في تنفيذ تلقائي", "بنفسك"):
            self.assertIn(text, sent.args[1])
        self.assertIn("منطقة الدخول:", sent.args[1])
        for detail in ("TP2", "M15", "M5", "M1", "10 ثانية", "ما بيضمن الربح"):
            self.assertIn(detail, sent.args[1])
        for detail in ("لقيت فرصة", "الدخول المقترح", "السبب", "شارت MT5"):
            self.assertIn(detail, sent.args[1])
        self.assertLess(len(sent.args[1]), 1400)
        buttons = sent.kwargs["reply_markup"].inline_keyboard[0]
        self.assertEqual(buttons[0].text, "جهّز على اللابتوب")
        for button in buttons:
            self.assertIsNotNone(notifications.MANUAL_CALLBACK_PATTERN.fullmatch(button.callback_data))
            self.assertLessEqual(len(button.callback_data.encode()), 64)
        self.manual_store["create_offer"].side_effect = None
        self.offer["status"] = "requested"
        self.manual_store["create_offer"].return_value = deepcopy(self.offer)
        await notifications.send_offers(self.service, self.bot)
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.store["create_offer"].assert_not_awaited()
        self.store["publish_offer"].assert_not_awaited()
        self.service.order_send.assert_not_awaited()

    async def test_manual_pairing_explains_human_native_buy_sell_with_auto_disabled(self):
        self.update.effective_message = message(user_id=OWNER_ID)
        self.context.args = ["AB12CD34EF56"]
        self.store["pair_device"].return_value = self.device
        await notifications.connect_mt5_command(self.update, self.context)
        self.enabled.assert_awaited_once()
        text = self.reply.await_args.args[0]
        self.assertIn("جهّز على اللابتوب", text)
        self.assertIn("Buy أو Sell بنفسك", text)
        self.assertNotIn("Accept", text)

    async def test_manual_results_distinguish_prepared_failed_unknown_and_never_claim_order_fill(self):
        for status in ("prepared", "failed", "unknown", "filled"):
            current = deepcopy(self.offer)
            current.update(status=status, preparing_at=NOW, result={"status": status, "order_ticket": 123456})
            self.manual_store["list_notifications"].return_value = [current]
            await notifications.send_results(self.service, self.bot)
            text = self.bot.send_message.await_args.args[1]
            self.assertNotIn("أكد MT5 تنفيذ", text)
            self.assertNotIn("123456", text)
            if status == "prepared":
                self.assertIn("تم تجهيز", text)
                self.assertIn("Buy أو Sell بنفسك", text)
                self.assertIn("لم تُرسل صفقة", text)
                self.assertIn("القرار والتنفيذ يدويان", text)
                self.assertIn("الأداء التاريخي لا يضمن", text)
                self.assertIn("إذا كانت مهلة الدخول ما زالت مفتوحة", text)
            elif status == "failed":
                self.assertIn("تعذّر تجهيز", text)
            else:
                self.assertIn("غير مؤكدة", text)
        self.assertEqual(self.manual_store["mark_notified"].await_count, 4)
        self.store["list_notifications"].assert_not_awaited()
        self.store["mark_notified"].assert_not_awaited()
        self.service.order_send.assert_not_awaited()

    def test_prepared_receipt_cannot_extend_absolute_m1_window_or_missing_clocks(self):
        current = deepcopy(self.offer)
        current.update(status="prepared", preparing_at=NOW, result={"status": "prepared"})
        # This fixture deliberately retains the old five-minute offer expiry.
        # A new receipt must not make its ten-second M1 admission valid again.
        current["completed_at"] = NOW + timedelta(seconds=11)
        fresh = notifications._manual_result_text(current, NOW + timedelta(seconds=10))
        self.assertIn("Buy أو Sell بنفسك", fresh)
        for stamp in (NOW - timedelta(seconds=1), NOW + timedelta(seconds=10, microseconds=1), NOW + timedelta(seconds=11)):
            with self.subTest(stamp=stamp):
                text = notifications._manual_result_text(current, stamp)
                self.assertIn("ألغِ", text)
                self.assertIn("انتظر فرصة جديدة", text)
                self.assertNotIn("Buy", text)
                self.assertNotIn("Sell", text)
        for field in ("bar_time", "decision_time"):
            malformed = deepcopy(current)
            malformed["payload"].pop(field)
            with self.subTest(missing_clock=field):
                text = notifications._manual_result_text(malformed, NOW)
                self.assertIn("ألغِ", text)
                self.assertNotIn("Buy", text)

    async def test_result_delivery_rechecks_window_after_device_lookup(self):
        current = deepcopy(self.offer)
        current.update(status="prepared", preparing_at=NOW, result={"status": "prepared"})
        self.manual_store["list_notifications"].return_value = [current]
        clock = [NOW]

        async def delayed_device(*args):
            clock[0] = NOW + timedelta(seconds=11)
            return deepcopy(self.device)

        self.store["get_device"].side_effect = delayed_device
        with patch.object(notifications.market_monitor, "utc_now", side_effect=lambda: clock[0]):
            await notifications.send_results(self.service, self.bot)
        text = self.bot.send_message.await_args.args[1]
        self.assertIn("ألغِ", text)
        self.assertIn("انتظر فرصة جديدة", text)
        self.assertNotIn("Buy", text)
        self.manual_store["mark_notified"].assert_awaited_once()
        self.manual_store["decide"].assert_not_awaited()
        self.service.order_send.assert_not_awaited()

    async def test_old_prepared_button_replaces_stale_buy_sell_instructions(self):
        self.offer.update(status="prepared", preparing_at=NOW, result={"status": "prepared"})
        self.query.message = Message(
            MESSAGE_ID, NOW, Chat(OWNER_ID, "private"), from_user=User(BOT_ID, "synthetic", True),
            text="اقتراح قديم: اضغط Buy أو Sell بنفسك داخل MT5.",
        )
        with patch.object(notifications.market_monitor, "utc_now", return_value=NOW + timedelta(seconds=11)):
            await self.run_manual_callback()
        for text in (self.query.answer.await_args.args[0], self.query.edit_message_text.await_args.args[0]):
            self.assertIn("ألغِ", text)
            self.assertIn("انتظر فرصة جديدة", text)
            self.assertNotIn("Buy", text)
            self.assertNotIn("Sell", text)
        self.assertIsNone(self.query.edit_message_text.await_args.kwargs["reply_markup"])
        self.manual_store["decide"].assert_not_awaited()
        self.feed.assert_not_awaited()
        self.service.order_send.assert_not_awaited()

    async def test_callback_edit_rechecks_window_after_telegram_acknowledgement(self):
        self.offer.update(status="prepared", preparing_at=NOW, result={"status": "prepared"})
        clock = [NOW]

        async def delayed_answer(*args, **kwargs):
            clock[0] = NOW + timedelta(seconds=11)

        self.query.answer.side_effect = delayed_answer
        with patch.object(notifications.market_monitor, "utc_now", side_effect=lambda: clock[0]):
            await self.run_manual_callback()
        self.assertIn("تجهيز", self.query.answer.await_args.args[0])
        self.assertIn("الأداء التاريخي لا يضمن", self.query.answer.await_args.args[0])
        edited = self.query.edit_message_text.await_args.args[0]
        self.assertIn("ألغِ", edited)
        self.assertNotIn("Buy", edited)
        self.manual_store["decide"].assert_not_awaited()

    async def test_lost_decision_race_uses_current_prepared_window_for_acknowledgement(self):
        clock = [NOW]

        async def already_prepared(*args):
            self.offer.update(status="prepared", preparing_at=NOW, result={"status": "prepared"})
            clock[0] = NOW + timedelta(seconds=11)
            return None

        self.manual_store["decide"].side_effect = already_prepared
        with patch.object(notifications.market_monitor, "utc_now", side_effect=lambda: clock[0]):
            await self.run_manual_callback()
        self.manual_store["decide"].assert_awaited_once()
        self.assertIn("ألغِ", self.query.answer.await_args.args[0])
        self.assertNotIn("Buy", self.query.edit_message_text.await_args.args[0])
        self.service.order_send.assert_not_awaited()

    async def test_expired_or_cancelled_button_removes_original_execution_instructions(self):
        for status in ("expired", "cancelled"):
            with self.subTest(status=status):
                self.offer["status"] = status
                self.query.message = Message(
                    MESSAGE_ID, NOW, Chat(OWNER_ID, "private"), from_user=User(BOT_ID, "synthetic", True),
                    text="اقتراح قديم: اضغط Buy أو Sell بنفسك داخل MT5.",
                )
                await self.run_manual_callback()
                edited = self.query.edit_message_text.await_args.args[0]
                self.assertIn("ألغِ", edited)
                self.assertIn("انتظر فرصة جديدة", edited)
                self.assertNotIn("Buy", edited)
        self.manual_store["decide"].assert_not_awaited()

    async def test_inconsistent_prepared_result_is_unknown_and_notification_failure_is_not_acknowledged(self):
        current = deepcopy(self.offer)
        current.update(status="prepared", preparing_at=NOW, result={"status": "failed"})
        self.manual_store["list_notifications"].return_value = [current]
        await notifications.send_results(self.service, self.bot)
        self.assertIn("غير مؤكدة", self.bot.send_message.await_args.args[1])
        self.manual_store["mark_notified"].reset_mock()
        self.bot.send_message.side_effect = RetryAfter(10)
        self.assertFalse(await notifications.send_results(self.service, self.bot))
        self.manual_store["mark_notified"].assert_not_awaited()

    async def test_automatic_mode_cannot_request_manual_ticket(self):
        self.service.manual_tickets_enabled = False
        self.service.trading_enabled = True
        await self.run_manual_callback()
        self.manual_store["get_offer"].assert_not_awaited()
        self.manual_store["decide"].assert_not_awaited()

    async def test_exact_expiry_and_unwatch_cannot_request_preparation(self):
        self.offer["expires_at"] = NOW
        await self.run_manual_callback()
        self.assertIn("انتهت", self.query.answer.await_args.args[0])
        self.offer = offer()
        self.store["subscription_active"].return_value = False
        await self.run_manual_callback()
        self.manual_store["decide"].assert_not_awaited()

    async def test_groups_inline_wrong_sender_and_malformed_callbacks_never_read_offer(self):
        cases = (
            {"message": message(chat_type="group")}, {"message": None},
            {"message": SimpleNamespace(chat=Chat(OWNER_ID, "private"), message_id=MESSAGE_ID)},
            {"inline_message_id": "inline-id"}, {"message": message(user_id=202)},
            {"from_user": User(202, "other", False)},
            {"data": "mt5manual:p:" + "x" * 64}, {"data": "mt5manual:x:" + OFFER_ID.hex}, {"data": 42},
        )
        baseline = dict(vars(self.query))
        baseline["data"] = f"mt5manual:p:{OFFER_ID.hex}"
        for changes in cases:
            with self.subTest(changes=list(changes)):
                self.query.__dict__.update(baseline)
                self.query.__dict__.update(changes)
                await notifications.manual_decision_callback(self.update, self.context)
        self.manual_store["get_offer"].assert_not_awaited()
        self.manual_store["decide"].assert_not_awaited()
        self.assertEqual(self.query.answer.await_count, len(cases))

    async def test_concurrent_prepare_and_reject_commit_one_decision(self):
        other = SimpleNamespace(**vars(self.query))
        other.data = f"mt5manual:r:{OFFER_ID.hex}"
        self.query.data = f"mt5manual:p:{OFFER_ID.hex}"
        await asyncio.gather(
            notifications.manual_decision_callback(self.update, self.context),
            notifications.manual_decision_callback(SimpleNamespace(callback_query=other), self.context),
        )
        self.assertIn(self.offer["status"], ("requested", "rejected"))
        self.assertEqual(self.manual_store["decide"].await_count, 1)
        self.service.order_send.assert_not_awaited()

    async def test_legacy_or_provisional_or_unpinned_signal_never_publishes_offer(self):
        await self.prepare_manual_offer()
        current = signal()
        for changes in ({"strategy_id": "ema9-21-atr14-v1"}, {"provisional": True},
                        {"qualification_id": "f" * 64}, {"confidence": .99, "qualification_id": None}):
            with self.subTest(changes=changes):
                result = dict(current, **changes)
                self.service.signals.return_value = result, "BUY 99%", "f" * 64
                await notifications.send_offers(self.service, self.bot)
        self.bot.send_message.assert_not_awaited()
        self.manual_store["create_offer"].assert_not_awaited()

    async def test_revoked_pin_blocks_existing_prepare_button(self):
        with patch.dict(notifications.os.environ, {"MT5_EVIDENCE_SHA256": "f" * 64}):
            await self.run_manual_callback()
        self.manual_store["decide"].assert_not_awaited()

    async def test_missing_frame_unknown_cost_or_existing_exposure_blocks_offer(self):
        await self.prepare_manual_offer()
        for condition in ("missing_m5", "unknown_costs", "exposure", "margin"):
            self.snapshot = snapshot()
            if condition == "missing_m5":
                self.snapshot["payload"]["timeframes"].pop("M5")
            else:
                self.snapshot["payload"]["risk_context"].update({
                    "unknown_costs": {"costs_verified": False}, "exposure": {"open_positions": 1},
                    "margin": {"free_margin": 1},
                }[condition])
            with self.subTest(condition=condition):
                await notifications.send_offers(self.service, self.bot)
        self.bot.send_message.assert_not_awaited()
        self.manual_store["create_offer"].assert_not_awaited()

    async def test_prepare_rechecks_quote_after_risk_await(self):
        async def change(*args):
            self.snapshot["payload"]["quote"]["time"] = (NOW - timedelta(seconds=11)).isoformat()
            return None
        self.service.risk_pause.side_effect = change
        await self.run_manual_callback()
        self.manual_store["decide"].assert_not_awaited()

    async def test_offer_rechecks_quote_after_draft_creation(self):
        await self.prepare_manual_offer()
        original = self.manual_store["create_offer"].side_effect
        async def change(*args):
            result = await original(*args)
            self.snapshot["payload"]["quote"]["time"] = (NOW - timedelta(seconds=11)).isoformat()
            return result
        self.manual_store["create_offer"].side_effect = change
        await notifications.send_offers(self.service, self.bot)
        self.manual_store["create_offer"].assert_awaited_once()
        self.bot.send_message.assert_not_awaited()
        self.manual_store["publish_offer"].assert_not_awaited()

    async def test_offer_drift_does_not_recalculate_frozen_stop_or_target(self):
        original = deepcopy(self.offer["payload"])
        for key in ("bid", "ask"):
            self.snapshot["payload"]["quote"][key] += .2
        await self.run_manual_callback()
        self.manual_store["decide"].assert_not_awaited()
        self.assertEqual(self.offer["payload"], original)

    async def test_send_failure_throttling_and_forbidden_preserve_publication_rules(self):
        await self.prepare_manual_offer()
        self.bot.send_message.side_effect = RuntimeError("PRIVATE endpoint")
        with self.assertLogs(notifications.logger, level="WARNING") as logs:
            await notifications.send_offers(self.service, self.bot)
        self.assertNotIn("PRIVATE", " ".join(logs.output))
        self.manual_store["publish_offer"].assert_not_awaited()
        self.bot.send_message.side_effect = RetryAfter(10)
        self.assertFalse(await notifications.send_offers(self.service, self.bot))
        self.service.telegram_backoff.assert_awaited_once()
        self.bot.send_message.side_effect = Forbidden("blocked")
        await notifications.send_offers(self.service, self.bot)
        self.disabled.assert_awaited_once_with(self.service.pool, BOT_ID, OWNER_ID)

    async def test_unpublished_offer_removes_buttons_and_requested_offer_not_resent(self):
        await self.prepare_manual_offer()
        self.manual_store["publish_offer"].return_value = False
        await notifications.send_offers(self.service, self.bot)
        self.bot.edit_message_reply_markup.assert_awaited_once_with(OWNER_ID, MESSAGE_ID, reply_markup=None)
        self.manual_store["create_offer"].side_effect = None
        self.manual_store["create_offer"].return_value = offer("requested")
        await notifications.send_offers(self.service, self.bot)
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_results_after_unwatch_but_owner_change_prevents_notification(self):
        current = deepcopy(self.offer)
        current.update(status="unknown", preparing_at=NOW, result={"status": "unknown"})
        self.manual_store["list_notifications"].return_value = [current]
        self.store["subscription_active"].return_value = False
        await notifications.send_results(self.service, self.bot)
        self.manual_store["mark_notified"].assert_awaited_once()
        self.store["subscription_active"].assert_not_awaited()
        self.bot.send_message.reset_mock()
        self.manual_store["mark_notified"].reset_mock()
        self.device["owner_user_id"] = 202
        await notifications.send_results(self.service, self.bot)
        self.bot.send_message.assert_not_awaited()
        self.manual_store["mark_notified"].assert_not_awaited()

    async def test_pair_code_hashed_and_never_echoed_or_logged(self):
        self.update.effective_message = message(user_id=OWNER_ID)
        code = "AB12CD34EF56"
        self.context.args = [code]
        self.store["pair_device"].return_value = self.device
        await notifications.connect_mt5_command(self.update, self.context)
        args = self.store["pair_device"].await_args.args
        self.assertEqual(args[2], hashlib.sha256(code.encode()).hexdigest())
        self.assertNotIn(code, self.reply.await_args.args[0])
        self.store["pair_device"].side_effect = RuntimeError(code)
        with self.assertLogs(notifications.logger, level="WARNING") as logs:
            await notifications.connect_mt5_command(self.update, self.context)
        self.assertNotIn(code, " ".join(logs.output))

    async def test_pairing_real_account_or_nondefault_volume_never_enables_alerts(self):
        self.update.effective_message = message(user_id=OWNER_ID)
        self.context.args = ["AB12CD34EF56"]
        for changes in ({"account_mode": "real"}, {"volume": .02}):
            self.store["pair_device"].return_value = dict(device(), **changes)
            await notifications.connect_mt5_command(self.update, self.context)
        self.enabled.assert_not_awaited()

    async def test_legacy_auto_flag_alone_disables_pair_offers_results_and_callbacks(self):
        self.service.manual_tickets_enabled = False
        self.service.trading_enabled = True
        self.update.effective_message = message(user_id=OWNER_ID)
        self.context.args = ["AB12CD34EF56"]
        await notifications.connect_mt5_command(self.update, self.context)
        self.query.data = f"mt5:a:{OFFER_ID.hex}"
        await notifications.decision_callback(self.update, self.context)
        await notifications.send_offers(self.service, self.bot)
        await notifications.send_results(self.service, self.bot)
        for name in ("pair_device", "get_offer", "create_offer", "list_notifications", "decide"):
            self.store[name].assert_not_awaited()
        self.manual_store["create_offer"].assert_not_awaited()

    def test_registration_separates_manual_and_disabled_legacy_callbacks(self):
        application = SimpleNamespace(add_handler=Mock())
        notifications.register_handlers(application)
        command, callback, manual_callback = [call.args[0] for call in application.add_handler.call_args_list]
        self.assertIn("connect_mt5", command.commands)
        self.assertIsNotNone(callback.pattern.match(f"mt5:a:{OFFER_ID.hex}"))
        self.assertIsNone(callback.pattern.match("command:help"))
        self.assertIsNotNone(manual_callback.pattern.match(f"mt5manual:p:{OFFER_ID.hex}"))
        self.assertIsNone(manual_callback.pattern.match(f"mt5:a:{OFFER_ID.hex}"))


if __name__ == "__main__":
    unittest.main()
