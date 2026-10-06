"""Legacy execution stays closed; manual registration remains private."""

from contextlib import ExitStack
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import FastAPI
import httpx

from bot import mt5_api, market_monitor as monitor
from bot.config import Settings
from bot.panel.app import create_app
from tests import test_multi_timeframe as candle_fixtures

NOW = candle_fixtures.MultiTimeframeTests.now
DEVICE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OFFER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
CLAIM = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
KEY = "local-demo-test-bridge-key-1234567890"


def feed():
    value = candle_fixtures.MultiTimeframeTests().feed(now=NOW)
    value["device_id"] = str(DEVICE)
    return value


class DemoAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = SimpleNamespace(pool=object(), bot_id=991, trading_enabled=True,
                                       manual_tickets_enabled=False, source="mt5",
                                       risk_pause=AsyncMock(return_value=None))
        self.app = FastAPI()
        mt5_api.install_routes(self.app, SimpleNamespace(bot_data={"market_service": self.service}),
                              SimpleNamespace(market_bridge_key=KEY))
        self.device = {"device_id": DEVICE, "owner_user_id": 11, "owner_chat_id": 11,
                       "symbol": "XAUUSD", "account_mode": "demo", "volume": 0.01}
        self.enterContext(patch.object(mt5_api, "now_utc", return_value=NOW))
        self.enterContext(patch.dict(os.environ, {"MARKET_GOLD_SYMBOL": "XAUUSD", "MT5_MANUAL_BRIDGE_KEY": KEY}, clear=True))

    async def post(self, route, payload, key=KEY):
        headers = {"Authorization": "Bearer " + key} if key else {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://local.test") as client:
            return await client.post("/api/mt5/" + route, content=json.dumps(payload), headers=headers)

    def registration(self):
        return {"device_id": str(DEVICE), "symbol": "XAUUSD", "account_mode": "demo",
                "volume": 0.01, "pair_code_hash": "a" * 64}

    def manual_mode(self):
        self.service.trading_enabled, self.service.manual_tickets_enabled = False, True

    async def test_automatic_only_service_cannot_register_claim_or_acknowledge_any_execution(self):
        with ExitStack() as stack:
            storage = []
            for module, names in ((mt5_api.trade_store, ("register_device", "get_device", "heartbeat", "expire_offers", "claim_offer", "complete_offer")),
                                  (mt5_api.market_store, ("get_cache",)),
                                  (mt5_api.manual_ticket_store, ("claim_offer", "complete_offer"))):
                for name in names:
                    storage.append(stack.enter_context(patch.object(module, name, new_callable=AsyncMock)))
            for enabled in (True, False):
                self.service.trading_enabled = enabled
                for route, body in (("register", self.registration()), ("poll", {"device_id": str(DEVICE)}),
                                    ("result", {"device_id": str(DEVICE), "offer_id": str(OFFER), "claim_id": str(CLAIM),
                                                "result": {"status": "filled", "code": 10009, "order_ticket": 123}}),
                                    ("manual/poll", {"device_id": str(DEVICE)}), ("manual/result", {})):
                    with self.subTest(enabled=enabled, route=route):
                        self.assertEqual((await self.post(route, body)).status_code, 404)
            for function in storage:
                function.assert_not_awaited()

    async def test_unauthorized_and_disabled_manual_registration_does_not_touch_store(self):
        self.manual_mode()
        with patch.object(mt5_api.trade_store, "register_device", new_callable=AsyncMock) as register:
            for key in (None, "incorrect"):
                self.assertEqual((await self.post("register", self.registration(), key=key)).status_code, 401)
            self.service.manual_tickets_enabled = False
            self.assertEqual((await self.post("register", self.registration())).status_code, 404)
            register.assert_not_awaited()

    async def test_live_account_different_volume_unknown_symbol_or_invalid_device_cannot_register(self):
        self.manual_mode()
        with patch.object(mt5_api.trade_store, "register_device", new_callable=AsyncMock) as register:
            for name, value in (("account_mode", "real"), ("volume", 0.02), ("volume", True),
                                ("symbol", "XAUUSD.other"), ("device_id", "invalid")):
                body = self.registration(); body[name] = value
                self.assertEqual((await self.post("register", body)).status_code, 422)
            register.assert_not_awaited()

    async def test_registration_does_not_disclose_pairing_hash_or_private_rows(self):
        self.manual_mode()
        private = {**self.device, "pair_code_hash": "a" * 64, "account_password": "private-test-only"}
        with patch.object(mt5_api.trade_store, "register_device", new_callable=AsyncMock, return_value=private) as register:
            response = await self.post("register", self.registration())
            self.assertEqual(response.json(), {"ok": True, "paired": True})
            self.assertEqual(register.await_args.args[2], DEVICE)
            self.assertNotIn("a" * 64, response.text)
            self.assertNotIn("private-test-only", response.text)

    async def test_execution_result_remains_closed_even_with_complete_proof_and_manual_mode(self):
        with patch.object(mt5_api.trade_store, "complete_offer", new_callable=AsyncMock) as complete:
            for manual in (False, True):
                self.service.manual_tickets_enabled = manual
                for outcome in ({"status": "filled", "code": 10009, "order_ticket": 123, "executed_at": NOW.isoformat()},
                                {"status": "filled", "code": 10010, "order_ticket": 123},
                                {"status": "filled", "account_password": "private"}):
                    response = await self.post("result", {"device_id": str(DEVICE), "offer_id": str(OFFER),
                                                           "claim_id": str(CLAIM), "result": outcome})
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn("private", response.text)
            complete.assert_not_awaited()


class BridgeConfigurationTests(unittest.TestCase):
    def test_bridge_http_without_panel_has_health_and_private_routes_but_no_admin_login(self):
        settings = Settings(bot_token="123:local-test", database_url="postgres://local", market_bridge_key=KEY)
        self.assertTrue(settings.http_enabled)
        self.assertFalse(settings.panel_enabled)
        app = create_app(SimpleNamespace(bot_data={}), settings)
        routes = {route.path for route in app.routes}
        self.assertIn("/healthz", routes)
        self.assertIn("/api/market/feed", routes)
        self.assertIn("/api/mt5/poll", routes)
        self.assertNotIn("/login", routes)
        self.assertNotIn("/", routes)

    def test_device_identity_keeps_strategy_levels_scoped_to_terminal(self):
        payload = feed()
        clean = monitor.validate_feed(payload, NOW)
        self.assertEqual(clean["device_id"], str(DEVICE))
        self.assertEqual(monitor._mt5_identity(clean), "mt5:XAUUSD:" + str(DEVICE))
        del payload["device_id"]
        self.assertEqual(monitor._mt5_identity(monitor.validate_feed(payload, NOW)), "mt5:XAUUSD")

    def test_broker_metadata_is_finite_strict_and_preserved(self):
        payload = feed()
        self.assertEqual(monitor.validate_feed(payload, NOW)["execution"], payload["execution"])
        for field, value in (("tick_size", 0), ("point", float("nan")), ("digits", True), ("stops_level", -1)):
            malformed = feed(); malformed["execution"][field] = value
            with self.assertRaises(ValueError):
                monitor.validate_feed(malformed, NOW)
