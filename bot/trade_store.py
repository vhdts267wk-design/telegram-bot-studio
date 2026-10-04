"""Durable, owner-bound MT5 offers with single-use execution claims.

This module stores authorization and execution reports. It never contacts MT5
or sends orders. A timed-out execution becomes unknown and cannot be reclaimed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from numbers import Real
import re
from uuid import UUID, uuid4


PAIR_TTL = timedelta(minutes=10)
HEARTBEAT_TTL = timedelta(seconds=180)
EXECUTION_TIMEOUT = timedelta(seconds=120)
MAX_OFFER_TTL = timedelta(minutes=15)
MAX_VOLUME = 1000.0
MAX_JSON_BYTES = 65536
MAX_QUERY_LIMIT = 100
RESULT_STATUSES = frozenset({"filled", "failed", "unknown"})

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mt5_devices (
        bot_id BIGINT NOT NULL,
        device_id UUID NOT NULL,
        symbol TEXT NOT NULL,
        account_mode TEXT NOT NULL CHECK (account_mode IN ('demo', 'real')),
        volume DOUBLE PRECISION NOT NULL CHECK (volume > 0 AND volume <= 1000),
        pair_code_hash TEXT NOT NULL,
        pair_expires_at TIMESTAMPTZ NOT NULL,
        owner_chat_id BIGINT,
        owner_user_id BIGINT,
        paired_at TIMESTAMPTZ,
        last_seen_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (bot_id, device_id),
        UNIQUE (bot_id, pair_code_hash),
        CHECK ((owner_chat_id IS NULL) = (owner_user_id IS NULL)),
        CHECK ((owner_user_id IS NULL) = (paired_at IS NULL))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS mt5_devices_owner_idx
        ON mt5_devices (bot_id, owner_chat_id) WHERE owner_user_id IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS mt5_trade_offers (
        id UUID PRIMARY KEY,
        bot_id BIGINT NOT NULL,
        device_id UUID NOT NULL,
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        signal_id TEXT NOT NULL,
        payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
        status TEXT NOT NULL DEFAULT 'draft' CHECK (
            status IN ('draft', 'offered', 'accepted', 'rejected', 'executing',
                'filled', 'failed', 'unknown', 'cancelled', 'expired')
        ),
        message_id BIGINT,
        created_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL CHECK (expires_at > created_at),
        published_at TIMESTAMPTZ,
        decided_at TIMESTAMPTZ,
        executing_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL,
        claim_id UUID,
        result JSONB CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
        notified BOOLEAN NOT NULL DEFAULT FALSE,
        UNIQUE (bot_id, device_id, chat_id, signal_id),
        UNIQUE (bot_id, chat_id, message_id),
        FOREIGN KEY (bot_id, device_id) REFERENCES mt5_devices (bot_id, device_id),
        CHECK ((message_id IS NULL) = (published_at IS NULL)),
        CHECK ((claim_id IS NULL) = (executing_at IS NULL))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS mt5_trade_offers_claim_idx
        ON mt5_trade_offers (bot_id, device_id, decided_at) WHERE status = 'accepted'
    """,
    """
    CREATE INDEX IF NOT EXISTS mt5_trade_offers_notifications_idx
        ON mt5_trade_offers (bot_id, updated_at)
        WHERE status IN ('filled', 'failed', 'unknown') AND NOT notified
    """,
    """
    CREATE INDEX IF NOT EXISTS mt5_trade_offers_expiry_idx
        ON mt5_trade_offers (bot_id, expires_at)
        WHERE status IN ('draft', 'offered', 'accepted', 'executing')
    """,
)

DEVICE_COLUMNS = """bot_id, device_id, symbol, account_mode, volume, pair_expires_at,
    owner_chat_id, owner_user_id, paired_at, last_seen_at, created_at, updated_at"""
OFFER_SELECT = """SELECT offer.*, device.symbol, device.account_mode, device.volume
    FROM mt5_trade_offers AS offer JOIN mt5_devices AS device
    ON device.bot_id = offer.bot_id AND device.device_id = offer.device_id"""


def _identifier(value, *, positive=False):
    if type(value) is not int or not -(2**63) <= value < 2**63 or (positive and value <= 0):
        raise ValueError("A valid integer identifier is required.")
    return value


