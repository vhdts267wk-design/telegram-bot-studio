"""Synthetic SDK, HTTP and native-window tests; never access a live MT5 UI."""

from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bridge import mt5_manual_bridge as manual
from bridge import mt5_native_ticket as native
from bridge import mt5_trade_bridge as trade
from tests import test_mt5_trade_bridge as fixtures


NOW = fixtures.NOW


class ManualBridgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TradeBridgeTests(methodName="test_demo_buy_preflight_attached_stops_and_one_execution")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        old = self.fixture.settings
        self.settings = manual.Settings(old.market, old.state_directory, old.account_mode, old.volume, True)
        self.mt5, self.ledger = self.fixture.mt5, self.fixture.ledger
        self.journal = manual.ManualJournal(self.ledger)
        self.preparation = deepcopy(self.fixture.offer)
        self.preparation["workflow"] = "manual_ticket"
        self.preparation["payload"].update(
            execution=trade.execution_metadata(self.fixture.symbol), price_digits=2,
            original_stop_distance=10.0,
        )
        # A manual helper must work while external algorithmic trading is off.
        self.fixture.terminal_info.trade_allowed = False
        self.fixture.terminal_info.tradeapi_disabled = True
        self.fixture.account.trade_expert = False
        self.mt5.order_send.side_effect = AssertionError("A draft must never send an order")
        self.mt5.order_check.side_effect = AssertionError("A draft must never call order_check")

    def draft(self, preparation=None):
        return manual.prepare_draft(self.mt5, self.settings, self.ledger, preparation or self.preparation, NOW)

    def test_manual_draft_preserves_binding_identity_and_protection_with_algo_disabled(self):
        identity, binding = self.ledger.device_id, self.ledger.value("binding")
        draft = self.draft()
        self.assertEqual(draft.symbol, "XAUUSD.test")
        self.assertEqual((draft.stop, draft.target, draft.volume), (Decimal("2490"), Decimal("2520"), Decimal("0.01")))
        self.assertEqual(self.ledger.device_id, identity)
        self.assertEqual(self.ledger.value("binding"), binding)
        self.mt5.order_send.assert_not_called()
        self.mt5.order_check.assert_not_called()

    def test_real_or_switched_account_and_changed_terminal_block_manual_preparation(self):
        for owner, name, changed in (
            (self.fixture.account, "trade_mode", 2), (self.fixture.account, "login", 1),
            (self.fixture.account, "server", "different server"), (self.fixture.account, "trade_allowed", False),
            (self.fixture.terminal_info, "path", str(self.fixture.terminal.parent / "another")),
        ):
            original = getattr(owner, name)
            with self.subTest(name=name):
                setattr(owner, name, changed)
                with self.assertRaises((trade.GuardError, manual.market.MarketDataError)):
                    self.draft()
                setattr(owner, name, original)
        self.mt5.order_send.assert_not_called()

    def test_price_grid_stale_quote_spread_drift_and_netting_protections_remain_enforced(self):
        for owner, name, changed in (
            (self.fixture.tick, "time", int(NOW.timestamp()) - 31),
            (self.fixture.tick, "time", int(NOW.timestamp()) + 6),
            (self.fixture.tick, "ask", 2502.0), (self.fixture.tick, "bid", 2500.2),
            (self.fixture.symbol, "trade_stops_level", 2000),
            (self.fixture.symbol, "trade_tick_size", 0.3),
            (self.fixture.symbol, "volume_step", 0.03),
        ):
            original = getattr(owner, name)
            with self.subTest(name=name, changed=changed):
                setattr(owner, name, changed)
                with self.assertRaises(trade.GuardError):
                    self.draft()
                setattr(owner, name, original)
        self.fixture.account.margin_mode = 0
        self.mt5.positions_get.return_value = (object(),)
        with self.assertRaises(trade.GuardError):
            self.draft()
        self.mt5.order_send.assert_not_called()
        self.mt5.order_check.assert_not_called()

    def test_payload_mode_lots_deadline_metadata_and_workflow_are_not_relaxed(self):
        for field, value in (
            ("account_mode", "real"), ("volume", 0.02), ("direction", "CLOSE"),
            ("original_stop_distance", 20), ("price_digits", 3), ("execution", {}),
        ):
            preparation = deepcopy(self.preparation)
            preparation["payload"][field] = value
            with self.subTest(field=field), self.assertRaises(trade.GuardError):
                self.draft(preparation)
        for top, value in (
            ("expires_at", trade.iso_date(NOW)),
            ("expires_at", trade.iso_date(NOW + timedelta(minutes=6))),
            ("workflow", "automatic_order"),
        ):
            preparation = deepcopy(self.preparation)
            preparation[top] = value
            with self.subTest(top=top), self.assertRaises(trade.GuardError):
                self.draft(preparation)
        self.mt5.order_send.assert_not_called()

    def test_success_is_only_prepared_and_a_duplicate_never_reopens_the_ticket(self):
        adapter = Mock()
        adapter.prepare.return_value = {"status": "prepared"}
        with redirect_stdout(io.StringIO()):
            first = manual.prepare_ticket(self.mt5, self.settings, self.ledger, self.journal, adapter, self.preparation, clock=lambda: NOW)
            second = manual.prepare_ticket(self.mt5, self.settings, self.ledger, self.journal, adapter, self.preparation, clock=lambda: NOW)
        self.assertEqual(first, {"status": "prepared"})
        self.assertEqual(second, first)
        adapter.prepare.assert_called_once()
        self.assertEqual(self.journal.pending()[0]["result"], {"status": "prepared"})
        self.assertEqual(self.ledger.pending(), [])
        self.mt5.order_send.assert_not_called()
        self.mt5.order_check.assert_not_called()

    def test_interrupted_and_failed_preparation_is_durable_without_ui_retry(self):
        self.journal.reserve(self.preparation)
        adapter = Mock()
        result = manual.prepare_ticket(self.mt5, self.settings, self.ledger, self.journal, adapter, self.preparation, clock=lambda: NOW)
        self.assertEqual(result, {"status": "failed"})
        adapter.prepare.assert_not_called()
        reopened = manual.ManualJournal(self.ledger)
        self.assertEqual(reopened.pending()[0]["result"], {"status": "failed"})

    def test_ui_failure_does_not_acknowledge_execution_or_flush_auto_results(self):
        self.ledger.reserve(self.fixture.offer)
        adapter = Mock()
        adapter.prepare.side_effect = native.TicketError("existing_ticket")
        with redirect_stderr(io.StringIO()):
            result = manual.prepare_ticket(self.mt5, self.settings, self.ledger, self.journal, adapter, self.preparation, clock=lambda: NOW)
        self.assertEqual(result, {"status": "failed"})
        post = Mock(return_value={})
        manual.flush_results(self.settings, self.ledger, self.journal, post)
        envelope = post.call_args.args[2]
        self.assertEqual(set(envelope), {"device_id", "offer_id", "claim_id", "result"})
        self.assertEqual(envelope["result"], {"status": "failed"})
        self.assertEqual(self.journal.pending(), [])
        self.assertEqual(len(self.ledger.pending()), 1)
        self.mt5.order_send.assert_not_called()

    def test_changed_payload_on_an_existing_draft_is_rejected_without_any_ui_call(self):
        self.journal.reserve(self.preparation)
        changed = deepcopy(self.preparation)
        changed["payload"]["stop"] = 2480.0
        with self.assertRaises(trade.GuardError):
            manual.prepare_ticket(self.mt5, self.settings, self.ledger, self.journal, Mock(), changed, clock=lambda: NOW)

    def test_native_adapter_account_callback_rechecks_quote_and_expiry_before_each_field(self):
        def prepare(draft, *, recheck_account):
            self.fixture.tick.ask = 2502.0
            recheck_account()
            return {"status": "prepared"}
        adapter = Mock()
        adapter.prepare.side_effect = prepare
        with redirect_stderr(io.StringIO()):
            result = manual.prepare_ticket(self.mt5, self.settings, self.ledger, self.journal, adapter, self.preparation, clock=lambda: NOW)
        self.assertEqual(result, {"status": "failed"})
        self.mt5.order_send.assert_not_called()

    def test_transport_uses_only_manual_routes_for_poll_results_and_rejects_auto_route(self):
        response = Mock(status=200)
        response.read.return_value = b'{"preparation":null}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(manual.request, "build_opener", return_value=opener):
            self.assertEqual(manual.api_post(self.settings, "poll", {"device_id": self.ledger.device_id}), {"preparation": None})
        self.assertTrue(opener.open.call_args.args[0].full_url.endswith("/api/mt5/manual/poll"))
        with self.assertRaises(trade.GuardError):
            manual.api_post(self.settings, "automatic", {})

    def test_complete_loop_uploads_prices_prepares_one_ticket_and_reports_only_draft_state(self):
        self.fixture.terminal_info.data_path = str(self.fixture.base)
        adapter = Mock()
        adapter.prepare.return_value = {"status": "prepared"}
        sent = []

        def post(settings, route, payload):
            sent.append((route, deepcopy(payload)))
            if route == "register":
                return {"paired": True}
            if route == "poll":
                return {"preparation": deepcopy(self.preparation)}
            return {}

        with patch.object(trade, "terminal_is_running", side_effect=[True, True, False]), patch.object(
            manual.market, "build_payload", return_value={"symbol": self.settings.market.symbol}
        ), redirect_stdout(io.StringIO()):
            result = manual.run_bridge(self.mt5, self.settings, post=post, clock=lambda: NOW,
                                       sleep=Mock(), adapter_factory=lambda: adapter)
        self.assertEqual(result, 0)
        self.assertEqual([route for route, payload in sent], ["register", "market", "poll", "result"])
        self.assertEqual(sent[-1][1]["result"], {"status": "prepared"})
        self.assertNotIn("order_ticket", json.dumps(sent))
        adapter.prepare.assert_called_once()
        self.mt5.order_send.assert_not_called()
        self.mt5.order_check.assert_not_called()
        self.mt5.shutdown.assert_called_once()

    def test_failed_native_startup_preflight_stops_before_registration_or_ready_message(self):
        self.fixture.terminal_info.data_path = str(self.fixture.base)
        adapter = Mock()
        adapter.backend.verify_absolute_stop_mode.side_effect = native.TicketError("absolute_mode_unverified")
        post = Mock()
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(trade, "terminal_is_running", return_value=True), redirect_stdout(output), redirect_stderr(errors):
            result = manual.run_bridge(self.mt5, self.settings, post=post, clock=lambda: NOW,
                                       adapter_factory=lambda: adapter)
        self.assertEqual(result, 1)
        adapter.backend.verify_absolute_stop_mode.assert_called_once()
        adapter.prepare.assert_not_called()
        post.assert_not_called()
        self.assertNotIn("ready", output.getvalue())
        self.assertNotIn("Native price mode verified.", output.getvalue())
        self.assertIn("absolute_mode_unverified", errors.getvalue())
        self.mt5.order_send.assert_not_called()

    def test_final_options_delay_cannot_report_prepared_with_stale_quote_or_expired_proposal(self):
        for index, delay in enumerate((timedelta(seconds=31), timedelta(minutes=5)), start=1):
            with self.subTest(delay=delay):
                preparation = deepcopy(self.preparation)
                preparation["id"] = "00000000-0000-4000-8000-00000000000" + str(index)
                current = {"time": NOW}
                backend = FakeNativeBackend()
                normal_check = backend.verify_absolute_stop_mode

                def delayed_final_mode_check():
                    normal_check()
                    if backend.comment:
                        current["time"] = NOW + delay

                backend.verify_absolute_stop_mode = delayed_final_mode_check
                clock = lambda: current["time"]
                adapter = native.NativeTicketAdapter(backend, clock=clock)
                with redirect_stderr(io.StringIO()):
                    result = manual.prepare_ticket(self.mt5, self.settings, self.ledger, self.journal,
                                                   adapter, preparation, clock=clock)
                self.assertEqual(result, {"status": "failed"})
                self.assertEqual(set(backend.values), {10333, 10334, 10336})
                self.assertIsNone(backend.ticket)
                self.assertFalse(any(event[0] == "unlock" for event in backend.events))
        self.mt5.order_send.assert_not_called()
        self.mt5.order_check.assert_not_called()

    def loop_probe(self):
        self.fixture.terminal_info.data_path = str(self.fixture.base)
        adapter = Mock()
        adapter.prepare.return_value = {"status": "prepared"}
        timer = {"seconds": 0.0}
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            timer["seconds"] += seconds

        return adapter, timer, sleep, sleeps

    def test_runtime_disconnect_recovers_once_and_never_polls_or_prepares_while_disconnected(self):
        adapter, timer, sleep, sleeps = self.loop_probe()
        calls, cycle = [], {"number": 0}

        def running(terminal):
            cycle["number"] += 1
            self.fixture.terminal_info.connected = cycle["number"] not in (2, 3)
            return cycle["number"] <= 4

        def post(settings, route, payload):
            calls.append((cycle["number"], route))
            return ({"paired": True} if route == "register" else {"preparation": self.preparation} if route == "poll" else {})

        output, errors = io.StringIO(), io.StringIO()
        with patch.object(trade, "terminal_is_running", side_effect=running), patch.object(manual.time, "monotonic", side_effect=lambda: timer["seconds"]), patch.object(
            manual.market, "build_payload", return_value={"symbol": self.settings.market.symbol}
        ), redirect_stdout(output), redirect_stderr(errors):
            result = manual.run_bridge(self.mt5, self.settings, post=post, clock=lambda: NOW,
                                       sleep=sleep, adapter_factory=lambda: adapter)
        self.assertEqual(result, 0)
        self.assertEqual(calls, [(1, "register"), (4, "market"), (4, "poll"), (4, "result")])
        self.assertEqual(errors.getvalue().count("MT5 feed unavailable (terminal_disconnected)."), 1)
        self.assertEqual(output.getvalue().count("Fresh MT5 feed ready."), 1)
        self.assertEqual(sleeps, [10, 10, 10])
        adapter.prepare.assert_called_once()
        self.mt5.order_send.assert_not_called()
        self.mt5.order_check.assert_not_called()

    def test_failed_feed_build_or_upload_retries_then_prepares_only_after_successful_fresh_upload(self):
        for failure in ("data", "http"):
            with self.subTest(failure=failure):
                # Use a distinct identity per trial so durable earlier attempts
                # cannot conceal an incorrectly repeated UI action.
                preparation = deepcopy(self.preparation)
                preparation["id"] = "00000000-0000-4000-8000-00000000000" + ("3" if failure == "data" else "4")
                adapter, timer, sleep, sleeps = self.loop_probe()
                calls, attempts = [], {"feed": 0}

                def build(mt5, settings, now):
                    if failure == "data":
                        attempts["feed"] += 1
                        if attempts["feed"] <= 2:
                            raise manual.market.MarketDataError("private SDK data detail")
                    return {"symbol": self.settings.market.symbol}

                def post(settings, route, payload):
                    calls.append((timer["seconds"], route))
                    if route == "market" and failure == "http":
                        attempts["feed"] += 1
                        if attempts["feed"] <= 2:
                            raise manual.HTTPError("https://private.invalid", 503, "private service detail", {}, None)
                    return ({"paired": True} if route == "register" else {"preparation": preparation} if route == "poll" else {})

                output, errors = io.StringIO(), io.StringIO()
                with patch.object(trade, "terminal_is_running", side_effect=[True, True, True, True, False]), patch.object(
                    manual.time, "monotonic", side_effect=lambda: timer["seconds"]
                ), patch.object(manual.market, "build_payload", side_effect=build), redirect_stdout(output), redirect_stderr(errors):
                    result = manual.run_bridge(self.mt5, self.settings, post=post, clock=lambda: NOW,
                                               sleep=sleep, adapter_factory=lambda: adapter)
                self.assertEqual(result, 0)
                self.assertEqual([stamp for stamp, route in calls if route == "poll"], [20])
                self.assertEqual([stamp for stamp, route in calls if route == "result"], [20])
                self.assertEqual(attempts["feed"], 3)
                self.assertEqual(errors.getvalue().count("MT5 feed unavailable ("), 1)
                self.assertNotIn("private", errors.getvalue())
                self.assertEqual(output.getvalue().count("Fresh MT5 feed ready."), 1)
                adapter.prepare.assert_called_once()
                self.assertEqual(self.journal.pending(), [])
        self.mt5.order_send.assert_not_called()
        self.mt5.order_check.assert_not_called()

    def test_runtime_terminal_mismatch_and_account_switch_remain_fatal_without_polling(self):
        for changed in ("terminal", "account"):
            with self.subTest(changed=changed):
                adapter, timer, sleep, sleeps = self.loop_probe()
                original_path, original_login = self.fixture.terminal_info.path, self.fixture.account.login
                count = {"calls": 0}

                def running(terminal):
                    count["calls"] += 1
                    if count["calls"] == 2:
                        if changed == "terminal":
                            self.fixture.terminal_info.path = str(self.fixture.base / "different")
                        else:
                            self.fixture.account.login = 1
                    return True

                post = Mock(return_value={"paired": True})
                with patch.object(trade, "terminal_is_running", side_effect=running), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    result = manual.run_bridge(self.mt5, self.settings, post=post, clock=lambda: NOW,
                                               sleep=sleep, adapter_factory=lambda: adapter)
                self.fixture.terminal_info.path, self.fixture.account.login = original_path, original_login
                self.assertEqual(result, 1)
                self.assertEqual([call.args[1] for call in post.call_args_list], ["register"])
                self.assertEqual(sleeps, [])
                adapter.prepare.assert_not_called()


