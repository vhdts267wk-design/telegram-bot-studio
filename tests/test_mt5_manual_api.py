"""Manual preparation transport must never expose the execution queue."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import FastAPI
import httpx

from bot import mt5_api
from tests import test_mtf_runtime as evidence_fixtures
from tests import test_proposal_overlay as overlay_fixtures

NOW = overlay_fixtures.NOW
DEVICE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OFFER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
CLAIM = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
MANUAL_KEY = "isolated-manual-preparation-test-key-1234567890"
AUTOMATIC_KEY = "isolated-automatic-execution-test-key-1234567890"


def feed():
    return overlay_fixtures.feed()


class ManualAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = SimpleNamespace(pool=object(), bot_id=991, trading_enabled=False,
                                       manual_tickets_enabled=True, source="mt5",
                                       risk_pause=AsyncMock(return_value=None))
        self.app = FastAPI()
        mt5_api.install_routes(self.app, SimpleNamespace(bot_data={"market_service": self.service}),
                              SimpleNamespace(market_bridge_key=AUTOMATIC_KEY))
        self.device = {"device_id": DEVICE, "owner_user_id": 11, "owner_chat_id": 11,
                       "symbol": "XAUUSD", "account_mode": "demo", "volume": 0.01, "last_seen_at": NOW}
        self.snapshot = {"payload": feed(), "updated_at": NOW}
        self.clock = patch.object(mt5_api, "now_utc", return_value=NOW)
        self.env = patch.dict(os.environ, {"MARKET_GOLD_SYMBOL": "XAUUSD", "MT5_MANUAL_BRIDGE_KEY": MANUAL_KEY}, clear=True)
        self.clock.start(); self.env.start()
        self.addCleanup(self.clock.stop); self.addCleanup(self.env.stop)
        self.pin = self.enterContext(evidence_fixtures.pinned_synthetic_evidence(self.snapshot["payload"], NOW))
        self.offer = overlay_fixtures.offer("preparing")

    async def post(self, route, body, key=MANUAL_KEY):
        headers = {"Authorization": "Bearer " + key} if key else {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://local.test") as client:
            return await client.post("/api/mt5/" + route, content=json.dumps(body), headers=headers)

    def poll_mocks(self):
        stack = ExitStack()
        stack.enter_context(patch.object(mt5_api.trade_store, "get_device", new_callable=AsyncMock, return_value=self.device))
        stack.enter_context(patch.object(mt5_api.trade_store, "heartbeat", new_callable=AsyncMock))
        stack.enter_context(patch.object(mt5_api.manual_ticket_store, "expire_offers", new_callable=AsyncMock))
        stack.enter_context(patch.object(mt5_api.market_store, "get_cache", new_callable=AsyncMock, return_value=self.snapshot))
        claim = stack.enter_context(patch.object(mt5_api.manual_ticket_store, "claim_offer", new_callable=AsyncMock, return_value=self.offer))
        automatic = stack.enter_context(patch.object(mt5_api.trade_store, "claim_offer", new_callable=AsyncMock))
        return stack, claim, automatic

    def registration(self):
        return {"device_id": str(DEVICE), "symbol": "XAUUSD", "account_mode": "demo",
                "volume": 0.01, "pair_code_hash": "a" * 64}

    async def test_manual_pairing_reuses_identity_and_requires_dedicated_key(self):
        with patch.object(mt5_api.trade_store, "register_device", new_callable=AsyncMock, return_value=self.device) as register:
            self.assertEqual((await self.post("register", self.registration(), AUTOMATIC_KEY)).status_code, 401)
            self.assertEqual((await self.post("register", self.registration(), key=None)).status_code, 401)
            register.assert_not_awaited()
            response = await self.post("register", self.registration())
            self.assertEqual(response.json(), {"ok": True, "paired": True})
            self.assertEqual(register.await_args.args[2], DEVICE)
            self.assertNotIn("a" * 64, response.text)

    async def test_manual_pairing_does_not_expand_account_or_volume_policy(self):
        with patch.object(mt5_api.trade_store, "register_device", new_callable=AsyncMock) as register:
            for name, value in (("account_mode", "real"), ("volume", 0.02), ("volume", True),
                                ("symbol", "XAUUSD.other"), ("workflow", "automatic")):
                body = self.registration(); body[name] = value
                self.assertEqual((await self.post("register", body)).status_code, 422)
            register.assert_not_awaited()

    async def test_manual_mode_cannot_reach_legacy_execution_claim_or_result(self):
        with patch.object(mt5_api.trade_store, "claim_offer", new_callable=AsyncMock) as claim, \
             patch.object(mt5_api.trade_store, "complete_offer", new_callable=AsyncMock) as complete:
            for key in (AUTOMATIC_KEY, MANUAL_KEY):
                self.assertEqual((await self.post("poll", {"device_id": str(DEVICE)}, key)).status_code, 404)
                self.assertEqual((await self.post("result", {"device_id": str(DEVICE), "offer_id": str(OFFER),
                                                          "claim_id": str(CLAIM), "result": {"status": "filled"}}, key)).status_code, 404)
            claim.assert_not_awaited(); complete.assert_not_awaited()

    async def test_automatic_only_or_disabled_modes_cannot_reach_manual_queue(self):
        with patch.object(mt5_api.manual_ticket_store, "claim_offer", new_callable=AsyncMock) as claim:
            for automatic, manual in ((True, False), (False, False)):
                self.service.trading_enabled, self.service.manual_tickets_enabled = automatic, manual
                self.assertEqual((await self.post("manual/poll", {"device_id": str(DEVICE)})).status_code, 404)
                self.assertEqual((await self.post("manual/result", {})).status_code, 404)
            claim.assert_not_awaited()

    async def test_stale_automatic_flag_does_not_disable_manual_requests_or_enable_execution(self):
        self.service.trading_enabled, self.service.manual_tickets_enabled = True, True
        stack, claim, automatic = self.poll_mocks()
        with stack:
            self.assertIsNotNone((await self.post("manual/poll", {"device_id": str(DEVICE)})).json()["preparation"])
            self.assertEqual((await self.post("poll", {"device_id": str(DEVICE)})).status_code, 404)
            claim.assert_awaited_once()
            automatic.assert_not_awaited()

    async def test_missing_or_short_manual_key_cannot_fall_back_to_automatic_key(self):
        with patch.object(mt5_api.trade_store, "register_device", new_callable=AsyncMock) as register:
            for key in ("", "short"):
                os.environ["MT5_MANUAL_BRIDGE_KEY"] = key
                self.assertEqual((await self.post("register", self.registration(), AUTOMATIC_KEY)).status_code, 404)
            register.assert_not_awaited()

    async def test_environment_manual_mode_blocks_stale_automatic_service(self):
        os.environ["MT5_MANUAL_TICKETS_ENABLED"] = "true"
        self.service.trading_enabled, self.service.manual_tickets_enabled = True, False
        with patch.object(mt5_api.trade_store, "claim_offer", new_callable=AsyncMock) as claim, \
             patch.object(mt5_api.trade_store, "register_device", new_callable=AsyncMock, return_value=self.device) as register:
            for key in (AUTOMATIC_KEY, MANUAL_KEY):
                self.assertEqual((await self.post("poll", {"device_id": str(DEVICE)}, key)).status_code, 404)
            self.assertEqual((await self.post("register", self.registration(), AUTOMATIC_KEY)).status_code, 401)
            self.assertEqual((await self.post("register", self.registration(), MANUAL_KEY)).json(), {"ok": True, "paired": True})
            claim.assert_not_awaited(); register.assert_awaited_once()

    async def test_malformed_service_mode_flags_fail_closed(self):
        with patch.object(mt5_api.trade_store, "register_device", new_callable=AsyncMock) as register:
            for name, value in (("trading_enabled", 1), ("manual_tickets_enabled", "true"), ("manual_tickets_enabled", None)):
                self.service.trading_enabled, self.service.manual_tickets_enabled = False, True
                setattr(self.service, name, value)
                self.assertEqual((await self.post("register", self.registration())).status_code, 404)
            register.assert_not_awaited()

    async def test_poll_returns_only_single_manual_preparation_envelope(self):
        stack, claim, automatic = self.poll_mocks()
        with stack:
            response = await self.post("manual/poll", {"device_id": str(DEVICE)})
            self.assertEqual(set(response.json()), {"preparation"})
            preparation = response.json()["preparation"]
            self.assertEqual(preparation["workflow"], "manual_ticket")
            self.assertEqual((preparation["id"], preparation["claim_id"]), (str(OFFER), str(CLAIM)))
            self.assertEqual(preparation["payload"], self.offer["payload"])
            self.assertEqual(preparation["expires_at"], self.offer["expires_at"].isoformat())
            expected_context = {key: self.offer["payload"][key] for key in (
                "direction", "bar_time", "confirmation_bar_time", "direction_bar_time",
                "broker_fingerprint", "policy_id", "cost_context", "execution")}
            claim.assert_awaited_once_with(self.service.pool, 991, DEVICE, NOW,
                                           qualification_id=self.pin,
                                           strategy_fingerprint=self.offer["payload"]["strategy_fingerprint"],
                                           proposal_context=expected_context)
            automatic.assert_not_awaited()

    async def test_invalid_auth_or_poll_fields_cannot_claim(self):
        stack, claim, automatic = self.poll_mocks()
        with stack:
            self.assertEqual((await self.post("manual/poll", {"device_id": str(DEVICE)}, AUTOMATIC_KEY)).status_code, 401)
            for body in ({"device_id": str(DEVICE), "workflow": "automatic"}, {"device_id": str(DEVICE).upper()},
                         {"device_id": True}, [], None):
                self.assertEqual((await self.post("manual/poll", body)).status_code, 422)
            claim.assert_not_awaited(); automatic.assert_not_awaited()

    async def test_quote_skew_and_stale_limits_stay_independent(self):
        stack, claim, _ = self.poll_mocks()
        # Ten seconds after the bar close, the oldest accepted quote is still
        # coherent with the completed history. Quote-age and candle-time
        # guards are both exercised without fabricating a post-quote candle.
        clock = NOW + timedelta(seconds=10)
        with stack, patch.object(mt5_api, "now_utc", return_value=clock), \
                patch.object(mt5_api.mtf_runtime, "_clock", return_value=clock):
            for seconds, allowed in ((5, True), (6, False), (-10, True), (-11, False)):
                with self.subTest(seconds=seconds):
                    claim.reset_mock()
                    self.snapshot["payload"]["quote"]["time"] = (clock + timedelta(seconds=seconds)).isoformat()
                    response = await self.post("manual/poll", {"device_id": str(DEVICE)})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["preparation"] is not None, allowed)
                    self.assertEqual(claim.await_count, int(allowed))

    async def test_device_and_feed_receipt_require_strict_current_timestamps(self):
        stack, claim, _ = self.poll_mocks()
        with stack:
            for target, field in ((self.device, "last_seen_at"), (self.snapshot, "updated_at")):
                for seconds, allowed in ((0, True), (-180, True), (-181, False), (1, False)):
                    with self.subTest(field=field, seconds=seconds):
                        claim.reset_mock(); target[field] = NOW + timedelta(seconds=seconds)
                        response = await self.post("manual/poll", {"device_id": str(DEVICE)})
                        self.assertEqual(response.json()["preparation"] is not None, allowed)
                        self.assertEqual(claim.await_count, int(allowed))
                target[field] = NOW

    async def test_owner_device_metadata_and_pause_guards_prevent_preparation(self):
        stack, claim, _ = self.poll_mocks()
        with stack:
            self.device["owner_user_id"] = 12
            self.assertEqual((await self.post("manual/poll", {"device_id": str(DEVICE)})).json(), {"preparation": None})
            self.device["owner_user_id"] = 11
            self.snapshot["payload"]["device_id"] = str(OFFER)
            self.assertEqual((await self.post("manual/poll", {"device_id": str(DEVICE)})).json(), {"preparation": None})
            self.snapshot["payload"] = feed(); del self.snapshot["payload"]["execution"]
            response = await self.post("manual/poll", {"device_id": str(DEVICE)})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json(), {"detail": "Invalid device or market data"})
            self.snapshot["payload"] = feed(); self.service.risk_pause.return_value = "paused"
            self.assertEqual((await self.post("manual/poll", {"device_id": str(DEVICE)})).json(), {"preparation": None})
            claim.assert_not_awaited()

    async def test_slow_risk_check_cannot_claim_a_now_stale_quote(self):
        stack, claim, _ = self.poll_mocks()
        with stack:
            with patch.object(mt5_api, "now_utc", side_effect=[NOW, NOW, NOW + timedelta(seconds=11)]):
                self.assertEqual((await self.post("manual/poll", {"device_id": str(DEVICE)})).json(), {"preparation": None})
            claim.assert_not_awaited()

    async def test_missing_operator_pin_unknown_costs_or_legacy_m15_feed_never_claims(self):
        stack, claim, automatic = self.poll_mocks()
        with stack:
            with patch.dict(os.environ, {"MT5_EVIDENCE_SHA256": ""}):
                self.assertEqual((await self.post("manual/poll", {"device_id": str(DEVICE)})).json(), {"preparation": None})
            for change in ("costs", "legacy"):
                self.snapshot["payload"] = feed()
                if change == "costs":
                    self.snapshot["payload"]["risk_context"]["costs_verified"] = False
                else:
                    for key in ("schema_version", "timeframes", "risk_context", "as_of", "broker_utc_offset_minutes"):
                        del self.snapshot["payload"][key]
                with self.subTest(change=change):
                    self.assertEqual((await self.post("manual/poll", {"device_id": str(DEVICE)})).json(), {"preparation": None})
            claim.assert_not_awaited(); automatic.assert_not_awaited()

    async def test_owner_switch_after_atomic_claim_cannot_return_previous_owner_request(self):
        stack, claim, _ = self.poll_mocks()
        changed = deepcopy(self.device)
        changed.update(owner_chat_id=12, owner_user_id=12)
        with stack, patch.object(mt5_api.trade_store, "get_device", new_callable=AsyncMock,
                                  side_effect=[self.device, self.device, self.device, changed]):
            self.assertEqual((await self.post("manual/poll", {"device_id": str(DEVICE)})).json(), {"preparation": None})
            claim.assert_awaited_once()

    async def test_storage_latency_is_included_when_checking_quote_freshness(self):
        stack, claim, _ = self.poll_mocks()
        with stack:
            with patch.object(mt5_api, "now_utc", side_effect=[NOW, NOW + timedelta(seconds=11)]):
                self.assertEqual((await self.post("manual/poll", {"device_id": str(DEVICE)})).json(), {"preparation": None})
            claim.assert_not_awaited()

    async def test_unknown_device_or_empty_queue_is_explicit(self):
        stack, claim, _ = self.poll_mocks()
        with stack:
            claim.return_value = None
            self.assertEqual((await self.post("manual/poll", {"device_id": str(DEVICE)})).json(), {"preparation": None})
            with patch.object(mt5_api.trade_store, "get_device", new_callable=AsyncMock, return_value=None):
                self.assertEqual((await self.post("manual/poll", {"device_id": str(DEVICE)})).status_code, 404)

    async def test_result_is_bound_to_exact_manual_claim_and_contains_no_execution_proof(self):
        with patch.object(mt5_api.manual_ticket_store, "complete_offer", new_callable=AsyncMock, return_value={"status": "prepared"}) as complete, \
             patch.object(mt5_api.trade_store, "complete_offer", new_callable=AsyncMock) as automatic:
            for status in ("prepared", "failed"):
                body = {"device_id": str(DEVICE), "offer_id": str(OFFER), "claim_id": str(CLAIM), "result": {"status": status}}
                self.assertEqual((await self.post("manual/result", body)).json(), {"ok": True})
                self.assertEqual(complete.await_args.args, (self.service.pool, 991, DEVICE, OFFER, CLAIM, {"status": status}, NOW))
            complete.return_value = None
            self.assertEqual((await self.post("manual/result", body)).status_code, 409)
            automatic.assert_not_awaited()

    async def test_execution_and_unknown_results_or_extra_fields_are_rejected(self):
        with patch.object(mt5_api.manual_ticket_store, "complete_offer", new_callable=AsyncMock) as complete:
            for outcome in ({"status": "filled", "code": 10009, "order_ticket": 12}, {"status": "unknown"},
                            {"status": "prepared", "executed_at": NOW.isoformat()}, {"status": "prepared", "code": 0},
                            {"status": "failed", "account_password": "private"}, {"status": []}, {}, None):
                body = {"device_id": str(DEVICE), "offer_id": str(OFFER), "claim_id": str(CLAIM), "result": outcome}
                response = await self.post("manual/result", body)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("private", response.text)
            complete.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
