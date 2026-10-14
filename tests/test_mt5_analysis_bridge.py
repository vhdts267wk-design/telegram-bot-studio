"""Mock-only analysis startup tests: no SDK, network, tickets, or trades."""

import ast
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import math
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from bridge import mt5_analysis_bridge as analysis
from bridge import mt5_chart_overlay as chart
from bridge import mt5_market_bridge as market


class AnalysisBridgeTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 15, 12, 30, tzinfo=timezone.utc)
        self.settings = market.Settings(Path("C:/synthetic/terminal64.exe"), "XAUUSD",
                                        "https://example.invalid/api/market/feed", "private-test-key")
        self.sdk = Mock(spec=[
            "initialize", "shutdown", "terminal_info", "symbol_info", "symbol_info_tick",
            "copy_rates_from_pos", "TIMEFRAME_M30", "TIMEFRAME_M15", "TIMEFRAME_M5", "TIMEFRAME_M1",
            "account_info", "positions_get", "orders_get", "order_calc_profit", "order_calc_margin",
            "ORDER_TYPE_BUY", "ORDER_TYPE_SELL", "ACCOUNT_TRADE_MODE_DEMO",
        ])
        self.sdk.initialize.return_value = True
        self.sdk.terminal_info.return_value = SimpleNamespace(connected=True, path=str(self.settings.terminal.parent))
        self.account = SimpleNamespace(trade_mode=0, login=123, server="Synthetic Demo", currency="USD",
                                       equity=10000.0, margin_free=9000.0)
        self.sdk.account_info.return_value = self.account
        self.symbol = SimpleNamespace(currency_base="XAU", currency_profit="USD", trade_tick_size=0.01,
                                      point=0.01, digits=2, trade_stops_level=10)
        self.sdk.symbol_info.return_value = self.symbol
        stamp = int(self.now.timestamp())
        self.sdk.symbol_info_tick.return_value = SimpleNamespace(bid=2500.0, ask=2500.2, time=stamp)
        self.sdk.ACCOUNT_TRADE_MODE_DEMO = 0
        self.sdk.ORDER_TYPE_BUY, self.sdk.ORDER_TYPE_SELL = 0, 1
        self.sdk.positions_get.return_value, self.sdk.orders_get.return_value = (), ()
        self.sdk.order_calc_margin.return_value = 25.0
        self.sdk.order_calc_profit.side_effect = lambda side, symbol, volume, start, end: (end - start) * (1 if side == 0 else -1)
        self.rates = {}
        for label, seconds in market.TIMEFRAMES.items():
            setattr(self.sdk, "TIMEFRAME_" + label, seconds)
            end = stamp // seconds * seconds
            self.rates[seconds] = [{"time": end - (64 - i) * seconds, "open": 2500.0, "high": 2502.0,
                                   "low": 2499.0, "close": 2501.0, "tick_volume": 100} for i in range(64)]
        self.sdk.copy_rates_from_pos.side_effect = lambda symbol, period, start, count: self.rates[period]
        self.events = []
        self.exporter = Mock(spec=["clear", "publish"])
        self.exporter.clear.side_effect = lambda clock: self.events.append("clear")
        self.exporter.publish.side_effect = lambda value, **kwargs: self.events.append("publish")
        self.factory = Mock(return_value=self.exporter)
        self.post = Mock(side_effect=lambda settings, payload: self.events.append("post"))
        self.output = io.StringIO()

    def run_once(self):
        with redirect_stdout(self.output), redirect_stderr(self.output):
            return analysis.run_bridge(self.sdk, self.settings, once=True, post=self.post,
                                       clock=lambda: self.now, exporter_factory=self.factory)

    def test_complete_feed_is_posted_before_waiting_readiness_and_shutdown_clears_display(self):
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.events, ["clear", "post", "publish", "clear"])
        self.sdk.shutdown.assert_called_once_with()
        settings, payload = self.post.call_args.args
        self.assertIs(settings, self.settings)
        self.assertEqual(payload["execution"], {"tick_size": 0.01, "point": 0.01, "digits": 2, "stops_level": 10})
        self.assertEqual(set(payload["timeframes"]), {"M30", "M15", "M5", "M1"})
        self.assertNotIn("device_id", payload)
        self.assertEqual(chart.feed_readiness(payload, symbol="XAUUSD", broker_offset_minutes=0,
                                              observed_at=self.now)[0], [64] * 4)
        self.assertEqual(self.exporter.publish.call_args.args, (None,))
        self.assertIs(self.exporter.publish.call_args.kwargs["feed"], payload)
        self.assertFalse(hasattr(self.sdk, "order_send"))
        self.assertFalse(hasattr(self.sdk, "order_check"))

    def test_complete_feed_passes_exact_server_ingestion_validation(self):
        try:
            from bot.market_monitor import validate_feed
        except ModuleNotFoundError:
            # The Windows helper needs no server packages. Execute the exact
            # pure validator definitions when FastAPI/Telegram are absent.
            source_path = Path(__file__).resolve().parents[1] / "bot" / "market_monitor.py"
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            selected = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef)
                                       and node.name in {"_utc", "_number", "validate_feed"}], type_ignores=[])
            namespace = {"datetime": datetime, "timezone": timezone, "timedelta": timedelta,
                         "math": math, "re": re, "UUID": UUID}
            exec(compile(selected, str(source_path), "exec"), namespace)
            validate_feed = namespace["validate_feed"]
        self.assertEqual(self.run_once(), 0)
        payload = self.post.call_args.args[1]
        cleaned = validate_feed(payload, self.now, "XAUUSD")
        self.assertEqual(cleaned["execution"], payload["execution"])
        self.assertEqual({frame: len(rows) for frame, rows in cleaned["timeframes"].items()},
                         {frame: 64 for frame in market.TIMEFRAMES})

    def test_transport_failure_never_publishes_and_diagnostics_do_not_repeat_private_error_text(self):
        self.post.side_effect = RuntimeError("private-test-key https://private.example/account/123")
        self.assertEqual(self.run_once(), 1)
        self.exporter.publish.assert_not_called()
        self.assertGreaterEqual(self.exporter.clear.call_count, 2)
        self.assertIn("RuntimeError", self.output.getvalue())
        for private in ("private-test-key", "private.example", "account/123"):
            self.assertNotIn(private, self.output.getvalue())

    def test_forming_candle_is_rejected_before_transport(self):
        self.rates[60][-1]["time"] = int(self.now.timestamp())
        self.assertEqual(self.run_once(), 1)
        self.post.assert_not_called()
        self.exporter.publish.assert_not_called()

    def test_real_account_is_rejected_without_post_or_export(self):
        self.account.trade_mode = 2
        self.assertEqual(self.run_once(), 2)
        self.post.assert_not_called()
        self.factory.assert_not_called()

    def test_account_switch_during_capture_blocks_transport_and_stops(self):
        original = self.sdk.copy_rates_from_pos.side_effect
        def switch(*args):
            self.account.login = 456
            return original(*args)
        self.sdk.copy_rates_from_pos.side_effect = switch
        self.assertEqual(self.run_once(), 2)
        self.post.assert_not_called()
        self.exporter.publish.assert_not_called()

    def test_account_switch_during_network_wait_cannot_renew_readiness(self):
        self.post.side_effect = lambda settings, payload: setattr(self.account, "login", 456)
        self.assertEqual(self.run_once(), 2)
        self.post.assert_called_once()
        self.exporter.publish.assert_not_called()

    def test_disconnected_or_wrong_terminal_never_uploads(self):
        for failure in ("disconnect", "wrong_terminal"):
            with self.subTest(failure=failure):
                self.setUp()
                info = self.sdk.terminal_info.return_value
                if failure == "disconnect":
                    info.connected = False
                else:
                    info.path = "C:/wrong-terminal"
                self.assertEqual(self.run_once(), 1)
                self.post.assert_not_called()
                self.factory.assert_not_called()

    def test_outage_retries_at_five_second_cadence_and_recovers(self):
        self.post.side_effect = [RuntimeError("private"), None]
        sleep = Mock(side_effect=[None, KeyboardInterrupt])
        with redirect_stdout(self.output), redirect_stderr(self.output), patch.object(analysis.time, "monotonic", return_value=10):
            result = analysis.run_bridge(self.sdk, self.settings, post=self.post, clock=lambda: self.now,
                                         sleep=sleep, exporter_factory=self.factory)
        self.assertEqual(result, 0)
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self.exporter.publish.call_count, 1)
        self.assertEqual([call.args for call in sleep.call_args_list], [(5.0,), (5.0,)])
        self.assertIn("Analysis feed restored.", self.output.getvalue())

    def test_initialization_failure_shuts_down_without_transport(self):
        self.sdk.initialize.return_value = False
        self.assertEqual(self.run_once(), 1)
        self.post.assert_not_called()
        self.sdk.shutdown.assert_called_once()

    def test_invalid_grid_metadata_never_uploads(self):
        for field, value in (("digits", True), ("digits", 11), ("trade_stops_level", -1),
                             ("trade_tick_size", 0), ("point", float("nan"))):
            with self.subTest(field=field, value=value):
                self.setUp()
                setattr(self.symbol, field, value)
                self.assertEqual(self.run_once(), 1)
                self.post.assert_not_called()
                self.exporter.publish.assert_not_called()

    def test_source_imports_only_market_and_chart_helpers(self):
        source = Path(analysis.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        helper_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "bridge":
                helper_imports.extend(alias.name for alias in node.names)
            if isinstance(node, ast.Import):
                helper_imports.extend(alias.name for alias in node.names if alias.name.startswith("mt5_"))
        self.assertEqual(set(helper_imports), {"mt5_market_bridge", "mt5_chart_overlay"})
        called_attributes = {node.func.attr for node in ast.walk(tree)
                             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertFalse(called_attributes & {"order_send", "order_check", "execute_offer", "prepare_ticket", "api_post"})
        self.assertIn("post_payload", source)
        self.assertNotIn("device_id", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
