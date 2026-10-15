"""Owner-approved Demo callbacks never execute orders in the Telegram process."""

from copy import deepcopy
from datetime import timedelta
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bot import mt5_notifications as notifications, mtf_runtime
from bridge.mt5_history_contract import closed_history_sha256
from tests import test_mt5_notifications as delivery


class DemoNotificationTests(unittest.IsolatedAsyncioTestCase):
    start = delivery.MT5NotificationTests.start
    decide = delivery.MT5NotificationTests.decide

    def setUp(self):
        delivery.MT5NotificationTests.setUp(self)
        self.service.manual_tickets_enabled = True
        self.service.trading_enabled = False
        self.enterContext(patch.dict(os.environ, {
            "MARKET_SOURCE": "mt5", "MT5_MANUAL_TICKETS_ENABLED": "true", "MT5_DEMO_ORDERS_ENABLED": "true",
        }))
        self.offer["payload"]["workflow"] = "demo_execution"
        self.offer["payload"]["closed_history_sha256"] = closed_history_sha256(self.snapshot["payload"])
        self.offer["expires_at"] = delivery.NOW + timedelta(seconds=10)
        self.query.data = f"mt5demo:a:{delivery.OFFER_ID.hex}"
        self.start(patch.object(notifications.mt5_exit_events, "list_notifications", AsyncMock(return_value=[])))

    async def test_accept_queues_once_and_never_calls_execution_in_bot(self):
        await notifications.demo_decision_callback(self.update, self.context)
        await notifications.demo_decision_callback(self.update, self.context)
        self.store["decide"].assert_awaited_once()
        self.assertEqual(self.offer["status"], "accepted")
        self.service.order_send.assert_not_awaited()

    async def test_decline_rejects_without_market_or_execution_request(self):
        self.query.data = f"mt5demo:r:{delivery.OFFER_ID.hex}"
        await notifications.demo_decision_callback(self.update, self.context)
        self.assertEqual(self.offer["status"], "rejected")
        self.feed.assert_not_awaited(); self.service.order_send.assert_not_awaited()

    async def test_old_manual_and_legacy_callbacks_cannot_authorize_demo(self):
        self.query.data = f"mt5manual:p:{delivery.OFFER_ID.hex}"
        await notifications.manual_decision_callback(self.update, self.context)
        self.query.data = f"mt5:a:{delivery.OFFER_ID.hex}"
        await notifications.decision_callback(self.update, self.context)
        self.store["decide"].assert_not_awaited()

    async def test_wrong_message_expiry_real_account_and_changed_history_reject(self):
        original_offer, original_device, original_snapshot = deepcopy(self.offer), deepcopy(self.device), deepcopy(self.snapshot)
        mutations = (
            lambda: self.offer.update(message_id=999),
            lambda: self.offer.update(expires_at=delivery.NOW),
            lambda: self.device.update(account_mode="real"),
            lambda: self.snapshot["payload"]["timeframes"]["M1"][-1].update(tick_volume=987654),
        )
        for mutate in mutations:
            self.offer, self.device, self.snapshot = deepcopy(original_offer), deepcopy(original_device), deepcopy(original_snapshot)
            mutate()
            await notifications.demo_decision_callback(self.update, self.context)
        self.store["decide"].assert_not_awaited()

    async def test_publish_accept_decline_with_frozen_hash_and_truthful_market_order_text(self):
        self.store["list_paired_devices"].return_value = [self.device]
        self.service.signals.return_value = (mtf_runtime.evaluate_feed(self.snapshot["payload"], delivery.NOW), "Demo", "f" * 64)
        async def create(pool, bot_id, device_id, chat_id, user_id, signal_id, payload, now, expires_at):
            self.offer.update(payload=payload, created_at=now, expires_at=expires_at, status="draft")
            return deepcopy(self.offer)
        self.store["create_offer"].side_effect = create
        await notifications.send_offers(self.service, self.bot)
        self.bot.send_message.assert_awaited_once()
        text = self.bot.send_message.await_args.args[1]
        markup = self.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0]
        self.assertEqual([button.text for button in markup], ["✅ Accept", "❌ Decline"])
        self.assertTrue(all(button.callback_data.startswith("mt5demo:") for button in markup))
        self.assertIn("TP2 مرجع فقط", text)
        self.assertNotIn("ما في تنفيذ تلقائي", text)
        self.assertEqual(self.offer["payload"]["closed_history_sha256"], closed_history_sha256(self.snapshot["payload"]))
        self.assertEqual(self.offer["expires_at"], delivery.NOW + timedelta(seconds=10))

    async def test_receipts_use_demo_filtered_transport(self):
        with patch.object(notifications.trade_store, "list_demo_notifications", AsyncMock(return_value=[])) as results:
            await notifications.send_results(self.service, self.bot)
            results.assert_awaited_once()
        self.store["list_notifications"].assert_not_awaited()

    async def test_disabled_demo_callback_cannot_queue_and_unwatch_cancels_both_pending_queues(self):
        with patch.dict(os.environ, {"MT5_DEMO_ORDERS_ENABLED": "false"}):
            await notifications.demo_decision_callback(self.update, self.context)
        self.store["decide"].assert_not_awaited()
        update = SimpleNamespace(effective_message=delivery.message(), effective_chat=SimpleNamespace(id=delivery.OWNER_ID, type="private"))
        with patch.object(notifications.trade_store, "cancel_offers", AsyncMock()) as demo, \
                patch.object(notifications.manual_ticket_store, "cancel_offers", AsyncMock()) as manual:
            await notifications.market_monitor.unwatch_command(update, self.context)
            demo.assert_awaited_once(); manual.assert_awaited_once()
        self.disabled.assert_awaited_once()
        self.assertIn("الصفقات المفتوحة", self.reply.await_args.args[0])
        self.assertNotIn("طلبات تجهيز", self.reply.await_args.args[0])
