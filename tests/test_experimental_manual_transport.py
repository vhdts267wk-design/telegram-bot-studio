"""Experimental preparation keeps atomic ownership and excludes certificates."""

from copy import deepcopy
from datetime import timedelta
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import httpx

from bot import manual_ticket_store, mt5_api, mtf_runtime
from tests import test_manual_ticket_store as store_fixtures
from tests import test_multi_timeframe as candle_fixtures


class ExperimentalClaimTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pool = SimpleNamespace(fetchrow=AsyncMock(return_value=store_fixtures.row(status="preparing")))
        self.context = deepcopy(store_fixtures.CONTEXT)
        self.context["policy_id"] = "mtf-manual-demo-estimated-cost-risk-v2"

    async def claim(self, **changes):
        options = dict(strategy_fingerprint=store_fixtures.FINGERPRINT,
                       proposal_context=self.context, signal_mode="experimental_demo")
        options.update(changes)
        return await manual_ticket_store.claim_offer(self.pool, store_fixtures.BOT,
                                                     store_fixtures.DEVICE, store_fixtures.NOW, **options)

    async def test_experimental_claim_requires_explicit_server_mode_without_certificate(self):
        with patch.dict(os.environ, {"MT5_SIGNAL_MODE": "qualified"}):
            self.assertIsNone(await self.claim())
        with patch.dict(os.environ, {"MT5_SIGNAL_MODE": "experimental_demo"}):
            self.assertIsNone(await self.claim(qualification_id="b" * 64))
            self.assertIsNone(await self.claim(signal_mode=None))
            self.assertIsNone(await self.claim(signal_mode="automatic"))
            self.assertIsNone(await self.claim(strategy_fingerprint="bad"))
            self.pool.fetchrow.assert_not_awaited()
            self.assertIsNotNone(await self.claim())

    async def test_experimental_atomic_claim_matches_profile_owner_subscription_and_window(self):
        with patch.dict(os.environ, {"MT5_SIGNAL_MODE": "experimental_demo"}):
            await self.claim()
        query, *args = self.pool.fetchrow.await_args.args
        candidate = store_fixtures.compact(query).split("), claimed AS (", 1)[0]
        for predicate in (
            "offer.payload->>'strategy_id' = 'mtf-ema-pullback-60m-demo-v2'",
            "offer.payload->>'strategy_version' = '2'",
            "offer.payload->>'signal_mode' = 'experimental_demo'",
            "offer.payload->'provisional' = 'true'::jsonb",
            "offer.payload->>'entry_window_seconds' = '30'",
            "COALESCE(offer.payload->>'qualification_id', '') = ''",
            "NOT (offer.payload ? 'evidence_metrics')",
            "offer.payload->>'strategy_fingerprint' = $6",
            "offer.payload->>'account_mode' = 'demo' AND offer.payload->>'volume' = '0.01'",
            "device.account_mode = 'demo' AND device.volume = 0.01",
            "offer.expires_at > $3 AND offer.decided_at <= $3",
            "offer.expires_at <= (offer.payload->>'bar_time')::timestamptz + INTERVAL '90 seconds'",
            "device.owner_chat_id = offer.chat_id AND device.owner_user_id = offer.user_id",
            "offer.chat_id = offer.user_id", "subscription.active", "NOT EXISTS",
            "active.status = 'preparing'", "FOR UPDATE OF offer, device SKIP LOCKED",
        ):
            self.assertIn(predicate, candidate)
        context = json.loads(args[-1])
        self.assertEqual(context["cost_context"], {"loss_cash_per_price_unit": 1, "profit_cash_per_price_unit": 1})
        self.assertEqual(args[-2], store_fixtures.FINGERPRINT)
        self.assertNotIn("commission_round_turn", context["cost_context"])

    async def test_wrong_experimental_policy_or_absent_cash_conversion_never_claims(self):
        with patch.dict(os.environ, {"MT5_SIGNAL_MODE": "experimental_demo"}):
            self.context["policy_id"] = "legacy"
            self.assertIsNone(await self.claim())
            self.context["policy_id"] = "mtf-manual-demo-estimated-cost-risk-v2"
            del self.context["cost_context"]["loss_cash_per_price_unit"]
            self.assertIsNone(await self.claim())
            self.pool.fetchrow.assert_not_awaited()


class ExperimentalPollTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = candle_fixtures.MultiTimeframeTests()
        self.now = fixture.now
        self.env = patch.dict(os.environ, {
            "MT5_SIGNAL_MODE": "experimental_demo", "MT5_MANUAL_BRIDGE_KEY": "test-only-manual-bridge-key-1234567890",
            "MARKET_GOLD_SYMBOL": "XAUUSD",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.clock = patch.object(mt5_api, "now_utc", return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.service = SimpleNamespace(pool=object(), bot_id=991, trading_enabled=False,
                                       manual_tickets_enabled=True, source="mt5", risk_pause=AsyncMock(return_value=None))
        self.app = FastAPI()
        mt5_api.install_routes(self.app, SimpleNamespace(bot_data={"market_service": self.service}),
                              SimpleNamespace(market_bridge_key="test-only-automatic-bridge-key-1234567890"))
        self.feed = fixture.feed()
        self.feed["risk_context"].update(costs_verified=False, commission_round_turn=None, slippage_price=None)
        self.device = dict(device_id=store_fixtures.DEVICE, owner_chat_id=11, owner_user_id=11,
                           account_mode="demo", volume=.01, symbol="XAUUSD", last_seen_at=self.now)
        self.feed["device_id"] = str(store_fixtures.DEVICE)
        self.snapshot = dict(payload=self.feed, updated_at=self.now)
        self.offer = store_fixtures.row(status="preparing", chat_id=11, user_id=11,
                                        preparing_at=self.now, claim_id=store_fixtures.CLAIM)
        self.offer["payload"] = dict(mtf_runtime.evaluate_feed(self.feed, self.now), workflow="manual_ticket")
        self.offer["expires_at"] = self.now + timedelta(seconds=30)

    async def post(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://local.test") as client:
            return await client.post("/api/mt5/manual/poll", json={"device_id": str(store_fixtures.DEVICE)},
                                     headers={"Authorization": "Bearer test-only-manual-bridge-key-1234567890"})

    def mocks(self, *, offer=True):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(mt5_api.trade_store, "get_device", new_callable=AsyncMock, return_value=self.device))
        stack.enter_context(patch.object(mt5_api.trade_store, "heartbeat", new_callable=AsyncMock))
        stack.enter_context(patch.object(mt5_api.manual_ticket_store, "expire_offers", new_callable=AsyncMock))
        stack.enter_context(patch.object(mt5_api.market_store, "get_cache", new_callable=AsyncMock, return_value=self.snapshot))
        claim = stack.enter_context(patch.object(mt5_api.manual_ticket_store, "claim_offer", new_callable=AsyncMock,
                                                return_value=self.offer if offer else None))
        automatic = stack.enter_context(patch.object(mt5_api.trade_store, "claim_offer", new_callable=AsyncMock))
        return stack, claim, automatic

    async def test_live_none_cost_poll_returns_experimental_preparation_without_fake_pin(self):
        stack, claim, automatic = self.mocks()
        with stack:
            response = await self.post()
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["signal_mode"], "experimental_demo")
            self.assertTrue(body["provisional"])
            self.assertEqual(body["entry_window_seconds"], 30)
            self.assertIsNotNone(body["preparation"], body)
            self.assertNotIn("qualification_id", body["preparation"]["payload"])
            self.assertIsNone(claim.await_args.kwargs["qualification_id"])
            self.assertEqual(claim.await_args.kwargs["signal_mode"], "experimental_demo")
            context = claim.await_args.kwargs["proposal_context"]["cost_context"]
            self.assertEqual(set(context), {"loss_cash_per_price_unit", "profit_cash_per_price_unit"})
            automatic.assert_not_awaited()

    async def test_empty_and_stale_experimental_poll_keeps_safe_mode_metadata(self):
        stack, claim, _ = self.mocks(offer=False)
        with stack:
            response = await self.post()
            self.assertEqual(response.json(), {"preparation": None, "signal_mode": "experimental_demo",
                                               "entry_window_seconds": 30, "provisional": True})
            clock = self.now + timedelta(seconds=11)
            self.feed["as_of"] = self.feed["risk_context"]["as_of"] = clock.isoformat()
            claim.reset_mock()
            with patch.object(mt5_api, "now_utc", return_value=clock):
                response = await self.post()
            self.assertEqual(response.json()["signal_mode"], "experimental_demo")
            self.assertIsNone(response.json()["preparation"])
            claim.assert_not_awaited()

    async def test_poll_caps_expiry_and_rejects_forged_claim_payload_after_claim(self):
        self.offer["expires_at"] = self.now + timedelta(minutes=5)
        stack, _, _ = self.mocks()
        with stack:
            response = await self.post()
            self.assertEqual(response.json()["preparation"]["expires_at"], (self.now + timedelta(seconds=30)).isoformat())
            self.offer["payload"]["qualification_id"] = "f" * 64
            response = await self.post()
            self.assertIsNone(response.json()["preparation"])


if __name__ == "__main__":
    unittest.main()
