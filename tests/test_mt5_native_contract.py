"""Native draft profile validation only; no SDK or native window actions."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import unittest

from bridge import mt5_native_ticket as native
from tests.test_mt5_chart_overlay import NOW, experimental_proposal, proposal


def draft(*, experimental=False):
    value = experimental_proposal() if experimental else proposal()
    return native.Draft(
        "XAUUSD", "BUY", Decimal("0.01"), Decimal("2490"), Decimal("2520"), 2,
        NOW + timedelta(seconds=30 if experimental else 10), display_timeframe="M1",
        **{key: value[key] for key in ("strategy_id", "strategy_version", "policy_id", "horizon_seconds",
                                     "strategy_fingerprint", "context_bar_times", "timeframe_context",
                                     "broker_utc_offset_minutes")},
        qualification_id=value.get("qualification_id", ""),
        signal_mode="experimental_demo" if experimental else "qualified",
        provisional=experimental, entry_window_seconds=30 if experimental else 10,
        cost_assumptions=value.get("cost_assumptions"),
        direction_bar_time=NOW - timedelta(minutes=15),
        confirmation_bar_time=NOW - timedelta(minutes=5), bar_time=NOW - timedelta(minutes=1),
    )


class NativeFourFrameContractTests(unittest.TestCase):
    def test_current_qualified_and_experimental_drafts_need_no_h4_reference(self):
        for experimental in (False, True):
            value = draft(experimental=experimental)
            self.assertEqual(set(value.context_bar_times), {"M30"})
            self.assertEqual(set(value.timeframe_context["trends"]), {"M30", "M15", "M5", "M1"})
            self.assertEqual(value.timeframe_context["levels_timeframe"], "M30")
            native.require_qualified(value, NOW)

    def test_old_five_frame_profiles_cannot_prepare_a_ticket(self):
        for experimental in (False, True):
            value = replace(draft(experimental=experimental),
                            strategy_id="mtf-ema-pullback-60m-demo-v3" if experimental else "mtf-ema-pullback-60m-v2",
                            strategy_version=3 if experimental else 2,
                            policy_id="mtf-manual-demo-estimated-cost-risk-v3" if experimental else "mtf-manual-demo-cost-risk-v2")
            with self.subTest(experimental=experimental), self.assertRaises(native.TicketError):
                native.require_qualified(value, NOW)

    def test_prior_h1_profiles_cannot_prepare_a_ticket_with_new_m30_context(self):
        for experimental in (False, True):
            value = replace(draft(experimental=experimental),
                            strategy_id="mtf-ema-pullback-60m-demo-v5" if experimental else "mtf-ema-pullback-60m-v4",
                            strategy_version=5 if experimental else 4,
                            policy_id="mtf-manual-demo-estimated-cost-risk-v5" if experimental else "mtf-manual-demo-cost-risk-v4")
            with self.subTest(experimental=experimental), self.assertRaises(native.TicketError):
                native.require_qualified(value, NOW)

    def test_old_h1_context_cannot_be_relabelled_as_current_m30_profile(self):
        for mutation in ("reference", "trend", "levels"):
            value = deepcopy(draft())
            if mutation == "reference":
                value.context_bar_times["H1"] = value.context_bar_times.pop("M30")
            elif mutation == "trend":
                value.timeframe_context["trends"]["H1"] = value.timeframe_context["trends"].pop("M30")
            else:
                value.timeframe_context["levels_timeframe"] = "H1"
            with self.subTest(mutation=mutation), self.assertRaises(native.TicketError):
                native.require_qualified(value, NOW)

    def test_m30_context_is_mandatory_and_old_h4_fields_are_rejected(self):
        for mutation in ("missing_m30", "old_h4_reference", "old_h4_trend", "missing_levels_source", "old_levels_source"):
            value = deepcopy(draft())
            if mutation == "missing_m30":
                value.context_bar_times.pop("M30")
            elif mutation == "old_h4_reference":
                value.context_bar_times["H4"] = (NOW - timedelta(hours=4)).isoformat()
            elif mutation == "old_h4_trend":
                value.timeframe_context["trends"]["H4"] = "BUY"
            elif mutation == "missing_levels_source":
                value.timeframe_context.pop("levels_timeframe")
            else:
                value.timeframe_context["levels_timeframe"] = "H4"
            with self.subTest(mutation=mutation), self.assertRaises(native.TicketError):
                native.require_qualified(value, NOW)


if __name__ == "__main__":
    unittest.main()
