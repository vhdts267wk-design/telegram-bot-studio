"""Startup diagnostics use mocked state/SDK operations and temporary reports."""

from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from decimal import Decimal
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError

from bridge import mt5_market_bridge as market
from bridge import mt5_trade_bridge as bridge


class StartupDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.report_path = self.base / "MT5-Startup-Status.json"
        self.expected = patch.object(bridge, "expected_startup_report", return_value=self.report_path)
        self.expected.start()
        self.addCleanup(self.expected.stop)
        self.settings = bridge.Settings(
            market.Settings(self.base / "terminal64.exe", "XAUUSD", "https://example.invalid/api/market/feed", "PRIVATEKEY" * 8),
            self.base / "private", "demo", Decimal("0.01"), True, self.report_path,
        )

    def test_process_lock_directory_and_open_failures_have_distinct_fixed_stages(self):
        directory = MagicMock(spec=Path)
        for failed_call, expected in (("mkdir", "lock_directory"), ("open", "lock_file_open")):
            with self.subTest(call=failed_call):
                diagnostic = bridge.StartupDiagnostics()
                directory.mkdir.side_effect = PermissionError("PRIVATE") if failed_call == "mkdir" else None
                with patch("builtins.open", side_effect=PermissionError("PRIVATE")) as opened:
                    with self.assertRaises(PermissionError):
                        bridge.ProcessLock(directory, diagnostics=diagnostic)
                self.assertEqual(diagnostic.stage, expected)
                if failed_call == "mkdir":
                    opened.assert_not_called()

    def test_lock_file_prepare_failure_keeps_exact_source_class_without_retry(self):
        directory, handle = MagicMock(spec=Path), MagicMock()
        handle.seek.side_effect = PermissionError("PRIVATE")
        diagnostic = bridge.StartupDiagnostics()
        with patch("builtins.open", return_value=handle) as opened:
            with self.assertRaises(bridge.GuardError):
                bridge.ProcessLock(directory, diagnostics=diagnostic)
        self.assertEqual(diagnostic.stage, "lock_file_prepare")
        self.assertEqual(diagnostic.original_error, "PermissionError")
        opened.assert_called_once()
        handle.close.assert_called_once()

    def test_ledger_directory_open_schema_and_identity_failures_are_separate(self):
        for condition, expected in (("mkdir", "ledger_directory"), ("open", "ledger_open"),
                                    ("schema", "ledger_schema"), ("identity", "ledger_identity")):
            with self.subTest(condition=condition):
                directory, database = MagicMock(spec=Path), MagicMock()
                diagnostic = bridge.StartupDiagnostics()
                if condition == "mkdir":
                    directory.mkdir.side_effect = PermissionError("PRIVATE")
                def execute(sql, *args):
                    if condition == "schema" or (condition == "identity" and sql.startswith("INSERT")):
                        raise sqlite3.OperationalError("PRIVATE")
                database.execute.side_effect = execute
                with patch.object(bridge.sqlite3, "connect", side_effect=PermissionError("PRIVATE") if condition == "open" else None,
                                  return_value=database):
                    with self.assertRaises((PermissionError, sqlite3.OperationalError)):
                        bridge.Ledger(directory, diagnostics=diagnostic)
                self.assertEqual(diagnostic.stage, expected)

    def test_startup_failure_report_has_phase_and_class_but_no_private_error_or_sdk_calls(self):
        sdk, diagnostic = Mock(), bridge.StartupDiagnostics(self.report_path)
        def lock(*args, **kwargs):
            kwargs["diagnostics"].set_stage("lock_file_open")
            raise PermissionError("PRIVATEKEY PRIVATEPATH PRIVATEACCOUNT")
        output = io.StringIO()
        with (patch.object(bridge, "ProcessLock", side_effect=lock), patch.object(bridge, "Ledger") as ledger,
              redirect_stderr(output)):
            self.assertEqual(bridge.run_bridge(sdk, self.settings, diagnostics=diagnostic), 1)
        saved = json.loads(self.report_path.read_text())
        self.assertEqual((saved["stage"], saved["error_class"], saved["status"]), ("lock_file_open", "PermissionError", "stopped"))
        self.assertFalse(saved["bridge_ready"])
        self.assertIsNone(saved["registration_succeeded"])
        self.assertIsNone(saved["first_feed_accepted"])
        self.assertNotIn("PRIVATE", json.dumps(saved) + output.getvalue())
        self.assertIn("opening the local process lock file", output.getvalue())
        ledger.assert_not_called()
        self.assertEqual(sdk.mock_calls, [])

    def test_optional_report_cannot_target_any_state_file_or_another_path(self):
        for path in (self.settings.state_directory / "bridge.lock", self.base / "different.json"):
            with self.assertRaises(bridge.GuardError):
                bridge.StartupDiagnostics(path)
            self.assertFalse(path.exists())

    def test_report_inside_configured_state_is_rejected_before_configuration_or_any_write(self):
        arguments = ["--terminal", "not-used.exe", "--symbol", "XAUUSD", "--account-mode", "demo",
                     "--volume", "0.01", "--state-directory", str(self.base), "--enable-orders",
                     "--startup-report", str(self.report_path)]
        with (patch.object(bridge, "load_settings") as settings,
              patch.object(bridge, "ProcessLock") as lock,
              patch.object(bridge.importlib, "import_module") as sdk,
              redirect_stderr(io.StringIO())):
            self.assertEqual(bridge.main(arguments), 1)
        self.assertFalse(self.report_path.exists())
        settings.assert_not_called()
        lock.assert_not_called()
        sdk.assert_not_called()

    def test_atomic_reporting_refuses_existing_alias_and_leaves_old_report_on_failure(self):
        self.report_path.write_text("original report", encoding="utf-8")
        diagnostic = bridge.StartupDiagnostics(self.report_path)
        with patch.object(bridge.os, "replace", side_effect=PermissionError("PRIVATE")):
            self.assertFalse(diagnostic.save("stopped", PermissionError("PRIVATE")))
        self.assertEqual(self.report_path.read_text(), "original report")
        self.assertEqual(list(self.base.iterdir()), [self.report_path])
        alias = self.base / "alias.json"
        bridge.os.link(self.report_path, alias)
        with self.assertRaises(bridge.GuardError):
            bridge.StartupDiagnostics(self.report_path)
        self.assertEqual(self.report_path.read_text(), "original report")

    def test_report_save_failure_does_not_echo_errors_or_create_directories(self):
        diagnostic = bridge.StartupDiagnostics(self.report_path)
        with patch.object(bridge.tempfile, "NamedTemporaryFile", side_effect=PermissionError("PRIVATE")), patch.object(Path, "mkdir") as mkdir:
            self.assertFalse(diagnostic.save("stopped", PermissionError("PRIVATE")))
        self.assertEqual(diagnostic.record["error_class"], "PermissionError")
        mkdir.assert_not_called()

    def test_market_data_error_class_is_reported_without_sdk_details(self):
        diagnostic = bridge.StartupDiagnostics(self.report_path)
        error = market.MarketDataError("PRIVATE SDK detail", reason_code="terminal_unavailable")
        self.assertTrue(diagnostic.save("stopped", error))
        saved = json.loads(self.report_path.read_text())
        self.assertEqual(saved["error_class"], "MarketDataError")
        self.assertNotIn("PRIVATE", json.dumps(saved))

    def run_ready(self, *, feed_failed=False, paired=None, feed_recovers=False):
        sdk, ledger, lock = Mock(), MagicMock(), Mock()
        ledger.pending.return_value = []
        ledger.device_id = "PRIVATEDEVICE"
        sdk.initialize.return_value = True
        sdk.symbol_info.return_value = Mock(currency_base="XAU", currency_profit="USD", volume_min=0.01, volume_max=1, volume_step=0.01)
        market_calls = []
        def post(settings, route, payload):
            if route == "register":
                return {} if paired is None else {"paired": paired}
            if route == "market":
                market_calls.append(payload)
                if feed_failed and (not feed_recovers or len(market_calls) == 1):
                    raise HTTPError("https://PRIVATEKEY.invalid", 409, "PRIVATEBODY", None, None)
            return {"trade": None} if route == "poll" else {}
        with (patch.object(bridge, "ProcessLock", return_value=lock), patch.object(bridge, "Ledger", return_value=ledger),
             patch.object(bridge, "terminal_is_running", side_effect=[True, True] + ([True] if feed_recovers else []) + [False]),
             patch.object(bridge.time, "monotonic", side_effect=[0, 0, 60, 60]),
             patch.object(bridge, "check_bound_account"), patch.object(bridge, "pairing_registration", return_value=("PRIVATEPAIRCODE", {"device_id": "PRIVATEDEVICE"})),
             patch.object(bridge.market, "build_payload", return_value={"PRIVATEQUOTE": 1234}), patch.object(bridge, "execution_metadata", return_value={}),
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            self.assertEqual(bridge.run_bridge(sdk, self.settings, post=post, sleep=Mock()), 0)
        return json.loads(self.report_path.read_text()), sdk

    def test_ready_registration_and_first_feed_acceptance_have_independent_fixed_evidence(self):
        saved, sdk = self.run_ready(paired=True)
        self.assertTrue(saved["registration_succeeded"])
        self.assertTrue(saved["bridge_ready"])
        self.assertTrue(saved["already_paired"])
        self.assertTrue(saved["first_feed_accepted"])
        self.assertNotIn("PRIVATE", json.dumps(saved))
        sdk.order_send.assert_not_called()

    def test_failed_first_upload_does_not_fake_acceptance_or_pairing(self):
        saved, sdk = self.run_ready(feed_failed=True)
        self.assertTrue(saved["registration_succeeded"])
        self.assertTrue(saved["bridge_ready"])
        self.assertFalse(saved["first_feed_accepted"])
        self.assertIsNone(saved["already_paired"])
        self.assertNotIn("PRIVATE", json.dumps(saved))
        sdk.order_send.assert_not_called()

    def test_failed_first_upload_then_success_updates_acceptance_evidence(self):
        saved, sdk = self.run_ready(feed_failed=True, feed_recovers=True, paired=True)
        self.assertTrue(saved["registration_succeeded"])
        self.assertTrue(saved["bridge_ready"])
        self.assertTrue(saved["already_paired"])
        self.assertTrue(saved["first_feed_accepted"])
        self.assertIsNone(saved["error_class"])
        self.assertNotIn("PRIVATE", json.dumps(saved))
        sdk.order_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
