"""Demo approval transport tests use synthetic feeds and stores, never MT5."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bot import mt5_api, mtf_runtime, proposal_overlay, trade_store
from bridge.mt5_history_contract import closed_history_sha256
from tests import test_mt5_manual_api as manual
from tests import test_proposal_overlay as chart
from tests import test_trade_store as persistence


class DemoAPITests(unittest.IsolatedAsyncioTestCase):
    post = manual.ManualAPITests.post

    def setUp(self):
        manual.ManualAPITests.setUp(self)
        self.enterContext(patch.dict(os.environ, {
            "MARKET_SOURCE": "mt5", "MT5_MANUAL_TICKETS_ENABLED": "true", "MT5_DEMO_ORDERS_ENABLED": "true",
        }))
        self.offer.update(status="executing", chat_id=11, user_id=11,
                          decided_at=manual.NOW, executing_at=manual.NOW,
                          expires_at=manual.NOW + timedelta(seconds=10))
        self.offer["payload"]["workflow"] = "demo_execution"
        self.offer["payload"]["closed_history_sha256"] = closed_history_sha256(self.snapshot["payload"])

    def mocks(self):
        stack = ExitStack()
        stack.enter_context(patch.object(trade_store, "get_device", AsyncMock(return_value=self.device)))
        stack.enter_context(patch.object(trade_store, "heartbeat", AsyncMock()))
        stack.enter_context(patch.object(trade_store, "expire_offers", AsyncMock()))
        stack.enter_context(patch.object(mt5_api.market_store, "get_cache", AsyncMock(side_effect=lambda *a: deepcopy(self.snapshot))))
        claim = stack.enter_context(patch.object(trade_store, "claim_demo_offer", AsyncMock(return_value=self.offer)))
        complete = stack.enter_context(patch.object(trade_store, "complete_demo_offer", AsyncMock(return_value=self.offer)))
        stack.enter_context(patch.object(trade_store, "subscription_active", AsyncMock(return_value=True)))
        return stack, claim, complete

    async def test_demo_envelope_preserves_frozen_protection_and_exact_deadline(self):
        stack, claim, complete = self.mocks()
        with stack:
            response = await self.post("demo/poll", {"device_id": str(manual.DEVICE)})
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()["trade"]
        self.assertEqual(value["workflow"], "demo_execution")
        self.assertEqual(value["payload"], self.offer["payload"])
        self.assertEqual(value["expires_at"], self.offer["expires_at"].isoformat())
        claim.assert_awaited_once()
        self.assertTrue(mtf_runtime.eligible_result(claim.await_args.kwargs["qualified"], manual.NOW))
        complete.assert_not_awaited()

    async def test_default_off_missing_prerequisites_and_bad_auth_cannot_claim(self):
        stack, claim, _ = self.mocks()
        with stack:
            for changes in ({"MT5_DEMO_ORDERS_ENABLED": "false"}, {"MT5_MANUAL_TICKETS_ENABLED": "false"}, {"MARKET_SOURCE": "reference"}):
                with patch.dict(os.environ, changes):
                    response = await self.post("demo/poll", {"device_id": str(manual.DEVICE)})
                    self.assertEqual(response.status_code, 404)
            response = await self.post("demo/poll", {"device_id": str(manual.DEVICE)}, "wrong-key")
            self.assertEqual(response.status_code, 401)
        claim.assert_not_awaited()

    async def test_old_execution_and_manual_routes_remain_closed_in_demo_mode(self):
        stack, claim, _ = self.mocks()
        with stack:
            for route in ("poll", "result", "manual/poll", "manual/result", "manual/chart"):
                self.assertEqual((await self.post(route, {"device_id": str(manual.DEVICE)})).status_code, 404)
        claim.assert_not_awaited()

    async def test_real_account_wrong_volume_stale_feed_and_pause_cannot_claim(self):
        stack, claim, _ = self.mocks()
        with stack:
            for key, value in (("account_mode", "real"), ("volume", .02)):
                old = self.device[key]; self.device[key] = value
                self.assertEqual((await self.post("demo/poll", {"device_id": str(manual.DEVICE)})).status_code, 422)
                self.device[key] = old
            old = self.snapshot["payload"]["quote"]["time"]
            self.snapshot["payload"]["quote"]["time"] = (manual.NOW - timedelta(seconds=11)).isoformat()
            self.assertEqual((await self.post("demo/poll", {"device_id": str(manual.DEVICE)})).status_code, 422)
            self.snapshot["payload"]["quote"]["time"] = old
            self.service.risk_pause.return_value = "paused"
            self.assertEqual((await self.post("demo/poll", {"device_id": str(manual.DEVICE)})).json(), {"trade": None})
        claim.assert_not_awaited()

    async def test_after_claim_data_change_settles_failed_without_returning_trade(self):
        stack, claim, complete = self.mocks()
        async def change_after_claim(*args, **kwargs):
            self.snapshot["payload"]["risk_context"]["open_positions"] = 1
            return self.offer
        claim.side_effect = change_after_claim
        with stack:
            self.assertEqual((await self.post("demo/poll", {"device_id": str(manual.DEVICE)})).json(), {"trade": None})
        complete.assert_awaited_once()
        self.assertEqual(complete.await_args.args[-2], {"status": "failed", "code": 0})

    async def test_result_requires_full_fill_proof_and_exact_workflow_store(self):
        body = {"device_id": str(manual.DEVICE), "offer_id": str(manual.OFFER), "claim_id": str(manual.CLAIM),
                "result": {"status": "filled", "code": 10009, "order_ticket": 123, "executed_at": manual.NOW.isoformat()}}
        with patch.object(trade_store, "complete_demo_offer", AsyncMock(return_value=self.offer)) as complete, \
                patch.object(trade_store, "complete_offer", AsyncMock()) as legacy:
            self.assertEqual((await self.post("demo/result", body)).json(), {"ok": True})
            incomplete = deepcopy(body); del incomplete["result"]["executed_at"]
            self.assertEqual((await self.post("demo/result", incomplete)).status_code, 422)
            complete.assert_awaited_once(); legacy.assert_not_awaited()

    async def test_chart_is_read_only_offered_only_and_clears_after_accept(self):
        stack, _, _ = self.mocks()
        self.offer.update(status="offered", claim_id=None, decided_at=None, executing_at=None)
        with stack, patch.object(trade_store, "get_demo_chart_offer", AsyncMock(return_value=self.offer)):
            dto = (await self.post("demo/chart", {"device_id": str(manual.DEVICE)})).json()["proposal"]
            self.assertEqual(dto["status"], "offered")
            self.assertEqual(dto["stop"], self.offer["payload"]["stop"])
            trade_store.heartbeat.assert_not_awaited(); trade_store.claim_demo_offer.assert_not_awaited()
            self.offer["status"] = "accepted"
            self.assertEqual((await self.post("demo/chart", {"device_id": str(manual.DEVICE)})).json(), {"proposal": None})

    async def test_experimental_profile_keeps_30_second_expiry_and_unproven_cost_labels(self):
        with patch.dict(os.environ, {"MT5_SIGNAL_MODE": "experimental_demo"}):
            self.snapshot["payload"]["risk_context"].update(costs_verified=False, commission_round_turn=0, slippage_price=0)
            candidate = mtf_runtime.evaluate_feed(self.snapshot["payload"], manual.NOW)
            self.assertEqual(candidate["state"], "signal", candidate)
            self.offer["payload"] = {**candidate, "workflow": "demo_execution", "max_drift_r": .1,
                                     "source_identity": f"mt5:XAUUSD:{manual.DEVICE}"}
            self.offer["expires_at"] = manual.NOW + timedelta(seconds=30)
            stack, claim, _ = self.mocks()
            with stack:
                response = await self.post("demo/poll", {"device_id": str(manual.DEVICE)})
            trade = response.json()["trade"]
            self.assertEqual(trade["expires_at"], self.offer["expires_at"].isoformat())
            self.assertTrue(trade["payload"]["provisional"])
            self.assertFalse(trade["payload"]["cost_assumptions"]["verified"])
            self.assertNotIn("qualification_id", trade["payload"])
            claim.assert_awaited_once()


class DemoStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"MARKET_SOURCE": "mt5", "MT5_MANUAL_TICKETS_ENABLED": "true", "MT5_DEMO_ORDERS_ENABLED": "true"}, clear=True))
        self.enterContext(manual.evidence_fixtures.pinned_synthetic_evidence(chart.feed(), manual.NOW))
        self.qualified = mtf_runtime.evaluate_feed(chart.feed(), manual.NOW)
        self.pool = SimpleNamespace(fetchrow=AsyncMock(return_value=None))
        self.connection = self.pool
        self.connection.fetchval = AsyncMock(return_value=True)
        self.transaction = MagicMock()
        self.transaction.__aenter__ = AsyncMock()
        self.transaction.__aexit__ = AsyncMock(return_value=False)
        self.connection.transaction = MagicMock(return_value=self.transaction)
        acquisition = MagicMock()
        acquisition.__aenter__ = AsyncMock(return_value=self.connection)
        acquisition.__aexit__ = AsyncMock(return_value=False)
        self.pool.acquire = MagicMock(return_value=acquisition)

    async def test_claim_filters_current_context_and_serializes_device_not_just_offer(self):
        await trade_store.claim_demo_offer(self.pool, 991, manual.DEVICE, manual.NOW, qualified=self.qualified)
        query, *args = self.pool.fetchrow.await_args.args
        query = persistence.compact(query)
        for guard in ("offer.status = 'accepted'", "payload->>'workflow' = 'demo_execution'", "device.account_mode = 'demo'",
                      "offer.payload @> $6::jsonb", "subscription.active", "active.status = 'executing'",
                      "FOR UPDATE OF offer, device SKIP LOCKED", "offer.chat_id = offer.user_id", "make_interval(secs => $7)"):
            self.assertIn(guard, query)
        context = json.loads(args[5])
        for key in ("timeframe_context", "context_bar_times", "strategy_fingerprint", "qualification_id"):
            self.assertEqual(context[key], self.qualified[key])
        self.assertEqual(args[6], 10)

    async def test_invalid_or_disabled_profile_never_reaches_database(self):
        invalid = deepcopy(self.qualified); invalid["account_mode"] = "real"
        self.assertIsNone(await trade_store.claim_demo_offer(self.pool, 991, manual.DEVICE, manual.NOW, qualified=invalid))
        with patch.dict(os.environ, {"MT5_DEMO_ORDERS_ENABLED": "false"}):
            self.assertIsNone(await trade_store.claim_demo_offer(self.pool, 991, manual.DEVICE, manual.NOW, qualified=self.qualified))
        self.pool.fetchrow.assert_not_awaited()
        self.pool.acquire.assert_not_called()

    async def test_each_claim_locks_device_then_reads_fresh_snapshot_in_transaction(self):
        order = []
        async def lock(*args):
            order.append("lock")
            return True
        async def claim(*args):
            order.append("claim")
            return None
        self.connection.fetchval.side_effect = lock
        self.connection.fetchrow.side_effect = claim
        for _ in range(2):
            await trade_store.claim_demo_offer(self.pool, 991, manual.DEVICE, manual.NOW, qualified=self.qualified)
        self.assertEqual(order, ["lock", "claim", "lock", "claim"])
        self.assertEqual(self.transaction.__aexit__.await_count, 2)
        for call in self.connection.transaction.call_args_list:
            self.assertEqual(call.kwargs, {"isolation": "read_committed"})
        self.assertIn("FOR UPDATE", self.connection.fetchval.await_args.args[0])

    async def test_demo_receipts_filter_old_workflow_before_limit(self):
        self.pool.fetch = AsyncMock(return_value=[])
        await trade_store.list_demo_notifications(self.pool, 991, limit=20)
        query = persistence.compact(self.pool.fetch.await_args.args[0])
        self.assertLess(query.index("payload->>'workflow' = 'demo_execution'"), query.index("LIMIT $2"))

    async def test_experimental_claim_pins_30_second_profile_without_fake_qualification(self):
        with patch.dict(os.environ, {"MT5_SIGNAL_MODE": "experimental_demo"}):
            feed = chart.feed()
            feed["risk_context"].update(costs_verified=False, commission_round_turn=0, slippage_price=0)
            candidate = mtf_runtime.evaluate_feed(feed, manual.NOW)
            await trade_store.claim_demo_offer(self.pool, 991, manual.DEVICE, manual.NOW, qualified=candidate)
        query, *args = self.pool.fetchrow.await_args.args
        context = json.loads(args[5])
        self.assertEqual(context["signal_mode"], "experimental_demo")
        self.assertTrue(context["provisional"])
        self.assertEqual(args[6], 30)
        self.assertTrue(args[7])
        self.assertNotIn("qualification_id", context)

    async def test_result_is_workflow_claim_owner_bound_and_identical_retry_only(self):
        result = {"status": "unknown", "code": 0}
        await trade_store.complete_demo_offer(self.pool, 991, manual.DEVICE, manual.OFFER, manual.CLAIM, result, manual.NOW)
        query = persistence.compact(self.pool.fetchrow.await_args.args[0])
        for guard in ("offer.claim_id = $4", "offer.status = 'executing'", "payload->>'workflow' = 'demo_execution'",
                      "owner.account_mode = 'demo'", "device.owner_user_id = resolved.user_id", "result = $6::jsonb"):
            self.assertIn(guard, query)
