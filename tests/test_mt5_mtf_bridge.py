"""Mock-only multi-timeframe feed, risk arithmetic and qualified native checks."""

import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from bridge import mt5_market_bridge as market
from bridge import mt5_native_ticket as native
from tests import test_mt5_bridge as fixtures


class MultiTimeframeFeedTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MT5BridgeTests("test_exact_symbol_completed_bar_read_and_utc_payload")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.sdk, self.settings, self.now = self.fixture.mt5, self.fixture.settings, self.fixture.now

    def feed(self, settings=None):
        return market.build_payload(self.sdk, settings or self.settings, self.now)

    def test_three_exact_closed_histories_share_asof_without_account_identity(self):
        value = self.feed()
        self.assertEqual(value["schema_version"], 2)
        self.assertEqual(value["as_of"], value["risk_context"]["as_of"])
        self.assertEqual(set(value["timeframes"]), {"M15", "M5", "M1"})
        self.assertEqual(value["candles"], value["timeframes"]["M15"])
        for label, seconds in market.TIMEFRAMES.items():
            bars = value["timeframes"][label]
            self.assertEqual(len(bars), 64)
            self.assertEqual(bars[-1]["time"], market.utc_timestamp(int(self.now.timestamp()) - seconds))
        encoded = json.dumps(value)
        self.assertNotIn('"login"', encoded)
        self.assertNotIn('"server"', encoded)
        self.assertNotIn(self.settings.key, encoded)

    def test_missing_old_duplicate_misaligned_or_forming_history_fails_closed(self):
        for label in (5, 1):
            original = deepcopy(self.fixture.mtf_rates[label])
            for failure in ("missing", "empty", "old", "duplicate", "misaligned", "forming", "future", "overlong"):
                changed = deepcopy(original)
                if failure == "missing":
                    changed = None
                elif failure == "empty":
                    changed = []
                elif failure == "old":
                    changed.pop()
                elif failure == "duplicate":
                    changed[-1]["time"] = changed[0]["time"]
                elif failure == "overlong":
                    changed.append(deepcopy(changed[-1]))
                else:
                    changed[-1]["time"] += {"misaligned": -2, "forming": label * 60, "future": label * 60 + 1}[failure]
                self.fixture.mtf_rates[label] = changed
                with self.subTest(label=label, failure=failure), self.assertRaises(market.MarketDataError):
                    self.feed()
            self.fixture.mtf_rates[label] = original

    def test_actual_short_histories_and_session_gaps_are_preserved_without_filling(self):
        for label, seconds in market.TIMEFRAMES.items():
            original = deepcopy(self.fixture.rates if label == "M15" else self.fixture.mtf_rates[seconds // 60])
            for count in (1, 21, 22, 64):
                short = deepcopy(original[-count:])
                if count > 1:
                    # A genuine session break precedes the last actual candle.
                    # The feed carries it unchanged, even with a short suffix;
                    # analysis, rather than collection, owns the warmup gate.
                    for bar in short[:-1]:
                        bar["time"] -= seconds * 4
                if label == "M15":
                    self.sdk.copy_rates_from_pos.return_value = short
                else:
                    self.fixture.mtf_rates[seconds // 60] = short
                with self.subTest(label=label, count=count):
                    bars = self.feed()["timeframes"][label]
                    self.assertEqual(len(bars), count)
                    self.assertEqual([bar["time"] for bar in bars], [market.utc_timestamp(bar["time"]) for bar in short])
                    self.assertEqual([bar["close"] for bar in bars], [bar["close"] for bar in short])
            if label == "M15":
                self.sdk.copy_rates_from_pos.return_value = original
            else:
                self.fixture.mtf_rates[seconds // 60] = original

    def test_quote_ten_second_and_five_second_future_boundaries_are_exact(self):
        for age in (10, -5):
            self.sdk.symbol_info_tick.return_value.time = int(self.now.timestamp()) - age
            self.feed()
        for age in (11, -6):
            self.sdk.symbol_info_tick.return_value.time = int(self.now.timestamp()) - age
            with self.subTest(age=age), self.assertRaises(market.MarketDataError):
                self.feed()

    def test_broker_arithmetic_is_for_exact_volume_and_conservative_both_directions(self):
        self.sdk.order_calc_profit.side_effect = [-2.5, 3.5, -4.5, 1.5]
        self.sdk.order_calc_margin.side_effect = [20.0, 30.0]
        value = self.feed()["risk_context"]
        self.assertEqual(value["loss_cash_per_price_unit"], 4.5)
        self.assertEqual(value["profit_cash_per_price_unit"], 1.5)
        self.assertEqual(value["margin_required"], 30.0)
        self.assertEqual(value["free_margin"], 9000.0)
        self.assertEqual([call.args[2] for call in self.sdk.order_calc_profit.call_args_list], [0.01] * 4)
        self.assertEqual([call.args[2] for call in self.sdk.order_calc_margin.call_args_list], [0.01] * 2)
        self.sdk.positions_get.assert_called_once_with()
        self.sdk.orders_get.assert_called_once_with()
        self.assertEqual(value["broker_fingerprint"], hashlib.sha256(b"MT5|synthetic|XAUUSD.test").hexdigest())

    def test_unknown_costs_are_never_assumed_zero_or_verified(self):
        risk = self.feed()["risk_context"]
        self.assertFalse(risk["costs_verified"])
        self.assertIsNone(risk["commission_round_turn"])
        self.assertIsNone(risk["slippage_price"])
        configured = replace(self.settings, cost_model_verified=True, commission_round_turn_per_lot=7.5, slippage_price=0.2)
        risk = self.feed(configured)["risk_context"]
        self.assertTrue(risk["costs_verified"])
        self.assertEqual(risk["commission_round_turn"], 0.075)
        self.assertEqual(risk["slippage_price"], 0.2)
        self.assertFalse(self.feed(replace(configured, slippage_price=None))["risk_context"]["costs_verified"])

    def test_cost_configuration_requires_explicit_flag_and_bounded_values(self):
        args = argparse.Namespace(terminal=str(self.settings.terminal), symbol=self.settings.symbol)
        base = {"MARKET_BRIDGE_URL": self.settings.url, "MARKET_BRIDGE_KEY": self.settings.key}
        settings = market.load_settings(args, base)
        self.assertEqual((settings.cost_model_verified, settings.commission_round_turn_per_lot, settings.slippage_price), (False, None, None))
        explicit = {**base, "MT5_COST_MODEL_VERIFIED": "true", "MT5_COMMISSION_ROUND_TURN_PER_LOT": "0", "MT5_SLIPPAGE_PRICE": "0.1"}
        settings = market.load_settings(args, explicit)
        self.assertTrue(settings.cost_model_verified)
        self.assertEqual((settings.commission_round_turn_per_lot, settings.slippage_price), (0.0, 0.1))
        self.assertFalse(self.feed(market.load_settings(args, {**base, "MT5_COST_MODEL_VERIFIED": "true"}))["risk_context"]["costs_verified"])
        for name in ("MT5_COMMISSION_ROUND_TURN_PER_LOT", "MT5_SLIPPAGE_PRICE"):
            for invalid in (True, 1, " 0", "0 ", "-1", "nan", "inf", "1e10", "unknown"):
                with self.subTest(name=name, invalid=invalid), self.assertRaises(market.ConfigurationError):
                    market.load_settings(args, {**explicit, name: invalid})
        for flag in (True, "yes", "1", ""):
            with self.subTest(flag=flag), self.assertRaises(market.ConfigurationError):
                market.load_settings(args, {**explicit, "MT5_COST_MODEL_VERIFIED": flag})

    def test_missing_real_account_or_uncalculable_risk_rejects_feed(self):
        originals = self.sdk.account_info.return_value, self.sdk.positions_get.return_value, self.sdk.orders_get.return_value
        for method, value in (("account_info", None), ("account_info", SimpleNamespace(trade_mode=2)),
                              ("positions_get", None), ("orders_get", None), ("order_calc_margin", None)):
            function = getattr(self.sdk, method)
            before = function.return_value
            function.return_value = value
            with self.subTest(method=method), self.assertRaises(market.MarketDataError):
                self.feed()
            function.return_value = before
        self.assertEqual(originals, (self.sdk.account_info.return_value, self.sdk.positions_get.return_value, self.sdk.orders_get.return_value))

    def test_account_wide_exposure_is_reported_for_eligibility_block_without_symbol_filter(self):
        self.sdk.positions_get.return_value = (object(), object())
        self.sdk.orders_get.return_value = (object(),)
        risk = self.feed()["risk_context"]
        self.assertEqual((risk["open_positions"], risk["pending_orders"]), (2, 1))
        self.sdk.positions_get.assert_called_once_with()
        self.sdk.orders_get.assert_called_once_with()

    def test_account_switch_during_cash_conversion_rejects_snapshot(self):
        old = self.sdk.account_info.return_value
        new = SimpleNamespace(**vars(old))
        new.login += 1
        self.sdk.account_info.side_effect = [old, new]
        with self.assertRaises(market.MarketDataError):
            self.feed()


class QualifiedNativeTests(unittest.TestCase):
    def setUp(self):
        self.now = fixtures.MT5BridgeTests("test_exact_symbol_completed_bar_read_and_utc_payload")
        self.now.setUp()
        self.addCleanup(self.now.doCleanups)
        self.time = self.now.now
        self.draft = native.Draft("XAUUSD", "BUY", Decimal("0.01"), Decimal("2490"), Decimal("2520"), 2,
                                  self.time + timedelta(minutes=5), display_timeframe="M1",
                                  strategy_id="mtf-ema-pullback-60m-v1", strategy_version=1,
                                  policy_id="mtf-manual-demo-cost-risk-v1", horizon_seconds=3600,
                                  strategy_fingerprint="a" * 64, qualification_id="b" * 64,
                                  direction_bar_time=self.time - timedelta(minutes=15),
                                  confirmation_bar_time=self.time - timedelta(minutes=5),
                                  bar_time=self.time - timedelta(minutes=1))

    def test_legacy_or_missing_qualification_never_operates_backend(self):
        for changed in (replace(self.draft, display_timeframe="M15"), replace(self.draft, qualification_id=""),
                        replace(self.draft, strategy_version=True), replace(self.draft, horizon_seconds=900),
                        replace(self.draft, direction_bar_time=self.time)):
            backend, recheck = Mock(), Mock()
            with self.subTest(draft=changed), self.assertRaises(native.TicketError):
                native.NativeTicketAdapter(backend, clock=lambda: self.time).prepare(changed, recheck_account=recheck)
            self.assertEqual(backend.mock_calls, [])
            recheck.assert_not_called()

    def test_qualified_draft_activates_m1_and_preserves_exact_stops_readback_and_locks(self):
        backend = Mock()
        backend.existing_tickets.return_value = ()
        edits = {}
        backend.set_edit.side_effect = lambda ticket, control, text: edits.update({control: text})
        backend.read_edit.side_effect = lambda ticket, control: edits[control]
        backend.read_comment.return_value = self.draft.comment
        result = native.NativeTicketAdapter(backend, clock=lambda: self.time).prepare(self.draft, recheck_account=Mock())
        self.assertEqual(result, {"status": "prepared"})
        backend.activate_chart.assert_called_once_with("XAUUSD", "M1")
        self.assertEqual(edits, {10333: "0.01", 10334: "2490.00", 10336: "2520.00"})
        backend.lock_ticket.assert_called_once()
        backend.unlock_ticket.assert_called_once()


if __name__ == "__main__":
    unittest.main()
