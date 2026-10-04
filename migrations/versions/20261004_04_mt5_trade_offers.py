"""Add owner-bound MT5 offers and single-use execution claims."""

from alembic import op

revision = "20261004_04"
down_revision = "20261004_03"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
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
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS mt5_devices_owner_idx
            ON mt5_devices (bot_id, owner_chat_id) WHERE owner_user_id IS NOT NULL
        """
    )
    op.execute(
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
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS mt5_trade_offers_claim_idx
            ON mt5_trade_offers (bot_id, device_id, decided_at) WHERE status = 'accepted'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS mt5_trade_offers_notifications_idx
            ON mt5_trade_offers (bot_id, updated_at)
            WHERE status IN ('filled', 'failed', 'unknown') AND NOT notified
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS mt5_trade_offers_expiry_idx
            ON mt5_trade_offers (bot_id, expires_at)
            WHERE status IN ('draft', 'offered', 'accepted', 'executing')
        """
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS mt5_trade_offers")
    op.execute("DROP TABLE IF EXISTS mt5_devices")
