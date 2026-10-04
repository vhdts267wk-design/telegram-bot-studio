"""Add durable paper trades and pending fifteen-minute reviews.

Revision ID: 20261004_03
"""
from alembic import op


revision = "20261004_03"
down_revision = "20261004_02"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
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
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS paper_trades_open_idx
            ON paper_trades (bot_id, review_due, opened_at) WHERE status = 'open'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS paper_trades_reviews_idx
            ON paper_trades (bot_id, updated_at) WHERE status <> 'open' AND NOT reviewed_sent
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS paper_trades_recent_idx
            ON paper_trades (bot_id, chat_id, opened_at DESC)
        """
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS paper_trades")
