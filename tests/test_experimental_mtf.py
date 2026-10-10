"""Experimental Demo changes remain explicit, costed and manually actionable."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import os
import unittest
from unittest.mock import patch

from bot import mtf_runtime, multi_timeframe as mtf, paper_signals
from tests import test_multi_timeframe as candle_fixtures


class ExperimentalMtfTests(unittest.TestCase):
    def setUp(self):
        self.fixture = candle_fixtures.MultiTimeframeTests()
        self.now = self.fixture.now
        self.feed = self.fixture.feed()
        self.feed["risk_context"]["costs_verified"] = False

    def evaluate(self, feed=None, now=None):
        return mtf_runtime.evaluate_feed(self.feed if feed is None else feed,
                                         self.now if now is None else now)

    def test_default_and_unknown_modes_keep_verified_cost_and_certificate_gates(self):
        for mode in ("qualified", "", "experimental", "false"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"MT5_SIGNAL_MODE": mode}):
                self.assertEqual(self.evaluate()["reason"], "unverified_costs")
                verified = deepcopy(self.feed)
                verified["risk_context"]["costs_verified"] = True
                with patch.dict(os.environ, {"MT5_EVIDENCE_PATH": "", "MT5_EVIDENCE_SHA256": ""}):
                    self.assertEqual(self.evaluate(verified)["reason"], "evidence_unavailable")

    def test_explicit_demo_mode_is_provisional_without_fake_evidence(self):
        original = deepcopy(self.feed)
        with patch.dict(os.environ, {"MT5_SIGNAL_MODE": "experimental_demo"}), \
                patch.object(mtf_runtime, "configured_evidence") as loader:
            result = self.evaluate()
            self.assertEqual(result["state"], "signal", result)
            self.assertEqual((result["strategy_id"], result["strategy_version"], result["policy_id"]),
                             (mtf.EXPERIMENTAL_STRATEGY_ID, 3, mtf.EXPERIMENTAL_POLICY_ID))
            self.assertTrue(result["provisional"])
            self.assertEqual(result["signal_mode"], "experimental_demo")
            self.assertEqual(result["entry_window_seconds"], 30)
            self.assertNotIn("qualification_id", result)
            self.assertNotIn("evidence_metrics", result)
            self.assertTrue(mtf_runtime.eligible_result(result, self.now))
            self.assertTrue(mtf_runtime.eligible_payload(dict(result, workflow="manual_ticket"), self.feed, self.now))
            loader.assert_not_called()
        self.assertEqual(self.feed, original)

    def test_profiles_have_separate_identities_and_research_does_not_enable_alerts(self):
        original = mtf.strategy_identity()
        experimental = mtf.strategy_identity(experimental_demo=True)
        self.assertNotEqual(original["fingerprint"], experimental["fingerprint"])
        result = mtf.analyze_multi_timeframe(self.feed, self.now, research_only=True)
        self.assertEqual(result["strategy_id"], mtf.STRATEGY_ID)
        with patch.dict(os.environ, {"MT5_SIGNAL_MODE": "experimental_demo"}):
            self.assertFalse(mtf_runtime.eligible_result(result, self.now))
        result = mtf.analyze_multi_timeframe(self.feed, self.now, research_only=True, experimental_demo=True)
        self.assertEqual(result["reason"], "invalid_signal_profile")

    def test_experimental_profile_never_promotes_counter_trend_or_missing_higher_context(self):
        with patch.dict(os.environ, {"MT5_SIGNAL_MODE": "experimental_demo"}):
            for sell in (False, True):
                feed = self.fixture.feed(sell=sell)
                feed["risk_context"]["costs_verified"] = False
                original = dict(self.evaluate(feed), workflow="manual_ticket")
                self.assertEqual(original["state"], "signal", original)
                self.assertTrue(mtf_runtime.eligible_payload(original, feed, self.now))
                for frame in ("H1", "H4"):
                    changed = deepcopy(feed)
                    changed["timeframes"][frame] = self.fixture.feed(sell=not sell)["timeframes"][frame]
                    result = self.evaluate(changed)
                    self.assertEqual((result["state"], result["reason"]), ("no_signal", "higher_timeframe_conflict"))
                    self.assertEqual(result["timeframe_context"]["confidence"], "reduced")
                    self.assertNotIn("entry", result)
                    self.assertNotIn("direction", result)
                    self.assertFalse(mtf_runtime.eligible_payload(original, changed, self.now))
                    del changed["timeframes"][frame]
                    self.assertFalse(mtf_runtime.eligible_payload(original, changed, self.now))
                legacy = deepcopy(original)
                legacy.update(strategy_id="mtf-ema-pullback-60m-demo-v2", strategy_version=2,
                              policy_id="mtf-manual-demo-estimated-cost-risk-v2")
                self.assertFalse(mtf_runtime.eligible_result(legacy, self.now))

    def test_estimates_are_nonzero_copy_inputs_and_never_claim_verification(self):
        self.feed["risk_context"].update(commission_round_turn=0, slippage_price=0, costs_verified=True)
        original = deepcopy(self.feed)
        effective = mtf.prepare_analysis_feed(self.feed, experimental_demo=True)
        self.assertEqual(effective["risk_context"]["commission_round_turn"], .01)
        self.assertEqual(effective["risk_context"]["slippage_price"], .002)
        self.assertFalse(effective["risk_context"]["costs_verified"])
        self.assertEqual(self.feed, original)
        self.assertEqual(mtf.prepare_analysis_feed(effective, experimental_demo=True), effective)
        result = mtf.analyze_multi_timeframe(self.feed, self.now, experimental_demo=True)
        self.assertEqual(result["cost_assumptions"], {
            "verified": False, "method": "spread_tick_floor_v1",
            "commission_round_turn": .01, "slippage_price": .002,
            "spread_price": .002, "tick_size": .001,
        })
        self.assertEqual(result["estimated_cost_cash"], .014)

    def test_spread_floors_and_larger_reported_costs_are_retained(self):
        self.feed["quote"].update(bid=2000, ask=2000.02)
        self.feed["risk_context"].update(commission_round_turn=0, slippage_price=0,
                                         loss_cash_per_price_unit=1.23456789)
        effective = mtf.prepare_analysis_feed(self.feed, experimental_demo=True)["risk_context"]
        self.assertGreaterEqual(Decimal(str(effective["commission_round_turn"])), Decimal("1.23456789") * Decimal(".02"))
        self.assertEqual(effective["slippage_price"], .01)
        self.feed["risk_context"].update(commission_round_turn=2, slippage_price=.3)
        effective = mtf.prepare_analysis_feed(self.feed, experimental_demo=True)["risk_context"]
        self.assertEqual((effective["commission_round_turn"], effective["slippage_price"]), (2, .3))

    def test_live_unknown_none_costs_use_floors_only_in_unverified_context(self):
        self.feed["risk_context"].update(commission_round_turn=None, slippage_price=None)
        with patch.dict(os.environ, {"MT5_SIGNAL_MODE": "experimental_demo"}):
            result = self.evaluate()
            self.assertEqual(result["state"], "signal", result)
            self.assertEqual(result["cost_context"]["commission_round_turn"], .01)
            self.assertEqual(result["cost_context"]["slippage_price"], .002)
            self.assertTrue(mtf_runtime.eligible_payload(dict(result, workflow="manual_ticket"), self.feed, self.now))
            self.assertIsNone(self.feed["risk_context"]["commission_round_turn"])
            self.assertIsNone(self.feed["risk_context"]["slippage_price"])
            for verified in (True, None, "false", 0):
                feed = deepcopy(self.feed)
                feed["risk_context"]["costs_verified"] = verified
                self.assertNotEqual(self.evaluate(feed)["state"], "signal", verified)
            for key in ("commission_round_turn", "slippage_price"):
                for value in (True, -1, float("nan"), float("inf"), "0"):
                    feed = deepcopy(self.feed)
                    feed["risk_context"][key] = value
                    self.assertEqual(self.evaluate(feed)["reason"], "invalid_number")

    def test_only_three_preceding_m5_candles_can_supply_pullback(self):
        for sell in (False, True):
            for offset in (2, 3, 4, 5):
                feed = self.fixture.feed(sell=sell)
                feed["risk_context"]["costs_verified"] = False
                bars = feed["timeframes"]["M5"]
                bars[-2]["high" if sell else "low"] = 2000.3 if sell else 1999.7
                fast = paper_signals._ema([bar["close"] for bar in bars], 9)
                bar = bars[-offset]
                bar["high" if sell else "low"] = fast[-offset] + (.02 if sell else -.02)
                with self.subTest(sell=sell, offset=offset):
                    result = mtf.analyze_multi_timeframe(feed, self.now, experimental_demo=True)
                    self.assertEqual(result["state"], "signal" if offset <= 4 else "no_signal", result)
                    if offset > 2:
                        strict = mtf.analyze_multi_timeframe(feed, self.now, research_only=True)
                        self.assertEqual(strict["reason"], "m5_pullback_not_confirmed")

    def test_latest_m5_recovery_and_m1_breakout_stay_required(self):
        for frame, changes, reason in (
            ("M5", {"open": 2000.0, "close": 1999.9}, "m5_pullback_not_confirmed"),
            ("M1", {"high": 2000.03, "close": 1999.99}, "m1_breakout_not_confirmed"),
        ):
            feed = deepcopy(self.feed)
            feed["timeframes"][frame][-1].update(changes)
            self.assertEqual(mtf.analyze_multi_timeframe(feed, self.now, experimental_demo=True)["reason"], reason)

    def test_thirty_second_window_boundaries_use_current_quote_and_risk(self):
        with patch.dict(os.environ, {"MT5_SIGNAL_MODE": "experimental_demo"}):
            frozen = self.evaluate()
            for seconds in (0, 10, 11, 30, 30.000001, 31):
                clock = self.now + timedelta(seconds=seconds)
                feed = deepcopy(self.feed)
                feed["as_of"] = feed["risk_context"]["as_of"] = feed["quote"]["time"] = clock.isoformat()
                with self.subTest(seconds=seconds):
                    result = self.evaluate(feed, clock)
                    self.assertEqual(result["state"], "signal" if seconds <= 30 else "blocked", result)
                    self.assertEqual(mtf_runtime.eligible_result(frozen, clock), seconds <= 30)
                    self.assertEqual(mtf_runtime.eligible_payload(dict(frozen, workflow="manual_ticket"), feed, clock), seconds <= 30)
                    if seconds > 30:
                        self.assertEqual(result["entry_window_seconds"], 30)
                        self.assertNotIn("direction", result)

    def test_mode_switches_tampered_markers_and_fake_certificates_fail_closed(self):
        with patch.dict(os.environ, {"MT5_SIGNAL_MODE": "experimental_demo"}):
            original = self.evaluate()
            for key, value in (("provisional", False), ("qualification_id", "a" * 64),
                               ("evidence_metrics", []), ("strategy_version", 1),
                               ("entry_window_seconds", 10), ("strategy_fingerprint", "f" * 64),
                               ("account_mode", "real"), ("volume", .02)):
                self.assertFalse(mtf_runtime.eligible_result(dict(original, **{key: value}), self.now), key)
            changed = deepcopy(original)
            changed["cost_assumptions"]["verified"] = True
            self.assertFalse(mtf_runtime.eligible_result(changed, self.now))
            changed = deepcopy(original)
            changed["cost_context"]["commission_round_turn"] = 0
            self.assertFalse(mtf_runtime.eligible_result(changed, self.now))
        with patch.dict(os.environ, {"MT5_SIGNAL_MODE": "qualified"}):
            self.assertFalse(mtf_runtime.eligible_result(original, self.now))
            self.assertFalse(mtf_runtime.eligible_payload(dict(original, workflow="manual_ticket"), self.feed, self.now))

    def test_experimental_estimates_preserve_exposure_margin_equity_and_cost_guards(self):
        with patch.dict(os.environ, {"MT5_SIGNAL_MODE": "experimental_demo"}):
            original = dict(self.evaluate(), workflow="manual_ticket")
            for key, value, reason in (
                ("open_positions", 1, "existing_exposure"),
                ("pending_orders", 1, "existing_exposure"),
                ("free_margin", 1, "insufficient_free_margin"),
                ("equity", 10, "equity_risk_limit"),
                ("commission_round_turn", 2, "insufficient_reward_after_costs"),
                ("account_mode", "real", "unsupported_demo_configuration"),
                ("volume", .02, "unsupported_demo_configuration"),
            ):
                feed = deepcopy(self.feed)
                feed["risk_context"][key] = value
                with self.subTest(key=key):
                    self.assertEqual(self.evaluate(feed)["reason"], reason)
                    self.assertFalse(mtf_runtime.eligible_payload(original, feed, self.now))

    def test_experimental_stale_excess_spread_drift_gap_and_future_candles_fail(self):
        with patch.dict(os.environ, {"MT5_SIGNAL_MODE": "experimental_demo"}):
            original = dict(self.evaluate(), workflow="manual_ticket")
            for defect in ("quote", "snapshot", "risk", "spread", "drift", "gap", "future"):
                feed = deepcopy(self.feed)
                if defect in {"quote", "snapshot", "risk"}:
                    old = (self.now - timedelta(seconds=31)).isoformat()
                    if defect == "quote":
                        feed["quote"]["time"] = old
                    elif defect == "snapshot":
                        feed["as_of"] = old
                    else:
                        feed["risk_context"]["as_of"] = old
                elif defect == "spread":
                    feed["quote"]["ask"] = 2000.05
                elif defect == "drift":
                    feed["quote"].update(bid=original["entry"] + .2, ask=original["entry"] + .202)
                elif defect == "gap":
                    feed["timeframes"]["M15"] = feed["timeframes"]["M15"][-21:]
                else:
                    feed["timeframes"]["M1"][-1]["time"] = self.now.isoformat()
                with self.subTest(defect=defect):
                    self.assertFalse(mtf_runtime.eligible_payload(original, feed, self.now))

    def test_changed_estimated_spread_can_prepare_with_worst_current_or_frozen_costs(self):
        self.feed["risk_context"].update(commission_round_turn=0, slippage_price=0)
        with patch.dict(os.environ, {"MT5_SIGNAL_MODE": "experimental_demo"}):
            original = dict(self.evaluate(), workflow="manual_ticket")
            feed = deepcopy(self.feed)
            feed["quote"]["ask"] = 2000.006
            current = self.evaluate(feed)
            self.assertEqual(current["state"], "signal", current)
            self.assertGreater(current["cost_context"]["slippage_price"], original["cost_context"]["slippage_price"])
            self.assertTrue(mtf_runtime.eligible_payload(original, feed, self.now))
            # The larger current spread/cost floor and executable entry can
            # exceed a cash budget even though the frozen estimate did not.
            self.feed["risk_context"]["equity"] = (original["estimated_loss_cash"] + .001) * 100
            near_limit = dict(self.evaluate(), workflow="manual_ticket")
            self.assertEqual(near_limit["state"], "signal", near_limit)
            feed["risk_context"]["equity"] = self.feed["risk_context"]["equity"]
            self.assertEqual(self.evaluate(feed)["reason"], "equity_risk_limit")
            self.assertFalse(mtf_runtime.eligible_payload(near_limit, feed, self.now))

    def test_lower_current_cost_estimates_never_replace_larger_frozen_assumptions(self):
        self.feed["risk_context"].update(commission_round_turn=.08, slippage_price=.02)
        with patch.dict(os.environ, {"MT5_SIGNAL_MODE": "experimental_demo"}):
            original = dict(self.evaluate(), workflow="manual_ticket")
            self.assertEqual(original["state"], "signal", original)
            feed = deepcopy(self.feed)
            feed["risk_context"].update(commission_round_turn=0, slippage_price=0)
            feed["risk_context"]["equity"] = (original["estimated_loss_cash"] - .01) * 100
            # Current estimates alone pass; the frozen higher costs do not.
            self.assertEqual(self.evaluate(feed)["state"], "signal")
            self.assertFalse(mtf_runtime.eligible_payload(original, feed, self.now))


if __name__ == "__main__":
    unittest.main()
