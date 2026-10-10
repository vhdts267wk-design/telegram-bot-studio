"""Research opt-in exercises the real delivery guards without network/orders."""

from copy import deepcopy
from datetime import timedelta
import unittest
from unittest.mock import patch

from bot import mt5_notifications as notifications, mtf_runtime, proposal_overlay
from tests import test_mt5_notifications as delivery
from tests import test_proposal_overlay as chart


class ExperimentalDeliveryTests(unittest.IsolatedAsyncioTestCase):
    start = delivery.MT5NotificationTests.start
    decide = delivery.MT5NotificationTests.decide
    prepare_manual_offer = delivery.ManualMT5NotificationTests.prepare_manual_offer
    run_manual_callback = delivery.ManualMT5NotificationTests.run_manual_callback

    def setUp(self):
        delivery.ManualMT5NotificationTests.setUp(self)
        self.start(patch.dict(notifications.os.environ, {"MT5_SIGNAL_MODE": "experimental_demo"}))
        self.snapshot["payload"]["risk_context"].update(
            costs_verified=False, commission_round_turn=0, slippage_price=0,
        )
        self.candidate = mtf_runtime.evaluate_feed(self.snapshot["payload"], delivery.NOW)
        self.assertEqual(self.candidate["state"], "signal", self.candidate)
        self.service.signals.return_value = self.candidate, "Demo experimental", "f" * 64
        self.offer["payload"] = {**self.candidate, "workflow": "manual_ticket", "max_drift_r": .1}

    async def test_owner_receives_labelled_proposal_with_30_second_absolute_expiry(self):
        await self.prepare_manual_offer()
        await notifications.send_offers(self.service, self.bot)
        self.bot.send_message.assert_awaited_once()
        text = self.bot.send_message.await_args.args[1]
        for detail in ("الأداء غير مثبت", "التكاليف افتراضات تقديرية", "30 ثانية", "Buy أو Sell", "0.01"):
            self.assertIn(detail, text)
        for detail in ("لقيت فرصة", "الدخول المقترح", "SL وقف", "TP1", "TP2", "السبب", "ما في تنفيذ تلقائي",
                       "H1", "H4", "متوافقة مع الدخول", "دعم", "مقاومة", "ليست نسبة نجاح"):
            self.assertIn(detail, text)
        self.assertLess(len(text), 1400)
        self.assertNotIn("مدة تقييم النجاح", text)
        self.assertNotIn("اجتازت بوابة الأدلة", text)
        self.assertNotIn("qualification_id", self.offer["payload"])
        self.assertEqual(self.offer["expires_at"], delivery.NOW + timedelta(seconds=30))
        self.assertFalse(self.offer["payload"]["cost_assumptions"]["verified"])
        self.service.order_send.assert_not_awaited()

    async def test_fresh_prepare_at_20_seconds_remains_manual_and_at_31_is_refused(self):
        for elapsed in (20, 31):
            later = delivery.NOW + timedelta(seconds=elapsed)
            self.offer["status"] = "offered"
            self.manual_store["decide"].reset_mock()
            for obj, key in ((self.snapshot["payload"], "as_of"),
                             (self.snapshot["payload"]["quote"], "time"),
                             (self.snapshot["payload"]["risk_context"], "as_of")):
                obj[key] = later.isoformat()
            with patch.object(notifications.market_monitor, "utc_now", return_value=later), \
                    patch.object(mtf_runtime, "_clock", side_effect=lambda now=None: later if now is None else now):
                await self.run_manual_callback()
            if elapsed == 20:
                self.assertEqual(self.offer["status"], "requested")
                self.manual_store["decide"].assert_awaited_once()
            else:
                self.manual_store["decide"].assert_not_awaited()
        self.service.order_send.assert_not_awaited()

    async def test_disabling_experimental_mode_revokes_existing_prepare(self):
        with patch.dict(notifications.os.environ, {"MT5_SIGNAL_MODE": "qualified"}):
            await self.run_manual_callback()
        self.manual_store["decide"].assert_not_awaited()

    async def test_unsubscribed_or_exposed_account_receives_no_proposal(self):
        await self.prepare_manual_offer()
        self.store["subscription_active"].return_value = False
        await notifications.send_offers(self.service, self.bot)
        self.store["subscription_active"].return_value = True
        self.snapshot["payload"]["risk_context"]["open_positions"] = 1
        await notifications.send_offers(self.service, self.bot)
        self.bot.send_message.assert_not_awaited()
        self.manual_store["create_offer"].assert_not_awaited()


class ExperimentalOverlayTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(notifications.os.environ, {"MT5_SIGNAL_MODE": "experimental_demo"}, clear=True))
        self.enterContext(patch.object(mtf_runtime, "_clock", side_effect=lambda now=None: chart.NOW if now is None else now))

    def test_chart_carries_unverified_profile_and_never_renews_deadline(self):
        value = chart.offer()
        original = deepcopy(value)
        dto = proposal_overlay.build_chart_overlay(value, chart.device(), chart.feed(), chart.NOW)
        self.assertIsNotNone(dto)
        self.assertEqual(dto["signal_mode"], "experimental_demo")
        self.assertTrue(dto["provisional"])
        self.assertEqual(dto["entry_window_seconds"], 30)
        self.assertNotIn("qualification_id", dto)
        self.assertFalse(dto["cost_assumptions"]["verified"])
        self.assertEqual(dto["expires_at"], (chart.NOW + timedelta(seconds=30)).isoformat())
        self.assertEqual(value, original)
        later = chart.NOW + timedelta(seconds=31)
        updated = chart.feed()
        updated["as_of"] = updated["quote"]["time"] = updated["risk_context"]["as_of"] = later.isoformat()
        self.assertIsNone(proposal_overlay.build_chart_overlay(value, chart.device(), updated, later))
