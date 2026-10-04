"""A fixed pause depends on trustworthy scoped outcomes, never predicted profit."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from bot import paper_journal, paper_policy


NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
SOURCE = "reference:XAUUSD"
STRATEGY = "ema9-21-atr14-v1"


def outcome(signal_id, closed_at, status="stop_observed", *, source=SOURCE, strategy=STRATEGY, covered=True):
    opened = closed_at - timedelta(minutes=15 if status in ("expired", "inconclusive") else 1)
    bar_slot = int(opened.timestamp()) // 900 * 900 - 900
    result = {
        "state": "signal", "strategy_id": strategy,
        "bar_time": datetime.fromtimestamp(bar_slot, timezone.utc).isoformat(),
        "direction": "BUY", "entry": 100.0, "stop": 97.0, "target": 106.0,
    }
    trade = paper_journal.create_trade(signal_id, result, source, opened)
    if status == "open":
        return trade
    if status == "inconclusive":
        return paper_journal.advance_trade(trade, [], closed_at)
    if status == "expired":
        samples = [
            {"time": (opened + timedelta(minutes=i)).isoformat(), "price": 101.0}
            for i in range(1, 16)
        ]
    else:
        samples = [{"time": closed_at.isoformat(), "price": 97.0 if status == "stop_observed" else 106.0}]
    trade = paper_journal.advance_trade(trade, samples, closed_at)
    if not covered:
        trade["coverage_complete"] = False
        trade["max_gap_seconds"] = 240.0
    return trade


class PaperPolicyTests(unittest.TestCase):
    def three_stops(self):
        return [outcome(f"stop-{i}", NOW - timedelta(minutes=i * 10)) for i in range(3)]

    def pause(self, trades, now=NOW, source=SOURCE, strategy=STRATEGY):
        return paper_policy.pause_until(trades, source, strategy, now)

    def test_three_latest_unique_complete_stops_trigger_fixed_pause_without_mutating_history(self):
        trades = self.three_stops()
        original = deepcopy(trades)
        self.assertEqual(self.pause(list(reversed(trades))), NOW + timedelta(minutes=45))
        self.assertEqual(trades, original)
        self.assertEqual(paper_policy.POLICY_ID, "three-complete-observed-stops-pause45-v1")

    def test_pause_ends_at_exact_deadline_and_does_not_restart_from_check_time(self):
        trades = self.three_stops()
        self.assertEqual(self.pause(trades, NOW + timedelta(minutes=44)), NOW + timedelta(minutes=45))
        self.assertIsNone(self.pause(trades, NOW + timedelta(minutes=45)))
        self.assertIsNone(self.pause(trades, NOW + timedelta(minutes=60)))

    def test_zero_one_or_two_stops_cannot_trigger_pause(self):
        trades = self.three_stops()
        for count in range(3):
            with self.subTest(count=count):
                self.assertIsNone(self.pause(trades[:count]))

    def test_newer_target_expiry_inconclusive_or_partial_stop_breaks_streak(self):
        for status, covered in (("target_observed", True), ("expired", True), ("inconclusive", True), ("stop_observed", False)):
            with self.subTest(status=status, covered=covered):
                trades = self.three_stops()
                trades.insert(0, outcome("break", NOW, status, covered=covered))
                self.assertIsNone(self.pause(trades))

    def test_older_nonstop_does_not_break_three_later_complete_observed_stops(self):
        for status in ("target_observed", "expired", "inconclusive"):
            with self.subTest(status=status):
                trades = self.three_stops() + [outcome("old-break", NOW - timedelta(hours=2), status)]
                self.assertEqual(self.pause(trades), NOW + timedelta(minutes=45))

    def test_source_and_strategy_isolation_prevents_unrelated_stops_from_counting(self):
        trades = self.three_stops()[:2]
        for source, strategy in (("mt5:XAUUSD", STRATEGY), (SOURCE, "other-strategy")):
            with self.subTest(source=source, strategy=strategy):
                extra = outcome("unrelated", NOW, source=source, strategy=strategy)
                self.assertIsNone(self.pause(trades + [extra]))
                self.assertEqual(self.pause(self.three_stops() + [extra]), NOW + timedelta(minutes=45))

    def test_open_rows_are_ignored_and_a_future_outcome_is_not_counted(self):
        trades = self.three_stops()
        self.assertEqual(self.pause(trades + [outcome("open", NOW, "open")]), NOW + timedelta(minutes=45))
        future = outcome("future", NOW + timedelta(minutes=1), "target_observed")
        self.assertEqual(self.pause(trades + [future]), NOW + timedelta(minutes=45))
        future_stop = outcome("future-stop", NOW + timedelta(minutes=1))
        self.assertIsNone(self.pause(trades[:2] + [future_stop]))

    def test_duplicate_identifiers_count_once_and_contradictory_copies_disable_pause(self):
        trades = self.three_stops()
        self.assertIsNone(self.pause([trades[0], deepcopy(trades[0]), trades[1]]))
        self.assertEqual(self.pause(trades + [deepcopy(trades[1])]), NOW + timedelta(minutes=45))
        conflict = outcome(trades[0]["id"], NOW, "target_observed")
        self.assertIsNone(self.pause(trades + [conflict]))

    def test_missing_invalid_or_partial_matching_records_cannot_be_silently_discarded(self):
        for changes in (
            {"id": ""}, {"status": "invalid"}, {"coverage_complete": 1},
            {"coverage_complete": True, "max_gap_seconds": 240},
            {"closed_at": None}, {"closed_at": "2026-10-04T12:00:00"},
            {"exit_price": 110.0, "last_observation_price": 110.0, "gross_r": 3.333333},
            {"gross_r": -2}, {"deadline": "2026-10-04T11:00:00Z"},
        ):
            with self.subTest(changes=changes):
                broken = deepcopy(self.three_stops()[0])
                broken.update(changes)
                self.assertIsNone(self.pause(self.three_stops() + [broken]))
        for missing in ("entry", "stop", "target", "opened_at", "deadline", "strategy_id", "source_identity"):
            with self.subTest(missing=missing):
                broken = deepcopy(self.three_stops()[0])
                del broken[missing]
                self.assertIsNone(self.pause(self.three_stops() + [broken]))

    def test_ambiguous_equal_time_boundary_nonstop_cannot_manufacture_streak(self):
        trades = self.three_stops()
        tie = outcome("aaa-boundary", NOW - timedelta(minutes=20), "target_observed")
        self.assertIsNone(self.pause(trades + [tie]))

    def test_input_bounds_invalid_parameters_and_naive_clock_disable_pause_safely(self):
        trades = self.three_stops()
        for invalid in (None, {}, trades * 34, [None], ["untrusted"]):
            with self.subTest(invalid=type(invalid).__name__):
                self.assertIsNone(self.pause(invalid))
        self.assertIsNone(self.pause(trades, NOW.replace(tzinfo=None)))
        self.assertIsNone(self.pause(trades, "2026-10-04T12:00:00Z"))
        self.assertIsNone(self.pause(trades, source=""))
        self.assertIsNone(self.pause(trades, strategy=None))
        self.assertIsNone(self.pause(trades, source="reference:XAUUSD\n"))
        self.assertEqual(self.pause([deepcopy(trades[0])] * 98 + trades[1:]), NOW + timedelta(minutes=45))

    def test_aware_local_clock_returns_utc_pause_timestamp(self):
        local_now = NOW.astimezone(timezone(timedelta(hours=3)))
        deadline = self.pause(self.three_stops(), local_now)
        self.assertEqual(deadline, NOW + timedelta(minutes=45))
        self.assertEqual(deadline.tzinfo, timezone.utc)


if __name__ == "__main__":
    unittest.main()
