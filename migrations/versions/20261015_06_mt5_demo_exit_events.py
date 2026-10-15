"""Persist owner-bound Demo position exit guard notifications."""

from alembic import op

revision = "20261015_06"
down_revision = "20261005_05"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE IF NOT EXISTS mt5_demo_exit_events (
        event_id UUID PRIMARY KEY,
        offer_id UUID NOT NULL REFERENCES mt5_trade_offers(id),
        bot_id BIGINT NOT NULL,
        device_id UUID NOT NULL,
        claim_id UUID NOT NULL,
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        event JSONB NOT NULL CHECK (jsonb_typeof(event) = 'object'),
        created_at TIMESTAMPTZ NOT NULL,
        notified BOOLEAN NOT NULL DEFAULT FALSE,
        notified_at TIMESTAMPTZ,
        FOREIGN KEY (bot_id, device_id) REFERENCES mt5_devices(bot_id, device_id)
    )""")
    op.execute("""CREATE INDEX IF NOT EXISTS mt5_demo_exit_events_notifications_idx
        ON mt5_demo_exit_events(bot_id, created_at, event_id) WHERE NOT notified""")


def downgrade():
    op.execute("DROP TABLE IF EXISTS mt5_demo_exit_events")
