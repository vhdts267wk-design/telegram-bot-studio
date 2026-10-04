"""All trade bridge tests use synthetic SDK/HTTP mocks; no live terminal calls."""

import argparse
from contextlib import redirect_stdout, redirect_stderr
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from uuid import uuid4

from bridge import mt5_market_bridge as market
from bridge import mt5_trade_bridge as bridge


NOW = datetime(2026, 10, 4, 12, 30, tzinfo=timezone.utc)


class TradeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.terminal = self.base / "terminal64.exe"
        self.terminal.write_bytes(b"fake executable; never run")
        self.settings = bridge.Settings(
            market.Settings(self.terminal, "XAUUSD.test", "https://example.invalid/api/market/feed", "private-test-key-" + "x" * 40),
            self.base / "private", "demo", Decimal("0.01"), True,
        )
        self.ledger = bridge.Ledger(self.settings.state_directory)
        self.addCleanup(self.ledger.close)
        self.account = SimpleNamespace(
            login=991122334, server="private-test-broker-server", trade_mode=0,
            trade_allowed=True, trade_expert=True, margin_mode=2,
            balance="PRIVATE_BALANCE_NOT_USED", equity="PRIVATE_EQUITY_NOT_USED",
        )
        self.terminal_info = SimpleNamespace(
            connected=True, path=str(self.terminal.parent), trade_allowed=True, tradeapi_disabled=False,
        )
        self.symbol = SimpleNamespace(
            currency_base="XAU", currency_profit="USD", trade_mode=4, order_mode=49,
            volume_min=0.01, volume_max=10.0, volume_step=0.01,
            trade_tick_size=0.01, point=0.01, digits=2, trade_stops_level=10,
            filling_mode=3, trade_exemode=1,
        )
        self.tick = SimpleNamespace(bid=2499.9, ask=2500.1, time=int(NOW.timestamp()) - 1)
        self.mt5 = Mock(spec=[
            "initialize", "shutdown", "terminal_info", "account_info", "symbol_info", "symbol_info_tick",
            "positions_get", "orders_get", "order_check", "order_send", "ACCOUNT_TRADE_MODE_DEMO",
            "ORDER_FILLING_FOK", "ORDER_FILLING_IOC", "TRADE_ACTION_DEAL", "ORDER_TYPE_BUY", "ORDER_TYPE_SELL",
            "ORDER_TIME_GTC", "TRADE_RETCODE_DONE",
        ])
        self.mt5.ACCOUNT_TRADE_MODE_DEMO = 0
        self.mt5.ORDER_FILLING_FOK = 0
        self.mt5.ORDER_FILLING_IOC = 1
        self.mt5.TRADE_ACTION_DEAL = 1
        self.mt5.ORDER_TYPE_BUY = 0
        self.mt5.ORDER_TYPE_SELL = 1
        self.mt5.ORDER_TIME_GTC = 0
        self.mt5.TRADE_RETCODE_DONE = 10009
        self.mt5.initialize.return_value = True
        self.mt5.terminal_info.return_value = self.terminal_info
        self.mt5.account_info.return_value = self.account
        self.mt5.symbol_info.return_value = self.symbol
        self.mt5.symbol_info_tick.return_value = self.tick
        self.mt5.positions_get.return_value = ()
        self.mt5.orders_get.return_value = ()
        self.mt5.order_check.return_value = SimpleNamespace(retcode=0)
        self.mt5.order_send.return_value = SimpleNamespace(retcode=10009, volume=0.01, order=440011)
        bridge.check_bound_account(self.mt5, self.settings, self.ledger)
        self.offer = {
            "id": str(uuid4()), "claim_id": str(uuid4()), "expires_at": bridge.iso_date(NOW + timedelta(minutes=5)),
            "payload": {
                "symbol": "XAUUSD.test", "account_mode": "demo", "volume": 0.01,
                "direction": "BUY", "entry": 2500.0, "stop": 2490.0, "target": 2520.0,
                "bar_time": "2026-10-04T12:00:00Z", "strategy_id": bridge.STRATEGY_ID, "max_drift_r": 0.1,
            },
        }

    def execute(self, offer=None, *, clock=None):
        return bridge.execute_offer(self.mt5, self.settings, self.ledger, self.offer if offer is None else offer, clock or (lambda: NOW))

    def test_demo_buy_preflight_attached_stops_and_one_execution(self):
        def sent(request):
            # The reservation is committed and visible through another connection
            # before the first order_send can reach a broker.
            other = bridge.Ledger(self.settings.state_directory)
            try:
                self.assertEqual(other.pending()[0]["result"], {"status": "unknown", "code": 0})
            finally:
                other.close()
            return SimpleNamespace(retcode=10009, volume=0.01, order=440011)
        self.mt5.order_send.side_effect = sent
        result = self.execute()
        self.assertEqual(result["status"], "filled")
        self.assertEqual(result["code"], 10009)
        self.assertEqual(result["order_ticket"], 440011)
        self.mt5.order_check.assert_called_once()
        self.mt5.order_send.assert_called_once()
        request = self.mt5.order_send.call_args.args[0]
        self.assertEqual(request["action"], 1)
        self.assertEqual(request["type"], 0)
        self.assertEqual(request["volume"], 0.01)
        self.assertEqual((request["sl"], request["tp"]), (2490.0, 2520.0))
        self.assertEqual(request["price"], 2500.1)
        self.assertEqual(request["type_filling"], 0)
        self.assertLessEqual(len(request["comment"]), 31)
        self.assertNotIn("position", request)
        self.assertNotIn("order", request)
        self.mt5.positions_get.assert_not_called()

    def test_sell_and_market_execution_omit_price_and_use_ioc_when_needed(self):
        self.offer["payload"].update(direction="SELL", stop=2510.0, target=2480.0)
        self.symbol.trade_exemode = 2
        self.symbol.filling_mode = 2
        self.assertEqual(self.execute()["status"], "filled")
        request = self.mt5.order_send.call_args.args[0]
        self.assertEqual(request["type"], 1)
        self.assertEqual(request["type_filling"], 1)
        self.assertNotIn("price", request)

    def test_duplicate_claim_and_restart_never_send_again(self):
        first = self.execute()
        self.offer["claim_id"] = str(uuid4())
        restarted = bridge.Ledger(self.settings.state_directory)
        try:
            result = bridge.execute_offer(self.mt5, self.settings, restarted, self.offer, lambda: NOW)
            self.assertEqual(result, first)
            self.assertEqual(restarted.device_id, self.ledger.device_id)
        finally:
            restarted.close()
        self.mt5.order_send.assert_called_once()
        self.mt5.order_check.assert_called_once()

    def test_interrupted_reservation_is_unknown_and_never_replayed(self):
        self.ledger.reserve(self.offer)
        restarted = bridge.Ledger(self.settings.state_directory)
        try:
            result = bridge.execute_offer(self.mt5, self.settings, restarted, self.offer, lambda: NOW)
            self.assertEqual(result, {"status": "unknown", "code": 0})
        finally:
            restarted.close()
        self.mt5.order_send.assert_not_called()
        self.mt5.order_check.assert_not_called()

    def test_original_offer_cannot_be_changed_after_it_was_reserved(self):
        self.ledger.reserve(self.offer)
        self.offer["payload"]["stop"] = 2480.0
        with self.assertRaises(bridge.GuardError):
            self.execute()
        self.mt5.order_send.assert_not_called()

    def test_real_account_or_switched_login_server_or_terminal_blocks_execution(self):
        changes = [
            (self.account, "trade_mode", 2), (self.account, "login", 334455667),
            (self.account, "server", "another-private-server"),
            (self.terminal_info, "path", str(self.terminal.parent / "other")),
        ]
        for obj, field, changed in changes:
            original = getattr(obj, field)
            with self.subTest(field=field):
                setattr(obj, field, changed)
                offer = copy.deepcopy(self.offer)
                offer["id"] = str(uuid4())
                self.assertEqual(self.execute(offer)["status"], "failed")
                setattr(obj, field, original)
        self.mt5.order_send.assert_not_called()

    def test_capability_mode_symbol_volume_and_expiry_guards_never_round_or_send(self):
        modifications = [
            (self.terminal_info, "trade_allowed", False), (self.terminal_info, "tradeapi_disabled", True),
            (self.account, "trade_allowed", False), (self.account, "trade_expert", False),
            (self.symbol, "currency_base", "EUR"), (self.symbol, "currency_profit", "EUR"),
            (self.symbol, "trade_mode", 3), (self.symbol, "order_mode", 1),
            (self.symbol, "volume_min", 0.1), (self.symbol, "volume_step", 0.03),
            (self.symbol, "trade_tick_size", 7),
        ]
        for obj, field, changed in modifications:
            original = getattr(obj, field)
            with self.subTest(field=field):
                setattr(obj, field, changed)
                offer = copy.deepcopy(self.offer)
                offer["id"] = str(uuid4())
                self.assertEqual(self.execute(offer)["status"], "failed")
                setattr(obj, field, original)
        for name, value in (
            ("symbol", "XAUUSD.wrong"), ("volume", 0.02), ("volume", True),
            ("account_mode", "real"), ("direction", "CLOSE"), ("entry", float("inf")),
            ("strategy_id", "other"), ("max_drift_r", 1.0),
        ):
            with self.subTest(payload=name):
                offer = copy.deepcopy(self.offer)
                offer["id"] = str(uuid4())
                offer["payload"][name] = value
                if name == "entry":
                    with self.assertRaises(ValueError):
                        self.execute(offer)
                else:
                    self.assertEqual(self.execute(offer)["status"], "failed")
        for expiry in (NOW, NOW - timedelta(seconds=1), NOW + timedelta(hours=1)):
            offer = copy.deepcopy(self.offer)
            offer["id"], offer["expires_at"] = str(uuid4()), bridge.iso_date(expiry)
            self.assertEqual(self.execute(offer)["status"], "failed")
        self.mt5.order_send.assert_not_called()

    def test_fresh_quote_and_spread_plus_drift_guards(self):
        changes = [
            ("time", int(NOW.timestamp()) - 31), ("time", int(NOW.timestamp()) + 1),
            ("bid", 2500.2), ("ask", 2501.0), ("ask", float("nan")),
        ]
        for name, changed in changes:
            original = getattr(self.tick, name)
            with self.subTest(name=name, changed=changed):
                setattr(self.tick, name, changed)
                offer = copy.deepcopy(self.offer)
                offer["id"] = str(uuid4())
                self.assertEqual(self.execute(offer)["status"], "failed")
                setattr(self.tick, name, original)
        # Each component is within 0.1R, but their sum exceeds the limit.
        self.tick.bid, self.tick.ask = 2499.8, 2500.6
        self.assertEqual(self.execute()["status"], "failed")
        self.mt5.order_send.assert_not_called()

    def test_revalidation_after_order_check_blocks_account_change_expiry_and_price_move(self):
        for change in ("account", "price", "expiry"):
            self.account.login = 991122334
            self.tick.ask = 2500.1
            offer = copy.deepcopy(self.offer)
            offer["id"] = str(uuid4())
            clock = Mock(side_effect=[NOW, NOW + timedelta(minutes=6)]) if change == "expiry" else (lambda: NOW)
            def checked(request):
                if change == "account": self.account.login = 112233445
                if change == "price": self.tick.ask = 2502.0
                return SimpleNamespace(retcode=0)
            self.mt5.order_check.side_effect = checked
            with self.subTest(change=change):
                self.assertEqual(self.execute(offer, clock=clock)["status"], "failed")
        self.mt5.order_send.assert_not_called()

    def test_unsupported_fill_policy_fails_without_return_policy_or_retry(self):
        self.symbol.trade_exemode = 2
        self.symbol.filling_mode = 4
        self.assertEqual(self.execute()["status"], "failed")
        self.mt5.order_send.assert_not_called()
        self.mt5.order_check.assert_not_called()

    def test_broker_preflight_failure_sends_nothing(self):
        self.mt5.order_check.return_value = SimpleNamespace(retcode=10019, balance="private-do-not-send")
        self.assertEqual(self.execute(), {"status": "failed", "code": 10019})
        self.mt5.order_send.assert_not_called()

    def test_partial_timeout_placed_missing_result_and_exception_are_unknown_and_never_retried(self):
        cases = [
            SimpleNamespace(retcode=10010, volume=0.005, order=123),
            SimpleNamespace(retcode=10012, volume=0, order=0),
            SimpleNamespace(retcode=10008, volume=0.01, order=123),
            SimpleNamespace(retcode=10031, volume=0, order=0),
            SimpleNamespace(retcode=10009, volume=0.005, order=123),
            SimpleNamespace(retcode=10009, volume=0.01, order=0),
            SimpleNamespace(retcode=10009, volume=0.01, order=-1),
            SimpleNamespace(retcode=10009, volume=0.01, order=True),
            SimpleNamespace(retcode=10009, volume=0.01),
            None, RuntimeError("private-provider-details"),
        ]
        for response in cases:
            offer = copy.deepcopy(self.offer)
            offer["id"] = str(uuid4())
            self.mt5.order_send.reset_mock()
            self.mt5.order_send.side_effect = response if isinstance(response, Exception) else None
            self.mt5.order_send.return_value = response
            with self.subTest(response=type(response).__name__):
                result = self.execute(offer)
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(self.execute(offer), result)
                self.mt5.order_send.assert_called_once()
                self.assertNotIn("private", json.dumps(result))

    def test_normal_rejection_is_failed_and_never_resubmitted(self):
        self.mt5.order_send.return_value = SimpleNamespace(retcode=10030, order=0)
        self.assertEqual(self.execute(), {"status": "failed", "code": 10030})
        self.assertEqual(self.execute()["status"], "failed")
        self.mt5.order_send.assert_called_once()

    def test_closed_result_claim_is_acknowledged_and_never_resends_the_order(self):
        self.execute()
        error = HTTPError(self.settings.market.url, 409, "private-provider-detail", {}, None)
        errors = io.StringIO()
        with redirect_stderr(errors):
            bridge.flush_results(self.settings, self.ledger, Mock(side_effect=error))
        self.assertEqual(self.ledger.pending(), [])
        self.assertIn("claim is closed", errors.getvalue())
        self.assertNotIn("private-provider-detail", errors.getvalue())
        self.assertNotIn(self.settings.market.key, errors.getvalue())
        self.assertEqual(self.execute()["status"], "filled")
        self.mt5.order_send.assert_called_once()

    def test_nondefinitive_http_failure_keeps_result_outbox(self):
        self.execute()
        error = HTTPError(self.settings.market.url, 503, "private-provider-detail", {}, None)
        with self.assertRaises(HTTPError):
            bridge.flush_results(self.settings, self.ledger, Mock(side_effect=error))
        self.assertEqual(len(self.ledger.pending()), 1)
        self.mt5.order_send.assert_called_once()

    def test_netting_existing_position_or_pending_order_blocks_open(self):
        self.account.margin_mode = 0
        for getter in (self.mt5.positions_get, self.mt5.orders_get):
            getter.return_value = (SimpleNamespace(ticket=123),)
            offer = copy.deepcopy(self.offer)
            offer["id"] = str(uuid4())
            self.assertEqual(self.execute(offer)["status"], "failed")
            getter.return_value = ()
        self.mt5.order_send.assert_not_called()

    def test_registration_never_sends_account_identity_key_or_plain_pairing_code(self):
        code, registration = bridge.pairing_registration(self.settings, self.ledger)
        self.assertRegex(code, r"^[A-Z2-7]{16}$")
        self.assertEqual(registration["pair_code_hash"], hashlib.sha256(code.encode()).hexdigest())
        self.assertEqual(set(registration), {"device_id", "symbol", "account_mode", "volume", "pair_code_hash"})
        serialized = json.dumps(registration)
        binding = self.ledger.value("binding")
        for private in (str(self.account.login), self.account.server, self.settings.market.key, "PRIVATE_BALANCE_NOT_USED"):
            self.assertNotIn(private, serialized)
            self.assertNotIn(private, binding)
        self.assertNotIn(code, serialized)

    def test_result_outbox_retries_delivery_without_reexecuting(self):
        self.execute()
        failed_transport = Mock(side_effect=TimeoutError("private key"))
        with self.assertRaises(TimeoutError):
            bridge.flush_results(self.settings, self.ledger, failed_transport)
        self.assertEqual(len(self.ledger.pending()), 1)
        successful_transport = Mock(return_value={"ok": True})
        bridge.flush_results(self.settings, self.ledger, successful_transport)
        body = successful_transport.call_args.args[2]
        self.assertEqual(set(body), {"device_id", "offer_id", "claim_id", "result"})
        self.assertEqual(self.ledger.pending(), [])
        self.mt5.order_send.assert_called_once()

    def test_http_is_bounded_disables_redirects_and_keeps_key_in_header_only(self):
        response = Mock(status=200)
        response.read.return_value = b'{"trade":null}'
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(bridge.request, "build_opener", return_value=opener) as create:
            result = bridge.api_post(self.settings, "poll", {"device_id": self.ledger.device_id})
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, "https://example.invalid/api/mt5/poll")
        self.assertEqual(req.get_header("Authorization"), "Bearer " + self.settings.market.key)
        self.assertNotIn(self.settings.market.key, req.full_url)
        self.assertNotIn(self.settings.market.key.encode(), req.data)
        response.read.assert_called_once_with(bridge.MAX_JSON_BYTES + 1)
        self.assertTrue(any(isinstance(arg, market.NoRedirect) for arg in create.call_args.args))
        self.assertFalse(any(isinstance(arg, bridge.request.ProxyHandler) for arg in create.call_args.args))
        self.assertEqual(result, {"trade": None})

    def test_execution_metadata_contains_only_valid_broker_grid_restrictions(self):
        self.assertEqual(bridge.execution_metadata(self.symbol), {
            "tick_size": 0.01, "point": 0.01, "digits": 2, "stops_level": 10,
        })
        for name, value in (
            ("trade_tick_size", 0), ("trade_tick_size", float("nan")),
            ("point", True), ("point", float("inf")),
            ("digits", True), ("digits", -1), ("digits", 11),
            ("trade_stops_level", -1), ("trade_stops_level", 1000001),
            ("trade_stops_level", True),
        ):
            original = getattr(self.symbol, name)
            with self.subTest(name=name, value=value):
                setattr(self.symbol, name, value)
                with self.assertRaises((bridge.GuardError, market.MarketDataError)):
                    bridge.execution_metadata(self.symbol)
                setattr(self.symbol, name, original)

    def test_already_paired_registration_has_no_new_pairing_command(self):
        def post(settings, route, payload):
            return {"paired": True} if route == "register" else {"trade": None}
        output = io.StringIO()
        with patch.object(bridge, "terminal_is_running", side_effect=[True, False]), redirect_stdout(output):
            self.assertEqual(bridge.run_bridge(self.mt5, self.settings, post=post, clock=lambda: NOW, sleep=Mock()), 0)
        self.assertIn("already paired", output.getvalue())
        self.assertNotIn("/connect_mt5", output.getvalue())
        self.mt5.order_send.assert_not_called()

    def test_explicit_enable_demo_and_exact_volume_required_before_sdk_import(self):
        args = argparse.Namespace(terminal=str(self.terminal), symbol="XAUUSD.test", state_directory=str(self.base / "private"), account_mode="demo", volume="0.01", enable_orders=False)
        env = {"MARKET_BRIDGE_URL": self.settings.market.url, "MARKET_BRIDGE_KEY": self.settings.market.key}
        with self.assertRaises(bridge.GuardError): bridge.load_settings(args, env)
        args.enable_orders = True
        args.account_mode = "real"
        with self.assertRaises(bridge.GuardError): bridge.load_settings(args, env)
        args.account_mode, args.volume = "demo", "0.02"
        with self.assertRaises(bridge.GuardError): bridge.load_settings(args, env)
        args.volume = "0.01"
        self.assertEqual(bridge.load_settings(args, env).volume, Decimal("0.01"))

    def test_terminal_not_running_prevents_sdk_initialization(self):
        with patch.object(bridge, "terminal_is_running", return_value=False), redirect_stderr(io.StringIO()):
            self.assertEqual(bridge.run_bridge(self.mt5, self.settings), 1)
        self.mt5.initialize.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_only_one_bridge_can_hold_the_local_process_lock(self):
        first = bridge.ProcessLock(self.settings.state_directory)
        try:
            with self.assertRaises(bridge.GuardError):
                bridge.ProcessLock(self.settings.state_directory)
        finally:
            first.close()
        restarted = bridge.ProcessLock(self.settings.state_directory)
        restarted.close()

    def test_polling_survives_feed_failure_and_stops_when_terminal_closes(self):
        calls = []
        def post(settings, route, payload):
            calls.append((route, payload))
            if route == "market": raise TimeoutError("private-provider-detail")
            return {"trade": None} if route == "poll" else {"ok": True}
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(bridge, "terminal_is_running", side_effect=[True, True, False]), patch.object(
            market, "build_payload", return_value={"symbol": "XAUUSD.test"}
        ), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(bridge.run_bridge(self.mt5, self.settings, post=post, clock=lambda: NOW, sleep=Mock()), 0)
        self.assertIn("poll", [route for route, _ in calls])
        self.assertIn("device_id", next(payload for route, payload in calls if route == "market"))
        self.assertEqual(next(payload for route, payload in calls if route == "market")["execution"], {
            "tick_size": 0.01, "point": 0.01, "digits": 2, "stops_level": 10,
        })
        self.assertIn("/connect_mt5 ", output.getvalue())
        self.assertNotIn("private-provider-detail", errors.getvalue())
        self.mt5.order_send.assert_not_called()
        self.mt5.shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
