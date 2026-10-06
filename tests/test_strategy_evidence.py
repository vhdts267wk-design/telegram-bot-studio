"""Synthetic evidence fixtures test qualification, never claim performance."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bot import strategy_evidence as evidence


IDENTITY = {"strategy_id": evidence.STRATEGY_ID, "policy_id": "synthetic-test-policy",
            "horizon_seconds": 3600, "fingerprint": "a" * 64}
NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)


def outcome(segment, entry, *, win=True, scenario="base", timeout=False):
    deadline = entry + timedelta(hours=1)
    return {"segment": segment, "scenario": scenario,
            "candidate_time": (entry - timedelta(seconds=1)).isoformat(),
            "entry_time": entry.isoformat(), "deadline": deadline.isoformat(),
            "exit_time": (deadline if timeout else entry + timedelta(minutes=20)).isoformat(),
            "direction": "BUY", "entry_bid": 100, "entry_ask": 100.1,
            "exit_bid": 100 if timeout else 103 if win else 98.9,
            "exit_ask": 100.1 if timeout else 103.1 if win else 99,
            "stop": 99, "target": 102.5,
            "status": "timeout" if timeout else "target" if win else "stop",
            "complete": True, "maximum_tick_gap_seconds": 1}


def seal_dependence(value):
    """Synthetic attestation for validator tests; never empirical evidence."""
    cohort = [row for row in value["outcomes"] if row["segment"] == "oos"]
    value["dataset"]["dependence_assessment"] = {
        "verified": True, "wilson_applicable": True,
        "method_sha256": "b" * 64, "evidence_sha256": "b" * 64,
        "method_declared_at": "2026-07-01T00:00:00Z",
        "oos_start": value["splits"]["oos"]["start"], "oos_end": value["splits"]["oos"]["end"],
        "outcomes_sha256": hashlib.sha256(evidence.canonical_bytes(cohort)).hexdigest(),
        "effective_independent_trades": {cost["name"]: sum(row["scenario"] == cost["name"] for row in cohort)
                                         for cost in value["dataset"]["cost_scenarios"]},
    }
    return value


def report(*, wins=180, total=200):
    cost = {"name": "base", "verified": True, "evidence_sha256": "b" * 64,
            "effective_start": "2026-07-01T00:00:00Z", "effective_end": "2026-10-01T00:00:00Z",
            "commission_round_turn": 0.07, "slippage_price": 0.01,
            "profit_cash_per_price_unit": 1, "loss_cash_per_price_unit": 1,
            "financing_cash_per_hour": 0,
            "financing_verified": True, "financing_evidence_sha256": "b" * 64,
            "financing_coverage": evidence.FINANCING_COVERAGE}
    data = {"symbol": "XAUUSD", "broker_fingerprint": "c" * 64,
            "source": "MT5 broker history", "provenance_verified": True,
            "timestamp_basis": "UTC", "price_basis": "bid_ask_ticks",
            "coverage_start": "2026-07-01T00:00:00Z", "coverage_end": "2026-10-01T00:00:00Z",
            "files": [{"name": name + ".jsonl", "kind": kind, "timeframe": timeframe,
                       "sha256": "d" * 64} for name, kind, timeframe in
                      (("M1", "candles", "M1"), ("M5", "candles", "M5"),
                       ("M15", "candles", "M15"), ("ticks", "ticks", None))],
            "cost_scenarios": [cost],
            "execution_specs": {"tick_size": .01, "point": .01, "digits": 2, "stops_level": 0,
                                "verified": True, "evidence_sha256": "b" * 64,
                                "effective_start": "2026-07-01T00:00:00Z", "effective_end": "2026-10-01T00:00:00Z"},
            "risk_model": {"mode": "fixed_demo_0.01_simulation",
                                                      "equity": 100000, "free_margin": 100000, "margin_required": 1000}}
    segments = {"development": {"start": "2026-07-01T00:00:00Z", "end": "2026-08-01T00:00:00Z"},
                "validation": {"start": "2026-08-01T01:00:00Z", "end": "2026-09-01T00:00:00Z"},
                "oos": {"start": "2026-09-01T01:00:00Z", "end": "2026-10-01T00:00:00Z"}}
    rows = [outcome("development", datetime(2026, 7, 2, 6, tzinfo=timezone.utc)),
            outcome("validation", datetime(2026, 8, 2, 6, tzinfo=timezone.utc))]
    first = datetime(2026, 9, 2, 6, tzinfo=timezone.utc)
    rows += [outcome("oos", first + timedelta(minutes=61 * index), win=index < wins)
             for index in range(total)]
    value = {"version": 1, "kind": "strategy_evidence", "strategy": dict(IDENTITY),
            "evaluation_policy": deepcopy(evidence.EVALUATION_POLICY),
            "evaluator_fingerprint": evidence.evaluator_fingerprint(), "generated_at": NOW.isoformat(),
            "dataset": data, "splits": segments, "outcomes": rows, "blocked_reasons": []}
    return seal_dependence(value)


class StrategyEvidenceTests(unittest.TestCase):
    def assess(self, value):
        return evidence.validate_evidence_report(value, identity=IDENTITY)

    def test_wilson_count_and_threshold_have_uncertainty(self):
        self.assertLess(evidence.wilson_lower(140, 200), .7)
        self.assertGreater(evidence.wilson_lower(180, 200), .7)
        self.assertFalse(self.assess(report(wins=139)).qualified)
        self.assertFalse(self.assess(report(total=199, wins=199)).qualified)
        result = self.assess(report())
        self.assertTrue(result.qualified, result.reasons)
        self.assertEqual((result.scenario_metrics[0]["trades"], result.scenario_metrics[0]["wins"]), (200, 180))
        for args in ((True, 200), (201, 200), (0, 0), (-1, 10)):
            with self.assertRaises(ValueError): evidence.wilson_lower(*args)
        for n in (1, 2, 21, 200, 100000):
            self.assertEqual(evidence.wilson_lower(0, n), 0)
            self.assertTrue(0 <= evidence.wilson_lower(n, n) <= 1)

    def test_raw_confidence_forged_token_and_unknown_fields_never_qualify(self):
        self.assertFalse(evidence.evidence_allows_alerts({"confidence": .99, "qualified": True}, identity=IDENTITY))
        self.assertFalse(evidence.evidence_allows_alerts(evidence.EvidenceDecision(True, ()), identity=IDENTITY))
        value = report()
        value["confidence"] = .99
        self.assertFalse(self.assess(value).qualified)

    def test_fingerprints_and_frozen_policy_are_required(self):
        changes = (("strategy", {**IDENTITY, "fingerprint": "e" * 64}),
                   ("evaluator_fingerprint", "e" * 64),
                   ("evaluation_policy", {**evidence.EVALUATION_POLICY, "minimum_nonoverlapping_oos_trades": 1}))
        for key, changed in changes:
            value = report(); value[key] = changed
            self.assertFalse(self.assess(value).qualified)

    def test_dataset_and_cost_provenance_cannot_be_assumed(self):
        for key, changed in (("provenance_verified", False), ("price_basis", "candles_only"),
                             ("timestamp_basis", "broker_local"), ("symbol", "EURUSD"),
                             ("broker_fingerprint", "unknown")):
            value = report(); value["dataset"][key] = changed
            self.assertFalse(self.assess(value).qualified)
        for key, changed in (("verified", False), ("evidence_sha256", "unknown"),
                             ("commission_round_turn", float("nan")), ("profit_cash_per_price_unit", 0),
                             ("effective_start", "2026-09-01T00:00:00Z")):
            value = report(); value["dataset"]["cost_scenarios"][0][key] = changed
            self.assertFalse(self.assess(value).qualified)

    def test_financing_and_historical_execution_specs_must_be_verified(self):
        for key, changed in (("financing_cash_per_hour", .1), ("financing_verified", False),
                             ("financing_evidence_sha256", "unknown"), ("financing_coverage", "assumed_no_overnight")):
            value = report(); value["dataset"]["cost_scenarios"][0][key] = changed
            self.assertFalse(self.assess(value).qualified)
        for key, changed in (("verified", False), ("evidence_sha256", "unknown"),
                             ("effective_start", "2026-09-01T00:00:00Z"),
                             ("tick_size", 0), ("point", .1), ("digits", True), ("stops_level", -1)):
            value = report(); value["dataset"]["execution_specs"][key] = changed
            self.assertFalse(self.assess(value).qualified)

    def test_independence_requires_predeclared_documented_full_oos_scope(self):
        for key, changed in (("verified", False), ("wilson_applicable", False),
                             ("method_sha256", "unknown"), ("evidence_sha256", "unknown"),
                             ("method_declared_at", "2026-10-01T00:00:00Z"),
                             ("oos_start", "2026-09-02T00:00:00Z"), ("outcomes_sha256", "e" * 64),
                             ("effective_independent_trades", {"base": 199}),
                             ("effective_independent_trades", {"base": 201})):
            value = report(); value["dataset"]["dependence_assessment"][key] = changed
            self.assertFalse(self.assess(value).qualified, key)

    def test_overlap_duplicate_incomplete_and_zero_spread_block(self):
        for mode in ("overlap", "duplicate", "incomplete", "zero_spread", "ambiguous"):
            value = report()
            if mode == "overlap": value["outcomes"][3] = outcome("oos", evidence.utc(value["outcomes"][2]["entry_time"]) + timedelta(minutes=30))
            elif mode == "duplicate": value["outcomes"].append(deepcopy(value["outcomes"][-1]))
            elif mode == "incomplete": value["outcomes"][2]["complete"] = False
            elif mode == "zero_spread": value["outcomes"][2]["entry_ask"] = value["outcomes"][2]["entry_bid"]
            else: value["outcomes"][2]["status"] = "ambiguous"
            self.assertFalse(self.assess(seal_dependence(value)).qualified, mode)

    def test_causal_entry_horizon_purge_and_first_barrier_validation(self):
        for mode in ("entry", "horizon", "split", "target", "deadline_touch"):
            value = report(); row = value["outcomes"][2]
            if mode == "entry": row["candidate_time"] = row["entry_time"]
            elif mode == "horizon": row["deadline"] = (evidence.utc(row["entry_time"]) + timedelta(minutes=30)).isoformat()
            elif mode == "split": value["splits"]["oos"]["end"] = row["deadline"]
            elif mode == "target": row["exit_bid"] = 100; row["exit_ask"] = 100.1
            else: row["exit_time"] = (evidence.utc(row["deadline"]) + timedelta(seconds=1)).isoformat()
            self.assertFalse(self.assess(seal_dependence(value)).qualified)

    def test_timeouts_are_nonwins_and_all_cost_scenarios_must_pass(self):
        value = report(wins=200)
        for index in range(2, 202):
            value["outcomes"][index] = outcome("oos", evidence.utc(value["outcomes"][index]["entry_time"]), timeout=True)
        result = self.assess(seal_dependence(value))
        self.assertFalse(result.qualified)
        self.assertEqual(result.scenario_metrics[0]["wins"], 0)
        value = report()
        cost = {**value["dataset"]["cost_scenarios"][0], "name": "stress", "commission_round_turn": 1000}
        value["dataset"]["cost_scenarios"].append(cost)
        value["outcomes"] += [{**row, "scenario": "stress"} for row in list(value["outcomes"])]
        self.assertFalse(self.assess(seal_dependence(value)).qualified)

    def test_pinned_file_gate_binds_costs_broker_age_and_strategy(self):
        value = report()
        with tempfile.TemporaryDirectory() as temporary, patch.object(evidence, "current_identity", return_value=IDENTITY):
            path = Path(temporary) / "synthetic-evidence.json"
            raw = json.dumps(value, allow_nan=False).encode(); path.write_bytes(raw)
            pin = hashlib.sha256(raw).hexdigest()
            self.assertFalse(evidence.load_strategy_evidence(path, identity=IDENTITY, now=NOW).qualified)
            self.assertFalse(evidence.load_strategy_evidence(path, expected_sha256="f" * 64, identity=IDENTITY, now=NOW).qualified)
            decision = evidence.load_strategy_evidence(path, expected_sha256=pin, identity=IDENTITY, now=NOW)
            self.assertTrue(evidence.evidence_allows_alerts(decision, identity=IDENTITY, now=NOW))
            candidate = {"state": "signal", **IDENTITY, "strategy_fingerprint": IDENTITY["fingerprint"],
                         "broker_fingerprint": "c" * 64, "provisional": False,
                         "execution": {"tick_size": .01, "point": .01, "digits": 2, "stops_level": 0},
                         "cost_context": {key: value["dataset"]["cost_scenarios"][0][key] for key in
                                          ("commission_round_turn", "slippage_price", "profit_cash_per_price_unit", "loss_cash_per_price_unit")}}
            self.assertTrue(evidence.qualification_gate(candidate, decision, now=NOW))
            for key, changed in (("provisional", True), ("provisional", None), ("broker_fingerprint", "e" * 64), ("strategy_fingerprint", "e" * 64), ("horizon_seconds", 60),
                                 ("execution", {"tick_size": .1, "point": .01, "digits": 2, "stops_level": 0}),
                                 ("execution", {"tick_size": .01, "point": .01, "digits": 2, "stops_level": 1})):
                self.assertFalse(evidence.qualification_gate({**candidate, key: changed}, decision, now=NOW))
            with self.assertRaises(TypeError):
                decision.cost_scenarios[0]["slippage_price"] = .2
            altered = deepcopy(candidate); altered["cost_context"]["slippage_price"] = .2
            self.assertFalse(evidence.qualification_gate(altered, decision, now=NOW))
            self.assertFalse(evidence.evidence_allows_alerts(decision, identity=IDENTITY, now=NOW + timedelta(days=91)))
            self.assertFalse(evidence.load_strategy_evidence(path, expected_sha256=pin, identity=IDENTITY, now=NOW - timedelta(days=1)).qualified)


if __name__ == "__main__": unittest.main()
