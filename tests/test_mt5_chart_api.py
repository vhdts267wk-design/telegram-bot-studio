"""The chart transport is authenticated and SELECT-only, never a preparation."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from bot import mt5_api
from tests import test_mt5_manual_api as api_fixtures
from tests import test_proposal_overlay as overlay_fixtures

NOW, DEVICE, OFFER = overlay_fixtures.NOW, overlay_fixtures.DEVICE, overlay_fixtures.OFFER


class ChartAPITests(unittest.IsolatedAsyncioTestCase):
    setUp = api_fixtures.ManualAPITests.setUp
    post = api_fixtures.ManualAPITests.post

    def chart_mocks(self):
        stack = ExitStack()
        self.device = overlay_fixtures.device()
        self.snapshot = {"payload": overlay_fixtures.feed(), "updated_at": NOW}
        self.offer = overlay_fixtures.offer()
        readers = {}
        for module, name, result in (
            (mt5_api.trade_store, "get_device", self.device),
            (mt5_api.market_store, "get_cache", self.snapshot),
            (mt5_api.trade_store, "subscription_active", True),
            (mt5_api.manual_ticket_store, "get_chart_offer", self.offer),
        ):
            readers[name] = stack.enter_context(patch.object(module, name, new_callable=AsyncMock, return_value=result))
        writes = []
        for module, names in (
            (mt5_api.trade_store, ("heartbeat", "claim_offer", "complete_offer", "expire_offers", "register_device")),
            (mt5_api.manual_ticket_store, ("claim_offer", "complete_offer", "expire_offers", "decide", "publish_offer", "create_offer")),
        ):
            for name in names:
                writes.append(stack.enter_context(patch.object(module, name, new_callable=AsyncMock)))
        return stack, readers, writes

    async def chart(self, **kwargs):
        return await self.post("manual/chart", {"device_id": str(DEVICE)}, **kwargs)

    async def test_valid_read_only_overlay_returns_public_frozen_contract(self):
        stack, readers, writes = self.chart_mocks()
        with stack:
            response = await self.chart()
            self.assertEqual(response.status_code, 200)
            dto = response.json()["proposal"]
            self.assertEqual((dto["offer_id"], dto["workflow"], dto["entry_zone_low"], dto["entry_zone_high"]),
                             (str(OFFER), "chart_overlay", 1999, 2001))
            self.assertEqual(dto["stop"], self.offer["payload"]["stop"])
            self.assertEqual(dto["target"], self.offer["payload"]["target"])
            self.assertFalse({"claim_id", "device_id", "user_id", "chat_id", "account_mode"} & dto.keys())
            readers["get_chart_offer"].assert_awaited_once()
            self.assertEqual(readers["get_device"].await_count, 2)
            self.assertEqual(readers["get_cache"].await_count, 2)
            for mutation in writes:
                mutation.assert_not_awaited()

    async def test_dedicated_auth_and_exact_request_fields_are_required(self):
        stack, readers, _ = self.chart_mocks()
        with stack:
            for key in (api_fixtures.AUTOMATIC_KEY, None, "wrong"):
                self.assertEqual((await self.chart(key=key)).status_code, 401)
            for body in ({}, {"device_id": str(DEVICE), "claim_id": str(OFFER)},
                         {"device_id": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"}, {"device_id": True}):
                self.assertEqual((await self.post("manual/chart", body)).status_code, 422)
            readers["get_device"].assert_not_awaited()

    async def test_automatic_disabled_and_conflicting_modes_have_no_chart_route_access(self):
        stack, readers, _ = self.chart_mocks()
        with stack:
            for automatic, manual in ((True, False), (False, False), (True, True)):
                self.service.trading_enabled, self.service.manual_tickets_enabled = automatic, manual
                self.assertEqual((await self.chart()).status_code, 404)
            readers["get_device"].assert_not_awaited()

    async def test_duplicate_json_fields_are_rejected_before_storage(self):
        stack, readers, _ = self.chart_mocks()
        with stack:
            body = '{"device_id":' + json.dumps(str(DEVICE)) + ',"device_id":' + json.dumps(str(DEVICE)) + '}'
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://local.test") as client:
                response = await client.post("/api/mt5/manual/chart", content=body,
                                             headers={"Authorization": "Bearer " + api_fixtures.MANUAL_KEY})
            self.assertEqual(response.status_code, 422)
            readers["get_device"].assert_not_awaited()

    async def test_absent_newest_or_ineligible_proposal_clears_without_fallback(self):
        stack, readers, writes = self.chart_mocks()
        with stack:
            for status in (None, "draft", "rejected", "expired", "cancelled", "failed", "unknown"):
                candidate = overlay_fixtures.offer(status) if status else None
                readers["get_chart_offer"].return_value = candidate
                self.assertEqual((await self.chart()).json(), {"proposal": None})
            self.assertEqual(readers["get_chart_offer"].await_count, 7)
            for mutation in writes:
                mutation.assert_not_awaited()

    async def test_expired_prepared_and_interrupted_preparation_are_hidden(self):
        stack, readers, _ = self.chart_mocks()
        with stack:
            for status in ("offered", "requested", "preparing", "prepared"):
                candidate = overlay_fixtures.offer(status); candidate["expires_at"] = NOW
                readers["get_chart_offer"].return_value = candidate
                self.assertEqual((await self.chart()).json(), {"proposal": None})
            candidate = overlay_fixtures.offer("preparing")
            readers["get_chart_offer"].return_value = candidate
            later = NOW + timedelta(seconds=121)
            self.device["last_seen_at"] = later
            self.snapshot["updated_at"] = later
            self.snapshot["payload"]["quote"]["time"] = later.isoformat()
            with patch.object(mt5_api, "now_utc", return_value=later):
                self.assertEqual((await self.chart()).json(), {"proposal": None})

    async def test_expiry_is_rechecked_after_offer_storage_latency(self):
        stack, readers, _ = self.chart_mocks()
        self.offer["expires_at"] = NOW + timedelta(seconds=1)
        clock = {"now": NOW}
        async def slow_read(*args):
            clock["now"] = NOW + timedelta(seconds=2)
            return self.offer
        with stack, patch.object(mt5_api, "now_utc", side_effect=lambda: clock["now"]):
            readers["get_chart_offer"].side_effect = slow_read
            self.assertEqual((await self.chart()).json(), {"proposal": None})

    async def test_quote_is_rechecked_after_async_risk_and_subscription_reads(self):
        for slow_stage in ("risk", "subscription", "offer"):
            stack, readers, _ = self.chart_mocks()
            clock = {"now": NOW}
            async def slow_read(*args):
                clock["now"] = NOW + timedelta(seconds=31)
                return {"risk": None, "subscription": True, "offer": self.offer}[slow_stage]
            with stack, patch.object(mt5_api, "now_utc", side_effect=lambda: clock["now"]):
                if slow_stage == "risk":
                    self.service.risk_pause.side_effect = slow_read
                else:
                    readers[{"subscription": "subscription_active", "offer": "get_chart_offer"}[slow_stage]].side_effect = slow_read
                self.assertEqual((await self.chart()).json(), {"proposal": None})
            self.service.risk_pause.side_effect = None

    async def test_snapshot_binding_receipt_freshness_and_quote_bounds(self):
        for condition in ("device", "owner", "old_receipt", "future_receipt", "old_quote", "future_quote", "old_heartbeat"):
            stack, readers, _ = self.chart_mocks()
            if condition == "device":
                self.snapshot["payload"]["device_id"] = str(OFFER)
            elif condition == "owner":
                self.device["owner_chat_id"] = 12
            elif condition in {"old_receipt", "future_receipt"}:
                self.snapshot["updated_at"] = NOW + timedelta(seconds=-181 if condition == "old_receipt" else 1)
            elif condition in {"old_quote", "future_quote"}:
                self.snapshot["payload"]["quote"]["time"] = (NOW + timedelta(seconds=-31 if condition == "old_quote" else 6)).isoformat()
            else:
                self.device["last_seen_at"] = NOW - timedelta(seconds=181)
            with stack, self.subTest(condition=condition):
                self.assertEqual((await self.chart()).json(), {"proposal": None})
                readers["get_chart_offer"].assert_not_awaited()

    async def test_owner_change_after_risk_check_cannot_reuse_previous_owner_proposal(self):
        stack, readers, _ = self.chart_mocks()
        changed = deepcopy(self.device); changed.update(owner_user_id=12, owner_chat_id=12)
        with stack:
            readers["get_device"].side_effect = [self.device, changed]
            self.assertEqual((await self.chart()).json(), {"proposal": None})
            self.service.risk_pause.assert_awaited_once_with(11)

    async def test_subscription_and_risk_pause_suppress_display(self):
        stack, readers, _ = self.chart_mocks()
        with stack:
            self.service.risk_pause.return_value = NOW + timedelta(minutes=15)
            self.assertEqual((await self.chart()).json(), {"proposal": None})
            readers["get_chart_offer"].assert_not_awaited()
            self.service.risk_pause.return_value = None
            readers["subscription_active"].return_value = False
            self.assertEqual((await self.chart()).json(), {"proposal": None})

    async def test_metadata_change_and_bad_immutable_risk_do_not_redraw_changed_levels(self):
        stack, _, _ = self.chart_mocks()
        with stack:
            self.snapshot["payload"]["execution"]["stops_level"] = 21
            self.assertEqual((await self.chart()).json(), {"proposal": None})
            self.snapshot["payload"]["execution"]["stops_level"] = 20
            self.offer["payload"]["original_stop_distance"] = 9
            self.assertEqual((await self.chart()).json(), {"proposal": None})

    async def test_unregistered_device_has_no_side_effects_or_proposal(self):
        stack, readers, writes = self.chart_mocks()
        with stack:
            readers["get_device"].return_value = None
            self.assertEqual((await self.chart()).json(), {"proposal": None})
            readers["get_chart_offer"].assert_not_awaited()
            for mutation in writes:
                mutation.assert_not_awaited()
