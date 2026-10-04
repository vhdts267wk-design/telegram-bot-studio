from datetime import datetime, timedelta, timezone
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

NOW = datetime(2026, 10, 4, 10, tzinfo=timezone.utc)
DEVICE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OFFER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
CLAIM = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
KEY = "local-demo-test-bridge-key-1234567890"


def feed():
    return {"device_id": str(DEVICE), "symbol": "XAUUSD", "timeframe": "M15", "source": "MetaTrader 5",
            "execution": {"tick_size": 0.01, "point": 0.01, "digits": 2, "stops_level": 20},
            "quote": {"bid": 2000, "ask": 2000.1, "time": NOW.isoformat()},
            "candles": [{"time": (NOW-timedelta(minutes=15*i)).isoformat(),
                         "open": 2000, "high": 2001, "low": 1999, "close": 2000, "tick_volume": 20}
                        for i in range(4, 0, -1)]}


class DemoAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = SimpleNamespace(pool=object(), bot_id=991, trading_enabled=True, source="mt5",
                                       risk_pause=AsyncMock(return_value=None))
        self.app = FastAPI()
        mt5_api.install_routes(self.app, SimpleNamespace(bot_data={"market_service": self.service}),
                              SimpleNamespace(market_bridge_key=KEY))
        self.device = {"device_id": DEVICE, "owner_user_id": 11, "owner_chat_id": 11,
                       "symbol": "XAUUSD", "account_mode": "demo", "volume": 0.01}
        self.clock = patch.object(mt5_api, "now_utc", return_value=NOW)
        self.env = patch.dict(os.environ, {"MARKET_GOLD_SYMBOL": "XAUUSD"}, clear=True)
        self.clock.start(); self.env.start()
        self.addCleanup(self.clock.stop); self.addCleanup(self.env.stop)

    async def post(self, route, payload, key=KEY):
        headers = {"Authorization": "Bearer " + key} if key else {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://local.test") as client:
            return await client.post("/api/mt5/" + route, content=json.dumps(payload), headers=headers)

    def registration(self):
        return {"device_id": str(DEVICE), "symbol": "XAUUSD", "account_mode": "demo",
                "volume": 0.01, "pair_code_hash": "a"*64}

    async def test_unauthorized_and_disabled_requests_do_not_touch_store(self):
        with patch.object(mt5_api.trade_store, "register_device", new_callable=AsyncMock) as register:
            self.assertEqual((await self.post("register", self.registration(), key="incorrect")).status_code, 401)
            self.service.trading_enabled = False
            self.assertEqual((await self.post("register", self.registration())).status_code, 404)
            register.assert_not_awaited()

    async def test_live_account_different_volume_and_unknown_symbol_cannot_register(self):
        with patch.object(mt5_api.trade_store, "register_device", new_callable=AsyncMock) as register:
            for name, value in (("account_mode", "real"), ("volume", 0.02), ("volume", True),
                                ("symbol", "XAUUSD.other"), ("device_id", "invalid")):
                body = self.registration(); body[name] = value
                self.assertEqual((await self.post("register", body)).status_code, 422)
            register.assert_not_awaited()

    async def test_registration_does_not_disclose_pairing_hash_or_private_rows(self):
        with patch.object(mt5_api.trade_store, "register_device", new_callable=AsyncMock, return_value=self.device) as register:
            response = await self.post("register", self.registration())
            self.assertEqual(response.json(), {"ok": True, "paired": True})
            self.assertEqual(register.await_args.args[2], DEVICE)
            self.assertNotIn("a"*64, response.text)

    async def test_poll_claims_only_current_device_and_fresh_provider_quotes(self):
        snapshot = {"payload": feed(), "updated_at": NOW}
        offer = {"id": OFFER, "claim_id": CLAIM, "payload": {"direction": "BUY"}, "expires_at": NOW+timedelta(minutes=5)}
        with patch.object(mt5_api.trade_store, "get_device", new_callable=AsyncMock, return_value=self.device), \
             patch.object(mt5_api.trade_store, "heartbeat", new_callable=AsyncMock), \
             patch.object(mt5_api.trade_store, "expire_offers", new_callable=AsyncMock), \
             patch.object(mt5_api.market_store, "get_cache", new_callable=AsyncMock, return_value=snapshot), \
             patch.object(mt5_api.trade_store, "claim_offer", new_callable=AsyncMock, return_value=offer) as claim:
            response = await self.post("poll", {"device_id": str(DEVICE)})
            self.assertEqual(response.json()["trade"]["claim_id"], str(CLAIM))
            claim.assert_awaited_once()
            claim.reset_mock()
            for field, value in (("time", (NOW-timedelta(seconds=31)).isoformat()),
                                 ("time", (NOW+timedelta(seconds=1)).isoformat())):
                snapshot["payload"]["quote"][field] = value
                self.assertEqual((await self.post("poll", {"device_id": str(DEVICE)})).json(), {"trade": None})
            snapshot["payload"] = feed(); snapshot["payload"]["device_id"] = str(OFFER)
            self.assertEqual((await self.post("poll", {"device_id": str(DEVICE)})).json(), {"trade": None})
            claim.assert_not_awaited()

    async def test_paused_or_missing_broker_metadata_cannot_claim_accepted_request(self):
        snapshot = {"payload": feed(), "updated_at": NOW}
        with patch.object(mt5_api.trade_store, "get_device", new_callable=AsyncMock, return_value=self.device), \
             patch.object(mt5_api.trade_store, "heartbeat", new_callable=AsyncMock), \
             patch.object(mt5_api.trade_store, "expire_offers", new_callable=AsyncMock), \
             patch.object(mt5_api.market_store, "get_cache", new_callable=AsyncMock, return_value=snapshot), \
             patch.object(mt5_api.trade_store, "claim_offer", new_callable=AsyncMock) as claim:
            self.service.risk_pause.return_value = "paused"
            self.assertEqual((await self.post("poll", {"device_id": str(DEVICE)})).json(), {"trade": None})
            self.service.risk_pause.return_value = None
            del snapshot["payload"]["execution"]
            self.assertEqual((await self.post("poll", {"device_id": str(DEVICE)})).json(), {"trade": None})
            claim.assert_not_awaited()

    async def test_partial_or_unproven_results_cannot_be_claimed_as_filled(self):
        with patch.object(mt5_api.trade_store, "complete_offer", new_callable=AsyncMock) as complete:
            for outcome in ({"status": "filled", "code": 10010, "order_ticket": 123},
                            {"status": "filled", "code": 10009},
                            {"status": "filled", "code": 10009, "order_ticket": True},
                            {"status": "filled", "code": 10009, "order_ticket": 123, "account_password": "private"}):
                response = await self.post("result", {"device_id": str(DEVICE), "offer_id": str(OFFER),
                                                       "claim_id": str(CLAIM), "result": outcome})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("private", response.text)
            complete.assert_not_awaited()

    async def test_full_execution_ack_is_scoped_to_device_offer_and_single_claim(self):
        outcome = {"status": "filled", "code": 10009, "order_ticket": 123, "executed_at": NOW.isoformat()}
        with patch.object(mt5_api.trade_store, "complete_offer", new_callable=AsyncMock, return_value={"status": "filled"}) as complete:
            response = await self.post("result", {"device_id": str(DEVICE), "offer_id": str(OFFER),
                                                   "claim_id": str(CLAIM), "result": outcome})
            self.assertEqual(response.json(), {"ok": True})
            self.assertEqual(complete.await_args.args[2:5], (DEVICE, OFFER, CLAIM))
            complete.return_value = None
            self.assertEqual((await self.post("result", {"device_id": str(DEVICE), "offer_id": str(OFFER),
                                                        "claim_id": str(CLAIM), "result": outcome})).status_code, 409)


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

    def test_device_identity_keeps_frozen_strategy_levels_scoped_to_terminal(self):
        payload = feed()
        clean = monitor.validate_feed(payload, NOW)
        self.assertEqual(clean["device_id"], str(DEVICE))
        self.assertEqual(monitor._mt5_identity(clean), "mt5:XAUUSD:"+str(DEVICE))
        del payload["device_id"]
        self.assertEqual(monitor._mt5_identity(monitor.validate_feed(payload, NOW)), "mt5:XAUUSD")

    def test_broker_metadata_is_finite_strict_and_preserved(self):
        payload = feed()
        self.assertEqual(monitor.validate_feed(payload, NOW)["execution"], payload["execution"])
        for field, value in (("tick_size", 0), ("point", float("nan")), ("digits", True), ("stops_level", -1)):
            malformed = feed(); malformed["execution"][field] = value
            with self.assertRaises(ValueError):
                monitor.validate_feed(malformed, NOW)
