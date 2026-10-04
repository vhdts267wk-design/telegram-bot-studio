"""Entrypoint for the Telegram bot (and the optional admin panel)."""

import asyncio
import logging

import uvicorn
from telegram import Update
from telegram.error import InvalidToken
from telegram.ext import Application, ApplicationBuilder

from bot import commands, db, market_monitor
from bot.config import Settings
from bot.logging_utils import configure_logging
from bot.handlers import (
    DB_KEY,
    register_handlers,
    set_bot_commands,
)
from bot.panel.app import create_app


logger = logging.getLogger(__name__)


async def _connect_database(url: str, *, required: bool):
    """Connect to Postgres, only degrading gracefully for a bot-only process.

    A panel with no database is unusable, so fail startup instead of serving a
    permanently disconnected UI after a transient or configuration error.
    """
    if not url:
        logger.warning("DATABASE_URL not set - running without PostgreSQL persistence.")
        return None
    try:
        return await db.create_pool(url)
    except Exception as exc:
        if required:
            raise RuntimeError(
                "PostgreSQL connection failed. Check DATABASE_URL in the bot "
                "service and confirm the Railway Postgres service is running."
            ) from None
        logger.warning(
            "PostgreSQL unavailable - running without persistence (%s).",
            type(exc).__name__,
        )
        return None


def build_application(settings: Settings) -> Application:
    async def on_startup(application: Application) -> None:
        application.bot_data[DB_KEY] = await _connect_database(
            settings.database_url, required=settings.panel_enabled
        )
        # Load panel-managed commands before publishing the Telegram menu.
        await commands.reload(application.bot_data[DB_KEY])
        await market_monitor.setup(application)
        try:
            await set_bot_commands(application)
        except Exception as exc:
            # A rejected menu must not stop the bot from starting.
            logger.warning(
                "Failed to publish command menu on startup (%s).", type(exc).__name__
            )

    async def on_shutdown(application: Application) -> None:
        await market_monitor.close(application)
        pool = application.bot_data.get(DB_KEY)
        if pool is not None:
            await db.close_pool(pool)

    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .post_init(on_startup)
        .post_stop(market_monitor.stop)
        .post_shutdown(on_shutdown)
        .build()
    )
    register_handlers(application)
    return application


async def _shutdown_application(
    application: Application, *, suppress_errors: bool
) -> None:
    """Attempt every cleanup step, preserving an earlier startup/run error."""
    first_error = None

    async def attempt(step: str, callback) -> None:
        nonlocal first_error
        try:
            await callback()
        except BaseException as exc:
            # Continue closing other resources even after a stop/cancellation
            # failure. Never log raw exception text, which may contain tokens.
            if first_error is None:
                first_error = exc
            logger.warning("Bot cleanup failed during %s (%s).", step, type(exc).__name__)

    await attempt("market monitor stop", lambda: market_monitor.stop(application))
    if application.updater is not None and application.updater.running:
        await attempt("polling stop", application.updater.stop)
    if application.running:
        await attempt("application stop", application.stop)
    await attempt("application shutdown", application.shutdown)
    # Application.shutdown returns early after an incomplete initialize(),
    # whereas Bot.shutdown closes HTTP requests opened before getMe failed.
    # It is safe to call again after a complete application shutdown.
    await attempt("bot shutdown", application.bot.shutdown)
    if application.post_shutdown is not None:
        await attempt(
            "database shutdown", lambda: application.post_shutdown(application)
        )

    if first_error is not None and not suppress_errors:
        raise first_error


async def _run_with_panel(application: Application, settings: Settings) -> None:
    """Run Telegram polling and the admin panel together in one event loop."""
    # Application.initialize()/shutdown() deliberately do not invoke the
    # builder's post_init/post_shutdown callbacks. run_polling() normally does
    # that for us, but this custom runner must execute them explicitly.
    failed = False
    try:
        await application.initialize()
        if application.post_init is not None:
            await application.post_init(application)

        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        await application.start()
        logger.info(
            "Bot polling started; admin panel listening on port %d.", settings.port
        )

        web = create_app(application, settings)
        config = uvicorn.Config(
            web,
            host="0.0.0.0",
            port=settings.port,
            log_level=settings.log_level.lower(),
            log_config=None,
            access_log=False,
        )
        server = uvicorn.Server(config)
        await server.serve()  # blocks until SIGTERM/SIGINT
    except BaseException:
        failed = True
        raise
    finally:
        await _shutdown_application(application, suppress_errors=failed)


def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)

    application = build_application(settings)

    if settings.panel_enabled:
        asyncio.run(_run_with_panel(application, settings))
    else:
        logger.info(
            "Bot is running with polling (admin panel disabled: PANEL_PASSWORD not set)."
        )
        application.run_polling(allowed_updates=Update.ALL_TYPES)


def run() -> None:
    """Run the service with static, credential-safe startup diagnostics."""
    configure_logging()
    try:
        main()
    except InvalidToken:
        logger.error(
            "Telegram rejected BOT_TOKEN. Replace it in the active Railway bot "
            "service with the current BotFather token, then redeploy. "
            "Do not send the token in chat."
        )
        raise SystemExit(1) from None
    except Exception as exc:
        logger.error("Bot stopped (%s). Check the service configuration.", type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    run()
