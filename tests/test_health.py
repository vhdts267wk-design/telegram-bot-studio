import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from bot.handlers import DB_KEY
from bot.panel.app import create_app


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.pool = SimpleNamespace(fetchval=AsyncMock(return_value=1))
        self.application = SimpleNamespace(
            bot_data={DB_KEY: self.pool}, running=True,
            updater=SimpleNamespace(running=True),
        )
        settings = SimpleNamespace(
            panel_secret_key="test-only-session-secret", panel_secure_cookie=False
        )
        self.client = TestClient(create_app(self.application, settings))
        self.addCleanup(self.client.close)

    def test_healthy_poller_and_database_are_ready(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.pool.fetchval.assert_awaited_once_with("SELECT 1")

    def test_stopped_poller_is_not_ready(self):
        self.application.updater.running = False
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.pool.fetchval.assert_not_awaited()

    def test_missing_database_is_not_ready(self):
        self.application.bot_data.clear()
        self.assertEqual(self.client.get("/healthz").status_code, 503)

    def test_database_failure_does_not_expose_connection_details(self):
        detail = "private-database-connection-detail"
        self.pool.fetchval.side_effect = RuntimeError(detail)
        with self.assertLogs("bot.panel.app", level="WARNING") as captured:
            response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
        self.assertNotIn(detail, "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
