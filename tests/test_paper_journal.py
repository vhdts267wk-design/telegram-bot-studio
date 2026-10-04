"""Synthetic observations verify review timing, uncertainty and immutability."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
import unittest

from bot import paper_journal as journal


OPENED = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def signal(direction="BUY"):
    return {
        "state": "signal", "strategy_id": "ema9-21-atr14-v1",
        "bar_time": "2026-10-04T11:45:00Z", "direction": direction,
        "entry": 100.0,
        "stop": 97.0 if direction == "BUY" else 103.0,
        "target": 106.0 if direction == "BUY" else 94.0,
    }


def sample(minutes, price=101.0):
    return {"time": (OPENED + timedelta(minutes=minutes)).isoformat(), "price": price}


class PaperJournalTests(unittest.TestCase):
    def trade(self, direction="BUY"):
        return journal.create_trade("synthetic-setup", signal(direction), "reference:XAUUSD", OPENED)

    def test_create_freezes_signal_and_declares_availability_and_fifteen_minute_deadline(self):
        result = signal()
        trade = journal.create_trade("synthetic-setup", result, "reference:XAUUSD", OPENED)
        result.update(entry=200, stop=190, target=220)
        self.assertEqual((trade["entry"], trade["stop"], trade["target"]), (100, 97, 106))
        self.assertEqual(trade["opened_at"], "2026-10-04T12:00:00Z")
        self.assertEqual(trade["deadline"], "2026-10-04T12:15:00Z")
        self.assertEqual(trade["duration_minutes"], 15)
        self.assertEqual(trade["status"], "open")
        self.assertIsNone(trade["gross_r"])
        json.dumps(trade, allow_nan=False)

    def test_non_signal_forming_or_unaligned_bar_and_bad_levels_are_rejected(self):
        for changes in (
            {"state": "no_signal"}, {"direction": "HOLD"}, {"stop": 100},
            {"target": 99}, {"entry": math.nan}, {"stop": True},
            {"bar_time": "2026-10-04T12:00:00Z"},
            {"bar_time": "2026-10-04T11:46:00Z"},
            {"bar_time": "2026-10-04T11:45:00"},
        ):
            with self.subTest(changes=changes):
                result = signal()
                result.update(changes)
                with self.assertRaises(ValueError):
                    journal.create_trade("id", result, "reference:XAUUSD", OPENED)
        for duration in (0, -1, True, 1.5, 1441):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                journal.create_trade("id", signal(), "reference:XAUUSD", OPENED, duration)

    def test_timezone_aware_inputs_normalize_to_utc(self):
        local = OPENED.astimezone(timezone(timedelta(hours=3)))
        trade = journal.create_trade("id", signal(), "reference:XAUUSD", local)
        observations = [{"time": (local + timedelta(minutes=1)).isoformat(), "price": 106}]
        reviewed = journal.advance_trade(trade, observations, local + timedelta(minutes=1))
        self.assertEqual(reviewed["closed_at"], "2026-10-04T12:01:00Z")
        with self.assertRaises(ValueError):
            journal.create_trade("id", signal(), "reference:XAUUSD", OPENED.replace(tzinfo=None))

    def test_buy_and_sell_target_and_stop_use_first_observed_price_not_assumed_fill(self):
        for direction, price, status, gross_r in (
            ("BUY", 107.5, "target_observed", 2.5),
            ("BUY", 95.5, "stop_observed", -1.5),
            ("SELL", 92.5, "target_observed", 2.5),
            ("SELL", 104.5, "stop_observed", -1.5),
        ):
            with self.subTest(direction=direction, status=status):
                trade = self.trade(direction)
                original = deepcopy(trade)
                reviewed = journal.advance_trade(trade, [sample(1, price)], OPENED + timedelta(minutes=1))
                self.assertEqual(reviewed["status"], status)
                self.assertEqual(reviewed["exit_price"], price)
                self.assertEqual(reviewed["gross_r"], gross_r)
                self.assertEqual(trade, original)
                self.assertTrue(reviewed["coverage_complete"])
                json.dumps(reviewed, allow_nan=False)

    def test_first_observed_threshold_closes_once_and_later_opposite_threshold_does_not_rewrite(self):
        reviewed = journal.advance_trade(
            self.trade(), [sample(1, 97), sample(2, 106)], OPENED + timedelta(minutes=2),
        )
        self.assertEqual(reviewed["status"], "stop_observed")
        self.assertEqual(reviewed["gross_r"], -1)
        self.assertEqual(reviewed["closed_at"], "2026-10-04T12:01:00Z")
        original = deepcopy(reviewed)
        again = journal.advance_trade(reviewed, [sample(3, 120)], OPENED + timedelta(minutes=20))
        self.assertEqual(again, original)

    def test_trigger_period_and_exact_opening_sample_cannot_determine_an_outcome(self):
        observations = [sample(-15, 120), sample(-1, 90), sample(0, 120), sample(1, 101)]
        reviewed = journal.advance_trade(self.trade(), observations, OPENED + timedelta(minutes=1))
        self.assertEqual(reviewed["status"], "open")
        self.assertEqual(reviewed["last_observation_price"], 101)
        self.assertIsNone(reviewed["gross_r"])

    def test_incremental_and_replayed_history_produce_same_record(self):
        observations = [sample(minute, 101) for minute in range(1, 16)]
        first = journal.advance_trade(self.trade(), observations[:5], OPENED + timedelta(minutes=5))
        reviewed = journal.advance_trade(first, observations, OPENED + timedelta(minutes=15))
        once = journal.advance_trade(self.trade(), observations, OPENED + timedelta(minutes=15))
        self.assertEqual(reviewed, once)
        self.assertEqual(reviewed["status"], "expired")
        self.assertEqual(reviewed["gross_r"], round(1 / 3, 6))
        self.assertEqual((reviewed["entry"], reviewed["stop"], reviewed["target"]), (100, 97, 106))

    def test_expiry_uses_last_in_window_price_and_ignores_post_deadline_crossing(self):
        observations = [sample(minute, 101) for minute in range(1, 16)] + [sample(16, 120)]
        reviewed = journal.advance_trade(self.trade(), observations, OPENED + timedelta(minutes=20))
        self.assertEqual(reviewed["status"], "expired")
        self.assertEqual(reviewed["exit_price"], 101)
        self.assertEqual(reviewed["closed_at"], "2026-10-04T12:15:00Z")
        self.assertEqual(reviewed["last_observation_at"], "2026-10-04T12:15:00Z")

    def test_deadline_sample_is_included_and_threshold_takes_precedence_over_expiry(self):
        observations = [sample(minute, 101) for minute in range(1, 15)] + [sample(15, 106)]
        reviewed = journal.advance_trade(self.trade(), observations, OPENED + timedelta(minutes=15))
        self.assertEqual(reviewed["status"], "target_observed")
        self.assertEqual(reviewed["gross_r"], 2)

    def test_still_open_before_deadline_even_when_latest_quote_is_within_three_minutes(self):
        reviewed = journal.advance_trade(self.trade(), [sample(1), sample(2)], OPENED + timedelta(minutes=14))
        self.assertEqual(reviewed["status"], "open")
        self.assertIsNone(reviewed["exit_price"])

    def test_exact_three_minute_start_between_sample_and_trailing_gaps_are_allowed(self):
        observations = [sample(minute) for minute in (3, 6, 9, 12)]
        reviewed = journal.advance_trade(self.trade(), observations, OPENED + timedelta(minutes=15))
        self.assertEqual(reviewed["status"], "expired")
        self.assertTrue(reviewed["coverage_complete"])
        self.assertEqual(reviewed["max_gap_seconds"], 180)

    def test_start_internal_or_stale_trailing_gaps_make_expiry_inconclusive(self):
        batches = (
            [sample(minute) for minute in range(4, 16)],
            [sample(minute) for minute in (1, 2, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15)],
            [sample(minute) for minute in range(1, 12)],
            [],
        )
        for observations in batches:
            with self.subTest(observations=observations):
                reviewed = journal.advance_trade(self.trade(), observations, OPENED + timedelta(minutes=15))
                self.assertEqual(reviewed["status"], "inconclusive")
                self.assertFalse(reviewed["coverage_complete"])
                self.assertIsNone(reviewed["exit_price"])
                self.assertIsNone(reviewed["gross_r"])
                self.assertGreater(reviewed["max_gap_seconds"], 180)

    def test_observed_threshold_after_gap_remains_observed_with_explicit_uncertainty(self):
        reviewed = journal.advance_trade(self.trade(), [sample(5, 107)], OPENED + timedelta(minutes=5))
        self.assertEqual(reviewed["status"], "target_observed")
        self.assertFalse(reviewed["coverage_complete"])
        self.assertEqual(reviewed["max_gap_seconds"], 300)
        text = journal.format_review(reviewed)
        self.assertIn("عبوراً سابقاً غير مرصود", text)
        self.assertIn("التغطية غير مكتملة", text)

    def test_invalid_future_unordered_duplicate_nonprice_or_unbounded_batches_are_rejected(self):
        trade = self.trade()
        original = deepcopy(trade)
        batches = (
            [sample(1), sample(3)], [sample(2), sample(1)], [sample(1), sample(1)],
            [{"time": sample(1)["time"], "high": 106, "low": 97}],
            [{"time": "2026-10-04T12:01:00", "price": 101}],
            [sample(1, True)], [sample(1, math.nan)], [sample(1, math.inf)],
            [sample(1, 0)], [sample(1, -1)], [sample(1, 100_000_001)],
            [None], {}, [sample(1)] * 1601,
        )
        for observations in batches:
            with self.subTest(observations=observations), self.assertRaises(ValueError):
                journal.advance_trade(trade, observations, OPENED + timedelta(minutes=2))
            self.assertEqual(trade, original)
        with self.assertRaises(ValueError):
            journal.advance_trade(trade, [], OPENED - timedelta(seconds=1))

    def test_entire_batch_is_validated_before_an_early_threshold_can_hide_future_data(self):
        trade = self.trade()
        with self.assertRaises(ValueError):
            journal.advance_trade(trade, [sample(1, 106), sample(10, 101)], OPENED + timedelta(minutes=2))
        self.assertEqual(trade["status"], "open")

    def test_persisted_record_guards_prevent_changed_deadline_or_invented_outcome(self):
        for change in (
            {"deadline": "2026-10-04T12:30:00Z"},
            {"signal_bar_time": "2026-10-04T12:00:00Z"},
            {"status": "expired", "closed_at": "2026-10-04T12:15:00Z", "exit_price": 110, "gross_r": 3},
            {"exit_price": 110, "gross_r": 3},
            {"coverage_complete": 1},
        ):
            with self.subTest(change=change):
                trade = self.trade()
                trade.update(change)
                with self.assertRaises(ValueError):
                    journal.advance_trade(trade, [], OPENED + timedelta(minutes=15))
        recorded = journal.advance_trade(self.trade(), [sample(2)], OPENED + timedelta(minutes=2))
        with self.assertRaises(ValueError):
            journal.advance_trade(recorded, [], OPENED + timedelta(minutes=1))

    def test_review_declares_reference_r_costs_and_proposals_without_automatic_rule_changes(self):
        for observations, status in (
            ([sample(1, 106)], "target_observed"),
            ([sample(1, 97)], "stop_observed"),
            ([sample(minute) for minute in range(1, 16)], "expired"),
            ([], "inconclusive"),
        ):
            with self.subTest(status=status):
                reviewed = journal.advance_trade(self.trade(), observations, OPENED + timedelta(minutes=15))
                text = journal.format_review(reviewed)
                for phrase in ("مراجعة ورقية", "15 دقيقة", "reference:XAUUSD", "UTC", "دخول مرجعي ثابت",
                               "اقتراح للمراجعة فقط", "تبقى مستويات الصفقة ومعاملات EMA/ATR ثابتة", "السبريد", "الانزلاق", "العمولات", "لا أوامر"):
                    self.assertIn(phrase, text)
                self.assertGreater(len(reviewed["review_notes"]), 1)
                if status == "inconclusive":
                    self.assertNotIn("الحركة المرجعية الإجمالية:", text)


if __name__ == "__main__":
    unittest.main()
