import asyncio
from datetime import date, datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from bot import market_store


NOW = datetime(2026, 10, 4, 8, tzinfo=timezone.utc)
LEASE = UUID("e4f584cf-d1d1-4c7f-934c-23e8aeff1c61")


def compact(statement):
    return " ".join(statement.split())


class MarketStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pool = SimpleNamespace(
            execute=AsyncMock(), fetch=AsyncMock(return_value=[]),
            fetchval=AsyncMock(return_value=True), fetchrow=AsyncMock(return_value=None),
        )

    def schema_pool(self):
        connection = SimpleNamespace(execute=AsyncMock())
        transaction = MagicMock()
        transaction.__aenter__ = AsyncMock()
        transaction.__aexit__ = AsyncMock(return_value=False)
        connection.transaction = MagicMock(return_value=transaction)
        acquisition = MagicMock()
        acquisition.__aenter__ = AsyncMock(return_value=connection)
        acquisition.__aexit__ = AsyncMock(return_value=False)
        pool = SimpleNamespace(acquire=MagicMock(return_value=acquisition))
        return pool, connection, transaction, acquisition

    async def test_schema_creation_is_atomic_and_namespaced(self):
        pool, connection, transaction, _ = self.schema_pool()
        await market_store.initialize_schema(pool)
        transaction.__aenter__.assert_awaited_once()
        statements = [call.args[0] for call in connection.execute.await_args_list]
        self.assertEqual(statements, list(market_store.SCHEMA_STATEMENTS))
        self.assertEqual(sum("PRIMARY KEY (bot_id," in query for query in statements), 3)

    async def test_schema_cancellation_exits_transaction_and_returns_connection(self):
        pool, connection, transaction, acquisition = self.schema_pool()
        connection.execute.side_effect = [None, asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await market_store.initialize_schema(pool)
        self.assertEqual(connection.execute.await_count, 2)
        self.assertIs(transaction.__aexit__.await_args.args[0], asyncio.CancelledError)
        self.assertIs(acquisition.__aexit__.await_args.args[0], asyncio.CancelledError)

    async def test_watch_preserves_due_and_lease_when_already_active(self):
        await market_store.enable_subscription(self.pool, 999, 101, NOW)
        query, bot_id, chat_id, due = self.pool.execute.await_args.args
        query = compact(query)
        self.assertEqual((bot_id, chat_id, due), (999, 101, NOW + timedelta(minutes=15)))
        self.assertIn("ON CONFLICT (bot_id, chat_id)", query)
        self.assertIn("THEN subscription.next_due ELSE EXCLUDED.next_due", query)
        self.assertIn("THEN subscription.lease_id ELSE NULL", query)
        self.assertIn("THEN subscription.leased_until ELSE NULL", query)

    async def test_unwatch_is_guarded_and_returns_real_boolean(self):
        for result in (True, False, None):
            self.pool.fetchval.return_value = result
            changed = await market_store.disable_subscription(self.pool, 999, 101)
            self.assertIs(changed, bool(result))
        query, bot_id, chat_id = self.pool.fetchval.await_args.args
        self.assertEqual((bot_id, chat_id), (999, 101))
        self.assertIn("bot_id = $1 AND chat_id = $2 AND active", compact(query))
        self.assertIn("lease_id = NULL, leased_until = NULL", compact(query))

    async def test_due_claim_is_one_atomic_statement_with_bounded_expiring_leases(self):
        row = {"bot_id": 999, "chat_id": 101, "lease_id": LEASE, "active": True}
        self.pool.fetch.return_value = [row]
        claimed = await market_store.claim_due(self.pool, 999, NOW)
        self.assertEqual(claimed, [row])
        self.assertIsNot(claimed[0], row)
        query, bot_id, now, limit, expires = self.pool.fetch.await_args.args
        self.assertEqual((bot_id, now, limit, expires), (999, NOW, 20, NOW + timedelta(minutes=5)))
        self.assertIn("FOR UPDATE SKIP LOCKED", query)
        self.assertIn("LIMIT $3", query)
        self.assertIn("gen_random_uuid()", query)
        self.assertIn("bot_id = $1 AND active AND next_due <= $2", compact(query))
        self.assertIn("leased_until IS NULL OR leased_until <= $2", compact(query))
        self.pool.fetch.assert_awaited_once()

    async def test_active_subscription_probe_is_scoped_to_this_bot_and_returns_boolean(self):
        for result in (True, False, None):
            self.pool.fetchval.return_value = result
            active = await market_store.has_subscriptions(self.pool, 999)
            self.assertIs(active, bool(result))
        query, bot_id = self.pool.fetchval.await_args.args
        self.assertEqual(bot_id, 999)
        self.assertIn("SELECT EXISTS", query)
        self.assertIn("WHERE bot_id = $1 AND active", compact(query))

    async def test_presend_probe_requires_active_subscription_and_current_unexpired_lease(self):
        for result in (True, False, None):
            self.pool.fetchval.return_value = result
            active = await market_store.delivery_active(self.pool, 999, 101, str(LEASE), NOW)
            self.assertIs(active, bool(result))
        query, bot_id, chat_id, lease_id, now = self.pool.fetchval.await_args.args
        self.assertEqual((bot_id, chat_id, lease_id, now), (999, 101, LEASE, NOW))
        self.assertIn("SELECT EXISTS", query)
        self.assertIn("bot_id = $1 AND chat_id = $2 AND active", compact(query))
        self.assertIn("lease_id = $3 AND leased_until > $4", compact(query))

    async def test_delivery_acknowledgment_checks_lease_and_avoids_catchup_schedule(self):
        for result in (True, None):
            self.pool.fetchval.return_value = result
            changed = await market_store.mark_delivered(self.pool, 999, 101, str(LEASE), NOW)
            self.assertIs(changed, bool(result))
        query, bot_id, chat_id, lease_id, sent_at, due = self.pool.fetchval.await_args.args
        self.assertEqual((bot_id, chat_id, lease_id, sent_at, due), (999, 101, LEASE, NOW, NOW + timedelta(minutes=15)))
        self.assertIn("bot_id = $1 AND chat_id = $2 AND active", compact(query))
        self.assertIn("lease_id = $3 AND leased_until > $4", compact(query))
        self.assertIn("last_sent = $4, next_due = $5", compact(query))

    async def test_failed_delivery_releases_only_current_lease_and_retries_after_five_minutes(self):
        await market_store.release_delivery(self.pool, 999, 101, LEASE, NOW)
        query, bot_id, chat_id, lease_id, now, retry_at = self.pool.execute.await_args.args
        self.assertEqual((bot_id, chat_id, lease_id, now, retry_at), (999, 101, LEASE, NOW, NOW + timedelta(minutes=5)))
        self.assertIn("lease_id = $3 AND leased_until > $4", compact(query))
        self.assertIn("bot_id = $1 AND chat_id = $2 AND active", compact(query))
        self.assertNotIn("last_sent =", query)

    async def test_news_cap_is_atomic_and_uses_utc_calendar_day(self):
        local_now = datetime(2026, 10, 4, 0, 30, tzinfo=timezone(timedelta(hours=3)))
        for result in (True, False, None):
            self.pool.fetchval.return_value = result
            granted = await market_store.claim_news_request(self.pool, 999, local_now)
            self.assertIs(granted, bool(result))
        query, bot_id, day, limit = self.pool.fetchval.await_args.args
        self.assertEqual((bot_id, day, limit), (999, date(2026, 10, 3), 96))
        self.assertIs(type(day), date)
        self.assertIn("ON CONFLICT (bot_id, day)", query)
        self.assertIn("WHERE usage.requests < $3", query)
        self.assertIn("requests = usage.requests + 1", query)

    async def test_zero_limits_do_not_claim_delivery_or_spend_requests(self):
        self.assertEqual(await market_store.claim_due(self.pool, 999, NOW, limit=0), [])
        self.assertFalse(await market_store.claim_news_request(self.pool, 999, NOW, limit=0))
        self.pool.fetch.assert_not_awaited()
        self.pool.fetchval.assert_not_awaited()

    async def test_invalid_limits_naive_dates_and_boolean_ids_fail_before_database_access(self):
        for limit in (-1, True, 101):
            with self.subTest(delivery_limit=limit), self.assertRaises(ValueError):
                await market_store.claim_due(self.pool, 999, NOW, limit=limit)
        for limit in (-1, True):
            with self.subTest(news_limit=limit), self.assertRaises(ValueError):
                await market_store.claim_news_request(self.pool, 999, NOW, limit=limit)
        with self.assertRaises(ValueError):
            await market_store.enable_subscription(self.pool, True, 101, NOW)
        with self.assertRaises(ValueError):
            await market_store.enable_subscription(self.pool, 999, 101, datetime(2026, 10, 4))
        self.pool.execute.assert_not_awaited()
        self.pool.fetch.assert_not_awaited()
        self.pool.fetchval.assert_not_awaited()

    async def test_cache_decodes_json_bool_and_binds_key_instead_of_interpolating_it(self):
        key = "news'); DROP TABLE users;--"
        payload = {"fresh": True, "headline": "خبر تعليمي"}
        self.pool.fetchrow.return_value = {"payload": json.dumps(payload), "updated_at": NOW}
        result = await market_store.get_cache(self.pool, 999, key)
        self.assertEqual(result, {"payload": payload, "updated_at": NOW})
        self.assertIs(result["payload"]["fresh"], True)
        query, bot_id, actual_key = self.pool.fetchrow.await_args.args
        self.assertEqual((bot_id, actual_key), (999, key))
        self.assertNotIn(key, query)

    async def test_cache_miss_or_corrupt_payload_is_a_miss(self):
        for row in (None, {"payload": "invalid-json", "updated_at": NOW}, {"payload": "[]", "updated_at": NOW}):
            self.pool.fetchrow.return_value = row
            self.assertIsNone(await market_store.get_cache(self.pool, 999, "news"))

    async def test_cache_save_preserves_json_types_and_supplied_timestamp(self):
        payload = {"fresh": False, "items": ["خبر تعليمي"]}
        await market_store.save_cache(self.pool, 999, "news", payload, NOW)
        query, bot_id, key, encoded, timestamp = self.pool.execute.await_args.args
        self.assertEqual((bot_id, key, timestamp), (999, "news", NOW))
        self.assertEqual(json.loads(encoded), payload)
        self.assertIn("$3::jsonb", query)
        self.assertIn("updated_at = EXCLUDED.updated_at", query)
        self.assertIn("WHERE market_cache.updated_at <= EXCLUDED.updated_at", query)

    async def test_feed_cache_acceptance_is_atomic_and_monotonic_in_both_timestamps(self):
        source_time = NOW - timedelta(minutes=1)
        payload = {"as_of": source_time.isoformat(), "received_at": NOW.isoformat(), "fresh": True}
        for result in (True, False, None):
            self.pool.fetchval.return_value = result
            accepted = await market_store.save_feed_cache(
                self.pool, 999, "quote:gold", payload, NOW, source_time
            )
            self.assertIs(accepted, bool(result))
        query, bot_id, key, encoded, updated_at, actual_source_time = self.pool.fetchval.await_args.args
        self.assertEqual((bot_id, key, updated_at, actual_source_time), (999, "quote:gold", NOW, source_time))
        self.assertEqual(json.loads(encoded), payload)
        query = compact(query)
        self.assertIn("ON CONFLICT (bot_id, key)", query)
        self.assertIn("cache.source_time IS NULL OR cache.source_time <= EXCLUDED.source_time", query)
        self.assertIn("AND cache.updated_at <= EXCLUDED.updated_at", query)
        self.assertIn("source_time = EXCLUDED.source_time", query)
        self.assertIn("RETURNING TRUE", query)

    async def test_equal_provider_stamp_can_refresh_receipt_without_rewriting_payload_as_of(self):
        source_time = NOW - timedelta(minutes=1)
        received_at = NOW + timedelta(seconds=30)
        payload = {"as_of": source_time.isoformat(), "received_at": received_at.isoformat()}
        await market_store.save_feed_cache(self.pool, 999, "quote:gold", payload, received_at, source_time)
        query, _, _, encoded, actual_received_at, actual_source_time = self.pool.fetchval.await_args.args
        self.assertIn("cache.source_time <= EXCLUDED.source_time", query)
        self.assertEqual(actual_received_at, received_at)
        self.assertEqual(actual_source_time, source_time)
        self.assertEqual(json.loads(encoded)["as_of"], source_time.isoformat())

    async def test_feed_cache_requires_aware_provider_timestamp_before_database_access(self):
        with self.assertRaises(ValueError):
            await market_store.save_feed_cache(
                self.pool, 999, "quote:gold", {}, NOW, datetime(2026, 10, 4)
            )
        self.pool.fetchval.assert_not_awaited()

    async def test_cancellation_of_atomic_claims_is_not_swallowed(self):
        self.pool.fetch.side_effect = asyncio.CancelledError()
        self.pool.fetchval.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await market_store.claim_due(self.pool, 999, NOW)
        with self.assertRaises(asyncio.CancelledError):
            await market_store.claim_news_request(self.pool, 999, NOW)
        self.pool.execute.assert_not_awaited()


class MarketMigrationTests(unittest.TestCase):
    def test_migration_revision_and_schema_match_runtime_initializer(self):
        path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "20261004_02_market_monitor.py"
        spec = importlib.util.spec_from_file_location("market_monitor_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        self.assertEqual(migration.revision, "20261004_02")
        self.assertEqual(migration.down_revision, "20260629_01")
        with patch.object(migration.op, "execute") as execute:
            migration.upgrade()
            statements = [compact(call.args[0]) for call in execute.call_args_list]
        self.assertEqual(statements, [compact(statement) for statement in market_store.SCHEMA_STATEMENTS])
        with patch.object(migration.op, "execute") as execute:
            migration.downgrade()
            drops = [call.args[0] for call in execute.call_args_list]
        self.assertEqual(drops, [
            "DROP TABLE IF EXISTS market_cache",
            "DROP TABLE IF EXISTS market_daily_usage",
            "DROP TABLE IF EXISTS market_subscriptions",
        ])


if __name__ == "__main__":
    unittest.main()
