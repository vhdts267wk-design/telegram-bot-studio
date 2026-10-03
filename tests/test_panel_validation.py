import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.datastructures import FormData
from telegram.error import TelegramError

from bot import commands, handlers
from bot.panel import app as panel
from bot.panel.auth import check_credentials, verify_csrf


def make_request(settings, *, app=None, form=None, session=None):
    if app is None:
        app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    return SimpleNamespace(
        app=app,
        session=session if session is not None else {},
        form=AsyncMock(return_value=FormData(form or {})),
    )


class PanelAuthenticationTests(unittest.TestCase):
    def test_unicode_credentials_compare_without_errors(self):
        settings = SimpleNamespace(panel_username="admin", panel_password="test-password")
        request = make_request(settings)
        for username, password in (
            ("مدير", "test-password"),
            ("admin", "كلمة🔑"),
            ("مدير", "كلمة🔑"),
        ):
            with self.subTest(username=username, password=password):
                self.assertFalse(check_credentials(request, username, password))

        settings.panel_username = "مدير"
        settings.panel_password = "كلمة🔑"
        self.assertTrue(check_credentials(request, "مدير", "كلمة🔑"))
        self.assertFalse(check_credentials(request, "admin", "test-password"))
        self.assertFalse(check_credentials(request, "مدير", "wrong-password"))

    def test_unicode_csrf_mismatch_is_a_controlled_client_error(self):
        request = make_request(None, session={"csrf": "test-csrf-token"})

        with self.assertRaises(HTTPException) as caught:
            verify_csrf(request, "رمز🔑")

        self.assertEqual(caught.exception.status_code, 400)
        self.assertNotIn("رمز🔑", caught.exception.detail)
        verify_csrf(request, "test-csrf-token")


class PanelValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pool = object()
        self.settings = SimpleNamespace(
            panel_username="admin",
            panel_password="test-password",
            panel_secret_key="test-session-secret",
            panel_secure_cookie=False,
        )
        self.application = SimpleNamespace(bot_data={handlers.DB_KEY: self.pool})
        self.app = panel.create_app(self.application, self.settings)

    def endpoint(self, path, method):
        return next(
            route.endpoint
            for route in self.app.routes
            if getattr(route, "path", None) == path
            and method in getattr(route, "methods", set())
        )

    def test_gold_cannot_be_overridden_by_a_dynamic_command(self):
        values, errors = panel._validate(
            {"name": "/gold", "reply_type": "text", "reply_text": "custom reply"}
        )

        self.assertEqual(values["name"], "gold")
        self.assertTrue(any("built-in command" in error for error in errors))
        self.assertEqual(
            set(commands.BUILTIN_COMMANDS), {name for name, _ in handlers.BOT_COMMANDS}
        )

    async def test_menu_button_accepts_gold_as_a_builtin_target(self):
        request = make_request(
            self.settings,
            app=self.app,
            form={"label": "Gold chart", "command_name": "gold", "enabled": "on"},
        )
        with (
            patch.object(panel, "verify_csrf"),
            patch.object(panel.db, "list_commands", new=AsyncMock(return_value=[])),
            patch.object(panel.db, "list_menu_buttons", new=AsyncMock(return_value=[])),
            patch.object(panel.db, "create_menu_button", new=AsyncMock()) as create,
            patch.object(panel, "_audit", new=AsyncMock()),
            patch.object(panel, "_refresh", new=AsyncMock()),
        ):
            response = await self.endpoint("/buttons/new", "POST")(request)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(create.await_args.kwargs["command_name"], "gold")

    async def test_response_buttons_accept_gold_without_a_dynamic_gold_command(self):
        request = make_request(
            self.settings,
            app=self.app,
            form={"command_id": "1", "target_commands": "gold"},
        )
        selected = {"id": 1, "name": "example", "keyboard": None}
        with (
            patch.object(panel, "verify_csrf"),
            patch.object(panel.db, "get_command", new=AsyncMock(return_value=selected)),
            patch.object(panel.db, "list_commands", new=AsyncMock(return_value=[selected])),
            patch.object(panel.db, "update_command_keyboard", new=AsyncMock()) as update,
            patch.object(panel, "_audit", new=AsyncMock()),
            patch.object(panel, "_refresh", new=AsyncMock()),
        ):
            response = await self.endpoint("/response-buttons", "POST")(request)

        self.assertEqual(response.status_code, 303)
        update.assert_awaited_once_with(self.pool, 1, [["/gold"]])

    async def test_menu_refresh_failure_redacts_sensitive_request_url(self):
        private_url = "https://api.telegram.org/botFAKE_BOT_TOKEN/setMyCommands"
        request = make_request(self.settings, app=self.app)
        with (
            patch.object(panel.commands, "reload", new=AsyncMock()),
            patch.object(
                panel,
                "set_bot_commands",
                new=AsyncMock(side_effect=TelegramError(private_url)),
            ),
            self.assertLogs(panel.logger, level="WARNING") as captured,
        ):
            await panel._refresh(request)

        output = "\n".join(captured.output)
        self.assertIn("TelegramError", output)
        self.assertNotIn(private_url, output)
        self.assertNotIn("FAKE_BOT_TOKEN", output)


if __name__ == "__main__":
    unittest.main()
