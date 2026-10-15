from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from bot import trade_timing as timing


class TradeTimingTests(unittest.TestCase):
    now = datetime(2026, 10, 14, 13, 59, tzinfo=timezone.utc)

    def context(self, *, clock=None, close=None):
        clock = self.now if clock is None else clock
        return {
            "source": timing.SESSION_SOURCE, "verified": True, "as_of": clock.isoformat(),
            "session_open": clock.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
            "session_close": (clock.replace(hour=23, minute=0, second=0, microsecond=0)
                              if close is None else close).isoformat(),
        }

    def plan(self, *, clock=None, context=None, window=30):
        clock = self.now if clock is None else clock
        return timing.build_trade_timing(self.context(clock=clock) if context is None else context,
                                         clock, clock.replace(second=0, microsecond=0), window)

    def assert_reason(self, context, expected, *, clock=None):
        clock = self.now if clock is None else clock
        with self.assertRaises(timing.TradeTimingError) as caught:
            timing.build_trade_timing(context, clock, clock.replace(second=0, microsecond=0), 30)
        self.assertEqual(caught.exception.reason, expected)

    def test_verified_plan_freezes_entry_window_and_latest_possible_hour(self):
        original = self.context()
        untouched = deepcopy(original)
        plan = self.plan(context=original)
        self.assertEqual(set(plan), timing.TIMING_FIELDS)
        self.assertEqual(plan["entry_deadline"], "2026-10-14T13:59:30+00:00")
        self.assertEqual(plan["latest_exit_at"], "2026-10-14T14:59:30+00:00")
        self.assertEqual(plan["session_exit_at"], "2026-10-14T19:00:00+00:00")
        self.assertTrue(timing.validate_trade_timing(plan, self.now, self.now - timedelta(minutes=1)))
        self.assertEqual(original, untouched)

    def test_broker_close_buffer_is_applied_and_full_hour_includes_entry_window(self):
        clock = self.now.replace(hour=16, minute=30)
        close = clock + timedelta(hours=1, minutes=15, seconds=30)
        plan = self.plan(clock=clock, context=self.context(clock=clock, close=close))
        self.assertEqual(plan["session_exit_at"], (close - timedelta(minutes=15)).isoformat())
        self.assertEqual(plan["latest_exit_at"], plan["session_exit_at"])
        self.assert_reason(self.context(clock=clock, close=close - timedelta(microseconds=1)),
                           "outside_full_horizon_session", clock=clock)

    def test_known_broker_session_is_required_instead_of_guessing_hours(self):
        for context in (None, {}, {"verified": False}, dict(self.context(), verified=1)):
            self.assert_reason(context, "broker_session_unavailable")
        for context in (dict(self.context(), source="operator_guess"),
                        dict(self.context(), extra=True),
                        {key: value for key, value in self.context().items() if key != "session_close"}):
            self.assert_reason(context, "invalid_broker_session")

    def test_stale_future_naive_and_unbounded_session_data_fail_closed(self):
        for seconds in (30.000001, -0.000001):
            self.assert_reason(dict(self.context(), as_of=(self.now - timedelta(seconds=seconds)).isoformat()),
                               "stale_broker_session")
        self.assertTrue(self.plan(context=dict(self.context(), as_of=(self.now - timedelta(seconds=30)).isoformat())))
        for context in (dict(self.context(), session_close="2026-10-14T23:00:00"),
                        dict(self.context(), session_close=self.context()["session_open"]),
                        dict(self.context(), session_close=(self.now + timedelta(days=8)).isoformat())):
            self.assert_reason(context, "invalid_broker_session")
        self.assert_reason(dict(self.context(), session_open=(self.now + timedelta(seconds=1)).isoformat()),
                           "outside_broker_session")
        self.assert_reason(dict(self.context(), session_close=self.now.isoformat()), "outside_broker_session")

    def test_all_aware_offsets_normalize_to_utc_before_comparing_boundaries(self):
        context = self.context()
        beirut = timezone(timedelta(hours=3))
        for key in ("as_of", "session_open", "session_close"):
            context[key] = datetime.fromisoformat(context[key]).astimezone(beirut).isoformat()
        plan = timing.build_trade_timing(context, self.now.astimezone(beirut),
                                         self.now.astimezone(beirut), 30)
        self.assertEqual(plan, self.plan())

    def test_entry_expiry_and_conservative_weekday_window_are_enforced(self):
        for seconds, expected in ((0, True), (30, True), (30.000001, False)):
            clock = self.now + timedelta(seconds=seconds)
            if expected:
                timing.build_trade_timing(self.context(clock=clock), clock, self.now, 30)
            else:
                with self.assertRaises(timing.TradeTimingError) as caught:
                    timing.build_trade_timing(self.context(clock=clock), clock, self.now, 30)
                self.assertEqual(caught.exception.reason, "entry_window_expired")
        for clock in (self.now.replace(hour=5), self.now.replace(hour=18), self.now.replace(day=17)):
            self.assert_reason(self.context(clock=clock), "outside_full_horizon_session", clock=clock)

    def test_frozen_deadlines_and_policy_cannot_be_tampered_with(self):
        plan = self.plan()
        for key, replacement in (("policy_id", "forged"), ("max_hold_seconds", True),
                                 ("exit_buffer_seconds", 0), ("session_source", "guessed"),
                                 ("latest_exit_at", "2026-10-14T15:00:00+00:00"),
                                 ("session_exit_at", "2026-10-14T20:00:00+00:00"),
                                 ("entry_deadline", "2026-10-14T14:00:00+00:00")):
            self.assertFalse(timing.validate_trade_timing(dict(plan, **{key: replacement}), self.now), key)
        self.assertFalse(timing.validate_trade_timing(dict(plan, extra=True), self.now))
        self.assertFalse(timing.validate_trade_timing(plan, self.now, self.now))

    def test_expired_report_can_render_but_entry_remains_forbidden(self):
        plan = self.plan()
        later = self.now + timedelta(hours=8)
        self.assertFalse(timing.validate_trade_timing(plan, later))
        self.assertTrue(timing.validate_trade_timing(plan, later, require_entry_open=False))

    def test_actual_exit_deadline_never_adds_entry_window_to_confirmed_holding_time(self):
        plan = self.plan()
        for seconds in (0, 17, 30):
            entered = self.now + timedelta(seconds=seconds)
            self.assertEqual(timing.actual_exit_deadline(plan, entered), entered + timedelta(hours=1))
        with self.assertRaises(timing.TradeTimingError) as caught:
            timing.actual_exit_deadline(plan, self.now + timedelta(seconds=31))
        self.assertEqual(caught.exception.reason, "entry_window_expired")


if __name__ == "__main__":
    unittest.main()
