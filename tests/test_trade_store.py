"""MT5 authorization persistence contracts; no MT5, database, or network calls."""

import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from bot import trade_store as store


NOW = datetime(2026, 10, 4, 14, tzinfo=timezone.utc)
DEVICE = UUID("f57ae895-fd4a-4e17-b3d8-c1a03d3e2d9d")
OFFER = UUID("dcd64b5a-7a53-4fcd-a314-eab09d33c623")
CLAIM = UUID("bdd8585b-2339-49ca-8343-69f8096b20c5")
BOT_ID, CHAT_ID, USER_ID = 9901, 4401, 4401
CODE_HASH = "ab" * 32


def compact(query):
    return " ".join(query.split())


def device_row(**changes):
    row = {
        "bot_id": BOT_ID, "device_id": DEVICE, "symbol": "XAUUSD.m",
        "account_mode": "demo", "volume": 0.01,
        "owner_chat_id": CHAT_ID, "owner_user_id": USER_ID, "paired_at": NOW,
        "pair_expires_at": NOW + timedelta(minutes=10), "last_seen_at": NOW,
        "created_at": NOW, "updated_at": NOW,
    }
    row.update(changes)
    return row


def offer_row(**changes):
    row = {
        "id": OFFER, "bot_id": BOT_ID, "device_id": DEVICE, "chat_id": CHAT_ID, "user_id": USER_ID,
        "signal_id": "signal-1", "payload": {"direction": "BUY", "entry": 2700.0, "stop": 2697.0, "target": 2706.0},
        "status": "draft", "created_at": NOW, "expires_at": NOW + timedelta(seconds=90),
        "updated_at": NOW, "published_at": None, "message_id": None, "decided_at": None,
        "executing_at": None, "completed_at": None, "claim_id": None, "result": None, "notified": False,
        "symbol": "XAUUSD.m", "account_mode": "demo", "volume": 0.01,
    }
    row.update(changes)
    return row


class TradeStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pool = SimpleNamespace(
            fetchrow=AsyncMock(return_value=offer_row()), fetch=AsyncMock(return_value=[]),
            fetchval=AsyncMock(return_value=True), execute=AsyncMock(),
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

    async def test_schema_transaction_preserves_owner_claim_and_offer_scope(self):
        pool, connection, transaction, _ = self.schema_pool()
        await store.initialize_schema(pool)
        transaction.__aenter__.assert_awaited_once()
        self.assertEqual([call.args[0] for call in connection.execute.await_args_list], list(store.SCHEMA_STATEMENTS))
        schema = " ".join(compact(statement) for statement in store.SCHEMA_STATEMENTS)
        self.assertIn("PRIMARY KEY (bot_id, device_id)", schema)
        self.assertIn("UNIQUE (bot_id, device_id, chat_id, signal_id)", schema)
        self.assertIn("UNIQUE (bot_id, chat_id, message_id)", schema)
        self.assertIn("FOREIGN KEY (bot_id, device_id)", schema)
        self.assertIn("(claim_id IS NULL) = (executing_at IS NULL)", schema)
        self.assertNotIn("password", schema)
        self.assertNotIn("account_number", schema)

    async def test_schema_cancellation_exits_transaction_and_returns_connection(self):
        pool, connection, transaction, acquisition = self.schema_pool()
        connection.execute.side_effect = [None, asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await store.initialize_schema(pool)
        self.assertIs(transaction.__aexit__.await_args.args[0], asyncio.CancelledError)
        self.assertIs(acquisition.__aexit__.await_args.args[0], asyncio.CancelledError)

    async def test_register_device_binds_configuration_and_does_not_return_pair_hash(self):
        self.pool.fetchrow.return_value = device_row(pair_code_hash=CODE_HASH)
        row = await store.register_device(self.pool, BOT_ID, str(DEVICE), "XAUUSD.m", "demo", 0.01, CODE_HASH.upper(), NOW)
        self.assertNotIn("pair_code_hash", row)
        self.assertEqual(row["device_id"], DEVICE)
        query, *args = self.pool.fetchrow.await_args.args
        self.assertEqual(args, [BOT_ID, DEVICE, "XAUUSD.m", "demo", 0.01, CODE_HASH, NOW + timedelta(minutes=10), NOW])
        query = compact(query)
        self.assertIn("ON CONFLICT (bot_id, device_id)", query)
        self.assertIn("device.symbol = EXCLUDED.symbol AND device.account_mode = EXCLUDED.account_mode", query)
        self.assertIn("device.volume = EXCLUDED.volume", query)
        self.assertIn("WHEN device.owner_user_id IS NULL THEN EXCLUDED.pair_code_hash ELSE device.pair_code_hash", query)
        self.assertIn("device.pair_code_hash <> EXCLUDED.pair_code_hash", query)
        self.assertNotIn("owner_chat_id =", query)
        self.assertNotIn("owner_user_id =", query)
        self.assertNotIn(CODE_HASH, query)

    async def test_registration_conflict_does_not_echo_code_or_supplied_configuration(self):
        self.pool.fetchrow.return_value = None
        with self.assertRaises(ValueError) as caught:
            await store.register_device(self.pool, BOT_ID, DEVICE, "XAUUSD.m", "demo", 0.01, CODE_HASH, NOW)
        self.assertNotIn(CODE_HASH, str(caught.exception))
        self.assertNotIn("XAUUSD.m", str(caught.exception))

    async def test_pairing_is_single_use_bot_scoped_and_expires_after_ten_minutes(self):
        self.pool.fetchrow.return_value = device_row()
        result = await store.pair_device(self.pool, BOT_ID, CODE_HASH, CHAT_ID, USER_ID, NOW)
        self.assertEqual(result["owner_chat_id"], CHAT_ID)
        query, *args = self.pool.fetchrow.await_args.args
        self.assertEqual(args, [BOT_ID, CODE_HASH, CHAT_ID, USER_ID, NOW])
        query = compact(query)
        self.assertIn("bot_id = $1 AND pair_code_hash = $2 AND owner_user_id IS NULL", query)
        self.assertIn("pair_expires_at > $5", query)
        self.assertIn("updated_at <= $5", query)
        self.pool.fetchrow.return_value = None
        self.assertIsNone(await store.pair_device(self.pool, BOT_ID, CODE_HASH, CHAT_ID, USER_ID, NOW))

    async def test_device_reads_are_bot_scoped_and_hide_hashes(self):
        self.pool.fetchrow.return_value = device_row()
        await store.get_device(self.pool, BOT_ID, DEVICE)
        query, bot_id, device_id = self.pool.fetchrow.await_args.args
        self.assertEqual((bot_id, device_id), (BOT_ID, DEVICE))
        self.assertIn("WHERE bot_id = $1 AND device_id = $2", compact(query))
        self.assertNotIn("pair_code_hash", query)
        self.pool.fetch.return_value = [device_row()]
        self.assertEqual(await store.list_paired_devices(self.pool, BOT_ID), [device_row()])
        query, bot_id, limit = self.pool.fetch.await_args.args
        self.assertEqual((bot_id, limit), (BOT_ID, 100))
        self.assertIn("owner_user_id IS NOT NULL", query)
        self.assertIn("LIMIT $2", query)

    async def test_heartbeat_is_scoped_and_cannot_move_last_seen_backwards(self):
        for value in (True, False, None):
            self.pool.fetchval.return_value = value
            self.assertIs(await store.heartbeat(self.pool, BOT_ID, DEVICE, NOW), bool(value))
        query, bot_id, device_id, timestamp = self.pool.fetchval.await_args.args
        self.assertEqual((bot_id, device_id, timestamp), (BOT_ID, DEVICE, NOW))
        self.assertIn("bot_id = $1 AND device_id = $2 AND last_seen_at <= $3", compact(query))

    async def test_duplicate_offer_preserves_payload_expiry_and_owner(self):
        frozen = offer_row()
        self.pool.fetchrow.return_value = frozen
        changed = {"direction": "SELL", "entry": 2710.0, "stop": 2720.0, "target": 2690.0}
        with patch.object(store, "uuid4", return_value=OFFER):
            result = await store.create_offer(self.pool, BOT_ID, DEVICE, CHAT_ID, USER_ID, "signal-1", changed, NOW, NOW + timedelta(minutes=2))
        self.assertEqual(result["payload"], frozen["payload"])
        self.assertEqual(result["expires_at"], frozen["expires_at"])
        result["payload"]["entry"] = 1.0
        self.assertEqual(frozen["payload"]["entry"], 2700.0)
        query, *args = self.pool.fetchrow.await_args.args
        self.assertEqual(args[:6], [OFFER, BOT_ID, DEVICE, CHAT_ID, USER_ID, "signal-1"])
        self.assertEqual(json.loads(args[6]), changed)
        self.assertEqual(args[7:], [NOW, NOW + timedelta(minutes=2)])
        query = compact(query)
        self.assertIn("ON CONFLICT (bot_id, device_id, chat_id, signal_id) DO UPDATE SET id = offer.id", query)
        self.assertIn("device.owner_chat_id = $4 AND device.owner_user_id = $5 AND subscription.active", query)
        conflict = query.split("ON CONFLICT", 1)[1].split("RETURNING", 1)[0]
        self.assertNotIn("expires_at =", conflict)
        self.assertNotIn("payload =", conflict)
        self.assertNotIn("user_id =", conflict)

    async def test_offer_without_current_owner_and_subscription_is_not_created(self):
        self.pool.fetchrow.return_value = None
        with self.assertRaises(ValueError):
            await store.create_offer(self.pool, BOT_ID, DEVICE, CHAT_ID, USER_ID, "signal-1", {}, NOW, NOW + timedelta(seconds=90))

    async def test_publish_only_binds_current_unexpired_draft_message_once(self):
        for value in (True, False, None):
            self.pool.fetchval.return_value = value
            self.assertIs(await store.publish_offer(self.pool, BOT_ID, OFFER, 71, NOW), bool(value))
        query, *args = self.pool.fetchval.await_args.args
        self.assertEqual(args, [BOT_ID, OFFER, 71, NOW])
        query = compact(query)
        self.assertIn("offer.bot_id = $1 AND offer.id = $2 AND offer.status = 'draft'", query)
        self.assertIn("offer.expires_at > $4", query)
        self.assertIn("published_at = $4", query)
        self.assertIn("device.owner_chat_id = offer.chat_id AND device.owner_user_id = offer.user_id", query)
        self.assertIn("subscription.active", query)

    async def test_offer_read_decodes_nested_json_and_keeps_bot_scope(self):
        self.pool.fetchrow.return_value = offer_row(payload=json.dumps({"result": {"direction": "BUY"}}), result=json.dumps({"status": "filled", "ticket": 123}))
        row = await store.get_offer(self.pool, BOT_ID, str(OFFER))
        self.assertEqual(row["payload"], {"result": {"direction": "BUY"}})
        self.assertEqual(row["result"], {"status": "filled", "ticket": 123})
        query, bot_id, offer_id = self.pool.fetchrow.await_args.args
        self.assertEqual((bot_id, offer_id), (BOT_ID, OFFER))
        self.assertIn("offer.bot_id = $1 AND offer.id = $2", compact(query))

    async def test_decision_is_atomic_owner_message_subscription_and_expiry_bound(self):
        for decision in ("accepted", "rejected"):
            self.pool.fetchrow.return_value = offer_row(status=decision)
            row = await store.decide(self.pool, BOT_ID, OFFER, CHAT_ID, USER_ID, 71, decision, NOW)
            self.assertEqual(row["status"], decision)
            query, *args = self.pool.fetchrow.await_args.args
            self.assertEqual(args, [BOT_ID, OFFER, CHAT_ID, USER_ID, 71, decision, NOW])
        query = compact(query)
        self.assertIn("offer.bot_id = $1 AND offer.id = $2 AND offer.chat_id = $3", query)
        self.assertIn("offer.user_id = $4 AND offer.message_id = $5 AND offer.status = 'offered'", query)
        self.assertIn("offer.expires_at > $7", query)
        self.assertIn("device.owner_chat_id = offer.chat_id AND device.owner_user_id = offer.user_id", query)
        self.assertIn("subscription.chat_id = offer.chat_id AND subscription.active", query)
        self.pool.fetchrow.return_value = None
        self.assertIsNone(await store.decide(self.pool, BOT_ID, OFFER, CHAT_ID, USER_ID, 71, "accepted", NOW))

    async def test_claim_is_single_atomic_lock_with_fresh_owner_and_consent_without_reclaim(self):
        self.pool.fetchrow.return_value = offer_row(status="executing", claim_id=CLAIM, executing_at=NOW)
        with patch.object(store, "uuid4", return_value=CLAIM):
            row = await store.claim_offer(self.pool, BOT_ID, DEVICE, NOW)
        self.assertEqual(row["claim_id"], CLAIM)
        query, *args = self.pool.fetchrow.await_args.args
        self.assertEqual(args, [BOT_ID, DEVICE, NOW, NOW - timedelta(seconds=180), CLAIM])
        query = compact(query)
        self.assertIn("offer.bot_id = $1 AND offer.device_id = $2 AND offer.status = 'accepted'", query)
        self.assertIn("offer.expires_at > $3", query)
        self.assertIn("device.last_seen_at >= $4 AND device.last_seen_at <= $3 AND subscription.active", query)
        self.assertIn("device.owner_chat_id = offer.chat_id AND device.owner_user_id = offer.user_id", query)
        self.assertIn("LIMIT 1 FOR UPDATE OF offer SKIP LOCKED", query)
        self.assertNotIn("OR offer.status = 'executing'", query)
        self.assertNotIn("leased_until", query)
        self.pool.fetchrow.return_value = None
        self.assertIsNone(await store.claim_offer(self.pool, BOT_ID, DEVICE, NOW))

    async def test_completion_requires_authoritative_device_offer_claim_and_executing_state(self):
        for status in ("filled", "failed", "unknown"):
            result = {"status": status, "ticket": 123 if status == "filled" else None}
            self.pool.fetchrow.return_value = offer_row(status=status, result=json.dumps(result))
            row = await store.complete_offer(self.pool, BOT_ID, DEVICE, OFFER, CLAIM, result, NOW)
            self.assertEqual(row["result"], result)
            query, *args = self.pool.fetchrow.await_args.args
            self.assertEqual(args[:5], [BOT_ID, DEVICE, OFFER, CLAIM, status])
            self.assertEqual(json.loads(args[5]), result)
            self.assertEqual(args[6], NOW)
        query = compact(query)
        self.assertIn("bot_id = $1 AND device_id = $2 AND id = $3 AND claim_id = $4", query)
        self.assertIn("status = 'executing' AND executing_at <= $7", query)
        self.pool.fetchrow.return_value = None
        self.assertIsNone(await store.complete_offer(self.pool, BOT_ID, DEVICE, OFFER, CLAIM, {"status": "filled"}, NOW))

    async def test_duplicate_result_ack_returns_unchanged_terminal_record_with_exact_claim_and_json(self):
        outcome = {"status": "filled", "code": 10009, "order_ticket": 123}
        terminal = offer_row(
            status="filled", result=outcome, claim_id=CLAIM,
            executing_at=NOW - timedelta(seconds=1), completed_at=NOW, updated_at=NOW,
        )
        self.pool.fetchrow.return_value = terminal
        # JSON object ordering does not change PostgreSQL JSONB equality.
        retry_outcome = {"order_ticket": 123, "code": 10009, "status": "filled"}
        row = await store.complete_offer(
            self.pool, BOT_ID, DEVICE, OFFER, CLAIM, retry_outcome, NOW + timedelta(seconds=60),
        )
        self.assertEqual(row, terminal)
        self.assertEqual(row["completed_at"], NOW)
        self.assertEqual(row["updated_at"], NOW)
        query, *args = self.pool.fetchrow.await_args.args
        self.assertEqual(args[:5], [BOT_ID, DEVICE, OFFER, CLAIM, "filled"])
        self.assertEqual(json.loads(args[5]), outcome)
        query = compact(query)
        self.assertIn("UNION ALL SELECT * FROM mt5_trade_offers", query)
        duplicate_branch = query.split("UNION ALL", 1)[1].split(") SELECT resolved", 1)[0]
        self.assertIn("bot_id = $1 AND device_id = $2 AND id = $3 AND claim_id = $4", duplicate_branch)
        self.assertIn("status = $5 AND status IN ('filled', 'failed', 'unknown') AND result = $6::jsonb", duplicate_branch)
        self.assertNotIn("UPDATE", duplicate_branch)
        self.assertNotIn("SET", duplicate_branch)
        self.assertEqual(query.count("UPDATE mt5_trade_offers"), 1)

    async def test_conflicting_terminal_result_or_claim_remains_unacknowledged(self):
        self.pool.fetchrow.return_value = None
        changed_claim = UUID("a0e5cbde-10bf-4af6-ae8c-487a0ab10c6e")
        changed_outcome = {"status": "failed", "code": 10006}
        self.assertIsNone(await store.complete_offer(
            self.pool, BOT_ID, DEVICE, OFFER, changed_claim, changed_outcome, NOW,
        ))
        query, *args = self.pool.fetchrow.await_args.args
        self.assertEqual(args[3], changed_claim)
        self.assertEqual(args[4], "failed")
        self.assertEqual(json.loads(args[5]), changed_outcome)
        # Both initial completion and repeated terminal acknowledgement bind
        # the same device/offer/claim; only the duplicate path adds exact JSON.
        self.assertEqual(compact(query).count("bot_id = $1 AND device_id = $2 AND id = $3 AND claim_id = $4"), 2)
        self.assertIn("AND result = $6::jsonb", compact(query))

    async def test_notifications_include_unacknowledged_results_after_unwatch_but_only_for_bound_owner(self):
        self.pool.fetch.return_value = [offer_row(status="unknown", result={"status": "unknown"})]
        rows = await store.list_notifications(self.pool, BOT_ID)
        self.assertEqual(rows[0]["result"], {"status": "unknown"})
        query, bot_id, limit = self.pool.fetch.await_args.args
        self.assertEqual((bot_id, limit), (BOT_ID, 20))
        query = compact(query)
        self.assertIn("offer.bot_id = $1 AND NOT offer.notified", query)
        self.assertIn("offer.status IN ('filled', 'failed', 'unknown')", query)
        self.assertIn("device.owner_chat_id = offer.chat_id AND device.owner_user_id = offer.user_id", query)
        self.assertNotIn("market_subscriptions", query)
        self.assertNotIn("subscription.active", query)
        self.assertIn("LIMIT $2", query)
        await store.subscription_active(self.pool, BOT_ID, CHAT_ID)
        query, *args = self.pool.fetchval.await_args.args
        self.assertEqual(args, [BOT_ID, CHAT_ID])
        self.assertIn("bot_id = $1 AND chat_id = $2 AND active", compact(query))

    async def test_notification_acknowledgment_is_terminal_scoped_and_idempotent(self):
        for value in (True, False, None):
            self.pool.fetchval.return_value = value
            self.assertIs(await store.mark_notified(self.pool, BOT_ID, OFFER, NOW), bool(value))
        query, *args = self.pool.fetchval.await_args.args
        self.assertEqual(args, [BOT_ID, OFFER, NOW])
        self.assertIn("bot_id = $1 AND id = $2 AND NOT notified", compact(query))
        self.assertIn("status IN ('filled', 'failed', 'unknown')", compact(query))

    async def test_approved_but_unclaimed_expiry_or_cancellation_is_notified_without_order_result(self):
        pending_rows = [
            offer_row(status=status, decided_at=NOW, executing_at=None, result=None)
            for status in ("expired", "cancelled")
        ]
        self.pool.fetch.return_value = pending_rows
        rows = await store.list_notifications(self.pool, BOT_ID)
        self.assertEqual([row["status"] for row in rows], ["expired", "cancelled"])
        self.assertTrue(all(row["result"] is None and row["executing_at"] is None for row in rows))
        query = compact(self.pool.fetch.await_args.args[0])
        self.assertIn("offer.status IN ('expired', 'cancelled') AND offer.decided_at IS NOT NULL AND offer.executing_at IS NULL", query)
        self.assertIn("device.owner_chat_id = offer.chat_id AND device.owner_user_id = offer.user_id", query)
        self.assertNotIn("subscription.active", query)
        for status in ("expired", "cancelled"):
            self.assertTrue(await store.mark_notified(self.pool, BOT_ID, OFFER, NOW))
        query = compact(self.pool.fetchval.await_args.args[0])
        self.assertIn("status IN ('expired', 'cancelled') AND decided_at IS NOT NULL AND executing_at IS NULL", query)

    async def test_unclicked_or_already_claimed_expired_offers_do_not_qualify_for_terminal_ack(self):
        # Both notification selection and acknowledgment require evidence of
        # the user's decision and absence of an execution claim timestamp.
        await store.list_notifications(self.pool, BOT_ID)
        selection = compact(self.pool.fetch.await_args.args[0])
        self.pool.fetchval.return_value = None
        self.assertFalse(await store.mark_notified(self.pool, BOT_ID, OFFER, NOW))
        acknowledgment = compact(self.pool.fetchval.await_args.args[0])
        for query in (selection, acknowledgment):
            self.assertIn("decided_at IS NOT NULL", query)
            self.assertIn("executing_at IS NULL", query)
            self.assertNotIn("status IN ('draft', 'offered', 'accepted')", query)
            self.assertNotIn("'rejected'", query)

    async def test_unwatch_cancels_only_unexecuted_offers_in_exact_chat(self):
        self.pool.fetchval.return_value = 3
        self.assertEqual(await store.cancel_offers(self.pool, BOT_ID, CHAT_ID, NOW), 3)
        query, *args = self.pool.fetchval.await_args.args
        self.assertEqual(args, [BOT_ID, CHAT_ID, NOW])
        self.assertIn("bot_id = $1 AND chat_id = $2 AND status IN ('draft', 'offered', 'accepted')", compact(query))
        self.assertNotIn("'executing'", query)

    async def test_sweep_expires_unused_offers_and_marks_timed_out_execution_unknown_without_retry(self):
        self.pool.fetchval.return_value = 4
        self.assertEqual(await store.expire_offers(self.pool, BOT_ID, NOW), 4)
        query, bot_id, timestamp, timeout, encoded = self.pool.fetchval.await_args.args
        self.assertEqual((bot_id, timestamp, timeout), (BOT_ID, NOW, NOW - timedelta(seconds=120)))
        self.assertEqual(json.loads(encoded), {"status": "unknown", "reason": "execution_timeout"})
        query = compact(query)
        self.assertIn("status IN ('draft', 'offered', 'accepted') AND expires_at <= $2", query)
        self.assertIn("status = 'unknown', result = $4::jsonb", query)
        self.assertIn("status = 'executing' AND executing_at < $3", query)
        self.assertNotIn("status = 'accepted'", query)
        self.assertNotIn("claim_id = NULL", query)

    async def test_zero_limits_do_not_read_database(self):
        self.assertEqual(await store.list_paired_devices(self.pool, BOT_ID, limit=0), [])
        self.assertEqual(await store.list_notifications(self.pool, BOT_ID, limit=0), [])
        self.pool.fetch.assert_not_awaited()

    async def test_invalid_ids_hashes_modes_sizes_prices_limits_and_dates_fail_before_database_access(self):
        for device_id in (True, "not-uuid", None):
            with self.assertRaises(ValueError):
                await store.get_device(self.pool, BOT_ID, device_id)
        for value in (True, 0, 2**63, "1"):
            with self.assertRaises(ValueError):
                await store.subscription_active(self.pool, value, CHAT_ID)
        for volume in (0, -0.01, True, float("nan"), float("inf"), 1001, "0.01"):
            with self.assertRaises(ValueError):
                await store.register_device(self.pool, BOT_ID, DEVICE, "XAUUSD", "demo", volume, CODE_HASH, NOW)
        for account_mode in (None, [], "REAL", "live"):
            with self.assertRaises(ValueError):
                await store.register_device(self.pool, BOT_ID, DEVICE, "XAUUSD", account_mode, 0.01, CODE_HASH, NOW)
        for code_hash in (None, "secret-code", "ff" * 31, "gg" * 32):
            with self.assertRaises(ValueError):
                await store.pair_device(self.pool, BOT_ID, code_hash, CHAT_ID, USER_ID, NOW)
        for limit in (True, -1, 101, "20"):
            with self.assertRaises(ValueError):
                await store.list_notifications(self.pool, BOT_ID, limit=limit)
        with self.assertRaises(ValueError):
            await store.heartbeat(self.pool, BOT_ID, DEVICE, datetime(2026, 10, 4))
        with self.assertRaises(ValueError):
            await store.decide(self.pool, BOT_ID, OFFER, CHAT_ID, USER_ID, 71, [], NOW)
        self.pool.fetch.assert_not_awaited()
        self.pool.fetchrow.assert_not_awaited()
        self.pool.fetchval.assert_not_awaited()

    async def test_json_is_finite_bounded_object_and_offer_deadline_is_bounded(self):
        cyclic = {}
        cyclic["self"] = cyclic
        bad_payloads = ([], None, {"price": float("nan")}, {"price": float("inf")},
                        {"time": NOW}, {1: "invalid-key"}, {"text": "x" * 16385},
                        {str(index): "x" * 16000 for index in range(5)}, cyclic)
        for payload in bad_payloads:
            with self.subTest(payload_type=type(payload).__name__), self.assertRaises(ValueError):
                await store.create_offer(self.pool, BOT_ID, DEVICE, CHAT_ID, USER_ID, "signal-1", payload, NOW, NOW + timedelta(seconds=90))
        for expiry in (NOW, NOW - timedelta(seconds=1), NOW + timedelta(minutes=16), datetime(2026, 10, 4)):
            with self.assertRaises(ValueError):
                await store.create_offer(self.pool, BOT_ID, DEVICE, CHAT_ID, USER_ID, "signal-1", {}, NOW, expiry)
        for result in ({"status": "accepted"}, {"status": "filled", "price": float("nan")}, {}):
            with self.assertRaises(ValueError):
                await store.complete_offer(self.pool, BOT_ID, DEVICE, OFFER, CLAIM, result, NOW)
        self.pool.fetchrow.assert_not_awaited()

    async def test_symbol_signal_and_message_values_are_bound_instead_of_interpolated(self):
        untrusted = "setup'); DROP TABLE mt5_trade_offers;--"
        await store.create_offer(self.pool, BOT_ID, DEVICE, CHAT_ID, USER_ID, untrusted, {"notes": "تعليمي"}, NOW, NOW + timedelta(seconds=90))
        query, *args = self.pool.fetchrow.await_args.args
        self.assertNotIn(untrusted, query)
        self.assertEqual(args[5], untrusted)
        self.assertEqual(json.loads(args[6]), {"notes": "تعليمي"})
        for symbol in ("EURUSD", "XAUUSD\nGOLD", "XAUUSD');drop"):
            with self.assertRaises(ValueError):
                await store.register_device(self.pool, BOT_ID, DEVICE, symbol, "demo", 0.01, CODE_HASH, NOW)
        with self.assertRaises(ValueError):
            await store.publish_offer(self.pool, BOT_ID, OFFER, True, NOW)

    async def test_cancellation_is_not_swallowed(self):
        self.pool.fetchrow.side_effect = asyncio.CancelledError()
        self.pool.fetchval.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await store.claim_offer(self.pool, BOT_ID, DEVICE, NOW)
        with self.assertRaises(asyncio.CancelledError):
            await store.expire_offers(self.pool, BOT_ID, NOW)


class TradeMigrationTests(unittest.TestCase):
    def test_migration_head_schema_and_dependency_order_match_runtime_initializer(self):
        path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "20261004_04_mt5_trade_offers.py"
        spec = importlib.util.spec_from_file_location("mt5_trade_offers_migration", path)
        migration = importlib.util.module_from_spec(spec)
        fake_alembic = SimpleNamespace(op=SimpleNamespace(execute=MagicMock()))
        with patch.dict(sys.modules, {"alembic": fake_alembic}):
            spec.loader.exec_module(migration)
        self.assertEqual(migration.revision, "20261004_04")
        self.assertEqual(migration.down_revision, "20261004_03")
        with patch.object(migration.op, "execute") as execute:
            migration.upgrade()
            self.assertEqual([compact(call.args[0]) for call in execute.call_args_list], [compact(query) for query in store.SCHEMA_STATEMENTS])
        with patch.object(migration.op, "execute") as execute:
            migration.downgrade()
            self.assertEqual([call.args[0] for call in execute.call_args_list], [
                "DROP TABLE IF EXISTS mt5_trade_offers", "DROP TABLE IF EXISTS mt5_devices",
            ])


if __name__ == "__main__":
    unittest.main()
