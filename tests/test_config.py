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


if __name__ == "__main__":
    unittest.main()
