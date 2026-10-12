"""Synthetic helper/chart integration; never starts MT5, UI or HTTP."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import timedelta
import io
import json
from unittest.mock import Mock, patch
import unittest

from bridge import mt5_chart_overlay as chart
from bridge import mt5_manual_bridge as manual
from bridge import mt5_trade_bridge as trade
from tests import test_mt5_trade_bridge as fixtures
from tests import test_mt5_chart_overlay as overlay_fixtures


class HelperOverlayTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TradeBridgeTests(methodName="test_demo_buy_preflight_attached_stops_and_one_execution")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        old = self.fixture.settings
        self.settings = manual.Settings(old.market, old.state_directory, old.account_mode, old.volume, True)
        self.mt5 = self.fixture.mt5
        self.fixture.terminal_info.data_path = str(self.fixture.base)
        self.mt5.order_send.side_effect = AssertionError("Chart display never sends orders")
        self.mt5.order_check.side_effect = AssertionError("Chart display never checks orders")
        self.adapter, self.exporter = Mock(), Mock()
        self.sent = []
        self.timer = 0.0

    def sleep(self, seconds):
        self.timer += seconds

    def post(self, settings, route, payload):
        self.sent.append((self.timer, route, deepcopy(payload)))
        if route == "register":
            return {"paired": True}
        if route == "chart":
            return {"proposal": None}
        if route == "poll":
            return {"preparation": None}
        return {}

    def run_helper(self, *, post=None, steps=3, sleep=None, exporter=None):
        feed = {"symbol": self.settings.market.symbol, "quote": {"bid": 2500, "ask": 2500.2, "time": fixtures.NOW.isoformat()}}
        self.output, self.errors = io.StringIO(), io.StringIO()
        with patch.object(trade, "terminal_is_running", side_effect=[True] * (steps + 1) + [False]), patch.object(
            manual.time, "monotonic", side_effect=lambda: self.timer
        ), patch.object(manual.market, "build_payload", return_value=feed), redirect_stdout(self.output), redirect_stderr(self.errors):
            result = manual.run_bridge(self.mt5, self.settings, post=post or self.post, clock=lambda: fixtures.NOW,
                                       sleep=sleep or self.sleep, adapter_factory=lambda: self.adapter,
                                       exporter_factory=lambda *args: exporter or self.exporter)
        self.mt5.order_send.assert_not_called()
        self.mt5.order_check.assert_not_called()
        self.adapter.prepare.assert_not_called()
        return result

    def test_chart_is_read_independently_each_cycle_and_manual_feed_refreshes_every_five_seconds(self):
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual([stamp for stamp, route, _ in self.sent if route == "market"], [0, 5, 10])
        self.assertEqual([stamp for stamp, route, _ in self.sent if route == "chart"], [0, 5, 10])
        self.assertEqual([stamp for stamp, route, _ in self.sent if route == "poll"], [0, 5, 10])
        self.assertEqual(self.exporter.publish.call_count, 3)
        for call in self.exporter.publish.call_args_list:
            self.assertIsNone(call.args[0])
            self.assertEqual(call.kwargs["execution"], trade.execution_metadata(self.fixture.symbol))
            self.assertEqual(call.kwargs["quote"]["bid"], 2500)
            self.assertEqual(call.kwargs["feed"]["quote"]["bid"], 2500)
        self.assertEqual(self.exporter.clear.call_count, 2)  # Startup and shutdown.
        self.assertEqual(trade.FEED_SECONDS, 60)  # Automatic/global cadence is unchanged.

    def test_chart_http_failure_clears_display_without_blocking_manual_poll(self):
        def post(settings, route, payload):
            result = self.post(settings, route, payload)
            if route == "chart":
                raise manual.HTTPError("https://private.invalid", 503, "private detail", {}, None)
            return result

        self.assertEqual(self.run_helper(post=post, steps=2), 0)
        self.assertEqual(sum(route == "poll" for _, route, _ in self.sent), 2)
        self.exporter.publish.assert_not_called()
        self.assertEqual(self.exporter.clear.call_count, 4)
        self.assertEqual(self.errors.getvalue().count("MT5 chart display unavailable."), 1)
        self.assertNotIn("private", self.errors.getvalue())
        self.assertNotIn("MT5 feed unavailable", self.errors.getvalue())

    def test_invalid_display_dto_clears_display_but_does_not_claim_extra_preparations(self):
        self.exporter.publish.side_effect = chart.OverlayError("synthetic invalid DTO")
        self.assertEqual(self.run_helper(steps=1), 0)
        self.assertEqual([route for _, route, _ in self.sent], ["register", "market", "chart", "poll"])
        self.assertEqual(self.exporter.clear.call_count, 3)

    def test_local_disconnect_clears_display_and_prevents_chart_and_claim_poll(self):
        def sleep(seconds):
            self.sleep(seconds)
            self.fixture.terminal_info.connected = False

        self.assertEqual(self.run_helper(steps=2, sleep=sleep), 0)
        self.assertEqual(sum(route == "chart" for _, route, _ in self.sent), 1)
        self.assertEqual(sum(route == "poll" for _, route, _ in self.sent), 1)
        self.assertEqual(self.exporter.clear.call_count, 3)
        self.assertIn("MT5 feed unavailable (terminal_disconnected).", self.errors.getvalue())

    def test_real_helper_without_verified_common_directory_fails_before_native_ui_or_http(self):
        post = Mock()
        with patch.object(trade, "terminal_is_running", return_value=True), patch.object(manual.native, "Win32Terminal") as native, redirect_stderr(io.StringIO()):
            result = manual.run_bridge(self.mt5, self.settings, post=post, clock=lambda: fixtures.NOW)
        self.assertEqual(result, 1)
        native.assert_not_called()
        post.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_refresh_chart_requires_only_device_id_and_does_not_use_claim_or_sdk(self):
        post = Mock(return_value={"proposal": None})
        feed = {"execution": {"digits": 2}, "quote": {"time": "synthetic"}}
        manual.refresh_chart(self.settings, "local-device", self.exporter, feed, post=post, clock=lambda: fixtures.NOW)
        post.assert_called_once_with(self.settings, "chart", {"device_id": "local-device"})
        self.exporter.publish.assert_called_once_with(None, execution=feed["execution"], quote=feed["quote"], observed_at=fixtures.NOW, feed=feed)
        self.adapter.prepare.assert_not_called()
        post.return_value = {"proposal": None, "claim_id": "forbidden"}
        with self.assertRaises(chart.OverlayError):
            manual.refresh_chart(self.settings, "local-device", self.exporter, feed, post=post)

    def test_chart_transport_uses_scoped_manual_route_and_bearer_without_live_http(self):
        response = Mock()
        response.status = 200
        response.read.return_value = b'{"proposal":null}'
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(manual.request, "build_opener", return_value=opener):
            self.assertEqual(manual.api_post(self.settings, "chart", {"device_id": "synthetic"}), {"proposal": None})
        request = opener.open.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/api/mt5/manual/chart"))
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.settings.market.key)
        self.assertEqual(json.loads(request.data), {"device_id": "synthetic"})

    def test_delayed_chart_response_uses_new_snapshot_only_after_strict_response_validation(self):
        events = []
        elapsed = [0]
        cached = {"execution": {"digits": 1}, "quote": {"time": "old"}}
        fresh = {"execution": {"digits": 3}, "quote": {"time": "new"}}

        def post(*args):
            events.append("chart response")
            elapsed[0] += 12
            return {"proposal": None}

        def capture():
            events.append("fresh local capture")
            return fresh

        manual.refresh_chart(self.settings, "local-device", self.exporter, cached,
                             post=post, fresh_feed=capture,
                             clock=lambda: fixtures.NOW + timedelta(seconds=elapsed[0]))
        self.assertEqual(events, ["chart response", "fresh local capture"])
        self.exporter.publish.assert_called_once_with(
            None, execution=fresh["execution"], quote=fresh["quote"], feed=fresh,
            observed_at=fixtures.NOW + timedelta(seconds=12))
        self.assertEqual(cached, {"execution": {"digits": 1}, "quote": {"time": "old"}})
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

        capture = Mock(return_value=fresh)
        for reply in ({"proposal": None, "claim_id": "forbidden"}, {}, []):
            with self.subTest(reply=reply), self.assertRaises(chart.OverlayError):
                manual.refresh_chart(self.settings, "local-device", self.exporter, cached,
                                     post=Mock(return_value=reply), fresh_feed=capture)
        capture.assert_not_called()
        self.assertEqual(self.exporter.publish.call_count, 1)

    def test_fresh_capture_failure_propagates_without_export_or_order_operations(self):
        for error in (trade.GuardError("Synthetic account changed"),
                      manual.market.MarketDataError("Synthetic quote unavailable")):
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                manual.refresh_chart(self.settings, "local-device", self.exporter, {},
                                     post=Mock(return_value={"proposal": None}),
                                     fresh_feed=Mock(side_effect=error))
        self.exporter.publish.assert_not_called()
        self.adapter.prepare.assert_not_called()
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_fresh_snapshot_cannot_extend_original_proposal_expiry(self):
        holder = overlay_fixtures.ExportTests(methodName="test_active_row_has_exact_18_fields_safe_path_and_unchanged_prices")
        holder.setUp()
        self.addCleanup(holder.doCleanups)
        proposal = overlay_fixtures.proposal()
        original_expiry = overlay_fixtures.NOW + timedelta(seconds=8)
        proposal["expires_at"] = original_expiry.isoformat()
        original = deepcopy(proposal)
        observed = overlay_fixtures.NOW + timedelta(seconds=3)
        fresh = overlay_fixtures.readiness_feed(observed)
        manual.refresh_chart(self.settings, "local-device", holder.exporter, {},
                             post=Mock(return_value={"proposal": proposal}),
                             fresh_feed=lambda: fresh, clock=lambda: observed)
        fields = holder.exporter.path.read_text(encoding="ascii").split(";")
        self.assertEqual(int(fields[12]), int(original_expiry.timestamp()))
        saved = holder.exporter.path.read_bytes()
        observed = original_expiry + timedelta(seconds=1)
        fresh = overlay_fixtures.readiness_feed(observed)
        with self.assertRaises(chart.OverlayError):
            manual.refresh_chart(self.settings, "local-device", holder.exporter, {},
                                 post=Mock(return_value={"proposal": proposal}),
                                 fresh_feed=lambda: fresh, clock=lambda: observed)
        self.assertEqual(holder.exporter.path.read_bytes(), saved)
        self.assertEqual(proposal, original)
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
