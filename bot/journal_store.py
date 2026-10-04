"""Durable, scoped paper-trade records and pending review acknowledgments.

Opening fields are immutable. Observation updates can compare the database
timestamp supplied by ``list_open`` so concurrent workers cannot replace a
newer observation or reopen a completed trade.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json


MAX_QUERY_LIMIT = 100
STATUSES = frozenset({"open", "target_observed", "stop_observed", "expired", "inconclusive"})

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS paper_trades (
        bot_id BIGINT NOT NULL,
        chat_id BIGINT NOT NULL,
        signal_id TEXT NOT NULL,
        payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
        status TEXT NOT NULL CHECK (
            status IN ('open', 'target_observed', 'stop_observed', 'expired', 'inconclusive')
        ),
        opened_at TIMESTAMPTZ NOT NULL,
        review_due TIMESTAMPTZ NOT NULL CHECK (review_due >= opened_at),
        updated_at TIMESTAMPTZ NOT NULL,
        reviewed_sent BOOLEAN NOT NULL DEFAULT FALSE,
        PRIMARY KEY (bot_id, chat_id, signal_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_trades_open_idx
        ON paper_trades (bot_id, review_due, opened_at) WHERE status = 'open'
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_trades_reviews_idx
        ON paper_trades (bot_id, updated_at) WHERE status <> 'open' AND NOT reviewed_sent
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_trades_recent_idx
        ON paper_trades (bot_id, chat_id, opened_at DESC)
    """,
)


def _identifier(value: int) -> int:
    if type(value) is not int or not -(2**63) <= value < 2**63:
        raise ValueError("A PostgreSQL-compatible integer identifier is required.")
    return value


