"""Exit reports use synthetic claims, clocks and transport; never broker calls."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import FastAPI
import httpx
from telegram.error import RetryAfter

from bot import mt5_api, mt5_exit_events as events, trade_timing


NOW = datetime(2026, 10, 14, 13, 56, tzinfo=timezone.utc)
DEVICE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OFFER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
CLAIM = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
EVENT = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")


def payload():
    return {"workflow": "demo_execution", "account_mode": "demo", "volume": .01,
            "symbol": "XAUUSD", "direction": "SELL", "bar_time": (NOW - timedelta(minutes=1)).isoformat(),
            "trade_timing": trade_timing.build_trade_timing({
                "source": trade_timing.SESSION_SOURCE, "verified": True, "as_of": NOW.isoformat(),
                "session_open": "2026-10-14T00:00:00+00:00", "session_close": "2026-10-14T21:00:00+00:00",
            }, NOW, NOW, 30)}


def event(status="armed", **changes):
    value = {"event_id": str(EVENT), "status": status, "deadline": (NOW + timedelta(hours=1, seconds=5)).isoformat(),
             "observed_at": (NOW + timedelta(seconds=6)).isoformat(), "reason": "holding_limit",
             "code": 0, "deal": 123456, "order": 234567}
    value.update(changes)
    return value


def device():
    return {"device_id": DEVICE, "owner_chat_id": 11, "owner_user_id": 11,
            "account_mode": "demo", "volume": .01, "symbol": "XAUUSD"}


def offer():
    return {"bot_id": 991, "id": OFFER, "device_id": DEVICE, "claim_id": CLAIM,
            "status": "filled", "executing_at": NOW, "chat_id": 11, "user_id": 11, "payload": payload()}


def report(**changes):
    value = {"event_id": EVENT, "bot_id": 991, "offer_id": OFFER, "device_id": DEVICE,
             "claim_id": CLAIM, "chat_id": 11, "user_id": 11, "payload": payload(), "event": event()}
    value.update(changes)
    return value


class ExitEventValidationTests(unittest.TestCase):
    def test_deadlines_bound_actual_broker_fill_and_allow_delayed_outbox_delivery(self):
        self.assertEqual(events.validate_event(event(), payload(), NOW + timedelta(days=2)), event())
        floored = event(deadline=(NOW + timedelta(seconds=3599, milliseconds=500)).isoformat())
        self.assertEqual(events.validate_event(floored, payload(), NOW + timedelta(seconds=6)), floored)
        for change in ({"deadline": (NOW + timedelta(hours=2)).isoformat()},
                       {"deadline": (NOW + timedelta(minutes=59)).isoformat()},
                       {"observed_at": (NOW - timedelta(seconds=1)).isoformat()},
                       {"observed_at": (NOW + timedelta(minutes=1)).isoformat()},
                       {"code": True}, {"deal": -1}, {"order": 2**63}, {"extra": "secret"},
                       {"deadline": "2026-10-14T14:56:05"}, {"reason": "arbitrary broker text"}):
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                events.validate_event(event(**change), payload(), NOW + timedelta(seconds=6))
        invalid = payload(); invalid["trade_timing"]["max_hold_seconds"] = 7200
        with self.assertRaises(ValueError):
            events.validate_event(event(), invalid, NOW + timedelta(seconds=6))
        with self.assertRaises(ValueError):
            events.validate_event(event("closed"), payload(), NOW + timedelta(seconds=6))
        closed = event("closed", observed_at=(NOW + timedelta(hours=1, seconds=6)).isoformat())
        self.assertEqual(events.validate_event(closed, payload(), NOW + timedelta(hours=1, seconds=6)), closed)
        self.assertEqual(events.validate_event(event("closed", reason="position_closed"), payload(), NOW + timedelta(seconds=6))["status"], "closed")

    def test_messages_show_local_times_and_guard_status_without_broker_ids(self):
        for status in ("armed", "closed", "failed", "unknown", "missing"):
            current = report(event=event(status))
            text = events.event_text(current, NOW + timedelta(seconds=7))
            for marker in ("Demo", "XAUUSD", "2026-10-14 17:56:05", "2026-10-14 16:56:06", "UTC+03:00"):
                self.assertIn(marker, text)
            for identifier in ("123456", "234567", str(EVENT), str(CLAIM)):
                self.assertNotIn(identifier, text)
            if status == "armed":
                self.assertIn("تفعيل حماية", text)
            elif status == "closed":
                self.assertIn("أكد جسر Demo أن الصفقة صارت مسكّرة", text)
            else:
                self.assertIn("لم تؤكد إغلاق", text)
        text = events.event_text(report(), NOW + timedelta(hours=2))
        self.assertIn("الموعد المذكور صار بالماضي", text)
        early = events.event_text(report(event=event("closed", reason="position_closed")), NOW + timedelta(seconds=7))
        self.assertIn("صارت مسكّرة", early)
        self.assertIn("وقته الفعلي موجودين بسجل MT5", early)
        late = events.event_text(report(event=event("closed")), NOW + timedelta(hours=2))
        self.assertNotIn("عند حدّ الوقت", late)


class ExitEventAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = SimpleNamespace(pool=object(), bot_id=991)
        self.device, self.offer = device(), offer()
        self.app = FastAPI()
        async def read_request(request, workflow):
            self.assertEqual(workflow, "demo_execution")
            return self.service, await request.json(), None
        events.install_routes(self.app, read_request, lambda: NOW + timedelta(seconds=7))
        self.enterContext(patch.dict(events.os.environ, {"MARKET_GOLD_SYMBOL": "XAUUSD"}))
        self.device_read = self.enterContext(patch.object(events.trade_store, "get_device", AsyncMock(side_effect=lambda *a: deepcopy(self.device))))
        self.offer_read = self.enterContext(patch.object(events.trade_store, "get_offer", AsyncMock(side_effect=lambda *a: deepcopy(self.offer))))
        self.record = self.enterContext(patch.object(events, "record_event", AsyncMock(return_value=True)))
        self.body = {"device_id": str(DEVICE), "offer_id": str(OFFER), "claim_id": str(CLAIM), "event": event()}

    async def post(self, body=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://local.test") as client:
            return await client.post("/api/mt5/demo/exit-result", json=self.body if body is None else body)

    async def test_bound_report_accepts_after_entry_expiry_without_fresh_feed(self):
        self.assertEqual((await self.post()).json(), {"ok": True})
        self.record.assert_awaited_once()
        self.assertEqual(self.record.await_args.args[1:5], (991, DEVICE, OFFER, CLAIM))
        self.offer["status"] = "unknown"
        self.assertEqual((await self.post()).status_code, 200)

    async def test_owner_workflow_account_and_claim_mismatches_never_store(self):
        initial_device, initial_offer = deepcopy(self.device), deepcopy(self.offer)
        for mutation in (lambda: self.device.update(owner_chat_id=22), lambda: self.offer.update(user_id=22),
                         lambda: self.offer.update(claim_id=EVENT), lambda: self.offer.update(bot_id=992),
                         lambda: self.offer.update(status="executing"), lambda: self.offer.update(executing_at=None),
                         lambda: self.offer["payload"].update(workflow="manual_ticket"),
                         lambda: self.device.update(account_mode="real"), lambda: self.device.update(volume=.02),
                         lambda: self.offer["payload"].update(symbol="OTHER")):
            self.device, self.offer = deepcopy(initial_device), deepcopy(initial_offer)
            mutation()
            self.assertEqual((await self.post()).status_code, 409)
        self.record.assert_not_awaited()

    async def test_duplicate_content_conflict_is_rejected_and_bad_event_never_writes(self):
        self.record.return_value = False
        self.assertEqual((await self.post()).status_code, 409)
        self.record.reset_mock()
        for change in ({"event": {**event(), "code": True}}, {"unexpected": "private"},
                       {"claim_id": "not-a-uuid"}, {"event": {**event(), "deadline": "bad"}}):
            body = {**self.body, **change}
            self.assertEqual((await self.post(body)).status_code, 422)
        self.record.assert_not_awaited()


class ExitEventPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_insert_rechecks_owner_claim_and_immutable_duplicate_contents(self):
        pool = SimpleNamespace(fetchrow=AsyncMock(return_value={"event_id": EVENT}))
        self.assertTrue(await events.record_event(pool, 991, DEVICE, OFFER, CLAIM, event(), NOW))
        query, *args = pool.fetchrow.await_args.args
        normalized = " ".join(query.split())
        for clause in ("offer.claim_id = $4", "offer.status IN ('filled', 'unknown')", "device.owner_chat_id = offer.chat_id",
                       "device.owner_user_id = offer.user_id", "offer.payload->>'workflow' = 'demo_execution'",
                       "saved.event = EXCLUDED.event", "saved.claim_id = EXCLUDED.claim_id"):
            self.assertIn(clause, normalized)
        self.assertNotIn("notified =", normalized)
        self.assertEqual(args[:5], [991, DEVICE, OFFER, CLAIM, EVENT])
        self.assertEqual(json.loads(args[5]), event())
        pool.fetchrow.return_value = None
        self.assertFalse(await events.record_event(pool, 991, DEVICE, OFFER, CLAIM, event(), NOW))

    async def test_owner_bound_notifications_do_not_require_subscription(self):
        pool = SimpleNamespace(fetch=AsyncMock(return_value=[report()]))
        self.assertEqual(await events.list_notifications(pool, 991), [report()])
        query = " ".join(pool.fetch.await_args.args[0].split())
        self.assertIn("device.owner_chat_id = report.chat_id", query)
        self.assertIn("device.owner_user_id = report.user_id", query)
        self.assertNotIn("subscription", query)


class ExitEventAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = SimpleNamespace(pool=object(), bot_id=991, trading_enabled=False,
                                       manual_tickets_enabled=True, source="mt5")
        self.app = FastAPI()
        self.key = "synthetic-demo-exit-authentication-key-00000"
        self.enterContext(patch.dict(events.os.environ, {
            "MARKET_SOURCE": "mt5", "MARKET_GOLD_SYMBOL": "XAUUSD", "MT5_MANUAL_TICKETS_ENABLED": "true",
            "MT5_DEMO_ORDERS_ENABLED": "true", "MT5_MANUAL_BRIDGE_KEY": self.key,
        }, clear=True))
        self.enterContext(patch.object(mt5_api, "now_utc", return_value=NOW + timedelta(seconds=7)))
        mt5_api.install_routes(self.app, SimpleNamespace(bot_data={"market_service": self.service}),
                              SimpleNamespace(market_bridge_key="separate-disabled-legacy-key-00000000"))
        self.enterContext(patch.object(events.trade_store, "get_device", AsyncMock(return_value=device())))
        self.enterContext(patch.object(events.trade_store, "get_offer", AsyncMock(return_value=offer())))
        self.record = self.enterContext(patch.object(events, "record_event", AsyncMock(return_value=True)))

    async def post(self, key=None, raw=None):
        body = {"device_id": str(DEVICE), "offer_id": str(OFFER), "claim_id": str(CLAIM), "event": event()}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://local.test") as client:
            return await client.post("/api/mt5/demo/exit-result", content=json.dumps(body) if raw is None else raw,
                                     headers={"Authorization": "Bearer " + (self.key if key is None else key)})

    async def test_dedicated_demo_auth_and_operator_workflow_gate_protect_exit_reports(self):
        self.assertEqual((await self.post("wrong-key")).status_code, 401)
        self.record.assert_not_awaited()
        for name, value in (("MT5_DEMO_ORDERS_ENABLED", "false"), ("MT5_MANUAL_TICKETS_ENABLED", "false"),
                            ("MARKET_SOURCE", "reference")):
            with patch.dict(events.os.environ, {name: value}):
                self.assertEqual((await self.post()).status_code, 404)
        self.record.assert_not_awaited()
        self.assertEqual((await self.post()).json(), {"ok": True})

    async def test_existing_parser_rejects_duplicate_nonfinite_and_oversized_event_bodies(self):
        for raw, status in ((b'{"device_id":"a","device_id":"b"}', 422),
                            (b'{"event":NaN}', 422), (b"x" * 8193, 413)):
            self.assertEqual((await self.post(raw=raw)).status_code, status)
        self.record.assert_not_awaited()


class ExitEventDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_ack_only_success_and_recheck_owner_after_storage_read(self):
        service = SimpleNamespace(pool=object(), bot_id=991, telegram_backoff=AsyncMock())
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(events, "list_notifications", AsyncMock(return_value=[report()])), \
                patch.object(events.trade_store, "get_device", AsyncMock(return_value=device())) as get_device, \
                patch.object(events, "mark_notified", AsyncMock()) as ack:
            self.assertTrue(await events.send_notifications(service, bot, lambda: NOW))
            ack.assert_awaited_once()
            ack.reset_mock(); bot.send_message.reset_mock()
            get_device.return_value = {**device(), "owner_user_id": 22}
            self.assertTrue(await events.send_notifications(service, bot, lambda: NOW))
            bot.send_message.assert_not_awaited(); ack.assert_not_awaited()
            get_device.return_value = device()
            bot.send_message.side_effect = RuntimeError("private endpoint")
            self.assertTrue(await events.send_notifications(service, bot, lambda: NOW))
            ack.assert_not_awaited()
            bot.send_message.side_effect = RetryAfter(1)
            self.assertFalse(await events.send_notifications(service, bot, lambda: NOW))
            service.telegram_backoff.assert_awaited_once(); ack.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