class FakeNativeBackend:
    def __init__(self):
        self.events = []
        self.ticket = None
        self.mode = "prices"
        self.units = "prices"
        self.values = {}
        self.comment = ""
        self.enabled = True
        self.locked_ticket = None

    def verify_terminal(self):
        self.events.append(("verify_terminal",))

    def existing_tickets(self):
        return [self.ticket] if self.ticket is not None else []

    def verify_absolute_stop_mode(self):
        if self.mode != "prices":
            raise native.TicketError("absolute_mode_unverified")

    def activate_chart(self, symbol, timeframe):
        self.events.append(("chart", symbol, timeframe))
        return 44

    def open_ticket(self, chart):
        self.events.append(("open", chart))
        self.ticket = 55
        return self.ticket

    def verify_ticket(self, ticket, symbol):
        if ticket != self.ticket:
            raise native.TicketError("ticket_changed")

    def verify_absolute_ticket_units(self, ticket):
        if self.units != "prices":
            raise native.TicketError("absolute_mode_unverified")

    def lock_ticket(self, ticket, symbol):
        self.verify_ticket(ticket, symbol)
        self.enabled = False
        self.locked_ticket = ticket
        self.events.append(("lock", ticket))

    def verify_ticket_locked(self, ticket, symbol):
        self.verify_ticket(ticket, symbol)
        if self.enabled or self.locked_ticket != ticket:
            raise native.TicketError("ticket_changed")

    def unlock_ticket(self, ticket, symbol):
        self.verify_ticket_locked(ticket, symbol)
        self.enabled = True
        self.locked_ticket = None
        self.events.append(("unlock", ticket))

    def cancel_owned_ticket(self, ticket, symbol):
        if self.locked_ticket != ticket or self.ticket != ticket:
            raise native.TicketError("ticket_changed")
        self.events.append(("cancel", ticket))
        self.ticket = self.locked_ticket = None

    def set_edit(self, ticket, field, value):
        self.events.append(("set_edit", field, value))
        self.values[field] = value

    def read_edit(self, ticket, field):
        return self.values[field]

    def set_comment(self, ticket, value):
        self.events.append(("comment", value))
        self.comment = value

    def read_comment(self, ticket):
        return self.comment


class NativeTicketTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeNativeBackend()
        self.adapter = native.NativeTicketAdapter(self.backend, clock=lambda: NOW)
        self.draft = native.Draft("XAUUSD", "BUY", Decimal("0.01"), Decimal("2490"), Decimal("2520"), 2, NOW + timedelta(minutes=5))

    def test_native_preparation_writes_only_three_fields_and_direction_expiry_comment(self):
        check = Mock(side_effect=lambda: self.backend.events.append(("guard",)))
        result = self.adapter.prepare(self.draft, recheck_account=check)
        self.assertEqual(result, {"status": "prepared"})
        edits = [event for event in self.backend.events if event[0] == "set_edit"]
        self.assertEqual(edits, [("set_edit", 10333, "0.01"), ("set_edit", 10334, "2490.00"), ("set_edit", 10336, "2520.00")])
        self.assertEqual(self.backend.comment, "BUY exp 04/10 12:35Z")
        self.assertLessEqual(len(self.backend.comment), 31)
        self.assertGreaterEqual(check.call_count, 6)
        self.assertEqual(self.backend.events[-2:], [("guard",), ("unlock", 55)])
        self.assertTrue(self.backend.enabled)
        self.assertLess(self.backend.events.index(("lock", 55)), self.backend.events.index(edits[0]))

    def test_existing_native_ticket_is_never_overwritten(self):
        self.backend.ticket = 99
        with self.assertRaisesRegex(native.TicketError, "existing_ticket"):
            self.adapter.prepare(self.draft, recheck_account=Mock())
        self.assertFalse(any(event[0] in ("open", "set_edit", "comment") for event in self.backend.events))
        self.assertFalse(any(event[0] == "cancel" for event in self.backend.events))

    def test_points_or_unverified_settings_and_ticket_unit_changes_block_filling(self):
        for setting in ("mode", "units"):
            with self.subTest(setting=setting):
                self.backend = FakeNativeBackend()
                setattr(self.backend, setting, "points")
                adapter = native.NativeTicketAdapter(self.backend, clock=lambda: NOW)
                with self.assertRaisesRegex(native.TicketError, "absolute_mode_unverified"):
                    adapter.prepare(self.draft, recheck_account=Mock())
                self.assertFalse(any(event[0] == "set_edit" for event in self.backend.events))

    def test_readback_mismatch_stops_before_remaining_fields_or_success(self):
        original = self.backend.read_edit
        self.backend.read_edit = lambda ticket, field: "2400" if field == 10334 else original(ticket, field)
        with self.assertRaisesRegex(native.TicketError, "readback_failed"):
            self.adapter.prepare(self.draft, recheck_account=Mock())
        self.assertNotIn(10336, self.backend.values)
        self.assertIsNone(self.backend.ticket)
        self.assertIn(("cancel", 55), self.backend.events)
        self.assertFalse(any(event[0] == "unlock" for event in self.backend.events))

    def test_expiry_or_account_change_blocks_before_native_fill(self):
        expired = native.NativeTicketAdapter(self.backend, clock=lambda: self.draft.expires_at)
        with self.assertRaisesRegex(native.TicketError, "expired"):
            expired.prepare(self.draft, recheck_account=Mock())
        self.assertEqual(self.backend.events, [])
        check = Mock(side_effect=native.TicketError("account_mismatch"))
        with self.assertRaisesRegex(native.TicketError, "account_mismatch"):
            self.adapter.prepare(self.draft, recheck_account=check)
        self.assertEqual(self.backend.events, [])

    def test_native_low_level_submit_and_unknown_fields_cannot_be_written(self):
        backend = object.__new__(native.Win32Terminal)
        for field in (10408, 10409, 11116, 10331, 1001, 1):
            with self.subTest(field=field), self.assertRaisesRegex(native.TicketError, "unsupported_ui"):
                backend.set_edit(55, field, "1")

    def test_invalid_draft_cannot_be_used_to_write_other_assets_or_lot_sizes(self):
        for symbol, direction, volume, stop, target in (
            ("EURUSD", "BUY", Decimal("0.01"), Decimal(2490), Decimal(2520)),
            ("XAUUSD", "BUY", Decimal("0.1"), Decimal(2490), Decimal(2520)),
            ("XAUUSD", "CLOSE", Decimal("0.01"), Decimal(2490), Decimal(2520)),
            ("XAUUSD", "BUY", Decimal("0.01"), Decimal(2520), Decimal(2490)),
        ):
            with self.subTest(symbol=symbol), self.assertRaises(native.TicketError):
                native.Draft(symbol, direction, volume, stop, target, 2, NOW + timedelta(minutes=5))

    def test_a_ticket_appearing_during_chart_selection_is_not_reused_or_overwritten(self):
        def activate(symbol, timeframe):
            self.backend.ticket = 88
            return 44
        self.backend.activate_chart = activate
        with self.assertRaisesRegex(native.TicketError, "existing_ticket"):
            self.adapter.prepare(self.draft, recheck_account=Mock())
        self.assertEqual(self.backend.values, {})
        self.assertFalse(any(event[0] == "cancel" for event in self.backend.events))

    def test_ticket_unexpectedly_reenabled_during_filling_is_cancelled_before_more_fields(self):
        normal_write = self.backend.set_edit

        def reenable(ticket, field, value):
            normal_write(ticket, field, value)
            self.backend.enabled = True

        self.backend.set_edit = reenable
        with self.assertRaisesRegex(native.TicketError, "ticket_changed"):
            self.adapter.prepare(self.draft, recheck_account=Mock())
        self.assertEqual(set(self.backend.values), {10333})
        self.assertIsNone(self.backend.ticket)
        self.assertFalse(any(event[0] == "unlock" for event in self.backend.events))

    def test_a_replacement_foreign_ticket_is_not_cancelled_after_preparation_failure(self):
        def replace(ticket, field, value):
            self.backend.ticket = 99
            raise native.TicketError("ticket_changed")

        self.backend.set_edit = replace
        with self.assertRaisesRegex(native.TicketError, "ticket_changed"):
            self.adapter.prepare(self.draft, recheck_account=Mock())
        self.assertEqual(self.backend.ticket, 99)
        self.assertFalse(any(event[0] in ("cancel", "unlock") for event in self.backend.events))


