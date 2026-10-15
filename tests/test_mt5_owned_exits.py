"""Owned Demo exit tests use synthetic SDK objects only; never live orders."""

from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from unittest.mock import patch
from uuid import uuid4

from bot.trade_timing import build_trade_timing
from bridge import mt5_demo_bridge as demo
from bridge import mt5_owned_exits as exits
from bridge import mt5_trade_bridge as trade
from tests import test_mt5_trade_bridge as fixtures


NOW = fixtures.NOW + timedelta(days=1)


class OwnedDemoExitTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TradeBridgeTests(methodName="test_demo_buy_preflight_attached_stops_and_one_execution")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        old = self.fixture.settings
        self.settings = demo.Settings(old.market, old.state_directory, "demo", Decimal("0.01"), True)
        self.ledger, self.mt5 = self.fixture.ledger, self.fixture.mt5
        self.mt5.mock_add_spec([*self.mt5._mock_methods, "history_deals_get"])
        self.exit_ledger = exits.ExitLedger(self.ledger)
        self.offer = {"id": str(uuid4()), "claim_id": str(uuid4()), "expires_at": trade.iso_date(NOW + timedelta(seconds=10)),
                      "payload": {"direction": "BUY", "bar_time": trade.iso_date(NOW - timedelta(minutes=1)),
                                  "trade_timing": build_trade_timing({
                                      "source": "mt5_symbol_info_session_trade", "verified": True,
                                      "as_of": trade.iso_date(NOW), "session_open": trade.iso_date(NOW.replace(hour=0)),
                                      "session_close": trade.iso_date(NOW.replace(hour=22)),
                                  }, NOW, NOW, 10)}}
        self.request = {"symbol": old.market.symbol, "magic": 123456, "comment": "tgd:" + "a" * 20,
                        "sl": 2490.0, "tp": 2520.0}
        self.ledger.reserve(self.offer)
        self.exit_ledger.stage(self.offer, self.request, NOW)
        self.entry = SimpleNamespace(ticket=111, order=222, position_id=333, symbol=old.market.symbol,
                                     type=0, entry=0, reason=3, volume=0.01, magic=123456,
                                     comment=self.request["comment"], time=int((NOW + timedelta(seconds=3)).timestamp()))
        self.position = SimpleNamespace(ticket=444, identifier=333, symbol=old.market.symbol,
                                        type=0, volume=0.01, magic=123456, comment=self.request["comment"],
                                        sl=2490.0, tp=2520.0)
        self.deals, self.positions = [self.entry], [self.position]
        self.mt5.positions_get.side_effect = lambda **kwargs: tuple(self.positions)
        self.mt5.history_deals_get.side_effect = lambda *args, **kwargs: tuple(self.deals)
        self.fixture.tick.time = int(NOW.timestamp())
        self.exit_ledger.entry_response(self.offer["id"], SimpleNamespace(order=222, deal=111))
        self.moment = NOW + timedelta(seconds=3)

    def process(self):
        return exits.process_exits(self.mt5, self.settings, self.ledger, demo.account_guard, lambda: self.moment)

    def arm(self):
        self.process()
        row = self.exit_ledger.get(self.offer["id"])
        self.assertEqual(row["state"], "armed")
        self.assertEqual(row["exit_at"], trade.iso_date(NOW + timedelta(seconds=3603)))
        return row

    def due(self):
        self.moment = NOW + timedelta(seconds=3603)
        self.fixture.tick.time = int(self.moment.timestamp())

    def close_success(self, request):
        self.assertEqual(request["position"], self.position.ticket)
        self.positions[:] = [position for position in self.positions if position.ticket != request["position"]]
        self.deals.append(SimpleNamespace(ticket=555, order=666, position_id=333, symbol=self.settings.market.symbol,
                                          type=1, entry=1, volume=0.01, time=int(self.moment.timestamp())))
        return SimpleNamespace(retcode=10009, volume=0.01, order=666, deal=555)

    def test_new_fill_is_durably_armed_from_broker_time_without_early_close(self):
        row = self.arm()
        self.assertEqual((row["entry_order"], row["entry_deal"], row["position_id"], row["position_ticket"]), (222, 111, 333, 444))
        self.assertEqual(row["filled_at"], trade.iso_date(NOW + timedelta(seconds=3)))
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()
        event = self.exit_ledger.pending_events()[0]["event"]
        self.assertEqual(event["status"], "armed")
        self.assertEqual(event["reason"], "holding_limit")
        trade.uuid_text(event["event_id"])
        self.assertNotIn("private-test-broker", str(event))

    def test_due_close_targets_exact_position_and_confirmation_requires_history(self):
        self.arm()
        self.due()
        def send(request):
            other = trade.Ledger(self.settings.state_directory)
            try:
                persisted = exits.ExitLedger(other).get(self.offer["id"])
                self.assertEqual((persisted["state"], persisted["close_attempted"]), ("unknown", 1))
            finally:
                other.close()
            return self.close_success(request)
        self.mt5.order_send.side_effect = send
        self.process()
        self.assertEqual(self.exit_ledger.get(self.offer["id"])["state"], "closed")
        request = self.mt5.order_send.call_args.args[0]
        self.assertEqual((request["type"], request["volume"], request["position"], request["price"]), (1, 0.01, 444, self.fixture.tick.bid))
        self.assertEqual(request["deviation"], 0)
        self.assertNotIn("sl", request)
        self.assertNotIn("tp", request)
        self.process()
        self.mt5.order_send.assert_called_once()

    def test_restart_unknown_close_never_sends_again_then_reconciles(self):
        row = self.arm()
        self.assertTrue(self.exit_ledger.reserve_close(row))
        self.due()
        other = trade.Ledger(self.settings.state_directory)
        try:
            exits.process_exits(self.mt5, self.settings, other, demo.account_guard, lambda: self.moment)
            self.mt5.order_send.assert_not_called()
            self.assertEqual(exits.ExitLedger(other).get(self.offer["id"])["state"], "unknown")
            self.close_success({"position": 444})
            exits.process_exits(self.mt5, self.settings, other, demo.account_guard, lambda: self.moment)
            self.assertEqual(exits.ExitLedger(other).get(self.offer["id"])["state"], "closed")
        finally:
            other.close()

    def test_missing_or_ambiguous_history_cannot_adopt_a_position(self):
        for history in (None, [], [self.entry, deepcopy(self.entry)]):
            self.mt5.history_deals_get.side_effect = None
            self.mt5.history_deals_get.return_value = history
            self.process()
            self.assertEqual(self.exit_ledger.get(self.offer["id"])["state"], "awaiting_fill")
        self.mt5.order_send.assert_not_called()

    def test_manual_wrong_comment_magic_direction_reason_and_volume_do_not_arm(self):
        for key, value in (("comment", "manual"), ("magic", 0), ("type", 1), ("reason", 0), ("volume", 0.02), ("position_id", 0)):
            original = getattr(self.entry, key)
            setattr(self.entry, key, value)
            self.process()
            with self.subTest(key=key):
                self.assertEqual(self.exit_ledger.get(self.offer["id"])["state"], "awaiting_fill")
            setattr(self.entry, key, original)
        self.mt5.order_send.assert_not_called()

    def test_unknown_entry_recovers_unique_marker_but_old_manual_positions_are_never_adopted(self):
        self.exit_ledger.entry_response(self.offer["id"], None)
        self.arm()
        self.positions.append(SimpleNamespace(ticket=999, identifier=777, symbol=self.settings.market.symbol,
                                              type=0, volume=0.01, magic=0, comment="manual"))
        self.due()
        self.mt5.order_send.side_effect = self.close_success
        self.process()
        self.assertEqual([position.ticket for position in self.positions], [999])
        self.assertEqual(self.mt5.order_send.call_args.args[0]["position"], 444)
        self.assertEqual(self.exit_ledger.db.execute("SELECT COUNT(*) FROM demo_owned_exits").fetchone()[0], 1)

    def test_position_identity_or_manual_volume_change_blocks_due_close(self):
        self.arm()
        self.due()
        for key, value in (("ticket", 999), ("comment", "manual"), ("magic", 0), ("symbol", "EURUSD"), ("type", 1), ("volume", 0.02)):
            original = getattr(self.position, key)
            setattr(self.position, key, value)
            self.process()
            setattr(self.position, key, original)
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_frozen_protection_is_persisted_and_manual_change_or_removal_blocks_close(self):
        row = self.arm()
        self.assertEqual((Decimal(row["intent"]["stop"]), Decimal(row["intent"]["target"])),
                         (Decimal("2490"), Decimal("2520")))
        self.due()
        for key, value in (("sl", 2490.01), ("sl", 0), ("sl", None),
                           ("tp", 2520.01), ("tp", 0), ("tp", None)):
            original = getattr(self.position, key)
            setattr(self.position, key, value)
            with self.subTest(key=key, value=value):
                self.process()
                self.mt5.order_check.assert_not_called()
                self.mt5.order_send.assert_not_called()
            setattr(self.position, key, original)

    def test_protection_change_after_close_preflight_cannot_send(self):
        self.arm()
        self.due()
        def remove_protection(request):
            self.position.tp = 0
            return SimpleNamespace(retcode=0)
        self.mt5.order_check.side_effect = remove_protection
        self.process()
        self.assertEqual(self.exit_ledger.get(self.offer["id"])["close_attempted"], 0)
        self.mt5.order_send.assert_not_called()

    def test_added_netting_entry_or_partial_close_blocks_further_close(self):
        self.arm()
        self.due()
        self.deals.append(deepcopy(self.entry))
        self.process()
        self.deals.pop()
        self.deals.append(SimpleNamespace(ticket=777, position_id=333, symbol=self.settings.market.symbol,
                                          type=1, entry=1, volume=0.005))
        self.position.volume = 0.005
        self.process()
        self.mt5.order_send.assert_not_called()

    def test_account_switch_is_fatal_and_never_closes(self):
        self.arm()
        self.due()
        self.fixture.account.login += 1
        with self.assertRaises(trade.GuardError):
            self.process()
        self.mt5.order_send.assert_not_called()

    def test_quote_and_preflight_can_recover_without_reserving_a_send(self):
        self.arm()
        self.due()
        self.fixture.tick.time -= 11
        self.process()
        self.assertEqual(self.exit_ledger.get(self.offer["id"])["close_attempted"], 0)
        self.mt5.order_send.assert_not_called()
        self.fixture.tick.time = int(self.moment.timestamp())
        self.mt5.order_check.return_value = SimpleNamespace(retcode=10019)
        self.process()
        self.mt5.order_send.assert_not_called()
        self.mt5.order_check.return_value = SimpleNamespace(retcode=0)
        self.mt5.order_send.side_effect = self.close_success
        self.process()
        self.mt5.order_send.assert_called_once()

    def test_partial_timeout_and_exception_are_unknown_and_never_resubmitted(self):
        self.arm()
        self.due()
        self.mt5.order_send.side_effect = RuntimeError("PRIVATE_EXCEPTION")
        self.process()
        self.process()
        self.assertEqual(self.exit_ledger.get(self.offer["id"])["state"], "unknown")
        self.mt5.order_send.assert_called_once()
        self.assertNotIn("PRIVATE_EXCEPTION", str(self.exit_ledger.pending_events()))

    def test_ioc_partial_close_is_unknown_and_never_closes_remaining_volume_again(self):
        self.arm()
        self.due()
        self.fixture.symbol.trade_exemode, self.fixture.symbol.filling_mode = 2, 2
        def partial(request):
            self.assertEqual(request["position"], 444)
            self.assertEqual(request["type_filling"], self.mt5.ORDER_FILLING_IOC)
            self.assertNotIn("price", request)
            self.position.volume = 0.005
            self.deals.append(SimpleNamespace(ticket=777, position_id=333, symbol=self.settings.market.symbol,
                                              type=1, entry=1, volume=0.005))
            return SimpleNamespace(retcode=10010, volume=0.005, order=888, deal=777)
        self.mt5.order_send.side_effect = partial
        self.process()
        self.process()
        self.assertEqual(self.exit_ledger.get(self.offer["id"])["state"], "unknown")
        self.mt5.order_send.assert_called_once()
        self.assertNotIn("closed", [event["event"]["status"] for event in self.exit_ledger.pending_events()])

    def test_done_response_without_absence_and_history_is_not_reported_closed(self):
        self.arm()
        self.due()
        self.mt5.order_send.return_value = SimpleNamespace(retcode=10009, volume=0.01)
        self.process()
        self.assertEqual(self.exit_ledger.get(self.offer["id"])["state"], "unknown")
        self.positions.clear()
        self.process()
        self.assertEqual(self.exit_ledger.get(self.offer["id"])["state"], "unknown")
        self.mt5.order_send.assert_called_once()

    def test_early_broker_or_manual_full_exit_is_confirmed_without_close_send(self):
        self.arm()
        self.close_success({"position": 444})
        self.process()
        self.assertEqual(self.exit_ledger.get(self.offer["id"])["state"], "closed")
        event = self.exit_ledger.pending_events()[-1]["event"]
        self.assertEqual((event["status"], event["reason"]), ("closed", "position_closed"))
        self.mt5.order_send.assert_not_called()

    def test_late_discovery_of_an_earlier_exit_does_not_claim_holding_limit_causation(self):
        self.arm()
        self.close_success({"position": 444})
        self.moment = NOW + timedelta(hours=2)
        self.process()
        event = self.exit_ledger.pending_events()[-1]["event"]
        self.assertEqual((event["status"], event["reason"]), ("closed", "position_closed"))
        self.assertGreater(trade.utc_date(event["observed_at"]), trade.utc_date(event["deadline"]))
        self.mt5.order_send.assert_not_called()

    def test_unresolved_older_attempts_cannot_hide_new_owned_or_awaiting_intents(self):
        intent = json.dumps(self.exit_ledger.get(self.offer["id"])["intent"])
        for older_state in ("awaiting_fill", "unknown", "armed"):
            with self.subTest(older_state=older_state), self.exit_ledger.db:
                self.exit_ledger.db.execute("DELETE FROM demo_owned_exits")
                for index in range(100):
                    self.exit_ledger.db.execute("""INSERT INTO demo_owned_exits (offer_id,intent,state,exit_at)
                        VALUES (?,?,?,?)""", (str(uuid4()), intent, older_state, trade.iso_date(NOW + timedelta(seconds=index))))
                new_armed, new_awaiting = str(uuid4()), str(uuid4())
                self.exit_ledger.db.execute("""INSERT INTO demo_owned_exits (offer_id,intent,state,exit_at)
                    VALUES (?,?,'armed',?)""", (new_armed, intent, trade.iso_date(NOW + timedelta(hours=1))))
                self.exit_ledger.db.execute("INSERT INTO demo_owned_exits (offer_id,intent,state) VALUES (?,?,'awaiting_fill')",
                                            (new_awaiting, intent))
            active = self.exit_ledger.active()
            identities = [row["offer_id"] for row in active]
            self.assertIn(new_armed, identities)
            self.assertIn(new_awaiting, identities)
            self.assertLessEqual(len(identities), 120)
            if older_state != "armed":
                self.assertEqual(identities[:2], [new_armed, new_awaiting])

    def test_event_delivery_is_durable_idempotent_and_uses_private_result_route(self):
        self.arm()
        first = deepcopy(self.exit_ledger.pending_events())
        post = Mock(side_effect=RuntimeError("offline"))
        with self.assertRaises(RuntimeError):
            demo.flush_exit_events(self.settings, self.ledger, post)
        self.assertEqual(first, self.exit_ledger.pending_events())
        post.side_effect = None
        demo.flush_exit_events(self.settings, self.ledger, post)
        self.assertEqual(self.exit_ledger.pending_events(), [])
        self.assertEqual(post.call_args.args[1], "exit-result")
        envelope = post.call_args.args[2]
        self.assertEqual(set(envelope), {"device_id", "offer_id", "claim_id", "event"})
        self.assertEqual(set(envelope["event"]), {"event_id", "status", "deadline", "observed_at", "reason", "code", "deal", "order"})

    def test_arm_state_and_outbox_insert_roll_back_together_on_fault(self):
        original = exits.ExitLedger._insert_event
        def interrupted(exit_ledger, *args, **kwargs):
            original(exit_ledger, *args, **kwargs)
            raise RuntimeError("synthetic crash after outbox insert")
        with patch.object(exits.ExitLedger, "_insert_event", interrupted), self.assertRaises(RuntimeError):
            self.process()
        other = trade.Ledger(self.settings.state_directory)
        try:
            durable = exits.ExitLedger(other)
            row = durable.get(self.offer["id"])
            self.assertEqual((row["state"], row["position_id"], row["exit_at"]), ("awaiting_fill", 0, None))
            self.assertEqual(durable.pending_events(), [])
        finally:
            other.close()
        self.arm()
        self.assertEqual([event["event"]["status"] for event in self.exit_ledger.pending_events()], ["armed"])
        self.mt5.order_send.assert_not_called()

    def test_terminal_state_and_outbox_insert_roll_back_together_on_fault(self):
        row = self.arm()
        self.exit_ledger.reserve_close(row)
        original = self.exit_ledger._insert_event
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic crash after outbox insert")
        for state in ("closed", "failed"):
            with self.subTest(state=state), patch.object(self.exit_ledger, "_insert_event", interrupted), self.assertRaises(RuntimeError):
                self.exit_ledger.finish(row, state, self.moment, 10009)
            other = trade.Ledger(self.settings.state_directory)
            try:
                durable = exits.ExitLedger(other)
                saved = durable.get(self.offer["id"])
                self.assertEqual((saved["state"], saved["result"], saved["close_attempted"]), ("unknown", {}, 1))
                self.assertEqual([event["event"]["status"] for event in durable.pending_events()], ["armed"])
            finally:
                other.close()
        self.exit_ledger.finish(row, "closed", self.moment, 10009)
        self.assertEqual(self.exit_ledger.get(self.offer["id"])["state"], "closed")
        self.assertEqual([event["event"]["status"] for event in self.exit_ledger.pending_events()], ["armed", "closed"])

    def test_frozen_policy_mutation_and_intent_replacement_are_rejected(self):
        with self.assertRaises(trade.GuardError):
            self.exit_ledger.stage(self.offer, self.request, NOW)
        changed = deepcopy(self.offer)
        changed["id"] = str(uuid4())
        changed["payload"]["trade_timing"]["max_hold_seconds"] = 7200
        with self.assertRaises(trade.GuardError):
            self.exit_ledger.stage(changed, self.request, NOW)

    def test_verified_broker_offset_normalizes_fill_time_once(self):
        # Changing offset requires a separate, freshly bound synthetic account.
        self.settings = replace(self.settings, market=replace(self.settings.market, broker_utc_offset_minutes=180))
        self.entry.time += 180 * 60
        self.fixture.tick.time += 180 * 60
        self.arm()
        self.mt5.order_send.assert_not_called()

    def test_last_permitted_fill_is_capped_by_verified_session_safety_deadline(self):
        offer = deepcopy(self.offer)
        offer["id"] = str(uuid4())
        offer["payload"]["trade_timing"] = build_trade_timing({
            "source": "mt5_symbol_info_session_trade", "verified": True, "as_of": trade.iso_date(NOW),
            "session_open": trade.iso_date(NOW.replace(hour=0, minute=0)),
            "session_close": trade.iso_date(NOW + timedelta(seconds=3610 + 900)),
        }, NOW, NOW, 10)
        self.ledger.reserve(offer)
        self.exit_ledger.stage(offer, self.request, NOW)
        self.entry.time = int((NOW + timedelta(seconds=10)).timestamp())
        self.moment = NOW + timedelta(seconds=10)
        self.exit_ledger.entry_rejected(self.offer["id"])
        self.exit_ledger.entry_response(offer["id"], SimpleNamespace(order=222, deal=111))
        self.process()
        row = self.exit_ledger.get(offer["id"])
        self.assertEqual(row["exit_at"], trade.iso_date(NOW + timedelta(seconds=3610)))
        self.assertEqual(self.exit_ledger.pending_events()[-1]["event"]["reason"], "session_cutoff")

    def test_broker_late_fill_remains_owned_without_extending_frozen_latest_exit(self):
        self.entry.time = int((NOW + timedelta(seconds=25)).timestamp())
        self.moment = NOW + timedelta(seconds=25)
        self.process()
        row = self.exit_ledger.get(self.offer["id"])
        self.assertEqual(row["state"], "armed")
        self.assertEqual(row["filled_at"], trade.iso_date(NOW + timedelta(seconds=25)))
        self.assertEqual(trade.utc_date(row["exit_at"]), trade.utc_date(self.offer["payload"]["trade_timing"]["latest_exit_at"]))
        self.mt5.order_send.assert_not_called()

    def test_fill_milliseconds_preserve_deadline_precision_and_inconsistent_clock_blocks(self):
        self.entry.time_msc = self.entry.time * 1000 + 250
        self.process()
        row = self.exit_ledger.get(self.offer["id"])
        self.assertEqual(trade.utc_date(row["filled_at"]), NOW + timedelta(seconds=3, milliseconds=250))
        self.assertEqual(trade.utc_date(row["exit_at"]), NOW + timedelta(seconds=3603, milliseconds=250))
        self.entry.time_msc += 1000
        self.due()
        self.process()
        self.mt5.order_send.assert_not_called()

    def test_restart_after_deadline_omits_expired_arm_event_and_sends_close_confirmation(self):
        self.moment = NOW + timedelta(hours=2)
        self.fixture.tick.time = int(self.moment.timestamp())
        self.mt5.order_send.side_effect = self.close_success
        self.process()
        events = self.exit_ledger.pending_events()
        self.assertEqual([event["event"]["status"] for event in events], ["closed"])
        self.mt5.order_send.assert_called_once()

    def test_quote_or_account_change_after_exit_preflight_never_sends(self):
        self.arm()
        self.due()
        def checked(request):
            self.fixture.tick.bid -= 0.01
            return SimpleNamespace(retcode=0)
        self.mt5.order_check.side_effect = checked
        self.process()
        self.mt5.order_send.assert_not_called()
        self.assertEqual(self.exit_ledger.get(self.offer["id"])["close_attempted"], 0)
        def changed_account(request):
            self.fixture.account.login += 1
            return SimpleNamespace(retcode=0)
        self.mt5.order_check.side_effect = changed_account
        with self.assertRaises(trade.GuardError):
            self.process()
        self.mt5.order_send.assert_not_called()

    def test_local_owned_exit_runs_before_broken_candle_feed_or_http(self):
        self.arm()
        self.due()
        self.mt5.order_send.side_effect = self.close_success
        calls = []
        def post(settings, route, payload):
            calls.append(route)
            if route == "register":
                return {"paired": True}
            if route == "exit-result":
                raise RuntimeError("synthetic disconnected network")
            return {}
        with patch.object(trade, "terminal_is_running", side_effect=[True, True, False]), redirect_stdout(io.StringIO()):
            result = demo.run_bridge(self.mt5, self.settings, post=post, clock=lambda: self.moment,
                                     sleep=lambda seconds: None, exporter_factory=lambda *args: Mock())
        self.assertEqual(result, 0)
        self.assertEqual(self.exit_ledger.get(self.offer["id"])["state"], "closed")
        self.mt5.order_send.assert_called_once()
        self.assertEqual(calls, ["register", "exit-result"])

    def test_offline_registration_restart_still_closes_owned_position_without_polling(self):
        self.arm()
        self.due()
        self.mt5.order_send.side_effect = self.close_success
        routes = []
        def offline(settings, route, payload):
            routes.append(route)
            raise RuntimeError("synthetic offline registration")
        with patch.object(trade, "terminal_is_running", side_effect=[True, True, True, False]), redirect_stdout(io.StringIO()):
            result = demo.run_bridge(self.mt5, self.settings, post=offline, clock=lambda: self.moment,
                                     sleep=lambda seconds: None, exporter_factory=lambda *args: Mock())
        self.assertEqual(result, 0)
        self.assertEqual(self.exit_ledger.get(self.offer["id"])["state"], "closed")
        self.mt5.order_send.assert_called_once()
        self.assertEqual(routes, ["register"])


if __name__ == "__main__":
    unittest.main()
