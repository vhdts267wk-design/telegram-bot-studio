"""Add durable opt-in market monitoring state.

Revision ID: 20261004_02
"""
from alembic import op


revision = "20261004_02"
down_revision = "20260629_01"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
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
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS market_subscriptions_due_idx
            ON market_subscriptions (bot_id, next_due) WHERE active
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS market_daily_usage (
            bot_id BIGINT NOT NULL,
            day DATE NOT NULL,
            requests INTEGER NOT NULL CHECK (requests >= 0),
            PRIMARY KEY (bot_id, day)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS market_cache (
            bot_id BIGINT NOT NULL,
            key TEXT NOT NULL,
            payload JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            source_time TIMESTAMPTZ,
            PRIMARY KEY (bot_id, key)
        )
        """
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS market_cache")
    op.execute("DROP TABLE IF EXISTS market_daily_usage")
    op.execute("DROP TABLE IF EXISTS market_subscriptions")
