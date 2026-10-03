import asyncio
import logging
import os

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool

from bot.logging_utils import configure_logging


config = context.config
database_url = os.getenv("DATABASE_URL", "")
if database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
elif database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql+asyncpg://", 1)
logger = logging.getLogger(__name__)


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations():
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations():
    # start.sh runs Alembic before bot.main, so install redaction here as well.
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    try:
        # Alembic's ConfigParser interprets %, including valid URL escapes in
        # database passwords. Escape it for config storage; retrieval restores
        # the original URL before SQLAlchemy parses it.
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
        if context.is_offline_mode():
            context.configure(url=database_url, literal_binds=True)
            with context.begin_transaction():
                context.run_migrations()
        else:
            asyncio.run(run_async_migrations())
    except Exception as exc:
        # Driver/configuration errors may include DATABASE_URL or its password.
        # Exit unsuccessfully without an uncaught traceback containing either.
        logger.error(
            "Database migration failed (%s). Check DATABASE_URL and PostgreSQL availability.",
            type(exc).__name__,
        )
        raise SystemExit(1) from None


run_migrations()