def _uuid(value):
    if isinstance(value, UUID):
        return value
    if type(value) is str:
        try:
            return UUID(value)
        except ValueError:
            pass
    raise ValueError("A valid UUID is required.")


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required.")
    return value.astimezone(timezone.utc)


def _hash(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise ValueError("A SHA-256 pairing-code hash is required.")
    return value.lower()


def _signal(value):
    if type(value) is not str or not value.strip() or len(value) > 512 or "\x00" in value:
        raise ValueError("A bounded signal identifier is required.")
    return value


def _symbol(value):
    if type(value) is not str or re.fullmatch(r"(?:XAUUSD|GOLD)[A-Za-z0-9._#-]{0,24}", value, re.I) is None:
        raise ValueError("A supported broker gold symbol is required.")
    return value


def _volume(value):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("A finite positive volume within the supported bound is required.")
    try:
        value = float(value)
    except OverflowError:
        raise ValueError("A finite positive volume within the supported bound is required.") from None
    if not math.isfinite(value) or not 0 < value <= MAX_VOLUME:
        raise ValueError("A finite positive volume within the supported bound is required.")
    return value


def _limit(value):
    if type(value) is not int or not 0 <= value <= MAX_QUERY_LIMIT:
        raise ValueError("The query limit must be an integer between 0 and 100.")
    return value


def _json_object(value):
    if type(value) is not dict:
        raise ValueError("A bounded JSON object is required.")

    def validate(item, depth=0):
        if depth > 12:
            raise ValueError("The JSON object exceeds the supported depth.")
        if item is None or type(item) in (bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is str and len(item) <= 16384 and "\x00" not in item:
            return
        if type(item) is list:
            for child in item:
                validate(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str or not key or len(key) > 128 or "\x00" in key:
                    raise ValueError("JSON keys must be bounded nonempty strings.")
                validate(child, depth + 1)
            return
        raise ValueError("The JSON object contains unsupported or non-finite values.")

    validate(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
            raise ValueError("The JSON object exceeds the supported size.")
    except (TypeError, ValueError, OverflowError, UnicodeError) as error:
        raise ValueError("A bounded JSON object is required.") from error
    return encoded


def _device(row):
    if row is None:
        return None
    result = dict(row)
    result.pop("pair_code_hash", None)
    return result


def _offer(row):
    if row is None:
        return None
    result = dict(row)
    for key in ("payload", "result"):
        value = result.get(key)
        if isinstance(value, str):
            value = json.loads(value)
        if value is not None:
            value = json.loads(_json_object(value))
        result[key] = value
    return result


async def initialize_schema(pool):
    async with pool.acquire() as connection:
        async with connection.transaction():
            for statement in SCHEMA_STATEMENTS:
                await connection.execute(statement)


async def register_device(pool, bot_id, device_id, symbol, account_mode, volume, pair_code_hash, now):
    bot_id, device_id = _identifier(bot_id, positive=True), _uuid(device_id)
    symbol, volume, code_hash, now = _symbol(symbol), _volume(volume), _hash(pair_code_hash), _utc(now)
    if type(account_mode) is not str or account_mode not in {"demo", "real"}:
        raise ValueError("The account mode must be demo or real.")
    row = await pool.fetchrow(
        f"""
        INSERT INTO mt5_devices AS device
            (bot_id, device_id, symbol, account_mode, volume, pair_code_hash,
                pair_expires_at, last_seen_at, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $8, $8)
        ON CONFLICT (bot_id, device_id) DO UPDATE SET
            pair_code_hash = CASE WHEN device.owner_user_id IS NULL
                THEN EXCLUDED.pair_code_hash ELSE device.pair_code_hash END,
            pair_expires_at = CASE WHEN device.owner_user_id IS NULL
                AND device.pair_code_hash <> EXCLUDED.pair_code_hash
                THEN EXCLUDED.pair_expires_at ELSE device.pair_expires_at END,
            last_seen_at = EXCLUDED.last_seen_at, updated_at = EXCLUDED.updated_at
        WHERE device.symbol = EXCLUDED.symbol AND device.account_mode = EXCLUDED.account_mode
            AND device.volume = EXCLUDED.volume AND device.updated_at <= EXCLUDED.updated_at
        RETURNING {DEVICE_COLUMNS}
        """,
        bot_id, device_id, symbol, account_mode, volume, code_hash, now + PAIR_TTL, now,
    )
    if row is None:
        raise ValueError("The device configuration is immutable or the registration is stale.")
    return _device(row)


async def pair_device(pool, bot_id, code_hash, chat_id, user_id, now):
    row = await pool.fetchrow(
        f"""
        UPDATE mt5_devices SET owner_chat_id = $3, owner_user_id = $4,
            paired_at = $5, updated_at = $5
        WHERE bot_id = $1 AND pair_code_hash = $2 AND owner_user_id IS NULL
            AND pair_expires_at > $5 AND updated_at <= $5
        RETURNING {DEVICE_COLUMNS}
        """,
        _identifier(bot_id, positive=True), _hash(code_hash), _identifier(chat_id),
        _identifier(user_id, positive=True), _utc(now),
    )
    return _device(row)


async def get_device(pool, bot_id, device_id):
    row = await pool.fetchrow(
        f"SELECT {DEVICE_COLUMNS} FROM mt5_devices WHERE bot_id = $1 AND device_id = $2",
        _identifier(bot_id, positive=True), _uuid(device_id),
    )
    return _device(row)


async def list_paired_devices(pool, bot_id, limit=100):
    bot_id, limit = _identifier(bot_id, positive=True), _limit(limit)
    if limit == 0:
        return []
    rows = await pool.fetch(
        f"""SELECT {DEVICE_COLUMNS} FROM mt5_devices
        WHERE bot_id = $1 AND owner_user_id IS NOT NULL ORDER BY device_id LIMIT $2""",
        bot_id, limit,
    )
    return [_device(row) for row in rows]


async def heartbeat(pool, bot_id, device_id, now):
    return bool(await pool.fetchval(
        """UPDATE mt5_devices SET last_seen_at = $3, updated_at = GREATEST(updated_at, $3)
        WHERE bot_id = $1 AND device_id = $2 AND last_seen_at <= $3 RETURNING TRUE""",
        _identifier(bot_id, positive=True), _uuid(device_id), _utc(now),
    ))


async def subscription_active(pool, bot_id, chat_id):
    return bool(await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM market_subscriptions WHERE bot_id = $1 AND chat_id = $2 AND active)",
        _identifier(bot_id, positive=True), _identifier(chat_id),
    ))


async def create_offer(pool, bot_id, device_id, chat_id, user_id, signal_id, payload, now, expires_at):
    bot_id, device_id = _identifier(bot_id, positive=True), _uuid(device_id)
    chat_id, user_id = _identifier(chat_id), _identifier(user_id, positive=True)
    signal_id, encoded, now, expires_at = _signal(signal_id), _json_object(payload), _utc(now), _utc(expires_at)
    if not now < expires_at <= now + MAX_OFFER_TTL:
        raise ValueError("An offer must expire within fifteen minutes after creation.")
    row = await pool.fetchrow(
        """
        WITH stored AS (
            INSERT INTO mt5_trade_offers AS offer
                (id, bot_id, device_id, chat_id, user_id, signal_id, payload, created_at, expires_at, updated_at)
            SELECT $1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $8
            FROM mt5_devices AS device
            JOIN market_subscriptions AS subscription
                ON subscription.bot_id = device.bot_id AND subscription.chat_id = device.owner_chat_id
            WHERE device.bot_id = $2 AND device.device_id = $3
                AND device.owner_chat_id = $4 AND device.owner_user_id = $5 AND subscription.active
            ON CONFLICT (bot_id, device_id, chat_id, signal_id) DO UPDATE SET id = offer.id
            RETURNING offer.*
        )
        SELECT stored.*, device.symbol, device.account_mode, device.volume FROM stored
        JOIN mt5_devices AS device ON device.bot_id = stored.bot_id AND device.device_id = stored.device_id
        """,
        uuid4(), bot_id, device_id, chat_id, user_id, signal_id, encoded, now, expires_at,
    )
    if row is None:
        raise ValueError("An active subscription and matching paired device owner are required.")
    return _offer(row)


async def publish_offer(pool, bot_id, offer_id, message_id, now):
    return bool(await pool.fetchval(
        """
        UPDATE mt5_trade_offers AS offer SET status = 'offered', message_id = $3,
            published_at = $4, updated_at = $4
        FROM mt5_devices AS device, market_subscriptions AS subscription
        WHERE offer.bot_id = $1 AND offer.id = $2 AND offer.status = 'draft'
            AND offer.expires_at > $4 AND offer.created_at <= $4
            AND device.bot_id = offer.bot_id AND device.device_id = offer.device_id
            AND device.owner_chat_id = offer.chat_id AND device.owner_user_id = offer.user_id
            AND subscription.bot_id = offer.bot_id AND subscription.chat_id = offer.chat_id AND subscription.active
        RETURNING TRUE
        """,
        _identifier(bot_id, positive=True), _uuid(offer_id), _identifier(message_id, positive=True), _utc(now),
    ))


async def get_offer(pool, bot_id, offer_id):
    return _offer(await pool.fetchrow(
        OFFER_SELECT + " WHERE offer.bot_id = $1 AND offer.id = $2",
        _identifier(bot_id, positive=True), _uuid(offer_id),
    ))


async def decide(pool, bot_id, offer_id, chat_id, user_id, message_id, decision, now):
    if type(decision) is not str or decision not in {"accepted", "rejected"}:
        raise ValueError("The offer decision must be accepted or rejected.")
    row = await pool.fetchrow(
        """
        WITH decided AS (
            UPDATE mt5_trade_offers AS offer SET status = $6, decided_at = $7, updated_at = $7
            FROM mt5_devices AS device, market_subscriptions AS subscription
            WHERE offer.bot_id = $1 AND offer.id = $2 AND offer.chat_id = $3
                AND offer.user_id = $4 AND offer.message_id = $5 AND offer.status = 'offered'
                AND offer.expires_at > $7 AND offer.published_at <= $7
                AND device.bot_id = offer.bot_id AND device.device_id = offer.device_id
                AND device.owner_chat_id = offer.chat_id AND device.owner_user_id = offer.user_id
                AND subscription.bot_id = offer.bot_id AND subscription.chat_id = offer.chat_id AND subscription.active
            RETURNING offer.*
        )
        SELECT decided.*, device.symbol, device.account_mode, device.volume FROM decided
        JOIN mt5_devices AS device ON device.bot_id = decided.bot_id AND device.device_id = decided.device_id
        """,
        _identifier(bot_id, positive=True), _uuid(offer_id), _identifier(chat_id),
        _identifier(user_id, positive=True), _identifier(message_id, positive=True), decision, _utc(now),
    )
    return _offer(row)


async def claim_offer(pool, bot_id, device_id, now):
    bot_id, device_id, now = _identifier(bot_id, positive=True), _uuid(device_id), _utc(now)
    row = await pool.fetchrow(
        """
        WITH candidate AS (
            SELECT offer.id FROM mt5_trade_offers AS offer
            JOIN mt5_devices AS device ON device.bot_id = offer.bot_id AND device.device_id = offer.device_id
            JOIN market_subscriptions AS subscription
                ON subscription.bot_id = offer.bot_id AND subscription.chat_id = offer.chat_id
            WHERE offer.bot_id = $1 AND offer.device_id = $2 AND offer.status = 'accepted'
                AND offer.expires_at > $3 AND offer.decided_at <= $3
                AND device.owner_chat_id = offer.chat_id AND device.owner_user_id = offer.user_id
                AND device.last_seen_at >= $4 AND device.last_seen_at <= $3 AND subscription.active
            ORDER BY offer.decided_at, offer.id LIMIT 1 FOR UPDATE OF offer SKIP LOCKED
        ), claimed AS (
            UPDATE mt5_trade_offers AS offer SET status = 'executing', claim_id = $5,
                executing_at = $3, updated_at = $3
            FROM candidate WHERE offer.id = candidate.id AND offer.bot_id = $1 AND offer.status = 'accepted'
            RETURNING offer.*
        )
        SELECT claimed.*, device.symbol, device.account_mode, device.volume FROM claimed
        JOIN mt5_devices AS device ON device.bot_id = claimed.bot_id AND device.device_id = claimed.device_id
        """,
        bot_id, device_id, now, now - HEARTBEAT_TTL, uuid4(),
    )
    return _offer(row)


async def complete_offer(pool, bot_id, device_id, offer_id, claim_id, result, now):
    """Finalize once, or acknowledge an identical retry without rewriting it.

    A different claim, status, or canonical JSON result cannot replace a
    terminal outcome. The read-only retry path handles a lost HTTP response.
    """
    encoded = _json_object(result)
    status = result.get("status")
    if type(status) is not str or status not in RESULT_STATUSES:
        raise ValueError("An execution result must be filled, failed, or unknown.")
    row = await pool.fetchrow(
        """
        WITH completed AS (
            UPDATE mt5_trade_offers SET status = $5, result = $6::jsonb,
                completed_at = $7, updated_at = $7
            WHERE bot_id = $1 AND device_id = $2 AND id = $3 AND claim_id = $4
                AND status = 'executing' AND executing_at <= $7
            RETURNING *
        ), resolved AS (
            SELECT * FROM completed
            UNION ALL
            SELECT * FROM mt5_trade_offers
            WHERE bot_id = $1 AND device_id = $2 AND id = $3 AND claim_id = $4
                AND status = $5 AND status IN ('filled', 'failed', 'unknown') AND result = $6::jsonb
        )
        SELECT resolved.*, device.symbol, device.account_mode, device.volume FROM resolved
        JOIN mt5_devices AS device ON device.bot_id = resolved.bot_id AND device.device_id = resolved.device_id
        """,
        _identifier(bot_id, positive=True), _uuid(device_id), _uuid(offer_id), _uuid(claim_id), status, encoded, _utc(now),
    )
    return _offer(row)


async def list_notifications(pool, bot_id, limit=20):
    """Deliver final execution acknowledgments to the bound owner.

    Unsubscribing stops new offers, but an approved request still deserves its
    final result, including cancellation or expiry before a claim. Unclicked
    expired offers stay silent. Owner bindings remain mandatory.
    """
    bot_id, limit = _identifier(bot_id, positive=True), _limit(limit)
    if limit == 0:
        return []
    rows = await pool.fetch(
        OFFER_SELECT + """
        WHERE offer.bot_id = $1 AND NOT offer.notified
            AND (offer.status IN ('filled', 'failed', 'unknown')
                OR (offer.status IN ('expired', 'cancelled')
                    AND offer.decided_at IS NOT NULL AND offer.executing_at IS NULL))
            AND device.owner_chat_id = offer.chat_id AND device.owner_user_id = offer.user_id
        ORDER BY offer.updated_at, offer.id LIMIT $2
        """,
        bot_id, limit,
    )
    return [_offer(row) for row in rows]


async def mark_notified(pool, bot_id, offer_id, now):
    return bool(await pool.fetchval(
        """UPDATE mt5_trade_offers SET notified = TRUE, updated_at = GREATEST(updated_at, $3)
        WHERE bot_id = $1 AND id = $2 AND NOT notified
            AND (status IN ('filled', 'failed', 'unknown')
                OR (status IN ('expired', 'cancelled') AND decided_at IS NOT NULL AND executing_at IS NULL))
        RETURNING TRUE""",
        _identifier(bot_id, positive=True), _uuid(offer_id), _utc(now),
    ))


async def cancel_offers(pool, bot_id, chat_id, now):
    return int(await pool.fetchval(
        """WITH cancelled AS (
            UPDATE mt5_trade_offers SET status = 'cancelled', updated_at = $3
            WHERE bot_id = $1 AND chat_id = $2 AND status IN ('draft', 'offered', 'accepted')
                AND created_at <= $3 RETURNING id
        ) SELECT count(*) FROM cancelled""",
        _identifier(bot_id, positive=True), _identifier(chat_id), _utc(now),
    ) or 0)


async def expire_offers(pool, bot_id, now):
    now = _utc(now)
    timeout_result = _json_object({"status": "unknown", "reason": "execution_timeout"})
    return int(await pool.fetchval(
        """
        WITH expired AS (
            UPDATE mt5_trade_offers SET status = 'expired', updated_at = $2
            WHERE bot_id = $1 AND status IN ('draft', 'offered', 'accepted') AND expires_at <= $2 RETURNING id
        ), unknown AS (
            UPDATE mt5_trade_offers SET status = 'unknown', result = $4::jsonb,
                completed_at = $2, updated_at = $2
            WHERE bot_id = $1 AND status = 'executing' AND executing_at < $3 RETURNING id
        ) SELECT (SELECT count(*) FROM expired) + (SELECT count(*) FROM unknown)
        """,
        _identifier(bot_id, positive=True), now, now - EXECUTION_TIMEOUT, timeout_result,
    ) or 0)
