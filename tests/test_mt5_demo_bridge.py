"""Synthetic Demo SDK, network and journal checks; no live MT5 calls."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from uuid import uuid4

from bridge import mt5_demo_bridge as demo
from bridge import mt5_trade_bridge as trade
from tests import test_mt5_manual_bridge as fixtures
from tests import test_multi_timeframe as candle_fixtures
from tests import test_mt5_chart_overlay as overlay_fixtures


NOW = fixtures.NOW


class DemoBridgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManualBridgeTests(methodName="test_manual_draft_preserves_binding_identity_and_protection_with_algo_disabled")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        old = self.fixture.settings
        self.settings = demo.Settings(old.market, old.state_directory, "demo", Decimal("0.01"), True)
        self.mt5 = self.fixture.mt5
        self.account = self.fixture.fixture.account
        self.symbol = self.fixture.fixture.symbol
        self.tick = self.fixture.fixture.tick
        self.fixture.fixture.terminal_info.trade_allowed = True
        self.fixture.fixture.terminal_info.tradeapi_disabled = False
        self.account.trade_expert = True
        self.settings = replace(self.settings, market=replace(self.settings.market, commission_round_turn_per_lot=2.0, slippage_price=0.002))
        self.symbol.trade_tick_size = self.symbol.point = 0.001
        self.symbol.digits, self.symbol.trade_stops_level = 3, 5
        self.tick.bid, self.tick.ask = 2000.001, 2000.003
        eligible = candle_fixtures.MultiTimeframeTests().feed(now=NOW, broker_offset=0)
        self.fixture.rates = {
            minutes: [{**bar, "time": int(trade.utc_date(bar["time"]).timestamp())} for bar in eligible["timeframes"][frame]]
            for frame, minutes in (("H1", 60), ("M15", 15), ("M5", 5), ("M1", 1))
        }
        self.mt5.order_check.side_effect = None
        self.mt5.order_check.return_value = SimpleNamespace(retcode=0)
        self.mt5.order_send.side_effect = None
        self.mt5.order_send.return_value = SimpleNamespace(retcode=10009, volume=0.01, order=123456)
        self.ledger = demo.DemoLedger(old.state_directory)
        self.addCleanup(self.ledger.close)
        self.offer = self.current_offer()

    def current_offer(self, *, experimental=False):
        feed = demo.market.build_payload(self.mt5, self.settings.market, NOW)
        feed["execution"] = trade.execution_metadata(self.symbol)
        candidate = demo.multi_timeframe.analyze_multi_timeframe(feed, NOW, experimental_demo=experimental)
        self.assertEqual(candidate["state"], "signal", candidate)
        if not experimental:
            candidate["qualification_id"] = "b" * 64
        candidate.update(workflow=demo.WORKFLOW, closed_history_sha256=demo.closed_history_sha256(feed))
        return {"id": str(uuid4()), "claim_id": str(uuid4()), "workflow": demo.WORKFLOW,
                "payload": candidate, "expires_at": trade.iso_date(NOW + timedelta(seconds=30 if experimental else 10))}

    def execute(self, offer=None, *, clock=lambda: NOW):
        with redirect_stdout(io.StringIO()):
            return demo.execute_offer(self.mt5, self.settings, self.ledger, self.offer if offer is None else offer, clock)

    def experimental(self):
        return self.current_offer(experimental=True)

    def test_qualified_buy_has_attached_frozen_sl_tp_and_durable_reservation_before_single_send(self):
        def sent(order):
            other = demo.DemoLedger(self.settings.state_directory)
            try:
                self.assertEqual(other.pending()[0]["result"], {"status": "unknown", "code": 0})
            finally:
                other.close()
            return SimpleNamespace(retcode=10009, volume=0.01, order=123456)
        self.mt5.order_send.side_effect = sent
        original = deepcopy(self.offer)
        self.assertEqual(self.execute()["status"], "filled")
        self.mt5.order_check.assert_called_once()
        self.mt5.order_send.assert_called_once()
        order = self.mt5.order_send.call_args.args[0]
        self.assertEqual((order["symbol"], order["volume"], order["sl"], order["tp"]), (self.settings.market.symbol, 0.01, self.offer["payload"]["stop"], self.offer["payload"]["target"]))
        self.assertEqual(order["type"], self.mt5.ORDER_TYPE_BUY)
        self.assertNotIn("position", order)
        self.assertEqual(self.offer, original)

    def test_experimental_demo_accepts_estimates_and_30_second_deadline(self):
        self.settings = replace(self.settings, market=replace(self.settings.market, cost_model_verified=False, commission_round_turn_per_lot=None, slippage_price=None))
        self.assertEqual(self.execute(self.experimental())["status"], "filled")

    def test_market_execution_uses_ioc_without_price(self):
        self.symbol.trade_exemode, self.symbol.filling_mode = 2, 2
        self.assertEqual(self.execute()["status"], "filled")
        request = self.mt5.order_send.call_args.args[0]
        self.assertNotIn("price", request)
        self.assertEqual(request["type_filling"], self.mt5.ORDER_FILLING_IOC)

    def test_genuine_closed_sell_signal_sends_fixed_sell_protection(self):
        eligible = candle_fixtures.MultiTimeframeTests().feed(now=NOW, broker_offset=0, sell=True)
        self.fixture.rates = {
            minutes: [{**bar, "time": int(trade.utc_date(bar["time"]).timestamp())} for bar in eligible["timeframes"][frame]]
            for frame, minutes in (("H1", 60), ("M15", 15), ("M5", 5), ("M1", 1))
        }
        self.tick.bid, self.tick.ask = 1999.997, 1999.999
        offer = self.current_offer()
        self.assertEqual(self.execute(offer)["status"], "filled")
        request = self.mt5.order_send.call_args.args[0]
        self.assertEqual(request["type"], self.mt5.ORDER_TYPE_SELL)
        self.assertEqual((request["sl"], request["tp"]), (offer["payload"]["stop"], offer["payload"]["target"]))

    def test_foreign_envelope_payload_workflow_and_non_signal_cannot_reserve_or_preflight(self):
        for top, key, value in ((True, "workflow", "manual_ticket"), (False, "workflow", "manual_ticket"), (False, "state", "waiting")):
            offer = deepcopy(self.offer)
            (offer if top else offer["payload"])[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(trade.GuardError):
                self.execute(offer)
        self.assertEqual(self.ledger.pending(), [])
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_real_account_and_disabled_algo_never_preflight(self):
        for target, key, value in ((self.account, "trade_mode", 2), (self.account, "trade_expert", False),
                                   (self.fixture.fixture.terminal_info, "trade_allowed", False),
                                   (self.fixture.fixture.terminal_info, "tradeapi_disabled", True)):
            original = getattr(target, key)
            setattr(target, key, value)
            offer = deepcopy(self.offer)
            offer["id"] = str(uuid4())
            with self.subTest(key=key):
                self.assertEqual(self.execute(offer)["status"], "failed")
            setattr(target, key, original)
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_wrong_volume_or_missing_explicit_opt_in_never_preflight(self):
        for setting in (replace(self.settings, enable_demo_orders=False), replace(self.settings, volume=Decimal("0.02")), replace(self.settings, account_mode="real")):
            with self.subTest(setting=setting.account_mode), self.assertRaises(trade.GuardError):
                demo.execute_offer(self.mt5, setting, self.ledger, self.offer, lambda: NOW)
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_account_switch_during_history_collection_is_blocked_before_preflight(self):
        original = demo.market.build_payload
        def changed(*args):
            result = original(*args)
            self.account.login += 1
            return result
        with patch.object(demo.market, "build_payload", side_effect=changed):
            self.assertEqual(self.execute()["status"], "failed")
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_account_switch_or_new_exposure_after_preflight_blocks_send(self):
        for field in ("trade_mode", "login", "positions", "orders"):
            def checked(request):
                if field == "trade_mode":
                    self.account.trade_mode = 2
                elif field == "login":
                    self.account.login += 1
                elif field == "positions":
                    self.mt5.positions_get.return_value = (object(),)
                else:
                    self.mt5.orders_get.return_value = (object(),)
                return SimpleNamespace(retcode=0)
            old_login = self.account.login
            self.mt5.order_check.side_effect = checked
            offer = deepcopy(self.offer)
            offer["id"] = str(uuid4())
            with self.subTest(field=field):
                self.assertEqual(self.execute(offer)["status"], "failed")
            self.account.trade_mode, self.account.login = 0, old_login
            self.mt5.positions_get.return_value = self.mt5.orders_get.return_value = ()
        self.mt5.order_send.assert_not_called()

    def test_higher_frame_history_and_account_wide_hedging_exposure_are_required(self):
        self.mt5.positions_get.return_value = (object(),)
        self.assertEqual(self.execute()["status"], "failed")
        self.mt5.positions_get.return_value = ()
        self.fixture.rates[60] = self.fixture.rates[60][:-1]
        offer = deepcopy(self.offer)
        offer["id"] = str(uuid4())
        self.assertEqual(self.execute(offer)["status"], "failed")
        self.mt5.order_send.assert_not_called()

    def test_four_frame_execution_still_requires_22_closed_h1_bars_and_rejects_old_profiles(self):
        self.fixture.rates[60] = self.fixture.rates[60][-21:]
        offer = deepcopy(self.offer)
        revised = demo.market.build_payload(self.mt5, self.settings.market, NOW)
        offer["payload"]["closed_history_sha256"] = demo.closed_history_sha256(revised)
        self.assertEqual(set(revised["timeframes"]), {"H1", "M15", "M5", "M1"})
        self.assertEqual(self.execute(offer)["status"], "failed")
        old = deepcopy(self.offer)
        old["id"] = str(uuid4())
        old["payload"].update(strategy_id="mtf-ema-pullback-60m-v2", strategy_version=2,
                              policy_id="mtf-manual-demo-cost-risk-v2")
        self.assertEqual(self.execute(old)["status"], "failed")
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_risk_reward_margin_and_frozen_grid_are_not_bypassed(self):
        for field in ("equity", "margin_free", "target", "stop", "strategy_id"):
            offer = deepcopy(self.offer)
            offer["id"] = str(uuid4())
            old_equity, old_margin = self.account.equity, self.account.margin_free
            if field == "equity":
                self.account.equity = 10.0
            elif field == "margin_free":
                self.account.margin_free = 49.0
            else:
                offer["payload"][field] = {"target": 2000.004, "stop": 1999.0005, "strategy_id": trade.STRATEGY_ID}[field]
            with self.subTest(field=field):
                self.assertEqual(self.execute(offer)["status"], "failed")
            self.account.equity, self.account.margin_free = old_equity, old_margin
        self.mt5.order_send.assert_not_called()

    def test_m1_deadlines_cannot_extend_and_slow_preflight_cannot_send_after_expiry(self):
        offer = deepcopy(self.offer)
        offer["expires_at"] = trade.iso_date(NOW + timedelta(seconds=11))
        self.assertEqual(self.execute(offer)["status"], "failed")
        self.mt5.order_check.assert_not_called()
        moment = [NOW]
        self.mt5.order_check.side_effect = lambda req: (moment.__setitem__(0, NOW + timedelta(seconds=10)) or SimpleNamespace(retcode=0))
        offer = deepcopy(self.offer)
        offer["id"] = str(uuid4())
        self.assertEqual(self.execute(offer, clock=lambda: moment[0])["status"], "failed")
        self.mt5.order_send.assert_not_called()

    def test_quote_changed_after_final_risk_read_is_rejected_without_send(self):
        original = demo.prepare_order
        count = [0]
        def prepared(*args):
            result = original(*args)
            count[0] += 1
            if count[0] == 2:
                self.tick.ask += 0.01
            return result
        with patch.object(demo, "prepare_order", side_effect=prepared):
            self.assertEqual(self.execute()["status"], "failed")
        self.mt5.order_send.assert_not_called()

    def test_closed_m1_breakout_tick_activity_and_m5_recovery_mutations_block_execution(self):
        original = deepcopy(self.fixture.rates)
        for mutation in ("breakout", "tick_activity", "pullback"):
            self.fixture.rates = deepcopy(original)
            if mutation == "breakout":
                self.fixture.rates[1][-1]["close"] = self.fixture.rates[1][-1]["open"]
            elif mutation == "tick_activity":
                self.fixture.rates[1][-1]["tick_volume"] = 0
            else:
                self.fixture.rates[5][-2].update(open=1999.9, close=1999.9, low=1999.9)
            offer = deepcopy(self.offer)
            offer["id"] = str(uuid4())
            with self.subTest(mutation=mutation, check="frozen_hash"):
                self.assertEqual(self.execute(offer)["status"], "failed")
            # Even a self-consistent revised hash cannot substitute for a
            # current local technical signal. No server or native UI is used.
            revised = demo.market.build_payload(self.mt5, self.settings.market, NOW)
            offer["id"] = str(uuid4())
            offer["payload"]["closed_history_sha256"] = demo.closed_history_sha256(revised)
            with self.subTest(mutation=mutation, check="full_local_analysis"):
                self.assertEqual(self.execute(offer)["status"], "failed")
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_live_spread_atr_limit_binds_even_when_frozen_r_drift_limit_permits(self):
        prepared = demo.prepare_order(self.mt5, self.settings, self.ledger, self.offer, lambda: NOW)
        self.tick.ask = self.tick.bid + 0.02
        payload = self.offer["payload"]
        used = Decimal(str(self.tick.ask - self.tick.bid)) + abs(Decimal(str(self.tick.ask)) - Decimal(str(payload["entry"])))
        self.assertLess(used, Decimal(str(payload["original_stop_distance"])) * trade.MAX_DRIFT_R)
        with self.assertRaisesRegex(trade.GuardError, "0.15"):
            demo.final_guard(self.mt5, self.settings, self.ledger, self.offer, prepared, lambda: NOW)
        self.mt5.order_send.assert_not_called()

    def test_missing_history_hash_and_stale_or_future_quote_block_before_preflight(self):
        for case in ("missing_hash", "stale", "future"):
            offer = deepcopy(self.offer)
            offer["id"] = str(uuid4())
            old_time = self.tick.time
            if case == "missing_hash": offer["payload"].pop("closed_history_sha256")
            elif case == "stale": self.tick.time = int(NOW.timestamp()) - 11
            else: self.tick.time = int(NOW.timestamp()) + 6
            with self.subTest(case=case):
                self.assertEqual(self.execute(offer)["status"], "failed")
            self.tick.time = old_time
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_history_hash_canonicalizes_utc_and_numeric_representation_but_rejects_bad_values(self):
        feed = demo.market.build_payload(self.mt5, self.settings.market, NOW)
        expected = demo.closed_history_sha256(feed)
        equivalent = deepcopy(feed)
        for bars in equivalent["timeframes"].values():
            for bar in bars:
                bar["time"] = trade.utc_date(bar["time"]).isoformat()
                if bar["close"] == int(bar["close"]): bar["close"] = int(bar["close"])
        self.assertEqual(demo.closed_history_sha256(equivalent), expected)
        for key, value in (("open", True), ("high", float("nan")), ("tick_volume", 1.5), ("time", "2026-10-05T12:00:00")):
            invalid = deepcopy(feed)
            invalid["timeframes"]["M1"][-1][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): demo.closed_history_sha256(invalid)

    def test_equity_drop_and_broker_policy_change_immediately_before_send_are_blocked(self):
        original = demo.prepare_order
        for field in ("equity", "filling_mode"):
            count = [0]
            def prepared(*args):
                result = original(*args)
                count[0] += 1
                if count[0] == 2:
                    if field == "equity": self.account.equity = 10.0
                    else: self.symbol.filling_mode = 2
                return result
            offer = deepcopy(self.offer)
            offer["id"] = str(uuid4())
            with self.subTest(field=field), patch.object(demo, "prepare_order", side_effect=prepared):
                self.assertEqual(self.execute(offer)["status"], "failed")
            self.account.equity, self.symbol.filling_mode = 10000.0, 3
        self.mt5.order_send.assert_not_called()

    def test_duplicate_restart_and_interrupted_attempt_never_resubmit(self):
        result = self.execute()
        self.assertEqual(self.execute(), result)
        other = demo.DemoLedger(self.settings.state_directory)
        try:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(demo.execute_offer(self.mt5, self.settings, other, self.offer, lambda: NOW), result)
            interrupted = deepcopy(self.offer)
            interrupted["id"] = str(uuid4())
            other.reserve(interrupted)
            self.assertEqual(self.execute(interrupted)["status"], "unknown")
        finally:
            other.close()
        self.mt5.order_send.assert_called_once()

    def test_changed_payload_same_id_is_blocked_without_second_send(self):
        self.execute()
        changed = deepcopy(self.offer)
        changed["payload"]["stop"] = 2480.0
        with self.assertRaises(trade.GuardError): self.execute(changed)
        self.mt5.order_send.assert_called_once()

    def test_partial_timeout_connection_missing_and_exception_are_unknown_never_retried(self):
        for response in (SimpleNamespace(retcode=10010, volume=0.005, order=123), SimpleNamespace(retcode=10012),
                         SimpleNamespace(retcode=10031), SimpleNamespace(retcode=10009, volume=0.01), None, RuntimeError("private")):
            offer = deepcopy(self.offer)
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

    def test_failed_preflight_does_not_send_and_explicit_rejection_never_retries(self):
        self.mt5.order_check.return_value = SimpleNamespace(retcode=10019)
        self.assertEqual(self.execute()["status"], "failed")
        self.mt5.order_send.assert_not_called()
        self.mt5.order_check.return_value = SimpleNamespace(retcode=0)
        self.mt5.order_send.return_value = SimpleNamespace(retcode=10030)
        offer = deepcopy(self.offer)
        offer["id"] = str(uuid4())
        self.assertEqual(self.execute(offer)["status"], "failed")
        self.execute(offer)
        self.mt5.order_send.assert_called_once()

    def test_demo_outbox_never_routes_legacy_attempt_and_retry_does_not_execute(self):
        legacy = deepcopy(self.fixture.fixture.offer)
        legacy["id"] = str(uuid4())
        self.fixture.ledger.reserve(legacy)
        self.execute()
        self.assertEqual(len(self.ledger.pending()), 1)
        post = Mock(side_effect=HTTPError("https://private.invalid", 503, "private", {}, None))
        with self.assertRaises(HTTPError): demo.flush_results(self.settings, self.ledger, post)
        post.side_effect, post.return_value = None, {}
        demo.flush_results(self.settings, self.ledger, post)
        self.assertEqual(self.ledger.pending(), [])
        self.assertEqual(len(self.fixture.ledger.pending()), 1)
        self.mt5.order_send.assert_called_once()
        envelope = post.call_args.args[2]
        self.assertEqual(set(envelope), {"device_id", "offer_id", "claim_id", "result"})

    def test_transport_uses_demo_routes_bounded_json_header_secret_and_no_redirect(self):
        response = Mock(status=200)
        response.read.return_value = b'{"trade":null}'
        response.__enter__, response.__exit__ = Mock(return_value=response), Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(demo.request, "build_opener", return_value=opener) as build:
            for route in ("poll", "result", "chart"):
                demo.api_post(self.settings, route, {"device_id": self.ledger.device_id})
                req = opener.open.call_args.args[0]
                self.assertEqual(req.full_url, "https://example.invalid/api/mt5/demo/" + route)
                self.assertNotIn(self.settings.market.key.encode(), req.data)
                self.assertEqual(req.get_header("Authorization"), "Bearer " + self.settings.market.key)
        self.assertTrue(any(isinstance(item, demo.market.NoRedirect) for item in build.call_args.args))
        response.read.assert_called_with(trade.MAX_JSON_BYTES + 1)
        with self.assertRaises(trade.GuardError): demo.api_post(self.settings, "automatic", {})

    def test_startup_requires_explicit_flag_before_sdk_import_and_same_lock_blocks_parallel_helper(self):
        args = argparse.Namespace(enable_demo_orders=False, account_mode="demo", volume="0.01")
        with self.assertRaises(trade.GuardError): demo.load_settings(args, {})
        lock = trade.ProcessLock(self.settings.state_directory)
        try:
            with redirect_stderr(io.StringIO()):
                self.assertEqual(demo.run_bridge(self.mt5, self.settings, post=Mock(), exporter_factory=Mock()), 1)
            self.mt5.initialize.assert_not_called()
        finally:
            lock.close()

    def test_complete_loop_uses_feed5_poll1_and_no_native_ticket(self):
        exporter, post = Mock(), Mock()
        calls = []
        def posted(settings, route, payload):
            calls.append((route, deepcopy(payload)))
            if route == "register": return {"paired": True}
            if route == "chart": return {"proposal": None}
            if route == "poll": return {"trade": self.offer if len([c for c in calls if c[0] == "poll"]) == 1 else None}
            return {}
        elapsed = [0]
        sleeps = Mock(side_effect=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
        output = io.StringIO()
        with patch.object(trade, "terminal_is_running", side_effect=[True, True, True, False]), patch.object(demo.time, "monotonic", side_effect=lambda: elapsed[0]), patch.object(demo.manual.native, "NativeTicketAdapter") as native, redirect_stdout(output):
            result = demo.run_bridge(self.mt5, self.settings, post=posted, sleep=sleeps, clock=lambda: NOW, exporter_factory=lambda *a: exporter)
        self.assertEqual(result, 0)
        self.assertEqual([c[0] for c in calls].count("market"), 1)
        self.assertEqual([c[0] for c in calls].count("poll"), 2)
        self.assertEqual(sleeps.call_args_list, [unittest.mock.call(1), unittest.mock.call(1)])
        self.assertEqual(demo.FEED_SECONDS, 5)
        self.mt5.order_send.assert_called_once()
        native.assert_not_called()
        feed = next(payload for route, payload in calls if route == "market")
        self.assertEqual(set(feed["timeframes"]), {"M1", "M5", "M15", "H1"})
        self.assertIn("execution", feed)
        self.assertEqual(feed["device_id"], self.ledger.device_id)
        self.assertIn("Demo approval helper ready.", output.getvalue())
        self.assertEqual(output.getvalue().count("Fresh MT5 feed ready."), 1)

    def test_feed_status_reports_initial_upload_outage_and_recovery(self):
        for failure, outage_text in ((demo.market.MarketDataError("synthetic outage"), "MT5 feed unavailable"),
                                     (RuntimeError("private transport detail"), "Demo connection unavailable")):
            exporter, output, errors = Mock(), io.StringIO(), io.StringIO()
            elapsed = [0]
            uploads = [0]
            polls = [0]
            def posted(settings, route, payload):
                if route == "register": return {"paired": True}
                if route == "market":
                    uploads[0] += 1
                    if uploads[0] == 2: raise failure
                if route == "chart": return {"proposal": None}
                if route == "poll":
                    polls[0] += 1
                    return {"trade": None}
                return {}
            with self.subTest(failure=type(failure).__name__), patch.object(trade, "terminal_is_running", side_effect=[True, True, True, True, False]), patch.object(demo.time, "monotonic", side_effect=lambda: elapsed[0]), redirect_stdout(output), redirect_stderr(errors):
                result = demo.run_bridge(self.mt5, self.settings, post=posted,
                                         sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + 5),
                                         clock=lambda: NOW, exporter_factory=lambda *a: exporter)
            self.assertEqual(result, 0)
            self.assertEqual(output.getvalue().count("Fresh MT5 feed ready."), 2)
            self.assertEqual(errors.getvalue().count(outage_text), 1)
            self.assertNotIn("private transport detail", errors.getvalue())
            self.assertEqual(polls[0], 2)  # The failed upload never claims work.
            self.mt5.order_send.assert_not_called()

    def test_local_quote_failure_recovers_at_next_second_without_claiming_invalid_data(self):
        elapsed, calls, exporter = [0.0], [], Mock()
        output, errors = io.StringIO(), io.StringIO()
        def clock(): return NOW + timedelta(seconds=elapsed[0])
        def collect(_mt5, settings, observed):
            if elapsed[0] == 0:
                raise demo.market.MarketDataError("A fresh, valid broker quote is required.")
            feed = overlay_fixtures.readiness_feed(observed)
            feed.update(symbol=settings.symbol, broker_utc_offset_minutes=settings.broker_utc_offset_minutes)
            return feed
        def posted(settings, route, payload):
            calls.append((elapsed[0], route))
            if route == "register": return {"paired": True}
            self.assertGreaterEqual(elapsed[0], 1, "Invalid local data reached the service or claim poll")
            if route == "chart": return {"proposal": None}
            if route == "poll": return {"trade": None}
            return {}
        with patch.object(trade, "terminal_is_running", side_effect=[True, True, True, False]), patch.object(
            demo.time, "monotonic", side_effect=lambda: elapsed[0]
        ), patch.object(demo.market, "build_payload", side_effect=collect), redirect_stdout(output), redirect_stderr(errors):
            result = demo.run_bridge(self.mt5, self.settings, post=posted, clock=clock,
                                     sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                                     exporter_factory=lambda *args: exporter)
        self.assertEqual(result, 0)
        self.assertEqual(calls, [(0, "register"), (1, "market"), (1, "chart"), (1, "poll")])
        self.assertEqual(errors.getvalue().count("MT5 feed unavailable"), 1)
        self.assertRegex(errors.getvalue(), r"Reason:\s*stale_quote")
        self.assertIn("UTC:", errors.getvalue())
        self.assertIn(NOW.strftime("%Y-%m-%dT%H:%M:%S"), errors.getvalue())
        self.assertEqual(output.getvalue().count("Fresh MT5 feed ready."), 1)
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_transport_failure_retains_five_second_backoff_and_never_claims_before_success(self):
        elapsed, calls, exporter = [0.0], [], Mock()
        output, errors = io.StringIO(), io.StringIO()
        def clock(): return NOW + timedelta(seconds=elapsed[0])
        def collect(_mt5, settings, observed):
            feed = overlay_fixtures.readiness_feed(observed)
            feed.update(symbol=settings.symbol, broker_utc_offset_minutes=settings.broker_utc_offset_minutes)
            return feed
        def posted(settings, route, payload):
            calls.append((elapsed[0], route))
            if route == "register": return {"paired": True}
            if route == "market" and elapsed[0] == 0:
                raise RuntimeError("PRIVATE_SYNTHETIC_TRANSPORT_DETAIL")
            if route == "chart": return {"proposal": None}
            if route == "poll": return {"trade": None}
            return {}
        with patch.object(trade, "terminal_is_running", side_effect=[True] * 7 + [False]), patch.object(
            demo.time, "monotonic", side_effect=lambda: elapsed[0]
        ), patch.object(demo.market, "build_payload", side_effect=collect), redirect_stdout(output), redirect_stderr(errors):
            result = demo.run_bridge(self.mt5, self.settings, post=posted, clock=clock,
                                     sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                                     exporter_factory=lambda *args: exporter)
        self.assertEqual(result, 0)
        self.assertEqual(calls, [(0, "register"), (0, "market"), (5, "market"), (5, "chart"), (5, "poll")])
        self.assertEqual(errors.getvalue().count("Demo connection unavailable"), 1)
        self.assertNotIn("PRIVATE_SYNTHETIC_TRANSPORT_DETAIL", errors.getvalue())
        self.assertEqual(output.getvalue().count("Fresh MT5 feed ready."), 1)
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_repeated_private_local_failures_log_fixed_code_and_never_contact_claim_queue(self):
        elapsed, exporter = [0.0], Mock()
        errors, post = io.StringIO(), Mock(return_value={"paired": True})
        failure = demo.market.MarketDataError("PRIVATE_SYNTHETIC_SDK_ACCOUNT_PATH_TOKEN")
        with patch.object(trade, "terminal_is_running", side_effect=[True] * 4 + [False]), patch.object(
            demo.time, "monotonic", side_effect=lambda: elapsed[0]
        ), patch.object(demo.market, "build_payload", side_effect=failure), redirect_stdout(io.StringIO()), redirect_stderr(errors):
            result = demo.run_bridge(self.mt5, self.settings, post=post,
                                     clock=lambda: NOW + timedelta(seconds=elapsed[0]),
                                     sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                                     exporter_factory=lambda *args: exporter)
        self.assertEqual(result, 0)
        self.assertEqual([call.args[1] for call in post.call_args_list], ["register"])
        self.assertEqual(errors.getvalue().count("MT5 feed unavailable"), 1)
        self.assertRegex(errors.getvalue(), r"Reason:\s*invalid_local_data")
        self.assertIn("UTC:", errors.getvalue())
        self.assertNotIn("PRIVATE_SYNTHETIC", errors.getvalue())
        exporter.publish.assert_not_called()
        exporter.refresh_readiness.assert_not_called()
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_local_failure_during_transport_backoff_cannot_shorten_network_retry_deadline(self):
        elapsed, calls, captures, exporter = [0.0], [], [], Mock()
        output, errors = io.StringIO(), io.StringIO()
        def clock(): return NOW + timedelta(seconds=elapsed[0])
        def collect(_mt5, settings, observed):
            captures.append(elapsed[0])
            if elapsed[0] == 1:
                raise demo.market.MarketDataError("A fresh, valid broker quote is required.")
            feed = overlay_fixtures.readiness_feed(observed)
            feed.update(symbol=settings.symbol, broker_utc_offset_minutes=settings.broker_utc_offset_minutes)
            return feed
        def posted(settings, route, payload):
            calls.append((elapsed[0], route))
            if route == "register": return {"paired": True}
            if route == "market" and elapsed[0] == 0:
                raise RuntimeError("PRIVATE_SYNTHETIC_TRANSPORT_FAILURE")
            if route == "chart": return {"proposal": None}
            if route == "poll": return {"trade": None}
            return {}
        with patch.object(trade, "terminal_is_running", side_effect=[True] * 7 + [False]), patch.object(
            demo.time, "monotonic", side_effect=lambda: elapsed[0]
        ), patch.object(demo.market, "build_payload", side_effect=collect), redirect_stdout(output), redirect_stderr(errors):
            result = demo.run_bridge(self.mt5, self.settings, post=posted, clock=clock,
                                     sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                                     exporter_factory=lambda *args: exporter)
        self.assertEqual(result, 0)
        self.assertIn(1, captures)
        self.assertIn(2, captures)  # Local market recovery occurred before the network retry deadline.
        self.assertEqual(calls, [(0, "register"), (0, "market"), (5, "market"), (5, "chart"), (5, "poll")])
        self.assertNotIn("PRIVATE_SYNTHETIC_TRANSPORT_FAILURE", errors.getvalue())
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_terminal_installation_mismatch_stops_without_retry_or_feed_claim(self):
        exporter, post = Mock(), Mock(return_value={"paired": True})
        failure = demo.market.MarketDataError("PRIVATE_SYNTHETIC_TERMINAL_PATH", reason_code="terminal_installation_mismatch")
        errors = io.StringIO()
        with patch.object(trade, "terminal_is_running", side_effect=[True, True, False]), patch.object(
            demo.time, "monotonic", return_value=0
        ), patch.object(demo.market, "build_payload", side_effect=failure) as capture, redirect_stdout(io.StringIO()), redirect_stderr(errors):
            result = demo.run_bridge(self.mt5, self.settings, post=post, clock=lambda: NOW,
                                     sleep=Mock(), exporter_factory=lambda *args: exporter)
        self.assertEqual(result, 1)
        capture.assert_called_once()
        self.assertEqual([call.args[1] for call in post.call_args_list], ["register"])
        self.assertNotIn("PRIVATE_SYNTHETIC", errors.getvalue())
        exporter.publish.assert_not_called()
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_successful_slow_transport_keeps_real_readiness_fresh_without_renewing_main_row(self):
        common = self.fixture.fixture.base / "display-common"
        data = self.fixture.fixture.base / ("A" * 32)
        common.mkdir()
        data.mkdir()
        exporter = demo.chart.ChartExporter(common, data, symbol=self.settings.market.symbol,
                                            broker_offset_minutes=self.settings.market.broker_utc_offset_minutes,
                                            account=self.account, nonce="12" * 16)
        elapsed = [0.0]
        uploads, samples, chart_feeds, previous_main = [], [], [], []

        def clock():
            return NOW + timedelta(seconds=elapsed[0])

        def check_readiness():
            if not exporter.readiness_path.exists():
                self.assertFalse(samples, "Readiness disappeared after a successful fresh update")
                return  # Startup, before the first chart response.
            fields = exporter.readiness_path.read_text(encoding="ascii").split(";")
            self.assertEqual(len(fields), 10)
            stamp = clock().timestamp()
            self.assertLessEqual(int(fields[1]), stamp)
            self.assertLess(stamp, int(fields[2]), f"Readiness expired at fake time {elapsed[0]}")
            self.assertEqual(fields[3:7], ["64"] * 4)
            samples.append(elapsed[0])

        def advance(seconds):
            target = elapsed[0] + seconds
            while elapsed[0] + 0.25 < target:
                elapsed[0] += 0.25
                check_readiness()
            elapsed[0] = target

        def collect(_mt5, settings, observed):
            feed = overlay_fixtures.readiness_feed(observed)
            feed["symbol"] = settings.symbol
            feed["broker_utc_offset_minutes"] = settings.broker_utc_offset_minutes
            return feed

        def posted(settings, route, payload):
            if route == "register":
                return {"paired": True}
            if route == "market":
                uploads.append((elapsed[0], trade.utc_date(payload["as_of"])))
                before = exporter.path.read_bytes()
                before_mtime = exporter.path.stat().st_mtime_ns
                if previous_main:
                    self.assertEqual(before, previous_main[-1][0])
                    self.assertEqual(before_mtime, previous_main[-1][1])
                advance(4)
                self.assertEqual(exporter.path.read_bytes(), before)
                self.assertEqual(exporter.path.stat().st_mtime_ns, before_mtime)
                return {}
            if route == "chart":
                advance(3)
                chart_feeds.append(elapsed[0])
                return {"proposal": None}
            if route == "poll":
                check_readiness()
                fields = exporter.path.read_text(encoding="ascii").split(";")
                self.assertEqual(fields[1], "waiting")
                self.assertEqual(int(fields[11]), int(clock().timestamp()))
                self.assertEqual(int(fields[12]) - int(fields[11]), 25)
                previous_main.append((exporter.path.read_bytes(), exporter.path.stat().st_mtime_ns))
                advance(2)
                return {"trade": None}
            self.fail("Unexpected transport route")

        output, errors = io.StringIO(), io.StringIO()
        with patch.object(trade, "terminal_is_running", side_effect=[True] * 4 + [False]), patch.object(
            demo.time, "monotonic", side_effect=lambda: elapsed[0]
        ), patch.object(demo.market, "build_payload", side_effect=collect), redirect_stdout(output), redirect_stderr(errors):
            result = demo.run_bridge(self.mt5, self.settings, post=posted, clock=clock, sleep=advance,
                                     exporter_factory=lambda *args: exporter)
        self.assertEqual(result, 0, errors.getvalue())
        self.assertEqual([stamp for stamp, _ in uploads], [0, 10, 20])
        self.assertEqual([stamp for _, stamp in uploads], [NOW + timedelta(seconds=t) for t in (0, 10, 20)])
        self.assertEqual(chart_feeds, [7, 17, 27])
        self.assertGreater(len(samples), 60)
        self.assertNotIn("unavailable", errors.getvalue())
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_account_change_during_chart_response_stops_before_claim_poll(self):
        exporter, calls, errors = Mock(), [], io.StringIO()
        def posted(settings, route, payload):
            calls.append(route)
            if route == "register": return {"paired": True}
            if route == "chart":
                self.account.login += 1
                return {"proposal": None}
            if route == "poll": self.fail("A failed fresh account check reached the claim poll")
            return {}
        with patch.object(trade, "terminal_is_running", side_effect=[True, True, False]), patch.object(
            demo.time, "monotonic", return_value=0
        ), redirect_stdout(io.StringIO()), redirect_stderr(errors):
            result = demo.run_bridge(self.mt5, self.settings, post=posted, clock=lambda: NOW,
                                     sleep=Mock(), exporter_factory=lambda *args: exporter)
        self.assertEqual(result, 1)
        self.assertEqual(calls, ["register", "market", "chart"])
        exporter.publish.assert_not_called()
        self.assertIn("Demo helper stopped:", errors.getvalue())
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_fresh_market_data_failure_after_chart_response_never_claims_work(self):
        exporter, calls = Mock(), []
        feed = demo.market.build_payload(self.mt5, self.settings.market, NOW)
        def posted(settings, route, payload):
            calls.append(route)
            if route == "register": return {"paired": True}
            if route == "chart": return {"proposal": None}
            if route == "poll": self.fail("A failed fresh market check reached the claim poll")
            return {}
        with patch.object(trade, "terminal_is_running", side_effect=[True, True, False]), patch.object(
            demo.time, "monotonic", return_value=0
        ), patch.object(demo.market, "build_payload", side_effect=[feed, demo.market.MarketDataError("Synthetic stale quote")]), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = demo.run_bridge(self.mt5, self.settings, post=posted, clock=lambda: NOW,
                                     sleep=Mock(), exporter_factory=lambda *args: exporter)
        self.assertEqual(result, 0)
        self.assertEqual(calls, ["register", "market", "chart"])
        exporter.publish.assert_not_called()
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_feed_refresh_deadline_includes_time_spent_uploading(self):
        elapsed, uploads, exporter = [0.0], [], Mock()
        def clock(): return NOW + timedelta(seconds=elapsed[0])
        def collect(_mt5, settings, observed):
            feed = overlay_fixtures.readiness_feed(observed)
            feed.update(symbol=settings.symbol, broker_utc_offset_minutes=settings.broker_utc_offset_minutes)
            return feed
        def posted(settings, route, payload):
            if route == "register": return {"paired": True}
            if route == "market":
                uploads.append(elapsed[0])
                elapsed[0] += 4
            if route == "chart": return {"proposal": None}
            if route == "poll": return {"trade": None}
            return {}
        with patch.object(trade, "terminal_is_running", side_effect=[True] * 4 + [False]), patch.object(
            demo.time, "monotonic", side_effect=lambda: elapsed[0]
        ), patch.object(demo.market, "build_payload", side_effect=collect), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = demo.run_bridge(self.mt5, self.settings, post=posted, clock=clock,
                                     sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                                     exporter_factory=lambda *args: exporter)
        self.assertEqual(result, 0)
        self.assertEqual(uploads, [0, 5, 10])
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_account_change_during_execution_metadata_capture_never_exports_or_uploads(self):
        exporter, post = Mock(), Mock(return_value={"paired": True})
        original_metadata = trade.execution_metadata
        def changed(symbol):
            metadata = original_metadata(symbol)
            self.account.server = "synthetic-switched-server"
            return metadata
        with patch.object(trade, "terminal_is_running", side_effect=[True, True, False]), patch.object(
            demo.time, "monotonic", return_value=0
        ), patch.object(trade, "execution_metadata", side_effect=changed), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = demo.run_bridge(self.mt5, self.settings, post=post, clock=lambda: NOW,
                                     sleep=Mock(), exporter_factory=lambda *args: exporter)
        self.assertEqual(result, 1)
        self.assertEqual([call.args[1] for call in post.call_args_list], ["register"])
        exporter.refresh_readiness.assert_not_called()
        exporter.publish.assert_not_called()
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()


class MarketFailureReasonTests(unittest.TestCase):
    def test_authored_quote_history_and_risk_messages_have_fixed_codes(self):
        cases = {
            "A fresh broker quote is required.": "missing_quote",
            "A fresh, valid broker quote is required.": "stale_quote",
            "Load completed bars in every required timeframe.": "incomplete_history",
            "Incomplete broker candle.": "incomplete_history",
            "Required closed histories must be current.": "history_not_current",
            "Invalid or unfinished broker candle.": "invalid_history",
            "Complete broker risk data is required.": "risk_data_unavailable",
            "Current Demo risk data is required.": "risk_data_unavailable",
        }
        for message, expected in cases.items():
            with self.subTest(expected=expected, message=message):
                self.assertEqual(demo.market_failure_reason(demo.market.MarketDataError(message)), expected)

    def test_only_whitelisted_terminal_codes_are_preserved(self):
        for code in demo.market.TERMINAL_FAILURE_MESSAGES:
            error = demo.market.MarketDataError("PRIVATE_SYNTHETIC_TERMINAL_DETAIL", reason_code=code)
            with self.subTest(code=code):
                self.assertEqual(demo.market_failure_reason(error), code)

    def test_unknown_private_text_and_forged_reason_codes_never_leave_fixed_fallback(self):
        for message in ("PRIVATE_SYNTHETIC_ACCOUNT_TOKEN_PATH", "Private prefix: A fresh broker quote is required."):
            error = demo.market.MarketDataError(message)
            error.reason_code = "PRIVATE_SYNTHETIC_FORGED_REASON"
            with self.subTest(message=message):
                result = demo.market_failure_reason(error)
                self.assertEqual(result, "invalid_local_data")
                self.assertNotIn("PRIVATE", result)
                self.assertNotIn(message, result)


if __name__ == "__main__":
    unittest.main()
