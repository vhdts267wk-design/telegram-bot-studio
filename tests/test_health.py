import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from telegram.error import InvalidToken, TimedOut

from bot.handlers import DB_KEY
from bot.panel import app as panel


def make_application():
    return SimpleNamespace(
        bot_data={DB_KEY: SimpleNamespace(fetchval=AsyncMock(return_value=1))},
        bot=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=1))),
        running=True,
        updater=SimpleNamespace(running=True),
    )


def make_settings():
    return SimpleNamespace(
        panel_secret_key="test-only-session-secret", panel_secure_cookie=False, panel_password=""
    )


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.application = make_application()
        self.pool = self.application.bot_data[DB_KEY]
        self.client = self.enterContext(
            TestClient(panel.create_app(self.application, make_settings()))
        )

    def test_healthy_poller_and_database_are_ready(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.pool.fetchval.assert_awaited_once_with("SELECT 1")
        self.application.bot.get_me.assert_awaited_once_with()

    def test_stopped_poller_is_not_ready(self):
        self.application.updater.running = False
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.pool.fetchval.assert_not_awaited()
        self.application.bot.get_me.assert_not_awaited()

    def test_missing_database_is_not_ready(self):
        self.application.bot_data.clear()
        self.assertEqual(self.client.get("/healthz").status_code, 503)
        self.application.bot.get_me.assert_not_awaited()

    def test_database_failure_does_not_expose_connection_details(self):
        detail = "private-database-connection-detail"
        self.pool.fetchval.side_effect = RuntimeError(detail)
        with self.assertLogs("bot.panel.app", level="WARNING") as captured:
            response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
        self.assertNotIn(detail, "\n".join(captured.output))
        self.application.bot.get_me.assert_not_awaited()

    def test_invalid_token_is_not_ready_even_when_updater_still_runs(self):
        detail = "https://api.telegram.org/botFAKE_BOT_TOKEN/getMe"
        self.application.bot.get_me.side_effect = InvalidToken(detail)

        with self.assertLogs("bot.panel.app", level="WARNING") as captured:
            response = self.client.get("/healthz")

        self.assertTrue(self.application.updater.running)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
        self.assertIn("InvalidToken", "\n".join(captured.output))
        self.assertNotIn(detail, "\n".join(captured.output) + response.text)
        self.assertNotIn("FAKE_BOT_TOKEN", "\n".join(captured.output) + response.text)

    def test_telegram_timeout_is_unavailable_without_private_details(self):
        detail = "https://api.telegram.org/botFAKE_BOT_TOKEN/getMe"
        self.application.bot.get_me.side_effect = TimedOut(detail)

        with self.assertLogs("bot.panel.app", level="WARNING") as captured:
            response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
        self.assertIn("TimedOut", "\n".join(captured.output))
        self.assertNotIn(detail, "\n".join(captured.output) + response.text)
        self.assertNotIn("FAKE_BOT_TOKEN", "\n".join(captured.output) + response.text)

    def test_missing_bot_identity_is_not_ready(self):
        self.application.bot.get_me.return_value = None
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})


class ReadinessProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.application = make_application()
        app = panel.create_app(self.application, make_settings())
        self.healthz = next(
            route.endpoint for route in app.routes if getattr(route, "path", None) == "/healthz"
        )

    async def test_concurrent_health_requests_share_one_auth_probe(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def auth_probe():
            started.set()
            await release.wait()
            return SimpleNamespace(id=1)

        self.application.bot.get_me.side_effect = auth_probe
        with patch.object(panel, "monotonic", return_value=100.0):
            requests = [asyncio.create_task(self.healthz()) for _ in range(20)]
            await asyncio.wait_for(started.wait(), timeout=1)
            self.assertEqual(self.application.bot.get_me.await_count, 1)
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(*requests), timeout=1)
            self.assertTrue(all(response == {"status": "ok"} for response in responses))
            self.application.bot.get_me.assert_awaited_once_with()
            await self.healthz()
            self.application.bot.get_me.assert_awaited_once_with()

    async def test_auth_cache_expires_and_rechecks_a_revoked_token(self):
        with patch.object(panel, "monotonic", return_value=100.0) as clock:
            self.assertEqual(await self.healthz(), {"status": "ok"})
            clock.return_value = 109.0
            self.assertEqual(await self.healthz(), {"status": "ok"})
            self.application.bot.get_me.assert_awaited_once_with()

            self.application.bot.get_me.side_effect = InvalidToken("private-token-detail")
            clock.return_value = 111.0
            with self.assertLogs("bot.panel.app", level="WARNING") as captured:
                response = await self.healthz()
            self.assertEqual(response.status_code, 503)
            self.assertEqual(self.application.bot.get_me.await_count, 2)
            self.assertNotIn("private-token-detail", "\n".join(captured.output))

            clock.return_value = 120.0
            self.assertEqual((await self.healthz()).status_code, 503)
            self.assertEqual(self.application.bot.get_me.await_count, 2)

    async def test_hanging_auth_probe_times_out_and_caches_failure(self):
        async def auth_probe():
            await asyncio.Event().wait()

        self.application.bot.get_me.side_effect = auth_probe
        with (
            patch.object(panel, "READINESS_TIMEOUT_SECONDS", 0.01),
            patch.object(panel, "monotonic", return_value=100.0),
            self.assertLogs("bot.panel.app", level="WARNING") as captured,
        ):
            response = await asyncio.wait_for(self.healthz(), timeout=0.5)
            self.assertEqual(response.status_code, 503)
            response = await self.healthz()
            self.assertEqual(response.status_code, 503)
            self.application.bot.get_me.assert_awaited_once_with()

        self.assertEqual(len(captured.output), 1)
        self.assertIn("TimeoutError", captured.output[0])


if __name__ == "__main__":
    unittest.main()