class ObservedNativeWindowTests(unittest.TestCase):
    """Exercise observed window shapes with fake handles, never a live desktop."""

    def window_backend(self):
        backend = object.__new__(native.Win32Terminal)
        backend.main, backend.pid = 100, 99
        backend.attested_ticket, backend.price_mode_attested = 55, True
        backend.locked_ticket = None
        backend.account_guard = Mock()
        backend.verify_terminal = Mock()
        backend.window_pid = Mock(return_value=99)
        backend.user = Mock()
        return backend

    def test_observed_comma_symbol_is_accepted_but_other_selected_symbol_is_blocked(self):
        backend = self.window_backend()
        backend.existing_tickets = Mock(return_value=[55])
        backend.class_name = Mock(return_value="#32770")
        backend.control = Mock(side_effect=lambda ticket, field, expected: field)
        labels = {55: "Order: XAUUSD - Gold vs US Dollar", 10325: "XAUUSD, Gold vs US Dollar",
                  11116: "Market Execution"}
        backend.text = lambda handle: labels[handle]
        backend.verify_ticket(55, "XAUUSD")
        labels[10325] = "XAUUSD-other, Gold vs US Dollar"
        with self.assertRaisesRegex(native.TicketError, "ticket_changed"):
            backend.verify_ticket(55, "XAUUSD")
        backend.attested_ticket = 56
        labels[10325] = "XAUUSD, Gold vs US Dollar"
        with self.assertRaisesRegex(native.TicketError, "ticket_changed"):
            backend.verify_ticket(55, "XAUUSD")

    def chart_backend(self, active):
        backend = self.window_backend()
        backend.windows = Mock(return_value=[65280, 65282, 70])
        backend.text = lambda handle: "XAUUSD,M15" if handle in (65280, 65282) else "EURUSD,M15"
        backend.user.GetParent.return_value = 200
        backend.class_name = Mock(return_value="MDIClient")
        backend.user.GetForegroundWindow.return_value = backend.main
        state = {"active": active}

        def message(handle, code, first=0, second=0):
            if code == 0x0222:
                state["active"] = first
                return 0
            self.assertEqual((handle, code), (200, 0x0229))
            return state["active"]

        backend._message = Mock(side_effect=message)
        return backend

    def test_duplicate_matching_charts_keep_current_active_chart_or_select_stable_match(self):
        for active, expected in ((65282, 65282), (70, 65280)):
            with self.subTest(active=active):
                backend = self.chart_backend(active)
                self.assertEqual(backend.activate_chart("XAUUSD", "M15"), expected)
                backend._message.assert_any_call(200, 0x0222, expected, 0)
                backend.user.SetForegroundWindow.assert_not_called()

    def test_duplicate_chart_activation_must_be_verified_and_use_one_mdi_parent(self):
        backend = self.chart_backend(70)
        backend._message = Mock(return_value=70)
        with self.assertRaisesRegex(native.TicketError, "focus_unavailable"):
            backend.activate_chart("XAUUSD", "M15")
        backend = self.chart_backend(70)
        backend.user.GetParent.side_effect = lambda handle: 200 if handle == 65280 else 201
        with self.assertRaisesRegex(native.TicketError, "unsupported_ui"):
            backend.activate_chart("XAUUSD", "M15")

    def mode_backend(self, ticket=55):
        backend = self.window_backend()
        state = {"options": False, "ticket": ticket, "selected": 1, "trade_index": 1,
                 "count": 4, "style": 0x50000000, "mode": "In Prices", "label": "Stop levels:",
                 "children": [12320, 10428, 10391]}
        backend.windows = lambda parent=None: state["children"] if parent == 77 else (
            [100] + ([state["ticket"]] if state["ticket"] else []) + ([77] if state["options"] else []))
        backend.existing_tickets = lambda: [state["ticket"]] if state["ticket"] else []
        classes = {77: "#32770", 12320: "SysTabControl32", 10428: "Static", 10391: "ComboBox"}
        backend.class_name = lambda handle: classes.get(handle, "other")
        backend.text = lambda handle: ("Options" if handle == 77 else state["mode"] if handle == 10391
                                       else state["label"] if handle == 10428 else "Order: XAUUSD - Gold vs US Dollar")
        backend.user.GetDlgCtrlID.side_effect = lambda handle: handle
        backend.user.GetWindowLongW.side_effect = lambda handle, index: state["style"]
        backend.user.IsWindowVisible.side_effect = lambda handle: (
            state["options"] and state["selected"] == state["trade_index"] if handle in (10428, 10391)
            else state["options"] if handle in (77, 12320) else True)
        backend.user.IsWindow = lambda handle: state["options"] if handle == 77 else True
        backend.user.IsWindowEnabled = lambda handle: not state["options"]
        backend._options_menu_command = Mock(return_value=33405)

        def message(handle, code, first=0, second=0):
            if (handle, code) == (77, 0x0010):
                state["options"] = False
            elif (handle, code) == (12320, 0x1304):
                return state["count"]
            elif (handle, code) == (12320, 0x130B):
                return state["selected"]
            elif (handle, code) == (12320, 0x1330):
                state["selected"] = first
            else:
                raise AssertionError("Only native tab navigation and cancelling Options are supported")
            return 1

        backend._message = Mock(side_effect=message)
        def post(handle, code, first, second):
            self.assertEqual((handle, code, first, second), (100, 0x0111, 33405, 0))
            state["options"] = True
            return True
        backend.user.PostMessageW.side_effect = post
        return backend, state

    def test_modeless_owned_ticket_requires_a_fresh_actual_options_read_and_cancel(self):
        backend, state = self.mode_backend()
        # A framework that cannot import must not affect the native reader.
        with patch.dict("sys.modules", {"pywinauto": None}):
            self.assertEqual(backend.read_stop_mode(), "prices")
        self.assertFalse(state["options"])
        self.assertTrue(backend.price_mode_attested)
        backend._message.assert_any_call(77, 0x0010)
        self.assertFalse(any(call.args[1] == 0x1330 for call in backend._message.call_args_list))

    def test_foreign_or_unattested_ticket_blocks_options_before_any_action(self):
        for foreign, prior_attestation in ((56, True), (55, False)):
            with self.subTest(foreign=foreign, prior_attestation=prior_attestation):
                backend, state = self.mode_backend(foreign)
                backend.price_mode_attested = prior_attestation
                with redirect_stderr(io.StringIO()), self.assertRaisesRegex(native.TicketError, "absolute_mode_unverified"):
                    backend.read_stop_mode()
                backend._message.assert_not_called()
                backend.user.PostMessageW.assert_not_called()
                self.assertFalse(state["options"])

    def test_points_changed_with_modeless_ticket_blocks_and_cancels_options(self):
        backend, state = self.mode_backend()
        state["mode"] = "In Points"
        with redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(native.TicketError, "absolute_mode_unverified"):
                backend.read_stop_mode()
        self.assertFalse(state["options"])
        self.assertFalse(backend.price_mode_attested)
        backend._message.assert_any_call(77, 0x0010)

    def test_initial_price_attestation_reads_options_without_opening_any_ticket(self):
        backend, state = self.mode_backend(None)
        backend.price_mode_attested = False
        self.assertEqual(backend.read_stop_mode(), "prices")
        self.assertIsNone(state["ticket"])
        self.assertFalse(state["options"])

    def locked_window_backend(self):
        backend = self.window_backend()
        state = {"enabled": True, "closed": False, "tickets": [55]}
        backend.existing_tickets = lambda: [] if state["closed"] else state["tickets"]
        backend.class_name = Mock(return_value="#32770")
        backend.control = Mock(side_effect=lambda ticket, field, expected: field)
        labels = {55: "Order: XAUUSD - Gold vs US Dollar", 10325: "XAUUSD, Gold vs US Dollar",
                  11116: "Market Execution"}
        backend.text = lambda handle: labels[handle]
        backend.user.IsWindow = lambda handle: not state["closed"]
        backend.user.IsWindowEnabled = lambda handle: state["enabled"]

        def enable(handle, enabled):
            self.assertEqual(handle, 55)
            state["enabled"] = enabled
            return not enabled

        def cancel(handle, message):
            self.assertEqual((handle, message), (55, 0x0010))
            state["closed"] = True
            return 0

        backend.user.EnableWindow.side_effect = enable
        backend._message = Mock(side_effect=cancel)
        return backend, state

    def test_native_lock_and_unlock_target_only_the_verified_whole_order_window(self):
        backend, state = self.locked_window_backend()
        backend.lock_ticket(55, "XAUUSD")
        self.assertFalse(state["enabled"])
        backend.verify_ticket_locked(55, "XAUUSD")
        backend.unlock_ticket(55, "XAUUSD")
        self.assertTrue(state["enabled"])
        self.assertIsNone(backend.locked_ticket)
        self.assertEqual(backend.user.EnableWindow.call_args_list, [unittest.mock.call(55, False), unittest.mock.call(55, True)])
        self.assertFalse(any(call.args[1] in (10408, 10409) for call in backend.control.call_args_list))
        backend._message.assert_not_called()

    def test_native_failure_cancels_only_the_locked_ticket_after_account_identity_check(self):
        backend, state = self.locked_window_backend()
        backend.lock_ticket(55, "XAUUSD")
        backend.cancel_owned_ticket(55, "XAUUSD")
        backend.account_guard.assert_called_once()
        backend._message.assert_called_once_with(55, 0x0010)
        self.assertTrue(state["closed"])
        self.assertIsNone(backend.attested_ticket)
        self.assertIsNone(backend.locked_ticket)
        self.assertFalse(backend.price_mode_attested)

    def test_native_cancel_rejects_changed_account_or_replacement_ticket_without_closing(self):
        for changed in ("account", "ticket"):
            with self.subTest(changed=changed):
                backend, state = self.locked_window_backend()
                backend.lock_ticket(55, "XAUUSD")
                if changed == "account":
                    backend.account_guard.side_effect = native.TicketError("account_mismatch")
                else:
                    state["tickets"] = [99]
                with self.assertRaises(native.TicketError):
                    backend.cancel_owned_ticket(55, "XAUUSD")
                backend._message.assert_not_called()
                self.assertFalse(state["closed"])
                self.assertFalse(state["enabled"])

    def test_mode_failure_diagnostic_preserves_read_stage_after_cancel_and_redacts_ui_text(self):
        for selected, expected in (("In Points", "points"), ("private unexpected UI value", "unknown")):
            with self.subTest(selected=selected):
                backend, state = self.mode_backend()
                state["mode"] = selected
                errors = io.StringIO()
                with redirect_stderr(errors):
                    with self.assertRaisesRegex(native.TicketError, "absolute_mode_unverified"):
                        backend.read_stop_mode()
                self.assertIn("stop_combo_read (TicketError, " + expected + ")", errors.getvalue())
                self.assertNotIn(selected, errors.getvalue())
                self.assertFalse(state["options"])
                self.assertFalse(backend.price_mode_attested)

    def test_native_tab_navigation_skips_reused_server_label_and_finds_real_trade_controls(self):
        backend, state = self.mode_backend()
        state["selected"], state["trade_index"] = 0, 2
        original_windows, original_text = backend.windows, backend.text
        original_visibility = backend.user.IsWindowVisible.side_effect
        backend.windows = lambda parent=None: ([12320, 10428] if parent == 77 and state["selected"] != 2
                                               else original_windows(parent))
        backend.text = lambda handle: ("News languages:" if handle == 10428 and state["selected"] != 2
                                       else original_text(handle))
        backend.user.IsWindowVisible.side_effect = lambda handle: (state["options"] if handle == 10428
                                                                  else original_visibility(handle))
        self.assertEqual(backend.read_stop_mode(), "prices")
        backend._message.assert_any_call(12320, 0x1330, 1, 0)
        backend._message.assert_any_call(12320, 0x1330, 2, 0)
        self.assertFalse(state["options"])
        self.assertEqual(state["mode"], "In Prices")
        self.assertTrue(all(call.args[1] in (0x1304, 0x130B, 0x1330, 0x0010)
                            for call in backend._message.call_args_list))

    def test_native_stop_source_requires_unique_visible_combo_and_exact_label(self):
        for change in ("missing_label", "wrong_label", "duplicate_combo"):
            with self.subTest(change=change):
                backend, state = self.mode_backend()
                if change == "missing_label":
                    state["children"].remove(10428)
                elif change == "wrong_label":
                    state["label"] = "News languages:"
                else:
                    state["children"].append(10391)
                with redirect_stderr(io.StringIO()), self.assertRaisesRegex(native.TicketError, "absolute_mode_unverified"):
                    backend.read_stop_mode()
                self.assertFalse(state["options"])
                self.assertFalse(backend.price_mode_attested)

    def test_native_tab_layout_and_bounds_fail_closed_before_navigation(self):
        for setting, value in (("count", 33), ("count", 0), ("selected", -1), ("style", 0x50000100)):
            with self.subTest(setting=setting, value=value):
                backend, state = self.mode_backend()
                state[setting] = value
                with redirect_stderr(io.StringIO()), self.assertRaisesRegex(native.TicketError, "absolute_mode_unverified"):
                    backend.read_stop_mode()
                self.assertFalse(any(call.args[1] == 0x1330 for call in backend._message.call_args_list))
                self.assertFalse(state["options"])

    def test_native_tab_selection_requires_confirmed_page_change(self):
        backend, state = self.mode_backend()
        state["selected"] = 0
        original = backend._message.side_effect
        backend._message.side_effect = lambda handle, code, *args: (0 if code == 0x1330 else original(handle, code, *args))
        with redirect_stderr(io.StringIO()), self.assertRaisesRegex(native.TicketError, "absolute_mode_unverified"):
            backend.read_stop_mode()
        self.assertEqual(backend.mode_stage, "trade_tab_verify")
        self.assertFalse(state["options"])


if __name__ == "__main__":
    unittest.main()
