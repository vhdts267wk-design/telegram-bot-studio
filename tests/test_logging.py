import logging
import unittest
from unittest.mock import patch

from bot.logging_utils import SecretRedactingFormatter


class LoggingPrivacyTests(unittest.TestCase):
    def test_credentials_are_redacted_in_message_arguments_and_traceback(self):
        private_values = {
            "BOT_TOKEN": "fake-configured-bot-secret",
            "OPENAI_API_KEY": "fake-configured-api-secret",
            "DATABASE_URL": "postgresql://fakeuser:fakepass@example.invalid/db",
            "PANEL_PASSWORD": "fake-panel-password",
            "PANEL_SECRET_KEY": "fake-session-secret",
        }
        try:
            raise RuntimeError("provider failure " + private_values["OPENAI_API_KEY"])
        except RuntimeError:
            import sys
            error = sys.exc_info()
        record = logging.LogRecord(
            "test", logging.WARNING, __file__, 1,
            "request failed %s", (" ".join(private_values.values()),), error,
        )
        with patch.dict("os.environ", private_values, clear=True):
            output = SecretRedactingFormatter("%(message)s").format(record)
        self.assertIn("RuntimeError", output)
        self.assertIn("request failed", output)
        self.assertIn("[REDACTED]", output)
        for value in private_values.values():
            self.assertNotIn(value, output)

    def test_unconfigured_credentials_in_network_errors_are_redacted(self):
        token = "1234567890:" + "x" * 35
        api_key = "sk-" + "y" * 32
        database_url = "postgresql://user:password@example.invalid/database"
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1,
            "failed https://api.telegram.org/bot%s/getUpdates %s %s",
            (token, api_key, database_url), None,
        )
        with patch.dict("os.environ", {}, clear=True):
            output = SecretRedactingFormatter("%(message)s").format(record)
        for value in (token, api_key, database_url):
            self.assertNotIn(value, output)
        self.assertIn("[REDACTED_BOT_TOKEN]", output)


if __name__ == "__main__":
    unittest.main()