def _signal_id(value: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > 512 or "\x00" in value:
        raise ValueError("A nonempty signal identifier of at most 512 characters is required.")
    return value


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required.")
    return value.astimezone(timezone.utc)


def _timestamp(value: str) -> datetime:
    if type(value) is not str:
        raise ValueError("A timezone-aware ISO timestamp is required in the trade payload.")
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (ValueError, OverflowError) as error:
        raise ValueError("A timezone-aware ISO timestamp is required in the trade payload.") from error


def _limit(value: int) -> int:
    if type(value) is not int or not 0 <= value <= MAX_QUERY_LIMIT:
        raise ValueError("The query limit must be an integer between 0 and 100.")
    return value


def _trade(trade: dict) -> tuple[str, str, str, datetime, datetime]:
    if not isinstance(trade, dict):
        raise ValueError("The paper trade must be a JSON object.")
    signal_id = _signal_id(trade.get("id"))
    status = trade.get("status")
    if type(status) is not str or status not in STATUSES:
        raise ValueError("An accepted paper-trade status is required.")
    opened_at, review_due = _timestamp(trade.get("opened_at")), _timestamp(trade.get("deadline"))
    if review_due < opened_at:
        raise ValueError("The review deadline must not precede the opening timestamp.")
    try:
        encoded = json.dumps(trade, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("The trade must contain only finite JSON values.") from error
    return encoded, signal_id, status, opened_at, review_due


def _reject_constant(value: str):
    raise ValueError(f"Non-finite JSON constant: {value}")


def _decode(payload) -> dict | None:
    try:
        if isinstance(payload, dict):
            # Detach JSON objects supplied by a configured asyncpg codec.
            payload = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        decoded = json.loads(payload, parse_constant=_reject_constant)
    except (TypeError, ValueError, OverflowError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _rows(rows) -> list[dict]:
    result = []
    for row in rows:
        payload = _decode(row["payload"])
        if payload is None:
            continue
        result.append({
            "chat_id": row["chat_id"], "signal_id": row["signal_id"],
            "payload": payload, "updated_at": _utc(row["updated_at"]),
        })
    return result


async def initialize_schema(pool) -> None:
    """Ensure the feature schema exists as a single transaction."""
    async with pool.acquire() as connection:
        async with connection.transaction():
            for statement in SCHEMA_STATEMENTS:
                await connection.execute(statement)


async def open_trade(pool, bot_id: int, chat_id: int, trade: dict, now: datetime) -> bool:
    """Insert once per bot/chat/setup; repeated signals never rewrite levels."""
    bot_id, chat_id, now = _identifier(bot_id), _identifier(chat_id), _utc(now)
    encoded, signal_id, status, opened_at, review_due = _trade(trade)
    if status != "open" or opened_at > now:
        raise ValueError("A new paper trade must be open and cannot start in the future.")
    result = await pool.fetchval(
        """
        INSERT INTO paper_trades
            (bot_id, chat_id, signal_id, payload, status, opened_at, review_due, updated_at)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8)
        ON CONFLICT (bot_id, chat_id, signal_id) DO NOTHING
        RETURNING TRUE
        """,
        bot_id, chat_id, signal_id, encoded, status, opened_at, review_due, now,
    )
    return bool(result)


async def list_open(pool, bot_id: int, limit: int = 100) -> list[dict]:
    """Read a bounded batch of this bot's oldest pending observations."""
    bot_id, limit = _identifier(bot_id), _limit(limit)
    if limit == 0:
        return []
    rows = await pool.fetch(
        """
        SELECT chat_id, signal_id, payload, updated_at FROM paper_trades
        WHERE bot_id = $1 AND status = 'open'
        ORDER BY review_due, opened_at, chat_id, signal_id
        LIMIT $2
        """,
        bot_id, limit,
    )
    return _rows(rows)


async def update_trade(
    pool, bot_id: int, chat_id: int, signal_id: str, trade: dict, now: datetime,
    *, expected_updated_at: datetime | None = None,
) -> bool:
    """Update only an open, unchanged record; opening fields stay immutable.

    Pass ``expected_updated_at`` from ``list_open`` for compare-and-set updates.
    Every accepted update advances the version even when clocks have equal
    microseconds, preventing the same expected version from succeeding twice.
    Completed trades cannot be changed or reopened.
    """
    bot_id, chat_id, signal_id = _identifier(bot_id), _identifier(chat_id), _signal_id(signal_id)
    now = _utc(now)
    expected = None if expected_updated_at is None else _utc(expected_updated_at)
    encoded, payload_id, status, opened_at, review_due = _trade(trade)
    if payload_id != signal_id:
        raise ValueError("The payload identifier must match the record identifier.")
    result = await pool.fetchval(
        """
        UPDATE paper_trades SET
            payload = $4::jsonb, status = $5,
            updated_at = GREATEST($8, updated_at + INTERVAL '1 microsecond')
        WHERE bot_id = $1 AND chat_id = $2 AND signal_id = $3 AND status = 'open'
            AND opened_at = $6 AND review_due = $7 AND updated_at <= $8
            AND ($9::timestamptz IS NULL OR updated_at = $9)
            AND payload -> 'id' IS NOT DISTINCT FROM $4::jsonb -> 'id'
            AND payload -> 'source_identity' IS NOT DISTINCT FROM $4::jsonb -> 'source_identity'
            AND payload -> 'strategy_id' IS NOT DISTINCT FROM $4::jsonb -> 'strategy_id'
            AND payload -> 'signal_bar_time' IS NOT DISTINCT FROM $4::jsonb -> 'signal_bar_time'
            AND payload -> 'direction' IS NOT DISTINCT FROM $4::jsonb -> 'direction'
            AND payload -> 'entry' IS NOT DISTINCT FROM $4::jsonb -> 'entry'
            AND payload -> 'stop' IS NOT DISTINCT FROM $4::jsonb -> 'stop'
            AND payload -> 'target' IS NOT DISTINCT FROM $4::jsonb -> 'target'
            AND payload -> 'opened_at' IS NOT DISTINCT FROM $4::jsonb -> 'opened_at'
            AND payload -> 'deadline' IS NOT DISTINCT FROM $4::jsonb -> 'deadline'
            AND payload -> 'duration_minutes' IS NOT DISTINCT FROM $4::jsonb -> 'duration_minutes'
        RETURNING TRUE
        """,
        bot_id, chat_id, signal_id, encoded, status, opened_at, review_due, now, expected,
    )
    return bool(result)


async def list_reviews(pool, bot_id: int, limit: int = 100) -> list[dict]:
    """Read completed, unacknowledged reviews for currently active opt-ins."""
    bot_id, limit = _identifier(bot_id), _limit(limit)
    if limit == 0:
        return []
    rows = await pool.fetch(
        """
        SELECT trade.chat_id, trade.signal_id, trade.payload, trade.updated_at
        FROM paper_trades AS trade
        JOIN market_subscriptions AS subscription
            ON subscription.bot_id = trade.bot_id AND subscription.chat_id = trade.chat_id
        WHERE trade.bot_id = $1 AND trade.status <> 'open' AND NOT trade.reviewed_sent
            AND subscription.active
        ORDER BY trade.updated_at, trade.chat_id, trade.signal_id
        LIMIT $2
        """,
        bot_id, limit,
    )
    return _rows(rows)


async def mark_review_sent(
    pool, bot_id: int, chat_id: int, signal_id: str, now: datetime,
) -> bool:
    """Acknowledge successful delivery once, scoped to this exact paper trade."""
    result = await pool.fetchval(
        """
        UPDATE paper_trades SET reviewed_sent = TRUE, updated_at = GREATEST(updated_at, $4)
        WHERE bot_id = $1 AND chat_id = $2 AND signal_id = $3
            AND status <> 'open' AND NOT reviewed_sent
        RETURNING TRUE
        """,
        _identifier(bot_id), _identifier(chat_id), _signal_id(signal_id), _utc(now),
    )
    return bool(result)


async def review_active(pool, bot_id: int, chat_id: int, signal_id: str) -> bool:
    """Recheck a pending review and consent immediately before sending it."""
    result = await pool.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM paper_trades AS trade
            JOIN market_subscriptions AS subscription
                ON subscription.bot_id = trade.bot_id AND subscription.chat_id = trade.chat_id
            WHERE trade.bot_id = $1 AND trade.chat_id = $2 AND trade.signal_id = $3
                AND trade.status <> 'open' AND NOT trade.reviewed_sent AND subscription.active
        )
        """,
        _identifier(bot_id), _identifier(chat_id), _signal_id(signal_id),
    )
    return bool(result)


async def recent_trades(pool, bot_id: int, chat_id: int, limit: int = 5) -> list[dict]:
    """Read a bounded chat history without sharing another chat's records."""
    bot_id, chat_id, limit = _identifier(bot_id), _identifier(chat_id), _limit(limit)
    if limit == 0:
        return []
    rows = await pool.fetch(
        """
        SELECT payload FROM paper_trades WHERE bot_id = $1 AND chat_id = $2
        ORDER BY opened_at DESC, signal_id DESC
        LIMIT $3
        """,
        bot_id, chat_id, limit,
    )
    return [payload for row in rows if (payload := _decode(row["payload"])) is not None]
