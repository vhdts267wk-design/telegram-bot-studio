"""Offline causal evaluator. No SDK, network, account access or order calls.

Input is an operator-supplied JSON manifest and UTC JSONL candle/tick files.
Missing verified data produces a blocked artifact. --exploratory-cost accepts
an explicitly declared simulated cost scenario, including spread_price, for
M1 OHLC bounds; those results are always provisional and cannot qualify.
"""

import argparse
from array import array
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import strategy_evidence as evidence


TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900}


def stamp(value):
    return int(evidence.utc(value).timestamp() * 1_000_000)


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def rows(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                raise ValueError("Blank historical row")
            try:
                value = json.loads(line)
            except ValueError:
                raise ValueError(f"Invalid JSONL historical row {number}") from None
            if type(value) is not dict:
                raise ValueError("Historical rows must be objects")
            yield value


class TickSeries:
    """Compact, chronological actual timestamps and Bid/Ask values."""

    def __init__(self):
        self.times, self.bids, self.asks = array("q"), array("d"), array("d")

    def append(self, row):
        if set(row) != {"time", "bid", "ask"}:
            raise ValueError("Exact UTC Bid/Ask tick columns are required")
        time = stamp(row["time"])
        bid, ask = float(evidence.finite(row["bid"], positive=True)), float(evidence.finite(row["ask"], positive=True))
        if ask <= bid:
            raise ValueError("Historical spread is zero, unknown or negative")
        if self.times and time <= self.times[-1]:
            if time == self.times[-1] and (bid, ask) == (self.bids[-1], self.asks[-1]):
                return
            raise ValueError("Tick order is ambiguous or timestamps regress")
        self.times.append(time)
        self.bids.append(bid)
        self.asks.append(ask)

    def __len__(self):
        return len(self.times)

    def tick(self, index):
        return {"time": iso(datetime.fromtimestamp(self.times[index] / 1_000_000, timezone.utc)),
                "bid": self.bids[index], "ask": self.asks[index]}


def load_data(manifest, base, *, include_ticks=True):
    candles = {tf: [] for tf in TF_SECONDS}
    ticks = TickSeries()
    fingerprints = []
    for item in manifest.get("files", []):
        if type(item) is not dict or not {"name", "kind", "timeframe", "path", "sha256"} <= set(item):
            raise ValueError("Dataset file path and fingerprint are required")
        path = Path(item["path"])
        if not path.is_absolute():
            path = base / path
        if not evidence.hex_digest(item["sha256"]) or file_hash(path) != item["sha256"]:
            raise ValueError("Historical file fingerprint mismatch")
        fingerprints.append({key: item[key] for key in ("name", "kind", "timeframe", "sha256")})
        if item["kind"] == "ticks" and item["timeframe"] is None:
            if include_ticks:
                for row in rows(path):
                    ticks.append(row)
        elif item["kind"] == "candles" and item["timeframe"] in candles:
            target = candles[item["timeframe"]]
            seconds = TF_SECONDS[item["timeframe"]]
            for row in rows(path):
                if set(row) != {"time", "open", "high", "low", "close", "tick_volume"}:
                    raise ValueError("Exact OHLC/tick-volume columns are required")
                time = evidence.utc(row["time"])
                prices = [float(evidence.finite(row[key], positive=True)) for key in ("open", "high", "low", "close")]
                if time.microsecond or int(time.timestamp()) % seconds or (target and time <= evidence.utc(target[-1]["time"])):
                    raise ValueError("Candles must be UTC aligned, ordered and unique")
                if prices[1] < max(prices[0], prices[2], prices[3]) or prices[2] > min(prices[0], prices[1], prices[3]):
                    raise ValueError("Invalid historical OHLC")
                if type(row["tick_volume"]) is not int or row["tick_volume"] < 0:
                    raise ValueError("Verified nonnegative tick volume is required")
                target.append({**row, "time": iso(time)})
        else:
            raise ValueError("Unsupported historical dataset kind")
    return candles, ticks, fingerprints


def segment_at(splits, decision):
    for name in ("development", "validation", "oos"):
        bounds = splits[name]
        start, end = evidence.utc(bounds["start"]), evidence.utc(bounds["end"])
        if start <= decision and decision + timedelta(seconds=3610) < end:
            return name
    return None


def window(candles, end_times, now, timeframe):
    end = bisect_right(end_times[timeframe], stamp(now))
    return candles[timeframe][max(0, end - 64):end]


def make_feed(manifest, windows, quote, now, cost, *, research=False):
    risk = manifest["risk_model"]
    context = {
        "account_mode": "demo", "volume": 0.01, "equity": risk["equity"],
        "free_margin": risk["free_margin"], "margin_required": risk["margin_required"],
        "open_positions": 0, "pending_orders": 0,
        "loss_cash_per_price_unit": cost["loss_cash_per_price_unit"],
        "profit_cash_per_price_unit": cost["profit_cash_per_price_unit"],
        "commission_round_turn": cost["commission_round_turn"], "slippage_price": cost["slippage_price"],
        "costs_verified": cost.get("verified") is True and not research,
        "as_of": iso(now), "broker_fingerprint": manifest.get("broker_fingerprint", ""),
    }
    return {"schema_version": 2, "as_of": iso(now), "source": "MetaTrader 5", "symbol": manifest["symbol"], "timeframe": "M15",
            "candles": windows["M15"], "timeframes": windows, "quote": quote,
            "execution": manifest["execution"], "risk_context": context}


def tick_outcome(ticks, candidate, decision, segment, scenario, *, cost=None, risk_model=None, execution=None):
    """Actual next-tick entry; first barrier, or actual deadline-close tick."""
    index = bisect_right(ticks.times, stamp(decision))
    if index >= len(ticks) or ticks.times[index] - stamp(decision) > 10_000_000:
        return None
    entry_tick = ticks.tick(index)
    entry_time = evidence.utc(entry_tick["time"])
    buy = candidate["direction"] == "BUY"
    stop, target = candidate["stop"], candidate["target"]
    actual_entry = entry_tick["ask" if buy else "bid"]
    if not (stop < actual_entry < target if buy else target < actual_entry < stop):
        return None
    original_risk = abs(candidate["entry"] - stop)
    if abs(actual_entry - candidate["entry"]) + entry_tick["ask"] - entry_tick["bid"] > 0.1 * original_risk:
        return None
    if not evidence.executable_entry_allowed(candidate["direction"], entry_tick["bid"], entry_tick["ask"], stop, target,
                                             cost, risk_model, execution):
        return None
    deadline = entry_time + timedelta(seconds=3600)
    last_time = entry_time
    maximum_gap = 0.0
    status, complete, exit_tick = "gap", False, entry_tick
    for current in range(index + 1, len(ticks)):
        quote = ticks.tick(current)
        time = evidence.utc(quote["time"])
        maximum_gap = max(maximum_gap, (time - last_time).total_seconds())
        last_time = time
        if maximum_gap > 30:
            exit_tick = quote
            break
        price = quote["bid" if buy else "ask"]
        if time > deadline:
            exit_tick = quote
            status, complete = ("timeout", True) if time <= deadline + timedelta(seconds=10) else ("gap", False)
            break
        if (price >= target if buy else price <= target):
            status, complete, exit_tick = "target", True, quote
            break
        if (price <= stop if buy else price >= stop):
            status, complete, exit_tick = "stop", True, quote
            break
        if time == deadline:
            status, complete, exit_tick = "timeout", True, quote
            break
        exit_tick = quote
    if not complete:
        # Incomplete records are reported separately, never inserted as fake
        # complete fixed-horizon trades with invented exit timestamps.
        return {"blocked": "A holding window has incomplete tick coverage"}
    return {"segment": segment, "scenario": scenario, "candidate_time": iso(decision),
            "entry_time": entry_tick["time"], "deadline": iso(deadline), "exit_time": exit_tick["time"],
            "direction": candidate["direction"], "entry_bid": entry_tick["bid"], "entry_ask": entry_tick["ask"],
            "exit_bid": exit_tick["bid"], "exit_ask": exit_tick["ask"], "stop": stop, "target": target,
            "status": status, "complete": complete, "maximum_tick_gap_seconds": maximum_gap}


def ohlc_bounds(bars, candidate, decision, spread, *, bar_times=None, cost=None):
    """Intervals only: do not invent the moment of an intrabar price touch."""
    bar_times = [stamp(bar["time"]) for bar in bars] if bar_times is None else bar_times
    # This is a declared OHLC interval model, not a timestamped tick fill.
    # The new bar's opening quote lies somewhere in this recorded interval.
    position = bisect_left(bar_times, stamp(decision))
    future = bars[position:position + 61]
    if not future:
        return None
    entry_bar = future[0]
    start = evidence.utc(entry_bar["time"])
    if start != decision:
        # A missing entry interval cannot be replaced by a later minute.
        # Even the provisional model must not invent a delayed fill.
        return None
    deadline = start + timedelta(seconds=3600)
    buy = candidate["direction"] == "BUY"
    stop, target = candidate["stop"], candidate["target"]
    entry_price = entry_bar["open"] + (spread if buy else 0)
    if not (stop < entry_price < target if buy else target < entry_price < stop):
        return None
    risk = abs(candidate["entry"] - stop)
    if abs(entry_price - candidate["entry"]) + spread > 0.1 * risk:
        return None
    net_target_positive = True
    if cost is not None:
        slip = float(evidence.finite(cost["slippage_price"]))
        movement = (target - entry_price) * (1 if buy else -1) - 2 * slip
        rate = float(evidence.finite(cost["profit_cash_per_price_unit"] if movement >= 0 else cost["loss_cash_per_price_unit"], positive=True))
        # Worst-case full-hour financing is charged even for an earlier touch.
        net_target_positive = movement * rate - cost["commission_round_turn"] - cost["financing_cash_per_hour"] > 0
    prior = start - timedelta(seconds=60)
    for bar in future:
        opened = evidence.utc(bar["time"])
        if opened - prior != timedelta(seconds=60):
            return {"status": "gap", "lower_win": False, "upper_win": True,
                    "entry_interval_start": iso(start), "deadline": iso(deadline)}
        if opened >= deadline:
            return {"status": "timeout", "lower_win": False, "upper_win": False,
                    "entry_interval_start": iso(start), "deadline": iso(deadline)}
        prior = opened
        high, low = bar["high"] + (0 if buy else spread), bar["low"] + (0 if buy else spread)
        hit_target = high >= target if buy else low <= target
        hit_stop = low <= stop if buy else high >= stop
        if hit_target or hit_stop:
            ambiguous = hit_target and hit_stop
            return {"status": "ambiguous" if ambiguous else "target" if hit_target else "stop",
                    "lower_win": hit_target and not ambiguous and net_target_positive,
                    "upper_win": hit_target and net_target_positive,
                    "entry_interval_start": iso(start), "exit_interval_start": iso(opened),
                    "exit_interval_end": iso(opened + timedelta(seconds=60)), "deadline": iso(deadline)}
    return {"status": "gap", "lower_win": False, "upper_win": True,
            "entry_interval_start": iso(start), "deadline": iso(deadline)}


def evaluate(manifest, candles, ticks, *, analyzer=None, exploratory_cost=None):
    if analyzer is None:
        from bot.multi_timeframe import analyze_multi_timeframe
        analyzer = analyze_multi_timeframe
    ends = {tf: [stamp(evidence.utc(bar["time"]) + timedelta(seconds=TF_SECONDS[tf])) for bar in values]
            for tf, values in candles.items()}
    costs = (exploratory_cost if type(exploratory_cost) is list else [exploratory_cost]) if exploratory_cost is not None else manifest.get("cost_scenarios", [])
    outcomes, blocked, provisional = [], [], []
    bar_times = [stamp(bar["time"]) for bar in candles["M1"]]
    if not costs:
        return [], ["No verified or explicitly declared cost model is available"], []
    if any(len(candles[tf]) < 22 for tf in TF_SECONDS):
        return [], ["Fewer than22 completed bars for M1, M5 or M15"], []
    for cost in costs:
        research = exploratory_cost is not None
        if research:
            if type(cost) is not dict or cost.get("verified") is not False:
                raise ValueError("Exploratory costs must be explicitly declared unverified")
            for key in ("commission_round_turn", "slippage_price", "financing_cash_per_hour"):
                evidence.finite(cost[key])
            for key in ("spread_price", "profit_cash_per_price_unit", "loss_cash_per_price_unit"):
                evidence.finite(cost[key], positive=True)
        prior_deadline = None
        counts = {name: {"trades": 0, "lower_wins": 0, "upper_wins": 0, "ambiguous": 0,
                         "timeouts": 0, "stops": 0, "targets": 0, "gaps": 0} for name in ("development", "validation", "oos")}
        reasons = {}
        for bar in candles["M1"]:
            decision = evidence.utc(bar["time"]) + timedelta(seconds=60)
            segment = segment_at(manifest["splits"], decision)
            if segment is None or (prior_deadline is not None and decision < prior_deadline):
                continue
            windows = {tf: window(candles, ends, decision, tf) for tf in TF_SECONDS}
            if any(len(values) < 22 for values in windows.values()):
                reasons["initial_warmup"] = reasons.get("initial_warmup", 0) + 1
                continue
            if research:
                spread = float(evidence.finite(cost["spread_price"], positive=True))
                quote = {"time": iso(decision), "bid": bar["close"], "ask": bar["close"] + spread}
            else:
                quote_index = bisect_right(ticks.times, stamp(decision)) - 1
                if quote_index < 0:
                    continue
                quote = ticks.tick(quote_index)
            feed = make_feed(manifest, windows, quote, decision, cost, research=research)
            candidate = analyzer(feed, now=decision, **({"research_only": True} if research else {}))
            if type(candidate) is not dict or candidate.get("state") != "signal":
                reason = candidate.get("reason", "invalid_analyzer_output") if type(candidate) is dict else "invalid_analyzer_output"
                reasons[reason] = reasons.get(reason, 0) + 1
                continue
            if research:
                bound = ohlc_bounds(candles["M1"], candidate, decision, spread, bar_times=bar_times, cost=cost)
                if bound is None:
                    reasons["no_causal_entry_within_zone"] = reasons.get("no_causal_entry_within_zone", 0) + 1
                    continue
                counts[segment]["trades"] += 1
                counts[segment]["lower_wins"] += bound["lower_win"]
                counts[segment]["upper_wins"] += bound["upper_win"]
                counts[segment]["ambiguous"] += bound["status"] in ("ambiguous", "gap")
                counters = {"timeout": "timeouts", "stop": "stops", "target": "targets", "gap": "gaps"}
                if bound["status"] in counters:
                    counts[segment][counters[bound["status"]]] += 1
                prior_deadline = evidence.utc(bound["deadline"])
            else:
                outcome = tick_outcome(ticks, candidate, decision, segment, cost["name"],
                                       cost=cost, risk_model=manifest["risk_model"], execution=manifest["execution"])
                if outcome is None:
                    continue
                if "blocked" in outcome:
                    blocked.append(outcome["blocked"])
                    prior_deadline = decision + timedelta(seconds=3610)
                    continue
                if evidence.utc(outcome["deadline"]) + timedelta(seconds=10) >= evidence.utc(manifest["splits"][segment]["end"]):
                    continue
                outcomes.append(outcome)
                prior_deadline = evidence.utc(outcome["deadline"])
        if research:
            for count in counts.values():
                n = count["trades"]
                count["observed_lower_win_rate"] = count["lower_wins"] / n if n else None
                count["observed_upper_win_rate"] = count["upper_wins"] / n if n else None
                count["wilson_lower_95_of_conservative_wins"] = evidence.wilson_lower(count["lower_wins"], n) if n else None
            provisional.append({"scenario": cost["name"], "method": "declared_cost_M1_OHLC_bounds_only",
                                "qualified": False, "declared_cost_model": cost, "segments": counts,
                                "discarded_or_waiting_reasons": reasons,
                                "entry_assumption": "new_M1_open_price_interval_unknown_latency_not_a_verified_tick_fill",
                                "assumed_execution": dict(manifest["execution"]),
                                "execution_verification": "not_certified_in_exploratory_mode",
                                "independent_trials_verified": False,
                                "statistical_note": "Wilson assumes a binomial model; nonoverlapping windows do not prove independence or remove regime dependence"})
            blocked.append("Candles-only simulated spreads/costs cannot qualify alerts")
    return outcomes, list(dict.fromkeys(blocked)), provisional


def evaluation_summary(assessment, report, provisional):
    text = evidence.arabic_summary(assessment)
    if not provisional:
        return text
    lines = [text, "", "التقييم الاستكشافي: اتجاه M15، تأكيد M5، توقيت M1؛ الهدف الأساسي TP1 = 2R والوقف الأصلي، مدة نموذجية60 دقيقة.",
             "الدخول بسعر افتتاح فترة M1 التالية مع سبريد افتراضي؛ وقت أول تيك وتأخر الدخول خلال10 ثوانٍ غير مثبتين بالشموع.",
             "التكاليف والنموذج المالي افتراضيان ومعلنان؛ ليست رسوماً تاريخية مثبتة أو رصيد الحساب الفعلي."]
    labels = {"development": "التطوير", "validation": "التحقق", "oos": "خارج العينة"}
    for name, bounds in report.get("splits", {}).items():
        lines.append(f"{labels.get(name, name)}: {bounds['start']} إلى {bounds['end']} (UTC وفق التطبيع المفترض).")
    for item in provisional:
        cost = item["declared_cost_model"]
        lines.append(f"\nسيناريو {item['scenario']}: سبريد {cost['spread_price']:.2f}، عمولة ذهاب وإياب {cost['commission_round_turn']:.2f}، انزلاق لكل طرف {cost['slippage_price']:.2f}، تمويل الساعة {cost['financing_cash_per_hour']:.2f}.")
        for name, counts in item["segments"].items():
            n = counts["trades"]
            if n:
                low, high = counts["lower_wins"] / n, counts["upper_wins"] / n
                observed = f"بين {low:.1%} و{high:.1%}"
                interval = f"{evidence.wilson_lower(counts['lower_wins'], n):.1%}"
            else:
                observed, interval = "غير متاح لغياب العينات", "غير متاح"
            lines.append(f"{labels[name]}: {n} نافذة غير متداخلة؛ نجاح الهدف بعد التكاليف {observed}؛ حد Wilson الأدنى المشروط بالنموذج {interval}؛ انتهاء المدة {counts['timeouts']}؛ وقف {counts['stops']}؛ حالات ترتيب/تغطية غير محسومة {counts['ambiguous']}.")
        reasons = item["discarded_or_waiting_reasons"]
        lines.append("استبعاد/انتظار: " + "; ".join(f"{key}={count}" for key, count in sorted(reasons.items())))
    specs = provisional[0]["assumed_execution"]
    lines.extend(["", "الحد الأدنى يحتسب لمس الهدف والوقف في نفس الشمعة كعدم نجاح؛ الحد الأعلى يفترض الترتيب الملائم. الفجوات تبقى غير محسومة.",
                  "الـ200 صفقة خارج العينة وحد الثقة الأدنى70% لا يثبتان بهذا التقييم: البيانات لا توفر تيكات Bid/Ask التاريخية الكاملة أو الرسوم والتحويل الزمني الموسمي المثبتين.",
                  f"مواصفات السعر التاريخية معلنة كافتراض: تيك السعر {specs['tick_size']}، درجة السعر {specs['point']}، الخانات {specs['digits']}، أدنى مسافة وقف {specs['stops_level']}؛ ليست مواصفات مثبتة لكل التاريخ.",
                  "الإشعارات محجوبة. النتائج تقديرية شرطية على النموذج، ولا تمثل تنفيذاً يدوياً فعلياً أو ضماناً لنجاح صفقة مقبلة."])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, help="Local JSON manifest: symbol/broker/provenance/UTC/coverage/files/splits/execution/risk_model/cost_scenarios")
    parser.add_argument("--output", type=Path, help="Write the strict evidence artifact, never raw history")
    parser.add_argument("--summary", type=Path, help="Write a concise Arabic summary")
    parser.add_argument("--report-output", type=Path, help="Write the machine report including provisional bounds; cannot be loaded as a trusted artifact")
    parser.add_argument("--exploratory-cost", help="Explicit JSON simulated cost scenario or list, including spread_price and verified=false; always provisional")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    blocked, provisional = [], []
    report = {"version": 1, "kind": "strategy_evidence", "strategy": {},
              "evaluation_policy": evidence.EVALUATION_POLICY, "evaluator_fingerprint": evidence.evaluator_fingerprint(),
              "generated_at": iso(now), "dataset": {}, "splits": {}, "outcomes": [], "blocked_reasons": []}
    try:
        report["strategy"] = evidence.current_identity()
        if args.manifest is None:
            raise ValueError("A local dataset/provenance/cost manifest is required")
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if type(manifest) is not dict or manifest.get("version") != 1:
            raise ValueError("Unsupported dataset manifest")
        candles, ticks, fingerprints = load_data(manifest, args.manifest.parent, include_ticks=args.exploratory_cost is None)
        report["dataset"] = {key: manifest[key] for key in evidence.DATASET_KEYS if key not in ("files", "execution_specs", "dependence_assessment")}
        report["dataset"]["execution_specs"] = manifest.get("execution_specs")
        report["dataset"]["dependence_assessment"] = manifest.get("dependence_assessment")
        report["dataset"]["files"] = fingerprints
        report["splits"] = manifest["splits"]
        exploratory = json.loads(args.exploratory_cost) if args.exploratory_cost else None
        if exploratory is None:
            if manifest.get("provenance_verified") is not True or manifest.get("price_basis") != "bid_ask_ticks" or not ticks:
                raise ValueError("Verified same-broker Bid/Ask history is unavailable; use only explicitly declared exploratory costs")
            evidence._costs(manifest["cost_scenarios"], evidence.utc(manifest["coverage_start"]), evidence.utc(manifest["coverage_end"]))
            specs = evidence._execution_specs(manifest.get("execution_specs"), evidence.utc(manifest["coverage_start"]), evidence.utc(manifest["coverage_end"]))
            if evidence._execution_context(manifest["execution"]) != specs:
                raise ValueError("Evaluated execution metadata differs from verified historical specifications")
        report["outcomes"], blocked, provisional = evaluate(manifest, candles, ticks, exploratory_cost=exploratory)
    except (ValueError, TypeError, KeyError, OSError, OverflowError, ImportError) as exc:
        blocked.append(str(exc)[:256] or "Required evaluation data unavailable")
    report["blocked_reasons"] = list(dict.fromkeys(blocked))
    assessment = evidence.validate_evidence_report(report, identity=report["strategy"])
    encoded = evidence.canonical_bytes(report) + b"\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(encoded)
    summary = evaluation_summary(assessment, report, provisional)
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(summary + "\n", encoding="utf-8")
    machine = {"qualified": assessment.qualified, "reasons": assessment.reasons,
               "scenario_metrics": assessment.scenario_metrics, "provisional": provisional,
               "artifact_sha256": hashlib.sha256(encoded).hexdigest()}
    if args.report_output:
        args.report_output.parent.mkdir(parents=True, exist_ok=True)
        args.report_output.write_bytes(evidence.canonical_bytes({**machine, "artifact": report}) + b"\n")
    print(json.dumps(machine, ensure_ascii=False, allow_nan=False))
    return 0 if assessment.qualified else 2


if __name__ == "__main__":
    raise SystemExit(main())
