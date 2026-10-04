import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bridge import mt5_market_bridge as bridge


class MT5BridgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.terminal = Path(self.directory.name) / "terminal64.exe"
        self.terminal.write_bytes(b"mock executable; never run")
        self.key = "private-test-bridge-key"
        self.url = "https://example.invalid/api/market/feed"
        self.settings = bridge.Settings(self.terminal, "XAUUSD.test", self.url, self.key)
        self.now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
        self.now_seconds = int(self.now.timestamp())
        self.rates = [
            {
                "time": self.now_seconds - (64 - index) * 900,
                "open": 2500.0,
                "high": 2504.0,
                "low": 2498.0,
                "close": 2502.0,
                "tick_volume": 120,
            }
            for index in range(64)
        ]
        # A restricted SDK mock makes any accidental account/trade API a failure.
        self.mt5 = Mock(
            spec=[
                "initialize", "shutdown", "terminal_info", "symbol_info",
                "symbol_info_tick", "copy_rates_from_pos", "TIMEFRAME_M15",
            ]
        )
        self.mt5.TIMEFRAME_M15 = 15
        self.mt5.initialize.return_value = True
        self.mt5.terminal_info.return_value = SimpleNamespace(
            connected=True, path=str(self.terminal.parent)
        )
        self.mt5.symbol_info.return_value = SimpleNamespace(
            currency_base="XAU", currency_profit="USD"
        )
        self.mt5.symbol_info_tick.return_value = SimpleNamespace(
            bid=2502.0, ask=2502.5, time=self.now_seconds - 15
        )
        self.mt5.copy_rates_from_pos.return_value = self.rates

    def payload(self):
        return bridge.build_payload(self.mt5, self.settings, self.now)

    def test_exact_symbol_completed_bar_read_and_utc_payload(self):
        result = self.payload()
        self.mt5.copy_rates_from_pos.assert_called_once_with("XAUUSD.test", 15, 1, 64)
        self.mt5.symbol_info.assert_called_once_with("XAUUSD.test")
        self.mt5.symbol_info_tick.assert_called_once_with("XAUUSD.test")
        self.assertEqual(set(result), {"symbol", "timeframe", "source", "quote", "candles"})
        self.assertEqual(result["symbol"], "XAUUSD.test")
        self.assertEqual(result["timeframe"], "M15")
        self.assertEqual(result["source"], "MetaTrader 5")
        self.assertEqual(result["quote"], {
            "bid": 2502.0, "ask": 2502.5, "time": "2026-10-04T11:59:45Z"
        })
        self.assertEqual(result["candles"][-1], {
            "time": "2026-10-04T11:45:00Z", "open": 2500.0, "high": 2504.0,
            "low": 2498.0, "close": 2502.0, "tick_volume": 120,
        })
        self.assertNotIn(self.key, json.dumps(result))
        self.assertNotIn(self.key, repr(self.settings))

    def test_broker_metadata_must_identify_gold_in_us_dollars(self):
        for metadata in (None, SimpleNamespace(),
                         SimpleNamespace(currency_base="EUR", currency_profit="USD"),
                         SimpleNamespace(currency_base="XAU", currency_profit="EUR")):
            with self.subTest(metadata=metadata):
                self.mt5.symbol_info.return_value = metadata
                with self.assertRaises(bridge.MarketDataError):
                    self.payload()
        self.mt5.symbol_info_tick.assert_not_called()
        self.mt5.copy_rates_from_pos.assert_not_called()

    def test_disconnected_or_different_terminal_is_rejected_before_market_reads(self):
        for info in (None, SimpleNamespace(connected=False),
                     SimpleNamespace(connected=True, path=None),
                     SimpleNamespace(connected=True, path=str(self.terminal.parent / "other"))):
            with self.subTest(info=info):
                self.mt5.terminal_info.return_value = info
                with self.assertRaises(bridge.MarketDataError):
                    self.payload()
        self.mt5.symbol_info.assert_not_called()

    def test_weekend_quote_keeps_original_time_and_gaps_are_never_filled(self):
        friday = self.now - timedelta(days=2)
        friday_seconds = int(friday.timestamp())
        self.mt5.symbol_info_tick.return_value.time = friday_seconds
        self.mt5.copy_rates_from_pos.return_value = [
            {**rate, "time": rate["time"] - 2 * 86400} for rate in self.rates[-4:]
        ]
        result = self.payload()
        self.assertEqual(result["quote"]["time"], "2026-10-02T12:00:00Z")
        self.assertEqual(len(result["candles"]), 4)
        self.assertEqual(result["candles"][-1]["time"], "2026-10-02T11:45:00Z")
        self.assertNotIn("received_at", result)

    def test_available_history_is_sorted_without_interpolation(self):
        self.mt5.copy_rates_from_pos.return_value = [self.rates[-1], self.rates[-4],
                                                   self.rates[-2], self.rates[-8]]
        result = self.payload()["candles"]
        self.assertEqual(len(result), 4)
        self.assertEqual([item["time"] for item in result], sorted(item["time"] for item in result))
        self.assertEqual(result[0]["time"], "2026-10-04T10:00:00Z")

    def test_missing_or_insufficient_history_is_rejected(self):
        for rates in (None, [], self.rates[:3], self.rates + [self.rates[-1]]):
            with self.subTest(count=None if rates is None else len(rates)):
                self.mt5.copy_rates_from_pos.return_value = rates
                with self.assertRaises(bridge.MarketDataError):
                    self.payload()

    def test_missing_crossed_nonfinite_zero_old_or_future_quotes_are_rejected(self):
        for tick in (
            None,
            SimpleNamespace(bid=0, ask=2502.5, time=self.now_seconds),
            SimpleNamespace(bid=float("nan"), ask=2502.5, time=self.now_seconds),
            SimpleNamespace(bid=2502, ask=float("inf"), time=self.now_seconds),
            SimpleNamespace(bid=2503, ask=2502, time=self.now_seconds),
            SimpleNamespace(bid=2502, ask=2502.5, time=self.now_seconds + 31),
            SimpleNamespace(bid=2502, ask=2502.5, time=self.now_seconds - 10 * 86400 - 1),
        ):
            with self.subTest(tick=tick):
                self.mt5.symbol_info_tick.return_value = tick
                with self.assertRaises(bridge.MarketDataError):
                    self.payload()
        self.mt5.copy_rates_from_pos.assert_not_called()

    def test_invalid_unfinished_duplicate_or_missing_candles_are_rejected(self):
        for change in (
            {"time": self.now_seconds}, {"time": self.now_seconds - 899},
            {"time": self.rates[0]["time"]}, {"open": 0}, {"close": float("nan")},
            {"high": 2499}, {"low": 2503}, {"tick_volume": -1}, {"tick_volume": 1.5},
        ):
            with self.subTest(change=change):
                self.mt5.copy_rates_from_pos.return_value = [
                    *self.rates[:-1], {**self.rates[-1], **change}
                ]
                with self.assertRaises(bridge.MarketDataError):
                    self.payload()
        self.mt5.copy_rates_from_pos.return_value = [*self.rates[:-1], {"time": self.now_seconds - 900}]
        with self.assertRaises(bridge.MarketDataError):
            self.payload()

    def test_timezone_aware_non_utc_clock_produces_same_utc_result(self):
        local_now = self.now.astimezone(timezone(timedelta(hours=3)))
        self.assertEqual(bridge.build_payload(self.mt5, self.settings, local_now), self.payload())
        with self.assertRaises(bridge.MarketDataError):
            bridge.build_payload(self.mt5, self.settings, self.now.replace(tzinfo=None))

    def test_safe_url_validation(self):
        self.assertEqual(bridge.validate_feed_url(self.url), self.url)
        for url in (
            "http://example.invalid/api/market/feed", "https:///api/market/feed",
            "https://user:private@example.invalid/api/market/feed",
            "https://example.invalid/api/market/feed?secret=private",
            "https://example.invalid/api/market/feed?", "https://example.invalid/api/market/feed#",
            "https://example.invalid/other", "https://example.invalid:bad/api/market/feed",
            " https://example.invalid/api/market/feed", "https://example.invalid/\napi/market/feed",
        ):
            with self.subTest(url=url):
                with self.assertRaises(bridge.ConfigurationError):
                    bridge.validate_feed_url(url)

    def test_settings_require_existing_executable_and_private_environment_key(self):
        args = argparse.Namespace(terminal=str(self.terminal), symbol="XAUUSD.test")
        env = {"MARKET_BRIDGE_URL": self.url, "MARKET_BRIDGE_KEY": self.key}
        self.assertEqual(bridge.load_settings(args, env), self.settings)
        for key in (None, "", " leading", "line\nsecret", "nonascii-\u00e9"):
            with self.subTest(key=key):
                bad_env = {"MARKET_BRIDGE_URL": self.url}
                if key is not None:
                    bad_env["MARKET_BRIDGE_KEY"] = key
                with self.assertRaises(bridge.ConfigurationError):
                    bridge.load_settings(args, bad_env)
        for terminal in (self.terminal.parent / "missing.exe", self.terminal.parent):
            with self.subTest(terminal=terminal):
                with self.assertRaises(bridge.ConfigurationError):
                    bridge.load_settings(argparse.Namespace(terminal=str(terminal), symbol="XAUUSD.test"), env)

    def test_http_payload_header_timeout_and_no_response_body_access(self):
        payload = self.payload()
        response = Mock(status=200)
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(bridge.request, "build_opener", return_value=opener) as create_opener:
            bridge.post_payload(self.settings, payload)
        self.assertIsInstance(create_opener.call_args.args[0], bridge.NoRedirect)
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, self.url)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), f"Bearer {self.key}")
        self.assertEqual(json.loads(req.data), payload)
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 15})
        response.read.assert_not_called()

    def test_redirects_are_not_followed_and_unsafe_url_cannot_reach_http(self):
        self.assertIsNone(bridge.NoRedirect().redirect_request(
            None, None, 302, "Moved", {}, "https://elsewhere.invalid/private"
        ))
        bad_settings = bridge.Settings(self.terminal, "XAUUSD.test", "http://unsafe.invalid/api/market/feed", self.key)
        with patch.object(bridge.request, "build_opener") as create_opener:
            with self.assertRaises(bridge.ConfigurationError):
                bridge.post_payload(bad_settings, {})
            create_opener.assert_not_called()

    def test_once_connects_exact_terminal_and_uses_only_read_apis(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(bridge, "datetime") as clock, patch.object(bridge, "post_payload") as post:
            clock.now.return_value = self.now
            clock.fromtimestamp.side_effect = datetime.fromtimestamp
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(bridge.run_bridge(self.mt5, self.settings, once=True), 0)
        self.mt5.initialize.assert_called_once_with(str(self.terminal), timeout=15000)
        self.mt5.shutdown.assert_called_once_with()
        post.assert_called_once()
        called = {call[0] for call in self.mt5.mock_calls}
        self.assertLessEqual(called, {
            "initialize", "shutdown", "terminal_info", "symbol_info",
            "symbol_info_tick", "copy_rates_from_pos",
        })
        self.assertEqual(stdout.getvalue(), "Market update sent.\n")
        self.assertEqual(stderr.getvalue(), "")

    def test_errors_do_not_print_key_url_or_provider_error_text(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        private_error = RuntimeError(f"{self.key} {self.url} private-terminal-error")
        with patch.object(bridge, "build_payload", side_effect=private_error), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(bridge.run_bridge(self.mt5, self.settings, once=True), 1)
        self.assertEqual(stderr.getvalue(), "Bridge unavailable (RuntimeError).\n")
        combined = stdout.getvalue() + stderr.getvalue()
        for private in (self.key, self.url, "private-terminal-error", str(self.terminal)):
            self.assertNotIn(private, combined)
        self.mt5.shutdown.assert_called_once_with()

    def test_http_failure_is_private_and_the_next_loop_waits_one_minute(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(bridge, "build_payload", return_value={}), \
                patch.object(bridge, "post_payload", side_effect=TimeoutError(self.key)), \
                patch.object(bridge.time, "monotonic", side_effect=[100, 102]), \
                patch.object(bridge.time, "sleep", side_effect=KeyboardInterrupt) as sleep, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(bridge.run_bridge(self.mt5, self.settings), 0)
        sleep.assert_called_once_with(58)
        self.assertEqual(stderr.getvalue(), "Bridge unavailable (TimeoutError).\n")
        self.assertEqual(stdout.getvalue(), "Bridge stopped.\n")
        self.mt5.shutdown.assert_called_once_with()

    def test_initialization_failure_stops_without_market_or_http_access(self):
        self.mt5.initialize.return_value = False
        with patch.object(bridge, "post_payload") as post, redirect_stderr(io.StringIO()):
            self.assertEqual(bridge.run_bridge(self.mt5, self.settings, once=True), 1)
        self.mt5.symbol_info.assert_not_called()
        post.assert_not_called()
        self.mt5.shutdown.assert_called_once_with()

    def test_cli_imports_sdk_only_after_safe_manual_configuration(self):
        args = ["--terminal", str(self.terminal), "--symbol", "XAUUSD.test", "--once"]
        with patch.object(bridge.os, "name", "nt"), \
                patch.object(bridge, "load_settings", return_value=self.settings), \
                patch.object(bridge.importlib, "import_module", return_value=self.mt5) as sdk_import, \
                patch.object(bridge, "run_bridge", return_value=0) as run:
            self.assertEqual(bridge.main(args), 0)
        sdk_import.assert_called_once_with("MetaTrader5")
        run.assert_called_once_with(self.mt5, self.settings, once=True)
        with patch.object(bridge.os, "name", "nt"), \
                patch.object(bridge, "load_settings", side_effect=bridge.ConfigurationError(self.key)), \
                patch.object(bridge.importlib, "import_module") as sdk_import, \
                redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(bridge.main(args), 1)
        sdk_import.assert_not_called()
        self.assertNotIn(self.key, errors.getvalue())

    def test_cli_rejects_non_windows_without_loading_sdk(self):
        with patch.object(bridge.os, "name", "posix"), \
                patch.object(bridge.importlib, "import_module") as sdk_import, \
                redirect_stderr(io.StringIO()):
            self.assertEqual(bridge.main(["--terminal", "terminal64.exe", "--symbol", "XAUUSD"]), 1)
        sdk_import.assert_not_called()

    def test_cli_does_not_accept_or_echo_account_credentials(self):
        with redirect_stderr(io.StringIO()) as errors:
            with self.assertRaises(SystemExit) as result:
                bridge.main(["--terminal", "terminal64.exe", "--symbol", "XAUUSD", "--password", self.key])
        self.assertEqual(result.exception.code, 2)
        self.assertNotIn(self.key, errors.getvalue())
        self.assertNotIn("password", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
