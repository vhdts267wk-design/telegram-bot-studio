from contextlib import nullcontext
import os
from pathlib import Path
import runpy
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from alembic import context
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, async_engine_from_config


PROJECT_DIR = Path(__file__).resolve().parents[1]
MIGRATION_ENV = PROJECT_DIR / "migrations" / "env.py"
TEST_URL = "postgresql://test-user:local%40test@example.invalid/test-db"
NORMALIZED_URL = TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1)


class MigrationSetupTests(unittest.TestCase):
    def run_environment(self, config, *, offline):
        with patch.dict(os.environ, {"DATABASE_URL": TEST_URL}, clear=True), patch.object(
            context, "config", config, create=True
        ), patch.object(context, "is_offline_mode", return_value=offline), patch(
            "bot.logging_utils.configure_logging"
        ) as configure_logging:
            result = runpy.run_path(str(MIGRATION_ENV), run_name="migration_setup_test")
            configure_logging.assert_called_once_with("INFO")
            return result

    def test_percent_encoded_password_is_preserved_in_offline_configuration(self):
        config = Config(str(PROJECT_DIR / "alembic.ini"))
        with patch.object(context, "configure") as configure, patch.object(
            context, "begin_transaction", return_value=nullcontext()
        ), patch.object(context, "run_migrations") as migrate:
            self.run_environment(config, offline=True)

        self.assertEqual(config.get_main_option("sqlalchemy.url"), NORMALIZED_URL)
        configure.assert_called_once_with(url=NORMALIZED_URL, literal_binds=True)
        migrate.assert_called_once_with()

    def test_online_engine_parses_encoded_password_without_connecting_to_database(self):
        config = Config(str(PROJECT_DIR / "alembic.ini"))
        engines = []
        connection = MagicMock(run_sync=AsyncMock())
        connection_manager = MagicMock()
        connection_manager.__aenter__ = AsyncMock(return_value=connection)
        connection_manager.__aexit__ = AsyncMock(return_value=False)

        def create_engine(configuration, **kwargs):
            engine = async_engine_from_config(configuration, **kwargs)
            engines.append(engine)
            return engine

        with patch(
            "sqlalchemy.ext.asyncio.async_engine_from_config", side_effect=create_engine
        ), patch.object(AsyncEngine, "connect", return_value=connection_manager):
            self.run_environment(config, offline=False)

        self.assertEqual(len(engines), 1)
        self.assertEqual(engines[0].url.password, "local@test")
        self.assertEqual(engines[0].url.drivername, "postgresql+asyncpg")
        connection.run_sync.assert_awaited_once()

    def test_migration_failure_exits_without_private_url_or_driver_details(self):
        config = Config(str(PROJECT_DIR / "alembic.ini"))
        detail = "private-driver-error " + TEST_URL + " decoded password local@test"
        with patch.object(context, "configure", side_effect=ValueError(detail)), self.assertLogs(
            "migration_setup_test", level="ERROR"
        ) as captured, self.assertRaises(SystemExit) as exited:
            self.run_environment(config, offline=True)

        self.assertEqual(exited.exception.code, 1)
        output = "\n".join(captured.output)
        self.assertIn("ValueError", output)
        self.assertIn("Check DATABASE_URL", output)
        self.assertNotIn(TEST_URL, output)
        self.assertNotIn("local@test", output)
        self.assertNotIn("private-driver-error", output)
        self.assertTrue(exited.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
