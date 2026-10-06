"""Offline synthetic execution/order/causality checks; no SDK or UI."""

from datetime import datetime, timedelta, timezone
import unittest

from tools import evaluate_multi_timeframe as evaluate


NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
CANDIDATE = {"direction": "BUY", "entry": 100.1, "stop": 95, "target": 110}
COST = {"commission_round_turn": .07, "slippage_price": .01,
        "loss_cash_per_price_unit": 1, "profit_cash_per_price_unit": 1}
RISK = {"equity": 100000, "free_margin": 100000, "margin_required": 1000}
EXECUTION = {"tick_size": .01, "point": .01, "digits": 2, "stops_level": 0}


def tick_outcome(series, candidate, *, cost=COST, risk=RISK):
    return evaluate.tick_outcome(series, candidate, NOW, "oos", "base", cost=cost, risk_model=risk, execution=EXECUTION)


def ticks(points):
    series = evaluate.TickSeries()
    for seconds, bid, ask in points:
        series.append({"time": (NOW + timedelta(seconds=seconds)).isoformat(), "bid": bid, "ask": ask})
    return series


class EvaluatorTests(unittest.TestCase):
    def test_next_tick_and_first_stop_precede_later_target(self):
        series = ticks([(0, 100, 100.1), (1, 100, 100.1), (2, 94.9, 95), (3, 111, 111.1)])
        result = tick_outcome(series, CANDIDATE)
        self.assertEqual(result["status"], "stop")
        self.assertEqual(result["entry_time"], (NOW + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"))
        self.assertEqual(result["deadline"], (NOW + timedelta(seconds=3601)).isoformat().replace("+00:00", "Z"))

    def test_sell_exits_on_ask_not_bid(self):
        series = ticks([(1, 100, 100.1), (2, 94.9, 95.1), (3, 94.8, 94.9)])
        candidate = {"direction": "SELL", "entry": 100, "stop": 102.5, "target": 95}
        result = tick_outcome(series, candidate)
        self.assertEqual(result["status"], "target")
        self.assertEqual(result["exit_time"], (NOW + timedelta(seconds=3)).isoformat().replace("+00:00", "Z"))

    def test_gap_late_entry_changed_zone_and_duplicate_order_fail_closed(self):
        self.assertIsNone(tick_outcome(ticks([(11, 100, 100.1)]), CANDIDATE))
        self.assertIsNone(tick_outcome(ticks([(1, 101, 101.1)]), CANDIDATE))
        result = tick_outcome(ticks([(1, 100, 100.1), (32, 111, 111.1)]), CANDIDATE)
        self.assertIn("blocked", result)
        with self.assertRaises(ValueError): ticks([(1, 100, 100.1), (1, 101, 101.1)])
        with self.assertRaises(ValueError): ticks([(1, 100, 100)])

    def test_timeout_uses_real_quote_time_and_crossing_after_deadline_is_not_win(self):
        points = [(second, 100, 100.1) for second in range(1, 3601, 20)]
        points += [(3602, 111, 111.1)]
        result = tick_outcome(ticks(points), CANDIDATE)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["exit_time"], (NOW + timedelta(seconds=3602)).isoformat().replace("+00:00", "Z"))

    def test_window_excludes_forming_and_future_bars(self):
        bars = [{"time": (NOW + timedelta(minutes=index)).isoformat()} for index in range(-100, 2)]
        tables = {"M1": bars}
        ends = {"M1": [evaluate.stamp(evaluate.evidence.utc(bar["time"]) + timedelta(minutes=1)) for bar in bars]}
        result = evaluate.window(tables, ends, NOW, "M1")
        self.assertEqual(len(result), 64)
        self.assertEqual(evaluate.evidence.utc(result[-1]["time"]), NOW - timedelta(minutes=1))

    def test_split_horizon_is_purged_without_training_on_future(self):
        splits = {name: {"start": (NOW + timedelta(days=index)).isoformat(), "end": (NOW + timedelta(days=index + 1)).isoformat()}
                  for index, name in enumerate(("development", "validation", "oos"))}
        self.assertEqual(evaluate.segment_at(splits, NOW), "development")
        self.assertIsNone(evaluate.segment_at(splits, NOW + timedelta(days=1, seconds=-3600)))

    def test_ohlc_both_barriers_have_conservative_lower_bound_and_no_fake_exit(self):
        bars = [{"time": NOW.isoformat(), "open": 100, "high": 111, "low": 94, "close": 101}]
        result = evaluate.ohlc_bounds(bars, CANDIDATE, NOW, .1)
        self.assertEqual(result["status"], "ambiguous")
        self.assertFalse(result["lower_win"])
        self.assertTrue(result["upper_win"])
        self.assertNotIn("exit_time", result)

    def test_ohlc_missing_entry_minute_cannot_be_replaced_with_later_open(self):
        bars = [{"time": (NOW + timedelta(minutes=1)).isoformat(), "open": 100, "high": 111, "low": 99, "close": 101}]
        self.assertIsNone(evaluate.ohlc_bounds(bars, CANDIDATE, NOW, .1))

    def test_ohlc_gap_crossing_deadline_stays_unresolved_not_timeout(self):
        bars = [{"time": (NOW + timedelta(minutes=index)).isoformat(), "open": 100, "high": 101, "low": 99, "close": 100}
                for index in list(range(59)) + [61]]
        result = evaluate.ohlc_bounds(bars, CANDIDATE, NOW, .1)
        self.assertEqual(result["status"], "gap")
        self.assertFalse(result["lower_win"])
        self.assertTrue(result["upper_win"])

    def test_next_tick_rechecks_cash_loss_and_effective_rr_without_moving_levels(self):
        no_cost = {**COST, "commission_round_turn": 0, "slippage_price": 0}
        self.assertIsNone(tick_outcome(ticks([(1, 100.1, 100.2)]), CANDIDATE,
                                      cost=no_cost, risk={**RISK, "equity": 516}))
        candidate = {**CANDIDATE, "target": 108.02}
        self.assertIsNone(tick_outcome(ticks([(1, 100.11, 100.21)]), candidate, cost=no_cost))
        self.assertIsNone(evaluate.tick_outcome(ticks([(1, 100, 100.1)]), CANDIDATE, NOW, "oos", "base"))


if __name__ == "__main__": unittest.main()
