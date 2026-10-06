"""Durable market subscriptions, delivery leases, usage limits, and snapshots."""

from datetime import datetime, timedelta, timezone
import json
from uuid import UUID


DELIVERY_INTERVAL = timedelta(minutes=15)
MTF_DELIVERY_INTERVAL = timedelta(minutes=1)
DELIVERY_LEASE = timedelta(minutes=5)
MAX_DELIVERY_BATCH = 100

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS market_subscriptions (
        bot_id BIGINT NOT NULL,
        chat_id BIGINT NOT NULL,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        next_due TIMESTAMPTZ NOT NULL,
        last_sent TIMESTAMPTZ,
        lease_id UUID,
        leased_until TIMESTAMPTZ,
        PRIMARY KEY (bot_id, chat_id),
        CHECK ((lease_id IS NULL) = (leased_until IS NULL))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS market_subscriptions_due_idx
        ON market_subscriptions (bot_id, next_due) WHERE active
    """,
    """
    CREATE TABLE IF NOT EXISTS market_daily_usage (
        bot_id BIGINT NOT NULL,
        day DATE NOT NULL,
        requests INTEGER NOT NULL CHECK (requests >= 0),
        PRIMARY KEY (bot_id, day)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS market_cache (
        bot_id BIGINT NOT NULL,
        key TEXT NOT NULL,
        payload JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        source_time TIMESTAMPTZ,
        PRIMARY KEY (bot_id, key)
    )
    """,
)


def _identifier(value: int) -> int:
    if type(value) is not int or not -(2**63) <= value < 2**63:
        raise ValueError("A PostgreSQL-compatible integer identifier is required.")
    return value


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required.")
    return value.astimezone(timezone.utc)


def _limit(value: int, *, maximum: int | None = None) -> int:
    if type(value) is not int or value < 0 or (maximum is not None and value > maximum):
        raise ValueError("The limit must be a nonnegative integer within the supported bound.")
    return value


def _cache_key(value: str) -> str:
    if type(value) is not str or not value:
        raise ValueError("A nonempty cache key is required.")
    return value


def _lease_id(value: UUID | str) -> UUID:
    if isinstance(value, UUID):
        return value
    if type(value) is str:
        try:
            return UUID(value)
        except ValueError:
            pass
    raise ValueError("A valid delivery lease identifier is required.")


async def initialize_schema(pool) -> None:
    """Ensure the feature schema exists, atomically even outside start.sh."""
    async with pool.acquire() as connection:
        async with connection.transaction():
            for statement in SCHEMA_STATEMENTS:
                await connection.execute(statement)


def _delivery_interval(value):
    if type(value) is not timedelta or value not in (DELIVERY_INTERVAL, MTF_DELIVERY_INTERVAL):
        raise ValueError("Unsupported market delivery interval")
    return value


async def enable_subscription(pool, bot_id: int, chat_id: int, now: datetime, *, interval=DELIVERY_INTERVAL) -> None:
    """Opt in; repeated /watch calls preserve an active subscription's due time."""
    await pool.execute(
        """
        INSERT INTO market_subscriptions AS subscription
            (bot_id, chat_id, active, next_due)
        VALUES ($1, $2, TRUE, $3)
        ON CONFLICT (bot_id, chat_id) DO UPDATE SET
            active = TRUE,
            next_due = CASE WHEN subscription.active
                THEN subscription.next_due ELSE EXCLUDED.next_due END,
            lease_id = CASE WHEN subscription.active
                THEN subscription.lease_id ELSE NULL END,
            leased_until = CASE WHEN subscription.active
                THEN subscription.leased_until ELSE NULL END
        """,
        _identifier(bot_id),
        _identifier(chat_id),
        _utc(now) + _delivery_interval(interval),
    )


async def disable_subscription(pool, bot_id: int, chat_id: int) -> bool:
    """Deactivate and invalidate any outstanding delivery claim."""
    result = await pool.fetchval(
        """
        UPDATE market_subscriptions SET
            active = FALSE, lease_id = NULL, leased_until = NULL
        WHERE bot_id = $1 AND chat_id = $2 AND active
        RETURNING TRUE
        """,
        _identifier(bot_id),
        _identifier(chat_id),
    )
    return bool(result)


async def has_subscriptions(pool, bot_id: int) -> bool:
    """Whether this bot has any active opt-ins, independent of other bots."""
    result = await pool.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM market_subscriptions WHERE bot_id = $1 AND active
        )
        """,
        _identifier(bot_id),
    )
    return bool(result)


async def delivery_active(
    pool, bot_id: int, chat_id: int, lease_id: UUID | str, now: datetime
) -> bool:
    """Recheck consent and the current unexpired lease immediately before sending."""
    result = await pool.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM market_subscriptions
            WHERE bot_id = $1 AND chat_id = $2 AND active
                AND lease_id = $3 AND leased_until > $4
        )
        """,
        _identifier(bot_id),
        _identifier(chat_id),
        _lease_id(lease_id),
        _utc(now),
    )
    return bool(result)


