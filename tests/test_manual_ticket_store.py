"""Persistence safety contracts for manual preparation; no MT5/order calls."""

import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from bot import manual_ticket_store as store

NOW = datetime(2026, 10, 5, 16, tzinfo=timezone.utc)
DEVICE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OFFER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
CLAIM = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
BOT, OWNER = 9901, 4401


def compact(value):
    return " ".join(value.split())


def row(**changes):
    value = dict(id=OFFER, bot_id=BOT, device_id=DEVICE, chat_id=OWNER, user_id=OWNER,
                 signal_id="signal-1", payload={"direction": "BUY", "entry": 2000, "stop": 1990, "target": 2020},
                 status="draft", created_at=NOW, expires_at=NOW + timedelta(minutes=5),
                 updated_at=NOW, published_at=None, message_id=None, decided_at=None,
                 preparing_at=None, completed_at=None, claim_id=None, result=None, notified=False,
                 symbol="XAUUSD", account_mode="demo", volume=0.01)
    value.update(changes)
    return value


class ManualTicketStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pool = SimpleNamespace(fetchrow=AsyncMock(return_value=row()), fetch=AsyncMock(return_value=[]),
                                    fetchval=AsyncMock(return_value=True))

    async def test_schema_is_separate_and_serializes_preparation_per_device(self):
        schema = " ".join(compact(statement) for statement in store.SCHEMA_STATEMENTS)
        self.assertIn("CREATE TABLE IF NOT EXISTS mt5_manual_ticket_offers", schema)
        self.assertNotIn("mt5_trade_offers", schema)
        self.assertNotIn("'accepted'", schema)
        self.assertNotIn("'filled'", schema)
        self.assertIn("FOREIGN KEY (bot_id, device_id) REFERENCES mt5_devices", schema)
        self.assertIn("UNIQUE (bot_id, device_id, chat_id, signal_id)", schema)
        self.assertIn("UNIQUE (bot_id, chat_id, message_id)", schema)
        self.assertIn("(claim_id IS NULL) = (preparing_at IS NULL)", schema)
        self.assertIn("expires_at <= created_at + INTERVAL '5 minutes'", schema)
        self.assertIn("CREATE UNIQUE INDEX", schema)
        self.assertIn("(bot_id, device_id) WHERE status = 'preparing'", schema)

    async def test_schema_creation_is_atomic_and_cancellation_releases_connection(self):
        connection = SimpleNamespace(execute=AsyncMock(side_effect=[None, asyncio.CancelledError()]))
        transaction = MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False))
        connection.transaction = MagicMock(return_value=transaction)
        acquisition = MagicMock(__aenter__=AsyncMock(return_value=connection), __aexit__=AsyncMock(return_value=False))
        pool = SimpleNamespace(acquire=MagicMock(return_value=acquisition))
        with self.assertRaises(asyncio.CancelledError):
            await store.initialize_schema(pool)
        self.assertIs(transaction.__aexit__.await_args.args[0], asyncio.CancelledError)
        self.assertIs(acquisition.__aexit__.await_args.args[0], asyncio.CancelledError)

    async def test_duplicate_signal_keeps_original_payload_and_expiry(self):
        original = row()
        self.pool.fetchrow.return_value = original
        changed = {"direction": "SELL", "entry": 2020, "stop": 2030, "target": 2000}
        result = await store.create_offer(self.pool, BOT, DEVICE, OWNER, OWNER, "signal-1", changed, NOW,
                                          NOW + timedelta(minutes=1))
        self.assertEqual(result["payload"], original["payload"])
        self.assertEqual(result["expires_at"], original["expires_at"])
        query = compact(self.pool.fetchrow.await_args.args[0])
        self.assertIn("ON CONFLICT (bot_id, device_id, chat_id, signal_id) DO UPDATE SET id = offer.id", query)
        self.assertIn("device.owner_chat_id = $4 AND device.owner_user_id = $5 AND subscription.active", query)
        self.assertNotIn("SET payload", query)

    async def test_offer_ttl_and_private_owner_are_enforced_before_storage(self):
        for expiry in (NOW, NOW - timedelta(seconds=1), NOW + timedelta(minutes=5, microseconds=1)):
            with self.subTest(expiry=expiry), self.assertRaises(ValueError):
                await store.create_offer(self.pool, BOT, DEVICE, OWNER, OWNER, "signal-1", {}, NOW, expiry)
        with self.assertRaises(ValueError):
            await store.create_offer(self.pool, BOT, DEVICE, -1, OWNER, "signal-1", {}, NOW, NOW + timedelta(minutes=1))
        self.pool.fetchrow.assert_not_awaited()
        await store.create_offer(self.pool, BOT, DEVICE, OWNER, OWNER, "signal-1", {}, NOW, NOW + timedelta(minutes=5))
        self.pool.fetchrow.assert_awaited_once()

    async def test_missing_owner_or_subscription_cannot_create_offer(self):
        self.pool.fetchrow.return_value = None
        with self.assertRaises(ValueError):
            await store.create_offer(self.pool, BOT, DEVICE, OWNER, OWNER, "signal-1", {}, NOW, NOW + timedelta(minutes=1))

    async def test_publication_and_decision_bind_exact_message_and_unexpired_owner(self):
        await store.publish_offer(self.pool, BOT, OFFER, 37, NOW)
        publish = compact(self.pool.fetchval.await_args.args[0])
        self.assertIn("offer.status = 'draft'", publish)
        self.assertIn("offer.expires_at > $4", publish)
        self.assertIn("owner_user_id = offer.user_id", publish)
        for decision in ("requested", "rejected"):
            await store.decide(self.pool, BOT, OFFER, OWNER, OWNER, 37, decision, NOW)
            query, *args = self.pool.fetchrow.await_args.args
            self.assertEqual(args[2:6], [OWNER, OWNER, 37, decision])
            query = compact(query)
            self.assertIn("offer.message_id = $5 AND offer.status = 'offered'", query)
            self.assertIn("offer.expires_at > $7 AND offer.published_at <= $7", query)
            self.assertIn("subscription.active", query)
        self.pool.fetchrow.reset_mock()
        for decision in ("accepted", "filled", "prepared", True):
            with self.assertRaises(ValueError):
                await store.decide(self.pool, BOT, OFFER, OWNER, OWNER, 37, decision, NOW)
        self.pool.fetchrow.assert_not_awaited()

    async def test_claim_requires_requested_owner_fresh_device_and_single_active_preparation(self):
        self.pool.fetchrow.return_value = row(status="preparing", claim_id=CLAIM, preparing_at=NOW)
        result = await store.claim_offer(self.pool, BOT, DEVICE, NOW)
        self.assertEqual(result["status"], "preparing")
        self.assertEqual(result["claim_id"], CLAIM)
        query, *args = self.pool.fetchrow.await_args.args
        query = compact(query)
        self.assertEqual(args[:4], [BOT, DEVICE, NOW, NOW - timedelta(seconds=180)])
        self.assertIn("offer.status = 'requested'", query)
        self.assertIn("offer.expires_at > $3 AND offer.decided_at <= $3", query)
        self.assertIn("device.last_seen_at >= $4 AND device.last_seen_at <= $3 AND subscription.active", query)
        self.assertIn("offer.chat_id = offer.user_id", query)
        self.assertIn("NOT EXISTS", query)
        self.assertIn("active.status = 'preparing'", query)
        self.assertIn("FOR UPDATE OF offer, device SKIP LOCKED", query)
        self.assertNotIn("status = 'unknown'", query)
        self.assertNotIn("status = 'prepared'", query)

    async def test_empty_queue_does_not_manufacture_request(self):
        self.pool.fetchrow.return_value = None
        self.assertIsNone(await store.claim_offer(self.pool, BOT, DEVICE, NOW))

    async def test_only_prepared_or_failed_results_can_be_written(self):
        for result in ({"status": "filled"}, {"status": "unknown"}, {"status": []},
                       {"status": "prepared", "order_ticket": 12}, {"status": "prepared", "code": 10009},
                       {"status": "prepared", "executed_at": NOW.isoformat()}, {}, None):
            with self.subTest(result=result), self.assertRaises(ValueError):
                await store.complete_offer(self.pool, BOT, DEVICE, OFFER, CLAIM, result, NOW)
        self.pool.fetchrow.assert_not_awaited()
        for status in ("prepared", "failed"):
            self.pool.fetchrow.return_value = row(status=status, result={"status": status})
            result = await store.complete_offer(self.pool, BOT, DEVICE, OFFER, CLAIM, {"status": status}, NOW)
            self.assertEqual(result["status"], status)
            args = self.pool.fetchrow.await_args.args[1:]
            self.assertEqual(args[:5], (BOT, DEVICE, OFFER, CLAIM, status))
            self.assertEqual(json.loads(args[5]), {"status": status})

    async def test_completion_cannot_replace_terminal_claim_and_prepared_requires_validity(self):
        await store.complete_offer(self.pool, BOT, DEVICE, OFFER, CLAIM, {"status": "prepared"}, NOW)
        query = compact(self.pool.fetchrow.await_args.args[0])
        self.assertIn("offer.id = $3 AND offer.claim_id = $4", query)
        self.assertIn("offer.status = 'preparing' AND offer.preparing_at <= $7", query)
        self.assertIn("offer.expires_at > $7 AND offer.preparing_at >= $8", query)
        self.assertEqual(self.pool.fetchrow.await_args.args[-1], NOW - store.PREPARATION_TIMEOUT)
        self.assertIn("status = $5 AND status IN ('prepared', 'failed') AND result = $6::jsonb", query)
        self.assertIn("device.owner_chat_id = resolved.chat_id AND device.owner_user_id = resolved.user_id", query)
        self.pool.fetchrow.return_value = None
        self.assertIsNone(await store.complete_offer(self.pool, BOT, DEVICE, OFFER, CLAIM, {"status": "prepared"}, NOW))

    async def test_timeout_or_expiry_is_unknown_and_never_requeued(self):
        await store.expire_offers(self.pool, BOT, NOW)
        query, bot, now, timeout, encoded = self.pool.fetchval.await_args.args
        self.assertEqual((bot, now, timeout), (BOT, NOW, NOW - store.PREPARATION_TIMEOUT))
        self.assertEqual(json.loads(encoded), {"status": "unknown", "reason": "preparation_interrupted"})
        query = compact(query)
        self.assertIn("status IN ('draft', 'offered', 'requested') AND expires_at <= $2", query)
        self.assertIn("status = 'preparing' AND (preparing_at < $3 OR expires_at <= $2)", query)
        self.assertNotIn("SET status = 'requested'", query)

    async def test_unwatch_cancels_pending_requests_and_results_remain_owner_bound(self):
        await store.cancel_offers(self.pool, BOT, OWNER, NOW)
        cancellation = compact(self.pool.fetchval.await_args.args[0])
        self.assertIn("status IN ('draft', 'offered', 'requested')", cancellation)
        self.assertNotIn("'preparing'", cancellation)
        self.pool.fetch.return_value = [row(status="prepared", result={"status": "prepared"})]
        self.assertEqual((await store.list_notifications(self.pool, BOT))[0]["status"], "prepared")
        notifications = compact(self.pool.fetch.await_args.args[0])
        self.assertIn("owner_chat_id = offer.chat_id AND device.owner_user_id = offer.user_id", notifications)
        self.assertIn("offer.decided_at IS NOT NULL AND offer.preparing_at IS NULL", notifications)
        self.assertNotIn("subscription.active", notifications)
        await store.mark_notified(self.pool, BOT, OFFER, NOW)
        self.assertIn("NOT notified", compact(self.pool.fetchval.await_args.args[0]))

    async def test_zero_notification_limit_and_invalid_identifiers_do_not_touch_storage(self):
        self.assertEqual(await store.list_notifications(self.pool, BOT, 0), [])
        self.pool.fetch.assert_not_awaited()
        for device in ("not-a-device", None, True):
            with self.assertRaises(ValueError):
                await store.claim_offer(self.pool, BOT, device, NOW)
        self.pool.fetchrow.assert_not_awaited()


class ManualTicketMigrationTests(unittest.TestCase):
    def test_fresh_and_existing_schema_use_the_same_additive_statements(self):
        path = Path(__file__).resolve().parents[1] / "migrations/versions/20261005_05_manual_mt5_tickets.py"
        spec = importlib.util.spec_from_file_location("manual_ticket_migration_test", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with patch.object(migration.op, "execute") as execute:
            migration.upgrade()
            self.assertEqual([compact(call.args[0]) for call in execute.call_args_list],
                             [compact(statement) for statement in store.SCHEMA_STATEMENTS])
            self.assertTrue(all("IF NOT EXISTS" in call.args[0] for call in execute.call_args_list))
            self.assertTrue(all("mt5_trade_offers" not in call.args[0] for call in execute.call_args_list))
            execute.reset_mock()
            migration.downgrade()
            execute.assert_called_once_with("DROP TABLE IF EXISTS mt5_manual_ticket_offers")
        self.assertEqual(migration.down_revision, "20261004_04")


if __name__ == "__main__":
    unittest.main()
