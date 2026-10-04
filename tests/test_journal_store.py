"""Persistence contract checks without a database, network, or credentials."""

import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bot import journal_store


NOW = datetime(2026, 10, 4, 8, tzinfo=timezone.utc)
BOT_ID, CHAT_ID = 9901, -4401
SIGNAL_ID = "mt5:XAUUSD:ema9-21:2026-10-04T07:45:00Z:BUY"


def compact(statement):
    return " ".join(statement.split())


def paper_trade(**changes):
    payload = {
        "id": SIGNAL_ID, "source_identity": "mt5:XAUUSD", "strategy_id": "ema9-21-atr14-v1",
        "signal_bar_time": "2026-10-04T07:45:00Z", "direction": "BUY",
        "entry": 2700.0, "stop": 2697.0, "target": 2706.0,
        "opened_at": NOW.isoformat(), "deadline": (NOW + timedelta(minutes=15)).isoformat(),
        "duration_minutes": 15, "status": "open", "last_observation_at": NOW.isoformat(),
        "last_observation_price": 2700.0, "coverage_complete": True,
        "max_gap_seconds": 0.0, "closed_at": None, "exit_price": None,
        "gross_r": None, "review_notes": ["اختبار ورقي"],
    }
    payload.update(changes)
    return payload


class JournalStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pool = SimpleNamespace(
            execute=AsyncMock(), fetch=AsyncMock(return_value=[]),
            fetchval=AsyncMock(return_value=True),
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
        return SimpleNamespace(acquire=MagicMock(return_value=acquisition)), connection, transaction, acquisition

    async def test_initializer_is_atomic_and_has_durable_scope_constraints(self):
        pool, connection, transaction, _ = self.schema_pool()
        await journal_store.initialize_schema(pool)
        transaction.__aenter__.assert_awaited_once()
        self.assertEqual([call.args[0] for call in connection.execute.await_args_list], list(journal_store.SCHEMA_STATEMENTS))
        schema = compact(journal_store.SCHEMA_STATEMENTS[0])
        self.assertIn("PRIMARY KEY (bot_id, chat_id, signal_id)", schema)
        self.assertIn("reviewed_sent BOOLEAN NOT NULL DEFAULT FALSE", schema)
        self.assertIn("review_due >= opened_at", schema)

    async def test_initializer_cancellation_releases_transaction_and_connection(self):
        pool, connection, transaction, acquisition = self.schema_pool()
        connection.execute.side_effect = [None, asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await journal_store.initialize_schema(pool)
        self.assertIs(transaction.__aexit__.await_args.args[0], asyncio.CancelledError)
        self.assertIs(acquisition.__aexit__.await_args.args[0], asyncio.CancelledError)

    async def test_opening_is_scoped_insert_only_with_json_and_fifteen_minute_deadline(self):
        trade = paper_trade()
        self.assertTrue(await journal_store.open_trade(self.pool, BOT_ID, CHAT_ID, trade, NOW))
        query, bot_id, chat_id, signal_id, encoded, status, opened_at, deadline, updated_at = self.pool.fetchval.await_args.args
        self.assertEqual((bot_id, chat_id, signal_id, status), (BOT_ID, CHAT_ID, SIGNAL_ID, "open"))
        self.assertEqual((opened_at, deadline, updated_at), (NOW, NOW + timedelta(minutes=15), NOW))
        self.assertEqual(json.loads(encoded), trade)
        self.assertIn("ON CONFLICT (bot_id, chat_id, signal_id) DO NOTHING", compact(query))
        self.assertNotIn("DO UPDATE", query)

    async def test_duplicate_opening_and_missing_record_updates_return_actual_boolean(self):
        for result in (True, False, None):
            self.pool.fetchval.return_value = result
            self.assertIs(await journal_store.open_trade(self.pool, BOT_ID, CHAT_ID, paper_trade(), NOW), bool(result))
            self.assertIs(await journal_store.update_trade(self.pool, BOT_ID, CHAT_ID, SIGNAL_ID, paper_trade(), NOW), bool(result))

    async def test_iso_offsets_are_converted_to_utc_for_database_columns(self):
        local = NOW.astimezone(timezone(timedelta(hours=3)))
        trade = paper_trade(opened_at=local.isoformat(), deadline=(local + timedelta(minutes=15)).isoformat())
        await journal_store.open_trade(self.pool, BOT_ID, CHAT_ID, trade, local)
        args = self.pool.fetchval.await_args.args
        self.assertEqual(args[-3:], (NOW, NOW + timedelta(minutes=15), NOW))
        self.assertEqual(json.loads(args[4])["opened_at"], local.isoformat())

    async def test_list_open_returns_detached_payload_and_compare_and_set_version(self):
        payload = paper_trade()
        self.pool.fetch.return_value = [{"chat_id": CHAT_ID, "signal_id": SIGNAL_ID, "payload": payload, "updated_at": NOW}]
        rows = await journal_store.list_open(self.pool, BOT_ID)
        self.assertEqual(rows, [{"chat_id": CHAT_ID, "signal_id": SIGNAL_ID, "payload": payload, "updated_at": NOW}])
        rows[0]["payload"]["review_notes"].append("changed")
        self.assertEqual(payload["review_notes"], ["اختبار ورقي"])
        query, bot_id, limit = self.pool.fetch.await_args.args
        self.assertEqual((bot_id, limit), (BOT_ID, 100))
        self.assertIn("WHERE bot_id = $1 AND status = 'open'", compact(query))
        self.assertIn("LIMIT $2", query)

    async def test_json_text_is_decoded_and_corrupt_records_are_skipped(self):
        payload = paper_trade()
        self.pool.fetch.return_value = [
            {"chat_id": CHAT_ID, "signal_id": SIGNAL_ID, "payload": value, "updated_at": NOW}
            for value in (json.dumps(payload), "broken-json", "[]", '{"entry":NaN}', None)
        ]
        rows = await journal_store.list_open(self.pool, BOT_ID)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"], payload)

    async def test_update_protects_open_status_immutable_fields_version_and_time_order(self):
        now = NOW + timedelta(minutes=3)
        closed = paper_trade(status="target_observed", closed_at=now.isoformat(), exit_price=2706.0, gross_r=2.0)
        await journal_store.update_trade(
            self.pool, BOT_ID, CHAT_ID, SIGNAL_ID, closed, now, expected_updated_at=NOW,
        )
        query, bot_id, chat_id, signal_id, encoded, status, opened_at, deadline, updated_at, expected = self.pool.fetchval.await_args.args
        self.assertEqual((bot_id, chat_id, signal_id, status), (BOT_ID, CHAT_ID, SIGNAL_ID, "target_observed"))
        self.assertEqual((opened_at, deadline, updated_at, expected), (NOW, NOW + timedelta(minutes=15), now, NOW))
        self.assertEqual(json.loads(encoded), closed)
        query = compact(query)
        self.assertIn("WHERE bot_id = $1 AND chat_id = $2 AND signal_id = $3 AND status = 'open'", query)
        self.assertIn("opened_at = $6 AND review_due = $7 AND updated_at <= $8", query)
        self.assertIn("$9::timestamptz IS NULL OR updated_at = $9", query)
        self.assertIn("updated_at + INTERVAL '1 microsecond'", query)
        for field in ("entry", "stop", "target", "direction", "source_identity", "strategy_id", "signal_bar_time", "duration_minutes"):
            self.assertIn(f"payload -> '{field}' IS NOT DISTINCT FROM $4::jsonb -> '{field}'", query)
        self.assertNotIn("opened_at =", query.split("WHERE")[0])
        self.assertNotIn("review_due =", query.split("WHERE")[0])

    async def test_optional_update_version_is_a_bound_null_not_interpolated_sql(self):
        await journal_store.update_trade(self.pool, BOT_ID, CHAT_ID, SIGNAL_ID, paper_trade(), NOW)
        self.assertIsNone(self.pool.fetchval.await_args.args[-1])

    async def test_reviews_join_active_subscription_with_both_bot_and_chat_scope(self):
        closed = paper_trade(status="expired")
        self.pool.fetch.return_value = [{"chat_id": CHAT_ID, "signal_id": SIGNAL_ID, "payload": json.dumps(closed), "updated_at": NOW}]
        rows = await journal_store.list_reviews(self.pool, BOT_ID, limit=10)
        self.assertEqual(rows[0]["payload"], closed)
        query, bot_id, limit = self.pool.fetch.await_args.args
        self.assertEqual((bot_id, limit), (BOT_ID, 10))
        query = compact(query)
        self.assertIn("subscription.bot_id = trade.bot_id AND subscription.chat_id = trade.chat_id", query)
        self.assertIn("trade.bot_id = $1 AND trade.status <> 'open' AND NOT trade.reviewed_sent", query)
        self.assertIn("AND subscription.active", query)

    async def test_presend_review_probe_checks_exact_record_and_active_opt_in(self):
        for result in (True, False, None):
            self.pool.fetchval.return_value = result
            self.assertIs(await journal_store.review_active(self.pool, BOT_ID, CHAT_ID, SIGNAL_ID), bool(result))
        query, bot_id, chat_id, signal_id = self.pool.fetchval.await_args.args
        self.assertEqual((bot_id, chat_id, signal_id), (BOT_ID, CHAT_ID, SIGNAL_ID))
        query = compact(query)
        self.assertIn("trade.bot_id = $1 AND trade.chat_id = $2 AND trade.signal_id = $3", query)
        self.assertIn("trade.status <> 'open' AND NOT trade.reviewed_sent AND subscription.active", query)

    async def test_review_acknowledgment_is_idempotent_and_only_for_completed_record(self):
        for result in (True, False, None):
            self.pool.fetchval.return_value = result
            self.assertIs(await journal_store.mark_review_sent(self.pool, BOT_ID, CHAT_ID, SIGNAL_ID, NOW), bool(result))
        query, bot_id, chat_id, signal_id, timestamp = self.pool.fetchval.await_args.args
        self.assertEqual((bot_id, chat_id, signal_id, timestamp), (BOT_ID, CHAT_ID, SIGNAL_ID, NOW))
        self.assertIn("bot_id = $1 AND chat_id = $2 AND signal_id = $3", compact(query))
        self.assertIn("status <> 'open' AND NOT reviewed_sent", compact(query))

    async def test_recent_history_is_chat_scoped_bounded_and_decoded(self):
        self.pool.fetch.return_value = [{"payload": json.dumps(paper_trade())}, {"payload": "[]"}]
        self.assertEqual(await journal_store.recent_trades(self.pool, BOT_ID, CHAT_ID), [paper_trade()])
        query, bot_id, chat_id, limit = self.pool.fetch.await_args.args
        self.assertEqual((bot_id, chat_id, limit), (BOT_ID, CHAT_ID, 5))
        self.assertIn("WHERE bot_id = $1 AND chat_id = $2", compact(query))
        self.assertIn("ORDER BY opened_at DESC", compact(query))
        self.assertIn("LIMIT $3", query)

    async def test_zero_query_limits_do_not_access_database(self):
        self.assertEqual(await journal_store.list_open(self.pool, BOT_ID, limit=0), [])
        self.assertEqual(await journal_store.list_reviews(self.pool, BOT_ID, limit=0), [])
        self.assertEqual(await journal_store.recent_trades(self.pool, BOT_ID, CHAT_ID, limit=0), [])
        self.pool.fetch.assert_not_awaited()

    async def test_query_limits_and_identifiers_fail_before_database_access(self):
        for value in (True, -1, 101, 1.0, "5"):
            for method in (journal_store.list_open, journal_store.list_reviews):
                with self.subTest(method=method.__name__, value=value), self.assertRaises(ValueError):
                    await method(self.pool, BOT_ID, limit=value)
            with self.assertRaises(ValueError):
                await journal_store.recent_trades(self.pool, BOT_ID, CHAT_ID, limit=value)
        for identifier in (True, 2**63, -(2**63) - 1, "1"):
            with self.assertRaises(ValueError):
                await journal_store.open_trade(self.pool, identifier, CHAT_ID, paper_trade(), NOW)
            with self.assertRaises(ValueError):
                await journal_store.recent_trades(self.pool, BOT_ID, identifier)
        self.pool.fetch.assert_not_awaited()
        self.pool.fetchval.assert_not_awaited()

    async def test_invalid_payload_dates_statuses_and_nonfinite_values_fail_before_write(self):
        changes = (
            {"id": ""}, {"id": " "}, {"id": "x" * 513}, {"id": "bad\x00id"},
            {"opened_at": "2026-10-04T08:00:00"}, {"deadline": "bad-date"},
            {"deadline": (NOW - timedelta(seconds=1)).isoformat()},
            {"status": "real_order_sent"}, {"entry": float("nan")}, {"entry": float("inf")},
            {"entry": datetime(2026, 10, 4)}, {"status": "expired"},
            {"opened_at": (NOW + timedelta(seconds=1)).isoformat()},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                await journal_store.open_trade(self.pool, BOT_ID, CHAT_ID, paper_trade(**change), NOW)
        for trade in (None, [], "trade"):
            with self.assertRaises(ValueError):
                await journal_store.open_trade(self.pool, BOT_ID, CHAT_ID, trade, NOW)
        self.pool.fetchval.assert_not_awaited()

    async def test_naive_clocks_and_wrong_update_identity_are_rejected(self):
        naive = datetime(2026, 10, 4, 8)
        with self.assertRaises(ValueError):
            await journal_store.open_trade(self.pool, BOT_ID, CHAT_ID, paper_trade(), naive)
        with self.assertRaises(ValueError):
            await journal_store.mark_review_sent(self.pool, BOT_ID, CHAT_ID, SIGNAL_ID, naive)
        with self.assertRaises(ValueError):
            await journal_store.update_trade(self.pool, BOT_ID, CHAT_ID, SIGNAL_ID, paper_trade(), NOW, expected_updated_at=naive)
        with self.assertRaises(ValueError):
            await journal_store.update_trade(self.pool, BOT_ID, CHAT_ID, "another-id", paper_trade(), NOW)
        self.pool.fetchval.assert_not_awaited()

    async def test_record_identifiers_are_bound_parameters(self):
        untrusted_id = "setup'); DROP TABLE paper_trades;--"
        trade = paper_trade(id=untrusted_id)
        await journal_store.open_trade(self.pool, BOT_ID, CHAT_ID, trade, NOW)
        query, *args = self.pool.fetchval.await_args.args
        self.assertNotIn(untrusted_id, query)
        self.assertEqual(args[2], untrusted_id)
        await journal_store.review_active(self.pool, BOT_ID, CHAT_ID, untrusted_id)
        query, *args = self.pool.fetchval.await_args.args
        self.assertNotIn(untrusted_id, query)
        self.assertEqual(args[2], untrusted_id)

    async def test_cancellation_is_not_swallowed_by_reads_or_writes(self):
        self.pool.fetch.side_effect = asyncio.CancelledError()
        self.pool.fetchval.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await journal_store.list_open(self.pool, BOT_ID)
        with self.assertRaises(asyncio.CancelledError):
            await journal_store.open_trade(self.pool, BOT_ID, CHAT_ID, paper_trade(), NOW)


class JournalMigrationTests(unittest.TestCase):
    def test_migration_head_and_schema_match_runtime_initializer(self):
        path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "20261004_03_paper_journal.py"
        spec = importlib.util.spec_from_file_location("paper_journal_migration", path)
        migration = importlib.util.module_from_spec(spec)
        # Migration SQL can be verified without installing the Alembic runner.
        fake_alembic = SimpleNamespace(op=SimpleNamespace(execute=MagicMock()))
        with patch.dict(sys.modules, {"alembic": fake_alembic}):
            spec.loader.exec_module(migration)
        self.assertEqual(migration.revision, "20261004_03")
        self.assertEqual(migration.down_revision, "20261004_02")
        with patch.object(migration.op, "execute") as execute:
            migration.upgrade()
            statements = [compact(call.args[0]) for call in execute.call_args_list]
        self.assertEqual(statements, [compact(statement) for statement in journal_store.SCHEMA_STATEMENTS])
        with patch.object(migration.op, "execute") as execute:
            migration.downgrade()
            self.assertEqual([call.args[0] for call in execute.call_args_list], ["DROP TABLE IF EXISTS paper_trades"])


if __name__ == "__main__":
    unittest.main()