async def claim_due(
    pool, bot_id: int, now: datetime, limit: int = 20
) -> list[dict]:
    """Claim due subscriptions with one atomic statement and expiring UUID leases."""
    bot_id = _identifier(bot_id)
    now = _utc(now)
    limit = _limit(limit, maximum=MAX_DELIVERY_BATCH)
    if limit == 0:
        return []
    rows = await pool.fetch(
        """
        WITH due AS (
            SELECT bot_id, chat_id
            FROM market_subscriptions
            WHERE bot_id = $1 AND active AND next_due <= $2
                AND (leased_until IS NULL OR leased_until <= $2)
            ORDER BY next_due, chat_id
            LIMIT $3
            FOR UPDATE SKIP LOCKED
        )
        UPDATE market_subscriptions AS subscription SET
            lease_id = gen_random_uuid(), leased_until = $4
        FROM due
        WHERE subscription.bot_id = due.bot_id
            AND subscription.chat_id = due.chat_id
        RETURNING subscription.*
        """,
        bot_id,
        now,
        limit,
        now + DELIVERY_LEASE,
    )
    return [dict(row) for row in rows]


async def mark_delivered(
    pool, bot_id: int, chat_id: int, lease_id: UUID | str, now: datetime, *, interval=DELIVERY_INTERVAL
) -> bool:
    """Acknowledge only the current lease and schedule the configured next check."""
    now = _utc(now)
    result = await pool.fetchval(
        """
        UPDATE market_subscriptions SET
            last_sent = $4, next_due = $5, lease_id = NULL, leased_until = NULL
        WHERE bot_id = $1 AND chat_id = $2 AND active
            AND lease_id = $3 AND leased_until > $4
        RETURNING TRUE
        """,
        _identifier(bot_id),
        _identifier(chat_id),
        _lease_id(lease_id),
        now,
        now + _delivery_interval(interval),
    )
    return bool(result)


async def release_delivery(
    pool, bot_id: int, chat_id: int, lease_id: UUID | str, now: datetime
) -> None:
    """Retry a failed current claim after five minutes, preserving last_sent."""
    now = _utc(now)
    await pool.execute(
        """
        UPDATE market_subscriptions SET
            next_due = $5, lease_id = NULL, leased_until = NULL
        WHERE bot_id = $1 AND chat_id = $2 AND active
            AND lease_id = $3 AND leased_until > $4
        """,
        _identifier(bot_id),
        _identifier(chat_id),
        _lease_id(lease_id),
        now,
        now + DELIVERY_LEASE,
    )


async def claim_news_request(
    pool, bot_id: int, now: datetime, limit: int = 96
) -> bool:
    """Reserve a request under the durable, per-bot UTC-day cap before spending."""
    bot_id = _identifier(bot_id)
    day = _utc(now).date()
    limit = _limit(limit)
    if limit == 0:
        return False
    result = await pool.fetchval(
        """
        INSERT INTO market_daily_usage AS usage (bot_id, day, requests)
        VALUES ($1, $2, 1)
        ON CONFLICT (bot_id, day) DO UPDATE SET requests = usage.requests + 1
            WHERE usage.requests < $3
        RETURNING TRUE
        """,
        bot_id,
        day,
        limit,
    )
    return bool(result)


async def get_cache(pool, bot_id: int, key: str) -> dict | None:
    """Read a stored snapshot; asyncpg returns JSONB as text by default."""
    row = await pool.fetchrow(
        """
        SELECT payload, updated_at FROM market_cache
        WHERE bot_id = $1 AND key = $2
        """,
        _identifier(bot_id),
        _cache_key(key),
    )
    if row is None:
        return None
    payload = row["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return None
    if not isinstance(payload, dict):
        return None
    return {"payload": payload, "updated_at": _utc(row["updated_at"])}


async def save_cache(
    pool, bot_id: int, key: str, payload: dict, now: datetime
) -> None:
    """Upsert JSON, preserving fetch time and refusing an older concurrent snapshot."""
    if not isinstance(payload, dict):
        raise ValueError("The cache payload must be a JSON object.")
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    await pool.execute(
        """
        INSERT INTO market_cache (bot_id, key, payload, updated_at)
        VALUES ($1, $2, $3::jsonb, $4)
        ON CONFLICT (bot_id, key) DO UPDATE SET
            payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at
            WHERE market_cache.updated_at <= EXCLUDED.updated_at
        """,
        _identifier(bot_id),
        _cache_key(key),
        serialized,
        _utc(now),
    )


async def save_feed_cache(
    pool, bot_id: int, key: str, payload: dict, now: datetime, source_time: datetime
) -> bool:
    """Accept a quote only when provider time and receive time do not regress.

    Equal provider timestamps can refresh receipt metadata while the payload's
    original as-of timestamp remains supplied by the provider.
    """
    if not isinstance(payload, dict):
        raise ValueError("The cache payload must be a JSON object.")
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    result = await pool.fetchval(
        """
        INSERT INTO market_cache AS cache
            (bot_id, key, payload, updated_at, source_time)
        VALUES ($1, $2, $3::jsonb, $4, $5)
        ON CONFLICT (bot_id, key) DO UPDATE SET
            payload = EXCLUDED.payload,
            updated_at = EXCLUDED.updated_at,
            source_time = EXCLUDED.source_time
        WHERE (cache.source_time IS NULL OR cache.source_time <= EXCLUDED.source_time)
            AND cache.updated_at <= EXCLUDED.updated_at
        RETURNING TRUE
        """,
        _identifier(bot_id),
        _cache_key(key),
        serialized,
        _utc(now),
        _utc(source_time),
    )
    return bool(result)
