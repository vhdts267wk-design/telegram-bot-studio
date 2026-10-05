"""Add owner-bound manual MT5 ticket preparation without execution approval."""

from alembic import op

revision = "20261005_05"
down_revision = "20261004_04"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
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
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS mt5_manual_ticket_claim_idx
            ON mt5_manual_ticket_offers (bot_id, device_id, decided_at)
            WHERE status = 'requested'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS mt5_manual_ticket_preparing_idx
            ON mt5_manual_ticket_offers (bot_id, device_id) WHERE status = 'preparing'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS mt5_manual_ticket_notifications_idx
            ON mt5_manual_ticket_offers (bot_id, updated_at)
            WHERE status IN ('prepared', 'failed', 'unknown') AND NOT notified
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS mt5_manual_ticket_expiry_idx
            ON mt5_manual_ticket_offers (bot_id, expires_at)
            WHERE status IN ('draft', 'offered', 'requested', 'preparing')
        """
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS mt5_manual_ticket_offers")
