import os
import unittest
from unittest.mock import patch

from bot.config import Settings


class SecureCookieConfigurationTests(unittest.TestCase):
    def settings(self, **variables):
        environment = {"BOT_TOKEN": "123:local-test-token", **variables}
        with patch.dict(os.environ, environment, clear=True):
            return Settings.from_env()

    def test_documented_railway_environment_enables_https_cookies(self):
        for name in ("RAILWAY_ENVIRONMENT_ID", "RAILWAY_ENVIRONMENT_NAME"):
            with self.subTest(name=name):
                settings = self.settings(**{name: "local-test-environment"})
                self.assertTrue(settings.panel_secure_cookie)

    def test_legacy_railway_environment_remains_supported(self):
        settings = self.settings(RAILWAY_ENVIRONMENT="local-test-environment")
        self.assertTrue(settings.panel_secure_cookie)

    def test_local_environment_defaults_to_http_cookies(self):
        self.assertFalse(self.settings().panel_secure_cookie)
        settings = self.settings(RAILWAY_ENVIRONMENT_ID="   ")
        self.assertFalse(settings.panel_secure_cookie)

    def test_blank_setting_uses_railway_default(self):
        settings = self.settings(
            RAILWAY_ENVIRONMENT_ID="local-test-environment", PANEL_SECURE_COOKIE="  "
        )
        self.assertTrue(settings.panel_secure_cookie)

    def test_explicit_true_enables_https_cookies_locally(self):
        for value in ("true", "1", "yes", "on", " TRUE "):
            with self.subTest(value=value):
                self.assertTrue(self.settings(PANEL_SECURE_COOKIE=value).panel_secure_cookie)

    def test_explicit_false_overrides_railway_default(self):
        for value in ("false", "0", "no", "off", " FALSE "):
            with self.subTest(value=value):
                settings = self.settings(
                    RAILWAY_ENVIRONMENT_ID="local-test-environment",
                    PANEL_SECURE_COOKIE=value,
                )
                self.assertFalse(settings.panel_secure_cookie)

    def test_manual_bridge_key_alone_enables_http_without_legacy_key_or_panel(self):
        key = "synthetic-manual-key-" + "x" * 32
        settings = self.settings(
            DATABASE_URL="postgres://local-test", MT5_MANUAL_BRIDGE_KEY=" " + key + " ",
            MT5_MANUAL_TICKETS_ENABLED="true", MT5_TRADING_ENABLED="false",
        )
        self.assertEqual(settings.manual_bridge_key, key)
        self.assertEqual(settings.market_bridge_key, "")
        self.assertFalse(settings.panel_enabled)
        self.assertTrue(settings.http_enabled)

    def test_manual_bridge_http_requires_database_and_error_names_both_bridge_keys(self):
        key = "synthetic-private-manual-key-" + "x" * 32
        with self.assertRaises(RuntimeError) as raised:
            self.settings(MT5_MANUAL_BRIDGE_KEY=key, MT5_MANUAL_TICKETS_ENABLED="true")
        text = str(raised.exception)
        for name in ("DATABASE_URL", "MARKET_BRIDGE_KEY", "MT5_MANUAL_BRIDGE_KEY"):
            self.assertIn(name, text)
        self.assertNotIn(key, text)

    def test_configured_manual_key_enables_http_but_does_not_enable_manual_mode_itself(self):
        settings = self.settings(
            DATABASE_URL="postgres://local-test", MT5_MANUAL_BRIDGE_KEY="x" * 32,
            MT5_MANUAL_TICKETS_ENABLED="false",
        )
        self.assertTrue(settings.http_enabled)
        self.assertFalse(settings.panel_enabled)
        self.assertEqual(settings.market_bridge_key, "")

    def test_short_manual_key_does_not_enable_http_or_require_database(self):
        settings = self.settings(MT5_MANUAL_BRIDGE_KEY="short")
        self.assertEqual(settings.manual_bridge_key, "short")
        self.assertEqual(settings.database_url, "")
        self.assertFalse(settings.http_enabled)


if __name__ == "__main__":
    unittest.main()
