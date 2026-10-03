from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from telegram.error import InvalidToken
from telegram.ext import Application
from telegram.request import HTTPXRequest

from bot import main as bot_main


class StartupLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def rejecting_application(self):
        def reject(request):
            self.assertTrue(request.url.path.endswith("/getMe"))
            return httpx.Response(
                401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
            )

        requests = [
            HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(reject)})
            for _ in range(2)
        ]
        application = (
            Application.builder()
            .token("123:local-startup-test-token")
            .request(requests[0])
            .get_updates_request(requests[1])
            .build()
        )
        for request in requests:
            self.addAsyncCleanup(request.shutdown)
        return application, requests

    async def test_public_bot_shutdown_closes_requests_after_rejected_initialization(self):
        application, requests = self.rejecting_application()
        with self.assertRaises(InvalidToken):
            await application.initialize()

        # Application.shutdown cannot clean up a partially initialized bot.
        await application.shutdown()
        self.assertTrue(all(not request._client.is_closed for request in requests))
        await application.bot.shutdown()
        self.assertTrue(all(request._client.is_closed for request in requests))

    async def test_authentication_failure_closes_clients_and_never_starts_server(self):
        application, requests = self.rejecting_application()
        with patch.object(bot_main, "create_app") as create_app, patch.object(
            bot_main.uvicorn, "Server"
        ) as server, self.assertRaises(InvalidToken):
            await bot_main._run_with_panel(application, SimpleNamespace())

        self.assertTrue(all(request._client.is_closed for request in requests))
        self.assertFalse(application.running)
        self.assertFalse(application.updater.running)
        create_app.assert_not_called()
        server.assert_not_called()

    def fake_application(self):
        return SimpleNamespace(
            initialize=AsyncMock(),
            post_init=None,
            updater=SimpleNamespace(
                running=True, start_polling=AsyncMock(), stop=AsyncMock()
            ),
            running=True,
            start=AsyncMock(),
            stop=AsyncMock(),
            shutdown=AsyncMock(),
            bot=SimpleNamespace(shutdown=AsyncMock()),
            post_shutdown=AsyncMock(),
        )

    async def test_stop_failure_does_not_skip_remaining_resources_or_database_cleanup(self):
        application = self.fake_application()
        stop_error = RuntimeError("PRIVATE_POLLING_STOP_DETAIL")
        application.updater.stop.side_effect = stop_error
        server = SimpleNamespace(serve=AsyncMock())
        settings = SimpleNamespace(port=8080, log_level="INFO")
        with patch.object(bot_main, "create_app"), patch.object(
            bot_main.uvicorn, "Config"
        ), patch.object(bot_main.uvicorn, "Server", return_value=server), self.assertLogs(
            bot_main.logger, level="WARNING"
        ) as captured, self.assertRaises(RuntimeError) as raised:
            await bot_main._run_with_panel(application, settings)

        self.assertIs(raised.exception, stop_error)
        application.stop.assert_awaited_once_with()
        application.shutdown.assert_awaited_once_with()
        application.bot.shutdown.assert_awaited_once_with()
        application.post_shutdown.assert_awaited_once_with(application)
        self.assertNotIn("PRIVATE_POLLING_STOP_DETAIL", "\n".join(captured.output))

    async def test_cleanup_failures_do_not_replace_original_initialization_error(self):
        application = self.fake_application()
        original_error = InvalidToken("PRIVATE_AUTHENTICATION_DETAIL")
        application.initialize.side_effect = original_error
        application.updater.running = False
        application.running = False
        application.shutdown.side_effect = RuntimeError("PRIVATE_SHUTDOWN_DETAIL")
        with self.assertLogs(bot_main.logger, level="WARNING") as captured, self.assertRaises(
            InvalidToken
        ) as raised:
            await bot_main._run_with_panel(application, SimpleNamespace())

        self.assertIs(raised.exception, original_error)
        application.bot.shutdown.assert_awaited_once_with()
        application.post_shutdown.assert_awaited_once_with(application)
        output = "\n".join(captured.output)
        self.assertNotIn("PRIVATE_SHUTDOWN_DETAIL", output)
        self.assertNotIn("PRIVATE_AUTHENTICATION_DETAIL", output)


class StartupDiagnosticTests(unittest.TestCase):
    def test_invalid_token_has_static_private_repair_guidance_and_failing_exit(self):
        private = "PRIVATE_AUTH_ERROR_WITH_LOCAL_TEST_TOKEN"
        with patch.object(bot_main, "main", side_effect=InvalidToken(private)), patch.object(
            bot_main, "configure_logging"
        ), self.assertLogs(bot_main.logger, level="ERROR") as captured, self.assertRaises(
            SystemExit
        ) as raised:
            bot_main.run()

        self.assertEqual(raised.exception.code, 1)
        self.assertTrue(raised.exception.__suppress_context__)
        output = "\n".join(captured.output)
        self.assertIn("Telegram rejected BOT_TOKEN", output)
        self.assertIn("BotFather", output)
        self.assertIn("active Railway bot service", output)
        self.assertNotIn(private, output)

    def test_other_startup_errors_still_exit_without_exception_content(self):
        private = "PRIVATE_OTHER_STARTUP_ERROR"
        with patch.object(bot_main, "main", side_effect=RuntimeError(private)), patch.object(
            bot_main, "configure_logging"
        ), self.assertLogs(bot_main.logger, level="ERROR") as captured, self.assertRaises(
            SystemExit
        ) as raised:
            bot_main.run()

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Bot stopped (RuntimeError)", "\n".join(captured.output))
        self.assertNotIn(private, "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
