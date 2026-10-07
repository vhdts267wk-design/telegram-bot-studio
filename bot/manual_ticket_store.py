"""Owner-bound requests to prepare a visible ticket, never authorize execution.

The separate queue cannot be consumed by the automatic trade executor. Claims
are single use; an interrupted preparation becomes unknown and is not retried.
Existing device pairing and historical execution records remain unchanged.
"""

from datetime import timedelta
from uuid import uuid4

from bot.trade_store import (
    HEARTBEAT_TTL, _identifier, _json_object, _limit, _offer, _signal, _utc, _uuid,
)

MAX_OFFER_TTL = timedelta(minutes=5)
PREPARATION_TIMEOUT = timedelta(seconds=120)
RESULT_STATUSES = frozenset({"prepared", "failed"})

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mt5_manual_ticket_offers (
        id UUID PRIMARY KEY,
        bot_id BIGINT NOT NULL,
        device_id UUID NOT NULL,
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        signal_id TEXT NOT NULL,
        payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
        status TEXT NOT NULL DEFAULT 'draft' CHECK (
            status IN ('draft', 'offered', 'requested', 'preparing', 'prepared',
                'failed', 'rejected', 'expired', 'cancelled', 'unknown')
        ),
        message_id BIGINT,
        created_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL CHECK (
            expires_at > created_at AND expires_at <= created_at + INTERVAL '5 minutes'
        ),
        published_at TIMESTAMPTZ,
        decided_at TIMESTAMPTZ,
        preparing_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL,
        claim_id UUID,
        result JSONB CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
        notified BOOLEAN NOT NULL DEFAULT FALSE,
        UNIQUE (bot_id, device_id, chat_id, signal_id),
        UNIQUE (bot_id, chat_id, message_id),
        FOREIGN KEY (bot_id, device_id) REFERENCES mt5_devices (bot_id, device_id),
        CHECK ((message_id IS NULL) = (published_at IS NULL)),
        CHECK ((claim_id IS NULL) = (preparing_at IS NULL))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS mt5_manual_ticket_claim_idx
        ON mt5_manual_ticket_offers (bot_id, device_id, decided_at)
        WHERE status = 'requested'
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS mt5_manual_ticket_preparing_idx
        ON mt5_manual_ticket_offers (bot_id, device_id) WHERE status = 'preparing'
    """,
    """
    CREATE INDEX IF NOT EXISTS mt5_manual_ticket_notifications_idx
        ON mt5_manual_ticket_offers (bot_id, updated_at)
        WHERE status IN ('prepared', 'failed', 'unknown') AND NOT notified
    """,
    """
    CREATE INDEX IF NOT EXISTS mt5_manual_ticket_expiry_idx
        ON mt5_manual_ticket_offers (bot_id, expires_at)
        WHERE status IN ('draft', 'offered', 'requested', 'preparing')
    """,
)

OFFER_SELECT = """SELECT offer.*, device.symbol, device.account_mode, device.volume
    FROM mt5_manual_ticket_offers AS offer JOIN mt5_devices AS device
    ON device.bot_id = offer.bot_id AND device.device_id = offer.device_id"""


async def initialize_schema(pool):
    """Run after the shared device/subscription schema has been initialized."""
    async with pool.acquire() as connection:
        async with connection.transaction():
            for statement in SCHEMA_STATEMENTS:
                await connection.execute(statement)


async def create_offer(pool, bot_id, device_id, chat_id, user_id, signal_id, payload, now, expires_at):
    bot_id, device_id = _identifier(bot_id, positive=True), _uuid(device_id)
    chat_id, user_id = _identifier(chat_id), _identifier(user_id, positive=True)
    signal_id, encoded, now, expires_at = _signal(signal_id), _json_object(payload), _utc(now), _utc(expires_at)
    if chat_id != user_id:
        raise ValueError("Manual ticket preparation requires the owner's private chat.")
    if not now < expires_at <= now + MAX_OFFER_TTL:
        raise ValueError("A preparation offer must expire within five minutes after creation.")
    row = await pool.fetchrow(
        """
        WITH stored AS (
            INSERT INTO mt5_manual_ticket_offers AS offer
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
        UPDATE mt5_manual_ticket_offers AS offer SET status = 'offered', message_id = $3,
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


async def get_chart_offer(pool, bot_id, device_id, now):
    """Read the newest published proposal without claiming or expiring rows.

    Choose the latest publication before eligibility checks. A newer rejected
    or expired proposal must not make an older recommendation visible again.
    """
    now = _utc(now)
    row = await pool.fetchrow(
        """
        WITH latest AS (
            SELECT * FROM mt5_manual_ticket_offers
            WHERE bot_id = $1 AND device_id = $2 AND published_at IS NOT NULL AND message_id IS NOT NULL
            ORDER BY published_at DESC, created_at DESC, id DESC LIMIT 1
        )
        SELECT latest.*, device.symbol, device.account_mode, device.volume FROM latest
        JOIN mt5_devices AS device ON device.bot_id = latest.bot_id AND device.device_id = latest.device_id
        JOIN market_subscriptions AS subscription
            ON subscription.bot_id = latest.bot_id AND subscription.chat_id = latest.chat_id
        WHERE latest.status IN ('offered', 'requested', 'preparing', 'prepared')
            AND latest.created_at <= $3 AND latest.published_at <= $3 AND latest.updated_at <= $3
            AND latest.expires_at > $3 AND latest.expires_at <= latest.created_at + INTERVAL '5 minutes'
            AND device.owner_chat_id = latest.chat_id AND device.owner_user_id = latest.user_id
            AND latest.chat_id = latest.user_id AND subscription.active
            AND device.last_seen_at >= $4 AND device.last_seen_at <= $3
            AND (latest.status = 'offered' OR latest.decided_at <= $3)
            AND (latest.status != 'preparing' OR (latest.preparing_at >= $5 AND latest.preparing_at <= $3))
            AND (latest.status != 'prepared' OR (latest.result = '{"status":"prepared"}'::jsonb
                AND latest.completed_at <= $3))
        """,
        _identifier(bot_id, positive=True), _uuid(device_id), now, now - HEARTBEAT_TTL, now - PREPARATION_TIMEOUT,
    )
    return _offer(row)


async def decide(pool, bot_id, offer_id, chat_id, user_id, message_id, decision, now):
    if type(decision) is not str or decision not in {"requested", "rejected"}:
        raise ValueError("The preparation decision must be requested or rejected.")
    chat_id, user_id = _identifier(chat_id), _identifier(user_id, positive=True)
    if chat_id != user_id:
        raise ValueError("Manual ticket preparation requires the owner's private chat.")
    row = await pool.fetchrow(
        """
        WITH decided AS (
            UPDATE mt5_manual_ticket_offers AS offer SET status = $6, decided_at = $7, updated_at = $7
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
        _identifier(bot_id, positive=True), _uuid(offer_id), chat_id, user_id,
        _identifier(message_id, positive=True), decision, _utc(now),
    )
    return _offer(row)


async def claim_offer(pool, bot_id, device_id, now, *, qualification_id=None, strategy_fingerprint=None, proposal_context=None, signal_mode=None):
    """Reserve one preparation; never reclaim an interrupted or final request."""
    bot_id, device_id, now = _identifier(bot_id, positive=True), _uuid(device_id), _utc(now)
    from bot.strategy_evidence import hex_digest
    from bot import mtf_runtime
    experimental = signal_mode == "experimental_demo"
    if signal_mode not in (None, "qualified", "experimental_demo"):
        return None
    if experimental:
        if mtf_runtime.signal_mode() != "experimental_demo" or qualification_id not in (None, ""):
            return None
    elif not hex_digest(qualification_id):
        return None
    if not hex_digest(strategy_fingerprint):
        return None
    required_context = {"direction", "bar_time", "confirmation_bar_time", "direction_bar_time", "broker_fingerprint", "policy_id", "cost_context", "execution"}
    if type(proposal_context) is not dict or set(proposal_context) != required_context:
        return None
    if experimental:
        return await _claim_experimental_offer(pool, bot_id, device_id, now, strategy_fingerprint, proposal_context)
    context = _json_object(proposal_context)
    row = await pool.fetchrow(
        """
        WITH candidate AS (
            SELECT offer.id FROM mt5_manual_ticket_offers AS offer
            JOIN mt5_devices AS device ON device.bot_id = offer.bot_id AND device.device_id = offer.device_id
            JOIN market_subscriptions AS subscription
                ON subscription.bot_id = offer.bot_id AND subscription.chat_id = offer.chat_id
            WHERE offer.bot_id = $1 AND offer.device_id = $2 AND offer.status = 'requested'
                AND offer.payload->>'strategy_id' = 'mtf-ema-pullback-60m-v1'
                AND offer.payload->>'qualification_id' = $6
                AND offer.payload->>'strategy_fingerprint' = $7
                AND offer.payload->>'strategy_version' = '1'
                AND offer.payload->>'horizon_seconds' = '3600'
                AND offer.payload->>'display_timeframe' = 'M1'
                AND offer.payload @> $8::jsonb
                AND offer.expires_at > $3 AND offer.decided_at <= $3
                AND device.owner_chat_id = offer.chat_id AND device.owner_user_id = offer.user_id
                AND offer.chat_id = offer.user_id
                AND device.last_seen_at >= $4 AND device.last_seen_at <= $3 AND subscription.active
                AND NOT EXISTS (
                    SELECT 1 FROM mt5_manual_ticket_offers AS active
                    WHERE active.bot_id = $1 AND active.device_id = $2 AND active.status = 'preparing'
                )
            ORDER BY offer.decided_at, offer.id LIMIT 1 FOR UPDATE OF offer, device SKIP LOCKED
        ), claimed AS (
            UPDATE mt5_manual_ticket_offers AS offer SET status = 'preparing', claim_id = $5,
                preparing_at = $3, updated_at = $3
            FROM candidate WHERE offer.id = candidate.id AND offer.bot_id = $1 AND offer.status = 'requested'
            RETURNING offer.*
        )
        SELECT claimed.*, device.symbol, device.account_mode, device.volume FROM claimed
        JOIN mt5_devices AS device ON device.bot_id = claimed.bot_id AND device.device_id = claimed.device_id
        """,
        bot_id, device_id, now, now - HEARTBEAT_TTL, uuid4(), qualification_id, strategy_fingerprint, context,
    )
    return _offer(row)


async def _claim_experimental_offer(pool, bot_id, device_id, now, fingerprint, proposal_context):
    """Claim only the server-selected experimental profile, under the same lock."""
    from bot import multi_timeframe
    if proposal_context.get("policy_id") != multi_timeframe.EXPERIMENTAL_POLICY_ID:
        return None
    costs = proposal_context.get("cost_context")
    if type(costs) is not dict or not {"loss_cash_per_price_unit", "profit_cash_per_price_unit"} <= set(costs):
        return None
    context = dict(proposal_context)
    context["cost_context"] = {key: costs[key] for key in ("loss_cash_per_price_unit", "profit_cash_per_price_unit")}
    row = await pool.fetchrow(
        """
        WITH candidate AS (
            SELECT offer.id FROM mt5_manual_ticket_offers AS offer
            JOIN mt5_devices AS device ON device.bot_id = offer.bot_id AND device.device_id = offer.device_id
            JOIN market_subscriptions AS subscription
                ON subscription.bot_id = offer.bot_id AND subscription.chat_id = offer.chat_id
            WHERE offer.bot_id = $1 AND offer.device_id = $2 AND offer.status = 'requested'
                AND offer.payload->>'strategy_id' = 'mtf-ema-pullback-60m-demo-v2'
                AND offer.payload->>'strategy_version' = '2'
                AND offer.payload->>'policy_id' = 'mtf-manual-demo-estimated-cost-risk-v2'
                AND offer.payload->>'signal_mode' = 'experimental_demo'
                AND offer.payload->'provisional' = 'true'::jsonb
                AND offer.payload->>'entry_window_seconds' = '30'
                AND COALESCE(offer.payload->>'qualification_id', '') = ''
                AND NOT (offer.payload ? 'evidence_metrics')
                AND offer.payload->>'strategy_fingerprint' = $6
                AND offer.payload->>'horizon_seconds' = '3600'
                AND offer.payload->>'display_timeframe' = 'M1'
                AND offer.payload->>'workflow' = 'manual_ticket'
                AND offer.payload->>'account_mode' = 'demo' AND offer.payload->>'volume' = '0.01'
                AND device.account_mode = 'demo' AND device.volume = 0.01
                AND offer.payload @> $7::jsonb
                AND offer.expires_at > $3 AND offer.decided_at <= $3
                AND offer.expires_at <= (offer.payload->>'bar_time')::timestamptz + INTERVAL '90 seconds'
                AND (offer.payload->>'bar_time')::timestamptz + INTERVAL '60 seconds' <= $3
                AND (offer.payload->>'bar_time')::timestamptz + INTERVAL '90 seconds' >= $3
                AND device.owner_chat_id = offer.chat_id AND device.owner_user_id = offer.user_id
                AND offer.chat_id = offer.user_id
                AND device.last_seen_at >= $4 AND device.last_seen_at <= $3 AND subscription.active
                AND NOT EXISTS (
                    SELECT 1 FROM mt5_manual_ticket_offers AS active
                    WHERE active.bot_id = $1 AND active.device_id = $2 AND active.status = 'preparing'
                )
            ORDER BY offer.decided_at, offer.id LIMIT 1 FOR UPDATE OF offer, device SKIP LOCKED
        ), claimed AS (
            UPDATE mt5_manual_ticket_offers AS offer SET status = 'preparing', claim_id = $5,
                preparing_at = $3, updated_at = $3
            FROM candidate WHERE offer.id = candidate.id AND offer.bot_id = $1 AND offer.status = 'requested'
            RETURNING offer.*
        )
        SELECT claimed.*, device.symbol, device.account_mode, device.volume FROM claimed
        JOIN mt5_devices AS device ON device.bot_id = claimed.bot_id AND device.device_id = claimed.device_id
        """,
        bot_id, device_id, now, now - HEARTBEAT_TTL, uuid4(), fingerprint, _json_object(context),
    )
    return _offer(row)


async def complete_offer(pool, bot_id, device_id, offer_id, claim_id, result, now):
    """Acknowledge preparation once; an identical HTTP retry is read only."""
    if (
        type(result) is not dict or set(result) != {"status"}
        or type(result.get("status")) is not str or result["status"] not in RESULT_STATUSES
    ):
        raise ValueError("A manual result must contain only prepared or failed status.")
    encoded, status = _json_object(result), result["status"]
    row = await pool.fetchrow(
        """
        WITH completed AS (
            UPDATE mt5_manual_ticket_offers AS offer SET status = $5, result = $6::jsonb,
                completed_at = $7, updated_at = $7
            FROM mt5_devices AS owner
            WHERE offer.bot_id = $1 AND offer.device_id = $2 AND offer.id = $3 AND offer.claim_id = $4
                AND offer.status = 'preparing' AND offer.preparing_at <= $7
                AND ($5 != 'prepared' OR (offer.expires_at > $7 AND offer.preparing_at >= $8))
                AND owner.bot_id = offer.bot_id AND owner.device_id = offer.device_id
                AND owner.owner_chat_id = offer.chat_id AND owner.owner_user_id = offer.user_id
            RETURNING offer.*
        ), resolved AS (
            SELECT * FROM completed
            UNION ALL
            SELECT * FROM mt5_manual_ticket_offers
            WHERE bot_id = $1 AND device_id = $2 AND id = $3 AND claim_id = $4
                AND status = $5 AND status IN ('prepared', 'failed') AND result = $6::jsonb
        )
        SELECT resolved.*, device.symbol, device.account_mode, device.volume FROM resolved
        JOIN mt5_devices AS device ON device.bot_id = resolved.bot_id AND device.device_id = resolved.device_id
        WHERE device.owner_chat_id = resolved.chat_id AND device.owner_user_id = resolved.user_id
        """,
        _identifier(bot_id, positive=True), _uuid(device_id), _uuid(offer_id), _uuid(claim_id),
        status, encoded, _utc(now), _utc(now) - PREPARATION_TIMEOUT,
    )
    return _offer(row)


async def list_notifications(pool, bot_id, limit=20):
    bot_id, limit = _identifier(bot_id, positive=True), _limit(limit)
    if limit == 0:
        return []
    rows = await pool.fetch(
        OFFER_SELECT + """
        WHERE offer.bot_id = $1 AND NOT offer.notified
            AND (offer.status IN ('prepared', 'failed', 'unknown')
                OR (offer.status IN ('expired', 'cancelled')
                    AND offer.decided_at IS NOT NULL AND offer.preparing_at IS NULL))
            AND device.owner_chat_id = offer.chat_id AND device.owner_user_id = offer.user_id
        ORDER BY offer.updated_at, offer.id LIMIT $2
        """,
        bot_id, limit,
    )
    return [_offer(row) for row in rows]


async def mark_notified(pool, bot_id, offer_id, now):
    return bool(await pool.fetchval(
        """UPDATE mt5_manual_ticket_offers SET notified = TRUE, updated_at = GREATEST(updated_at, $3)
        WHERE bot_id = $1 AND id = $2 AND NOT notified
            AND (status IN ('prepared', 'failed', 'unknown')
                OR (status IN ('expired', 'cancelled') AND decided_at IS NOT NULL AND preparing_at IS NULL))
        RETURNING TRUE""",
        _identifier(bot_id, positive=True), _uuid(offer_id), _utc(now),
    ))


async def cancel_offers(pool, bot_id, chat_id, now):
    """Stop pending requests; a ticket already being prepared needs its result."""
    return int(await pool.fetchval(
        """WITH cancelled AS (
            UPDATE mt5_manual_ticket_offers SET status = 'cancelled', updated_at = $3
            WHERE bot_id = $1 AND chat_id = $2 AND status IN ('draft', 'offered', 'requested')
                AND created_at <= $3 RETURNING id
        ) SELECT count(*) FROM cancelled""",
        _identifier(bot_id, positive=True), _identifier(chat_id), _utc(now),
    ) or 0)


async def expire_offers(pool, bot_id, now):
    now = _utc(now)
    timeout_result = _json_object({"status": "unknown", "reason": "preparation_interrupted"})
    return int(await pool.fetchval(
        """
        WITH expired AS (
            UPDATE mt5_manual_ticket_offers SET status = 'expired', updated_at = $2
            WHERE bot_id = $1 AND status IN ('draft', 'offered', 'requested') AND expires_at <= $2 RETURNING id
        ), unknown AS (
            UPDATE mt5_manual_ticket_offers SET status = 'unknown', result = $4::jsonb,
                completed_at = $2, updated_at = $2
            WHERE bot_id = $1 AND status = 'preparing'
                AND (preparing_at < $3 OR expires_at <= $2) RETURNING id
        ) SELECT (SELECT count(*) FROM expired) + (SELECT count(*) FROM unknown)
        """,
        _identifier(bot_id, positive=True), now, now - PREPARATION_TIMEOUT, timeout_result,
    ) or 0)
