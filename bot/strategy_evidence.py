"""Fail-closed qualification of a pinned, locally evaluated strategy artifact.

This module never interprets AI confidence or paper-journal observations as
trade evidence. A JSON/API dictionary is not a runtime qualification token.
The pin trusts an operator-reviewed evaluator/provenance workflow: file hashes
prove identity, not truthful prices, fees, complete tick coverage or first-barrier
sequence. Endpoint rows alone cannot establish those facts or independence.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
from types import MappingProxyType


STRATEGY_ID = "mtf-ema-pullback-60m-v1"
HORIZON_SECONDS = 3600
MIN_OOS_TRADES = 200
MIN_LOWER_BOUND = 0.70
WILSON_Z = 1.959963984540054
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_TRUSTED_FILE = object()
EVALUATION_POLICY = {
    "version": 1, "horizon_seconds": HORIZON_SECONDS,
    "minimum_nonoverlapping_oos_trades": MIN_OOS_TRADES,
    "minimum_lower_bound": MIN_LOWER_BOUND, "wilson_z": WILSON_Z,
    "confidence_interval": "two_sided_95_percent_wilson_lower_endpoint",
    "success": "primary_tp_before_sl_and_positive_net_cash",
    "timeouts": "nonwins", "intrabar_ambiguity": "never_certify",
    "overlap": "exclude_until_previous_entry_plus_3600_seconds",
    "split_purge_seconds": HORIZON_SECONDS,
    "entry": "first_tick_strictly_after_decision_within_10_seconds",
    "maximum_tick_gap_seconds": 30,
    "scenarios": "all_declared_verified_cost_scenarios_must_pass",
    "candles_only": "provisional_never_qualified",
    "maximum_evidence_age_days": 90,
    "timeout_exit": "first_tick_at_or_after_deadline_within_10_seconds",
    "execution_specs": "verified_constant_specs_cover_full_history_and_match_live",
    "runtime_financing": "verified_zero_applicability_for_frozen_session_and_horizon",
    "dependence": "verified_predeclared_full_oos_assessment_independent_count_equals_raw_count",
    "actual_entry": "recheck_frozen_protection_cash_risk_reward_margin_and_stop_distance",
}
REPORT_KEYS = frozenset({
    "version", "kind", "strategy", "evaluation_policy", "evaluator_fingerprint",
    "generated_at", "dataset", "splits", "outcomes", "blocked_reasons",
})
DATASET_KEYS = frozenset({
    "symbol", "broker_fingerprint", "source", "provenance_verified", "timestamp_basis",
    "price_basis", "coverage_start", "coverage_end", "files", "cost_scenarios", "risk_model", "execution_specs", "dependence_assessment",
})
COST_KEYS = frozenset({
    "name", "verified", "evidence_sha256", "effective_start", "effective_end",
    "commission_round_turn", "slippage_price", "profit_cash_per_price_unit",
    "loss_cash_per_price_unit", "financing_cash_per_hour",
    "financing_verified", "financing_evidence_sha256", "financing_coverage",
})
EXECUTION_KEYS = frozenset({"tick_size", "point", "digits", "stops_level"})
SPEC_KEYS = EXECUTION_KEYS | frozenset({"verified", "evidence_sha256", "effective_start", "effective_end"})
FINANCING_COVERAGE = "full_history_utc_06_19_3600s_horizon_10s_entry"
DEPENDENCE_KEYS = frozenset({
    "verified", "wilson_applicable", "method_sha256", "evidence_sha256", "method_declared_at",
    "oos_start", "oos_end", "outcomes_sha256", "effective_independent_trades",
})
OUTCOME_KEYS = frozenset({
    "segment", "scenario", "candidate_time", "entry_time", "deadline", "exit_time",
    "direction", "entry_bid", "entry_ask", "exit_bid", "exit_ask", "stop", "target",
    "status", "complete", "maximum_tick_gap_seconds",
})


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def source_hash(path):
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def evaluator_fingerprint():
    root = Path(__file__).resolve().parents[1]
    material = {"policy": EVALUATION_POLICY, "evidence_source": source_hash(__file__),
                "evaluator_source": source_hash(root / "tools" / "evaluate_multi_timeframe.py")}
    return hashlib.sha256(canonical_bytes(material)).hexdigest()


def utc(value):
    if type(value) is str and len(value) <= 40:
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("A UTC timestamp is required")
    return value.astimezone(timezone.utc)


def finite(value, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("A finite number is required")
    if value < 0 or (positive and value <= 0):
        raise ValueError("A nonnegative cost or positive price is required")
    return Decimal(str(value))


def hex_digest(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def valid_identity(identity):
    return (
        type(identity) is dict
        and set(identity) == {"strategy_id", "policy_id", "horizon_seconds", "fingerprint"}
        and identity["strategy_id"] == STRATEGY_ID
        and type(identity["policy_id"]) is str and 0 < len(identity["policy_id"]) <= 128
        and type(identity["horizon_seconds"]) is int and identity["horizon_seconds"] == HORIZON_SECONDS
        and hex_digest(identity["fingerprint"])
    )


def current_identity():
    from bot.multi_timeframe import strategy_identity
    identity = strategy_identity()
    if not valid_identity(identity):
        raise ValueError("Unsupported strategy identity")
    return identity


def wilson_lower(wins, total):
    if type(wins) is not int or type(total) is not int or total <= 0 or not 0 <= wins <= total:
        raise ValueError("Valid integer wins and trade count are required")
    if wins == 0:
        return 0.0
    p = wins / total
    z2 = WILSON_Z ** 2
    lower = (p + z2 / (2 * total) - WILSON_Z * math.sqrt(
        p * (1 - p) / total + z2 / (4 * total ** 2))) / (1 + z2 / total)
    return min(1.0, max(0.0, lower))


@dataclass(frozen=True)
class EvidenceAssessment:
    qualified: bool
    reasons: tuple[str, ...]
    scenario_metrics: tuple[dict, ...] = ()


@dataclass(frozen=True)
class EvidenceDecision:
    qualified: bool
    reasons: tuple[str, ...]
    scenario_metrics: tuple[dict, ...] = ()
    strategy_fingerprint: str = ""
    artifact_sha256: str = ""
    generated_at: datetime | None = None
    cost_scenarios: tuple[dict, ...] = ()
    broker_fingerprint: str = ""
    coverage_end: datetime | None = None
    execution_context: tuple[tuple[str, int | float], ...] = ()
    evaluator_fingerprint: str = ""
    _authority: object = field(default=None, repr=False, compare=False)


def _splits(value, coverage_start, coverage_end):
    if type(value) is not dict or set(value) != {"development", "validation", "oos"}:
        raise ValueError("Three chronological splits are required")
    previous_end = coverage_start
    ranges = {}
    for name in ("development", "validation", "oos"):
        segment = value[name]
        if type(segment) is not dict or set(segment) != {"start", "end"}:
            raise ValueError("Exact split boundaries are required")
        start, end = utc(segment["start"]), utc(segment["end"])
        if not previous_end <= start < end <= coverage_end:
            raise ValueError("Chronological nonoverlapping splits are required")
        ranges[name] = (start, end)
        previous_end = end
    return ranges


def _costs(value, start, end):
    if type(value) is not list or not 1 <= len(value) <= 10:
        raise ValueError("Verified cost scenarios are required")
    scenarios = {}
    for cost in value:
        if type(cost) is not dict or set(cost) != COST_KEYS:
            raise ValueError("Exact cost scenario metadata is required")
        name = cost["name"]
        if type(name) is not str or re.fullmatch(r"[a-z0-9_-]{1,32}", name) is None or name in scenarios:
            raise ValueError("Distinct bounded cost scenario names are required")
        if cost["verified"] is not True or not hex_digest(cost["evidence_sha256"]):
            raise ValueError("Unverified commissions or execution costs")
        if not utc(cost["effective_start"]) <= start < end <= utc(cost["effective_end"]):
            raise ValueError("Costs do not cover the historical period")
        for key in ("commission_round_turn", "slippage_price", "financing_cash_per_hour"):
            finite(cost[key])
        for key in ("profit_cash_per_price_unit", "loss_cash_per_price_unit"):
            finite(cost[key], positive=True)
        # Live risk_context does not yet bind a financing allowance. Only a
        # separately documented, verified zero applicability can qualify.
        if (finite(cost["financing_cash_per_hour"]) != 0 or cost["financing_verified"] is not True
            or not hex_digest(cost["financing_evidence_sha256"])
            or cost["financing_coverage"] != FINANCING_COVERAGE):
            raise ValueError("Runtime financing is nonzero or its full-session zero applicability is unverified")
        scenarios[name] = cost
    return scenarios


def _execution_context(value):
    if type(value) is not dict or set(value) != EXECUTION_KEYS:
        raise ValueError("Exact execution metadata is required")
    digits, stops = value["digits"], value["stops_level"]
    if type(digits) is not int or not 0 <= digits <= 8 or type(stops) is not int or not 0 <= stops <= 1_000_000:
        raise ValueError("Invalid execution precision or stop distance")
    tick, point = finite(value["tick_size"], positive=True), finite(value["point"], positive=True)
    quantum = Decimal(1).scaleb(-digits)
    if point != quantum or tick < quantum or tick % quantum:
        raise ValueError("Invalid execution price grid")
    return {"tick_size": float(tick), "point": float(point), "digits": digits, "stops_level": stops}


def _execution_specs(value, start, end):
    if type(value) is not dict or set(value) != SPEC_KEYS or value["verified"] is not True:
        raise ValueError("Historical execution specifications are unverified")
    if not hex_digest(value["evidence_sha256"]) or not utc(value["effective_start"]) <= start < end <= utc(value["effective_end"]):
        raise ValueError("Historical execution specifications do not cover the dataset")
    return _execution_context({key: value[key] for key in EXECUTION_KEYS})


def executable_entry_allowed(direction, entry_bid, entry_ask, stop, target, cost, risk_model, execution):
    """Recheck the frozen levels at the actual side, without moving SL/TP."""
    try:
        if direction not in ("BUY", "SELL"):
            return False
        bid, ask, stop, target = (finite(value, positive=True) for value in (entry_bid, entry_ask, stop, target))
        if bid >= ask:
            return False
        buy = direction == "BUY"
        price = ask if buy else bid
        if not (stop < price < target if buy else target < price < stop):
            return False
        specs = _execution_context(execution)
        minimum = Decimal(str(specs["point"])) * specs["stops_level"]
        barrier_side = bid if buy else ask
        if (buy and (barrier_side - stop < minimum or target - barrier_side < minimum)
            or not buy and (stop - barrier_side < minimum or barrier_side - target < minimum)):
            return False
        slip, commission = finite(cost["slippage_price"]), finite(cost["commission_round_turn"])
        loss = (abs(price - stop) + 2 * slip) * finite(cost["loss_cash_per_price_unit"], positive=True) + commission
        gain = (abs(target - price) - 2 * slip) * finite(cost["profit_cash_per_price_unit"], positive=True) - commission
        return (loss > 0 and loss <= finite(risk_model["equity"], positive=True) * Decimal("0.01")
                and gain / loss >= Decimal("1.5")
                and finite(risk_model["free_margin"], positive=True) >= 2 * finite(risk_model["margin_required"], positive=True))
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return False


def _dependence_assessment(value, ranges, outcomes, costs):
    if (type(value) is not dict or set(value) != DEPENDENCE_KEYS or value["verified"] is not True
        or value["wilson_applicable"] is not True):
        raise ValueError("Documented OOS independence and Wilson applicability are unverified")
    first, last = ranges["oos"]
    if not (utc(value["oos_start"]) == first and utc(value["oos_end"]) == last
            and utc(value["method_declared_at"]) < first):
        raise ValueError("Dependence assessment is not predeclared or does not cover the full OOS split")
    if any(not hex_digest(value[key]) for key in ("method_sha256", "evidence_sha256", "outcomes_sha256")):
        raise ValueError("Hashed dependence method and evidence are required")
    cohort = [row for row in outcomes if row["segment"] == "oos"]
    if hashlib.sha256(canonical_bytes(cohort)).hexdigest() != value["outcomes_sha256"]:
        raise ValueError("Dependence assessment does not bind the actual OOS outcomes")
    counts = value["effective_independent_trades"]
    if type(counts) is not dict or set(counts) != set(costs) or any(type(n) is not int or n < 0 for n in counts.values()):
        raise ValueError("Independent sample counts must cover every cost scenario")
    return counts


def net_cash(outcome, cost):
    buy = outcome["direction"] == "BUY"
    slip = finite(cost["slippage_price"])
    entry = finite(outcome["entry_ask"] if buy else outcome["entry_bid"], positive=True) + (slip if buy else -slip)
    exit_price = finite(outcome["exit_bid"] if buy else outcome["exit_ask"], positive=True) + (-slip if buy else slip)
    move = (exit_price - entry) * (1 if buy else -1)
    rate = finite(cost["profit_cash_per_price_unit"] if move >= 0 else cost["loss_cash_per_price_unit"], positive=True)
    hours = Decimal(str((utc(outcome["exit_time"]) - utc(outcome["entry_time"])).total_seconds())) / Decimal(3600)
    return float(move * rate - finite(cost["commission_round_turn"]) - hours * finite(cost["financing_cash_per_hour"]))


def validate_evidence_report(report, *, identity=None):
    """Recompute an assessment; this does not mint a trusted runtime token."""
    reasons = []
    metrics = []
    try:
        identity = current_identity() if identity is None else identity
        if not valid_identity(identity) or type(report) is not dict or set(report) != REPORT_KEYS:
            raise ValueError("Invalid evidence schema or strategy identity")
        if report["version"] != 1 or type(report["version"]) is not int or report["kind"] != "strategy_evidence":
            raise ValueError("Unsupported evidence version")
        if report["strategy"] != identity or report["evaluation_policy"] != EVALUATION_POLICY:
            raise ValueError("Strategy implementation or evaluation policy changed")
        if report["evaluator_fingerprint"] != evaluator_fingerprint():
            raise ValueError("Evaluator implementation changed")
        generated = utc(report["generated_at"])
        blocked = report["blocked_reasons"]
        if type(blocked) is not list or any(type(reason) is not str or not 0 < len(reason) <= 256 for reason in blocked):
            raise ValueError("Invalid blocked reasons")
        reasons.extend(blocked)
        dataset = report["dataset"]
        if type(dataset) is not dict or set(dataset) != DATASET_KEYS:
            raise ValueError("Exact dataset provenance is required")
        if (dataset["symbol"] != "XAUUSD" or dataset["provenance_verified"] is not True
            or not hex_digest(dataset["broker_fingerprint"]) or dataset["source"] != "MT5 broker history"
            or dataset["timestamp_basis"] != "UTC" or dataset["price_basis"] != "bid_ask_ticks"):
            raise ValueError("Same-broker UTC Bid/Ask tick provenance is unverified")
        start, end = utc(dataset["coverage_start"]), utc(dataset["coverage_end"])
        if not start < end <= generated:
            raise ValueError("Invalid historical coverage")
        execution = _execution_specs(dataset["execution_specs"], start, end)
        ranges = _splits(report["splits"], start, end)
        files = dataset["files"]
        if type(files) is not list or not 4 <= len(files) <= 20:
            raise ValueError("M1/M5/M15 and tick datasets are required")
        kinds = set()
        names = set()
        for item in files:
            if type(item) is not dict or set(item) != {"name", "kind", "timeframe", "sha256"}:
                raise ValueError("Exact dataset file fingerprints are required")
            if (type(item["name"]) is not str or re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", item["name"]) is None
                or item["name"] in names or not hex_digest(item["sha256"])):
                raise ValueError("Invalid dataset file fingerprint")
            names.add(item["name"])
            kinds.add((item["kind"], item["timeframe"]))
        if not {("candles", "M1"), ("candles", "M5"), ("candles", "M15"), ("ticks", None)} <= kinds:
            raise ValueError("Missing multi-timeframe or tick dataset")
        risk = dataset["risk_model"]
        if type(risk) is not dict or set(risk) != {"mode", "equity", "free_margin", "margin_required"} or risk["mode"] != "fixed_demo_0.01_simulation":
            raise ValueError("An explicit fixed Demo risk model is required")
        for key in ("equity", "free_margin", "margin_required"):
            finite(risk[key], positive=True)
        costs = _costs(dataset["cost_scenarios"], start, end)
        outcomes = report["outcomes"]
        if type(outcomes) is not list or len(outcomes) > 100000:
            raise ValueError("Complete bounded outcomes are required")
        grouped = {name: [] for name in costs}
        all_rows = {}
        for row in outcomes:
            if type(row) is not dict or set(row) != OUTCOME_KEYS or row["scenario"] not in costs or row["segment"] not in ranges:
                raise ValueError("Invalid complete outcome schema")
            if row["direction"] not in ("BUY", "SELL") or row["status"] not in ("target", "stop", "timeout", "ambiguous", "gap"):
                raise ValueError("Invalid trade outcome")
            decision, entry, deadline, exit_time = (utc(row[key]) for key in ("candidate_time", "entry_time", "deadline", "exit_time"))
            first, last = ranges[row["segment"]]
            if not (first <= decision < entry <= decision + timedelta(seconds=10)
                    and deadline == entry + timedelta(seconds=HORIZON_SECONDS)
                    and entry < exit_time <= deadline + timedelta(seconds=10) < last):
                raise ValueError("Outcome crosses a split, horizon or causal entry boundary")
            prices = [finite(row[key], positive=True) for key in ("entry_bid", "entry_ask", "exit_bid", "exit_ask", "stop", "target")]
            if prices[0] >= prices[1] or prices[2] >= prices[3]:
                raise ValueError("Zero or invalid historical spread is unverified")
            buy = row["direction"] == "BUY"
            raw_entry, raw_exit = prices[1 if buy else 0], prices[2 if buy else 3]
            stop, target = prices[4:]
            if not (stop < raw_entry < target if buy else target < raw_entry < stop):
                raise ValueError("Protective levels do not surround executable entry")
            if not executable_entry_allowed(row["direction"], row["entry_bid"], row["entry_ask"], row["stop"], row["target"], costs[row["scenario"]], risk, execution):
                raise ValueError("Actual entry exceeds the cash risk, reward, margin or stop-distance guard")
            if row["status"] in ("target", "stop") and exit_time > deadline:
                raise ValueError("Barrier outcome occurred after the holding horizon")
            if row["status"] == "target" and not (raw_exit >= target if buy else raw_exit <= target):
                raise ValueError("Claimed target was not reached")
            if row["status"] == "stop" and not (raw_exit <= stop if buy else raw_exit >= stop):
                raise ValueError("Claimed stop was not reached")
            if row["status"] == "timeout" and not deadline <= exit_time <= deadline + timedelta(seconds=10):
                raise ValueError("Invalid timeout outcome")
            gap = finite(row["maximum_tick_gap_seconds"])
            if row["complete"] is not True or gap > EVALUATION_POLICY["maximum_tick_gap_seconds"] or row["status"] in ("ambiguous", "gap"):
                reasons.append("Incomplete or ambiguous outcomes prevent qualification")
            key = (row["scenario"], row["segment"], entry)
            if key in all_rows:
                raise ValueError("Duplicate outcomes")
            all_rows[key] = row
            if row["segment"] == "oos":
                grouped[row["scenario"]].append(row)
        independent_counts = _dependence_assessment(dataset["dependence_assessment"], ranges, outcomes, costs)
        for name, rows in grouped.items():
            rows.sort(key=lambda row: utc(row["entry_time"]))
            prior_deadline = None
            for row in rows:
                if prior_deadline is not None and utc(row["entry_time"]) < prior_deadline:
                    raise ValueError("Overlapping OOS holding windows")
                prior_deadline = utc(row["deadline"])
            wins = sum(row["status"] == "target" and row["complete"] is True and net_cash(row, costs[name]) > 0 for row in rows)
            count = len(rows)
            if independent_counts[name] < MIN_OOS_TRADES or independent_counts[name] != count:
                reasons.append(f"{name}: independent OOS sample is below 200 or raw-count Wilson is not applicable")
            lower = wilson_lower(wins, count) if count else 0.0
            metrics.append({"scenario": name, "trades": count, "wins": wins,
                            "win_rate": wins / count if count else 0.0, "lower_95": lower,
                            "net_cash": sum(net_cash(row, costs[name]) for row in rows)})
            if count < MIN_OOS_TRADES:
                reasons.append(f"{name}: fewer than {MIN_OOS_TRADES} nonoverlapping OOS trades")
            if lower < MIN_LOWER_BOUND:
                reasons.append(f"{name}: lower 95% confidence bound is below 70%")
        # Empty development/validation cannot masquerade as an untouched study.
        if any(not any(row["segment"] == segment for row in outcomes) for segment in ("development", "validation")):
            reasons.append("Development or validation outcomes are absent")
    except (ValueError, TypeError, KeyError, OverflowError, InvalidOperation, OSError, ImportError) as exc:
        reasons.append(str(exc)[:256] or "Invalid evidence")
    return EvidenceAssessment(not reasons, tuple(dict.fromkeys(reasons)), tuple(metrics))


def load_strategy_evidence(path, *, expected_sha256=None, identity=None, now=None):
    """Load only an operator-pinned local artifact; never accept an API report."""
    try:
        if not hex_digest(expected_sha256):
            raise ValueError("Evidence artifact is not operator-pinned")
        file = Path(path)
        if not file.is_file() or file.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("Evidence artifact is unavailable or oversized")
        raw = file.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected_sha256:
            raise ValueError("Evidence artifact digest mismatch")
        report = json.loads(raw)
        clock = datetime.now(timezone.utc) if now is None else utc(now)
        generated = utc(report["generated_at"])
        if generated > clock:
            raise ValueError("Evidence artifact is from the future")
        identity = current_identity() if identity is None else identity
        assessment = validate_evidence_report(report, identity=identity)
        coverage_end = utc(report["dataset"]["coverage_end"])
        if clock - coverage_end > timedelta(days=90):
            raise ValueError("Evidence coverage is more than 90 days old")
        return EvidenceDecision(qualified=assessment.qualified, reasons=assessment.reasons,
                                scenario_metrics=assessment.scenario_metrics,
                                strategy_fingerprint=identity["fingerprint"], artifact_sha256=digest,
                                generated_at=generated,
                                cost_scenarios=tuple(MappingProxyType(dict(cost)) for cost in report["dataset"]["cost_scenarios"]),
                                broker_fingerprint=report["dataset"]["broker_fingerprint"],
                                coverage_end=coverage_end,
                                execution_context=tuple(sorted(_execution_context({key: report["dataset"]["execution_specs"][key] for key in EXECUTION_KEYS}).items())) if assessment.qualified else (),
                                evaluator_fingerprint=report["evaluator_fingerprint"], _authority=_TRUSTED_FILE)
    except (ValueError, TypeError, KeyError, OSError, OverflowError, ImportError) as exc:
        return EvidenceDecision(False, (str(exc)[:256] or "Evidence unavailable",))


def evidence_allows_alerts(decision, *, identity=None, now=None):
    if not isinstance(decision, EvidenceDecision) or decision._authority is not _TRUSTED_FILE or decision.qualified is not True:
        return False
    try:
        identity = current_identity() if identity is None else identity
        clock = datetime.now(timezone.utc) if now is None else utc(now)
        return (valid_identity(identity) and decision.strategy_fingerprint == identity["fingerprint"]
                and decision.generated_at is not None and decision.generated_at <= clock
                and decision.coverage_end is not None and timedelta(0) <= clock - decision.coverage_end <= timedelta(days=90)
                and decision.evaluator_fingerprint == evaluator_fingerprint()
                and hex_digest(decision.artifact_sha256))
    except (ValueError, TypeError, ImportError):
        return False


def load_verified_evidence(path, pinned_sha256, now=None):
    return load_strategy_evidence(path, expected_sha256=pinned_sha256, now=now)


def qualification_gate(candidate, evidence, now=None):
    """Bind a current candidate to the exact trusted study and verified fees."""
    if not evidence_allows_alerts(evidence, now=now) or type(candidate) is not dict:
        return False
    try:
        identity = current_identity()
        if (candidate.get("provisional") is not False or candidate.get("state") != "signal"
            or candidate.get("strategy_id") != identity["strategy_id"]
            or candidate.get("policy_id") != identity["policy_id"]
            or candidate.get("horizon_seconds") != HORIZON_SECONDS
            or candidate.get("strategy_fingerprint") != identity["fingerprint"]):
            return False
        context = candidate["cost_context"]
        if (type(context) is not dict or candidate.get("broker_fingerprint") != evidence.broker_fingerprint
            or tuple(sorted(_execution_context(candidate["execution"]).items())) != evidence.execution_context):
            return False
        keys = ("commission_round_turn", "slippage_price", "profit_cash_per_price_unit", "loss_cash_per_price_unit")
        if set(context) != set(keys):
            return False
        return any(all(finite(context[key]) == finite(cost[key]) for key in keys)
                   for cost in evidence.cost_scenarios)
    except (ValueError, TypeError, KeyError, InvalidOperation, ImportError):
        return False


def arabic_summary(assessment):
    lines = ["نتيجة التقييم: " + ("اجتاز شروط الأدلة التاريخية" if assessment.qualified else "الإشعارات محجوبة: الأدلة غير كافية")]
    for metric in assessment.scenario_metrics:
        lines.append(f"{metric['scenario']}: {metric['wins']}/{metric['trades']}، النجاح {metric['win_rate']:.1%}، الحد الأدنى بثقة 95%: {metric['lower_95']:.1%}.")
    if assessment.reasons:
        lines.append("الأسباب: " + "؛ ".join(assessment.reasons))
    lines.append("فاصل Wilson ذو طرفين بنسبة95% يستخدم نموذج نتائج ثنائي؛ عدم تداخل النوافذ لا يثبت استقلال الصفقات إحصائياً، وقد تؤثر أنظمة السوق والارتباط الزمني في تغطيته.")
    lines.append("هذه نتائج محاكاة خارج العينة بعد التكاليف، وليست ضماناً لصفقة مقبلة أو ثقة ذكاء اصطناعي.")
    return "\n".join(lines)
