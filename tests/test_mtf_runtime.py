"""Synthetic operator-pinned evidence tests authorization, not performance."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bot import mtf_runtime, multi_timeframe, strategy_evidence
from tests import test_multi_timeframe as candle_fixtures
from tests import test_strategy_evidence as evidence_fixtures


def synthetic_case():
    fixture = candle_fixtures.MultiTimeframeTests()
    feed = fixture.feed()
    result = multi_timeframe.analyze_multi_timeframe(feed, fixture.now)
    return feed, result, fixture.now


def synthetic_report(feed, now, *, wins=180, total=200):
    report = evidence_fixtures.report(wins=wins, total=total)
    report["strategy"] = multi_timeframe.strategy_identity()
    report["generated_at"] = (now - timedelta(days=1)).isoformat()
    report["dataset"]["broker_fingerprint"] = feed["risk_context"]["broker_fingerprint"]
    report["dataset"]["execution_specs"] = {
        **feed["execution"], "verified": True, "evidence_sha256": "e" * 64,
        "effective_start": report["dataset"]["coverage_start"],
        "effective_end": report["dataset"]["coverage_end"],
    }
    cost = report["dataset"]["cost_scenarios"][0]
    for key in ("commission_round_turn", "slippage_price", "profit_cash_per_price_unit", "loss_cash_per_price_unit"):
        cost[key] = feed["risk_context"][key]
    cost["financing_cash_per_hour"] = 0
    return report


@contextmanager
def pinned_synthetic_evidence(feed, now, *, report=None):
    """Use the real local loader/pin/gate; no fake trusted token is minted."""
    value = synthetic_report(feed, now) if report is None else report
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "synthetic-only-evidence.json"
        raw = json.dumps(value, allow_nan=False).encode("utf-8")
        path.write_bytes(raw)
        pin = hashlib.sha256(raw).hexdigest()
        with patch.dict(os.environ, {"MT5_EVIDENCE_PATH": str(path), "MT5_EVIDENCE_SHA256": pin}), \
                patch.object(mtf_runtime, "_clock", return_value=now):
            yield pin


class MtfRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.feed, self.candidate, self.now = synthetic_case()
        self.assertEqual(self.candidate["state"], "signal", self.candidate)

    def qualified(self):
        result = mtf_runtime.evaluate_feed(self.feed, self.now)
        self.assertEqual(result["state"], "signal", result)
        return result

    def test_only_real_operator_pinned_artifact_qualifies_and_preserves_candidate(self):
        original = deepcopy(self.feed)
        with pinned_synthetic_evidence(self.feed, self.now) as pin:
            result = self.qualified()
            self.assertEqual(result["qualification_id"], pin)
            self.assertEqual(result["evidence_metrics"][0]["trades"], 200)
            self.assertEqual(result["evidence_metrics"][0]["wins"], 180)
            self.assertGreaterEqual(result["evidence_metrics"][0]["lower_95"], .7)
            self.assertTrue(mtf_runtime.eligible_result(result, self.now))
            for key, value in self.candidate.items():
                self.assertEqual(result[key], value)
        self.assertEqual(self.feed, original)

    def test_missing_or_wrong_artifact_pin_blocks_all_direction_and_levels(self):
        with pinned_synthetic_evidence(self.feed, self.now):
            for pin in ("", "f" * 64):
                with patch.dict(os.environ, {"MT5_EVIDENCE_SHA256": pin}):
                    result = mtf_runtime.evaluate_feed(self.feed, self.now)
                    self.assertEqual((result["state"], result["reason"]), ("blocked", "evidence_unavailable"))
                    for key in ("direction", "entry", "stop", "target", "qualification_id"):
                        self.assertNotIn(key, result)

    def test_observed70_percent_or_under200_trades_cannot_open_alerts(self):
        for wins, total in ((140, 200), (199, 199)):
            with pinned_synthetic_evidence(self.feed, self.now, report=synthetic_report(self.feed, self.now, wins=wins, total=total)):
                result = mtf_runtime.evaluate_feed(self.feed, self.now)
                self.assertEqual(result["state"], "blocked")
                self.assertNotIn("direction", result)

    def test_raw_ai_confidence_or_forged_evidence_token_never_qualifies(self):
        with pinned_synthetic_evidence(self.feed, self.now):
            for value in ({"qualified": True, "confidence": .99}, strategy_evidence.EvidenceDecision(True, ())):
                with patch.object(mtf_runtime, "configured_evidence", return_value=value):
                    result = mtf_runtime.evaluate_feed(self.feed, self.now)
                    self.assertEqual(result["state"], "blocked")
                    self.assertNotIn("direction", result)
                self.assertFalse(strategy_evidence.qualification_gate(self.candidate, value, now=self.now))

    def test_unknown_current_costs_block_before_reading_evidence(self):
        self.feed["risk_context"]["costs_verified"] = False
        with patch.object(mtf_runtime, "configured_evidence") as loader:
            result = mtf_runtime.evaluate_feed(self.feed, self.now)
            self.assertEqual(result["reason"], "unverified_costs")
            loader.assert_not_called()

    def test_zero_or_unknown_spread_cannot_qualify_or_reuse_frozen_offer(self):
        with pinned_synthetic_evidence(self.feed, self.now):
            result = self.qualified()
            result["workflow"] = "manual_ticket"
            self.feed["quote"]["bid"] = self.feed["quote"]["ask"]
            current = mtf_runtime.evaluate_feed(self.feed, self.now)
            self.assertEqual(current["reason"], "excessive_or_unknown_spread")
            self.assertNotIn("direction", current)
            self.assertFalse(mtf_runtime.eligible_payload(result, self.feed, self.now))

    def test_provisional_legacy_and_wrong_study_bindings_cannot_reuse_certificate(self):
        with pinned_synthetic_evidence(self.feed, self.now):
            original = self.qualified()
            for key, value in (("provisional", True), ("strategy_id", "ema9-21-atr14-v1"),
                               ("policy_id", "legacy"), ("strategy_fingerprint", "f" * 64),
                               ("broker_fingerprint", "b" * 64), ("horizon_seconds", 900),
                               ("qualification_id", "f" * 64)):
                with self.subTest(key=key):
                    self.assertFalse(mtf_runtime.eligible_result({**original, key: value}, self.now))
            changed = deepcopy(original)
            changed["cost_context"]["slippage_price"] = .02
            self.assertFalse(mtf_runtime.eligible_result(changed, self.now))

    def test_complete_qualified_signal_fields_are_required_for_any_public_consumer(self):
        keys = ("direction", "symbol", "strategy_version", "display_timeframe", "decision_time", "quote_time",
                "bar_time", "confirmation_bar_time", "direction_bar_time", "entry", "stop", "target", "target2",
                "entry_zone_low", "entry_zone_high", "original_stop_distance", "execution", "price_digits",
                "account_mode", "volume", "nominal_reward_risk", "effective_reward_risk")
        with pinned_synthetic_evidence(self.feed, self.now):
            original = self.qualified()
            for key in keys:
                with self.subTest(missing=key):
                    changed = deepcopy(original)
                    del changed[key]
                    self.assertFalse(mtf_runtime.eligible_result(changed, self.now))

    def test_frozen_candidate_cannot_outlive_m1_timing_or_reference_future_bars(self):
        with pinned_synthetic_evidence(self.feed, self.now):
            original = self.qualified()
            with patch.object(mtf_runtime, "_clock", return_value=self.now + timedelta(seconds=76)):
                self.assertFalse(mtf_runtime.eligible_result(original, self.now + timedelta(seconds=76)))
            for key in ("bar_time", "confirmation_bar_time", "direction_bar_time", "decision_time"):
                changed = deepcopy(original)
                changed[key] = (self.now + timedelta(minutes=15)).isoformat()
                self.assertFalse(mtf_runtime.eligible_result(changed, self.now), key)

    def test_actionable_window_matches_ten_seconds_after_completed_m1(self):
        with pinned_synthetic_evidence(self.feed, self.now):
            original = self.qualified()
            for seconds in (0, 10, 10.000001, 11, 75):
                clock = self.now + timedelta(seconds=seconds)
                updated = deepcopy(self.feed)
                updated["as_of"] = updated["risk_context"]["as_of"] = clock.isoformat()
                updated["quote"]["time"] = clock.isoformat()
                with self.subTest(seconds=seconds), patch.object(mtf_runtime, "_clock", return_value=clock):
                    analyzed = multi_timeframe.analyze_multi_timeframe(updated, clock)
                    self.assertEqual(analyzed["state"], "signal", analyzed)
                    result = mtf_runtime.evaluate_feed(updated, clock)
                    if seconds <= 10:
                        self.assertEqual(result["state"], "signal", result)
                        self.assertTrue(mtf_runtime.eligible_result(original, clock))
                    else:
                        self.assertEqual((result["state"], result["reason"]), ("blocked", "entry_window_expired"))
                        self.assertFalse(mtf_runtime.eligible_result(original, clock))
                        self.assertFalse(mtf_runtime.eligible_payload(dict(original, workflow="manual_ticket"), updated, clock))
                        for key in ("direction", "entry", "stop", "target", "qualification_id"):
                            self.assertNotIn(key, result)

    def test_future_original_decision_cannot_enter_the_actionable_window(self):
        with pinned_synthetic_evidence(self.feed, self.now):
            original = self.qualified()
            changed = dict(original, decision_time=(self.now + timedelta(seconds=1)).isoformat())
            self.assertFalse(mtf_runtime.entry_window_open(changed, self.now))
            self.assertFalse(mtf_runtime.eligible_result(changed, self.now))
            with patch.object(multi_timeframe, "analyze_multi_timeframe", return_value=changed):
                self.assertEqual(mtf_runtime.evaluate_feed(self.feed, self.now)["reason"], "entry_window_expired")

    def test_malformed_cached_reference_types_fail_closed_without_exceptions(self):
        with pinned_synthetic_evidence(self.feed, self.now):
            original = self.qualified()
            for key in ("bar_time", "confirmation_bar_time", "direction_bar_time", "decision_time", "quote_time"):
                for value in (None, True, {}, "invalid"):
                    with self.subTest(key=key, value=value):
                        self.assertFalse(mtf_runtime.eligible_result({**original, key: value}, self.now))

    def test_frozen_zone_and_targets_must_still_match_the_evaluated_price_policy(self):
        with pinned_synthetic_evidence(self.feed, self.now):
            original = self.qualified()
            collapsed = {**original, "entry_zone_low": original["entry"], "entry_zone_high": original["entry"]}
            self.assertFalse(mtf_runtime.eligible_result(collapsed, self.now))
            widened = {**original, "entry_zone_low": original["stop"] + .001}
            self.assertFalse(mtf_runtime.eligible_result(widened, self.now))
            changed = {**original, "target": original["target2"], "nominal_reward_risk": 3,
                       "target2": round(original["entry"] + 4 * original["original_stop_distance"], 3)}
            self.assertFalse(mtf_runtime.eligible_result(changed, self.now))

    def test_valid_payload_rereads_current_risk_and_quote_without_rewriting_protection(self):
        with pinned_synthetic_evidence(self.feed, self.now):
            payload = {**self.qualified(), "workflow": "manual_ticket"}
            original = deepcopy(payload)
            updated = deepcopy(self.feed)
            updated["quote"].update(bid=2000.011, ask=2000.013)
            self.assertTrue(mtf_runtime.eligible_payload(payload, updated, self.now))
            self.assertEqual(payload, original)
            self.assertNotEqual(multi_timeframe.analyze_multi_timeframe(updated, self.now)["entry"], payload["entry"])

    def test_current_exposure_margin_cost_and_freshness_fail_closed_for_frozen_offer(self):
        with pinned_synthetic_evidence(self.feed, self.now):
            payload = {**self.qualified(), "workflow": "manual_ticket"}
            for key, value in (("open_positions", 1), ("pending_orders", 1), ("free_margin", 1),
                               ("equity", 10), ("costs_verified", False), ("slippage_price", .004)):
                updated = deepcopy(self.feed)
                updated["risk_context"][key] = value
                self.assertFalse(mtf_runtime.eligible_payload(payload, updated, self.now), key)
            updated = deepcopy(self.feed)
            updated["quote"]["time"] = (self.now - timedelta(seconds=11)).isoformat()
            self.assertFalse(mtf_runtime.eligible_payload(payload, updated, self.now))

    def test_current_broker_or_execution_metadata_cannot_change_behind_frozen_offer(self):
        with pinned_synthetic_evidence(self.feed, self.now):
            payload = {**self.qualified(), "workflow": "manual_ticket"}
            updated = deepcopy(self.feed)
            updated["risk_context"]["broker_fingerprint"] = "b" * 64
            self.assertFalse(mtf_runtime.eligible_payload(payload, updated, self.now))
            updated = deepcopy(self.feed)
            updated["execution"]["stops_level"] = 6
            self.assertFalse(mtf_runtime.eligible_payload(payload, updated, self.now))

    def test_frozen_spread_plus_drift_budget_uses_original_risk_distance(self):
        with pinned_synthetic_evidence(self.feed, self.now):
            payload = {**self.qualified(), "workflow": "manual_ticket"}
            updated = deepcopy(self.feed)
            # Tight spread remains acceptable, but entry drifts over 0.1R.
            updated["quote"].update(bid=payload["entry"] + .2, ask=payload["entry"] + .202)
            self.assertEqual(multi_timeframe.analyze_multi_timeframe(updated, self.now)["state"], "signal")
            self.assertFalse(mtf_runtime.eligible_payload(payload, updated, self.now))

    def test_offer_for_old_m1_bar_is_not_transferred_to_next_closed_candle(self):
        with pinned_synthetic_evidence(self.feed, self.now):
            payload = {**self.qualified(), "workflow": "manual_ticket"}
            clock = self.now + timedelta(minutes=1)
            updated = candle_fixtures.MultiTimeframeTests().feed(now=clock)
            with patch.object(mtf_runtime, "_clock", return_value=clock):
                self.assertFalse(mtf_runtime.eligible_payload(payload, updated, clock))

    def test_nonmanual_real_or_wrong_volume_payload_never_passes(self):
        with pinned_synthetic_evidence(self.feed, self.now):
            payload = {**self.qualified(), "workflow": "manual_ticket"}
            for key, value in (("workflow", "automatic"), ("account_mode", "real"), ("volume", .02)):
                self.assertFalse(mtf_runtime.eligible_payload({**payload, key: value}, self.feed, self.now), key)


if __name__ == "__main__":
    unittest.main()
