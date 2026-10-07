from copy import deepcopy
from datetime import datetime, timedelta
import os
import unittest
from unittest.mock import patch

from bot import mtf_presentation as presentation, mtf_runtime, multi_timeframe
from tests import test_mtf_runtime as fixtures
from tests import test_multi_timeframe as candle_fixtures


class MtfPresentationTests(unittest.TestCase):
    def setUp(self):
        self.feed, self.candidate, self.now = fixtures.synthetic_case()

    def assert_no_trade(self, text):
        self.assertNotRegex(text, r"(?i)\b(?:BUY|SELL)\b")
        for marker in ("الدخول المقترح:", "منطقة الدخول:", "SL:", "TP1:", "TP2:"):
            self.assertNotIn(marker, text)

    def qualified(self, feed=None):
        result = mtf_runtime.evaluate_feed(self.feed if feed is None else feed, self.now)
        self.assertEqual(result["state"], "signal", result)
        return result

    def test_unqualified_alignment_and_feed_confidence_never_display_trade(self):
        with fixtures.pinned_synthetic_evidence(self.feed, self.now):
            for candidate in (self.candidate, {**self.candidate, "qualification_id": "f" * 64,
                                               "confidence": .999, "evidence_metrics": [{"wins": 1000, "trades": 1000, "lower_95": .99}]}):
                text = presentation.format_proposal(candidate)
                self.assert_no_trade(text)
                self.assertIn("لا توجد فرصة مؤكدة الشروط", text)
                self.assertIn("70%", text)
                self.assertIn("200", text)

    def test_qualified_proposal_keeps_levels_and_reports_empirical_uncertainty(self):
        with fixtures.pinned_synthetic_evidence(self.feed, self.now):
            result = self.qualified()
            original = deepcopy(result)
            text = presentation.format_proposal(result, symbol="XAUUSD", manual_ticket_enabled=True)
            for marker in ("شراء BUY", "XAUUSD", "M15", "M5", "M1", "شموع مكتملة",
                           "SL:", "TP1:", "TP2:", "180/200", "95%", "60 دقيقة",
                           "موافقتك اليدوية لكل صفقة", "بنفسك", "الوقف والهدف ثابتين",
                           "ليست احتمالاً مثبتاً لهذه الصفقة", "التكاليف",
                           "نافذة الدخول 10 ثوانٍ من إغلاق M1", "الإلغاء: مرور 10 ثوانٍ بعد إغلاق M1",
                           "إن فاتت النافذة انتظر إشارة جديدة"):
                self.assertIn(marker, text)
            for key in ("entry", "stop", "target", "target2", "entry_zone_low", "entry_zone_high"):
                self.assertIn(f"{result[key]:.3f}", text)
            self.assertEqual(result, original)
            self.assertNotIn("http", text)

    def test_expired_entry_window_explains_waiting_even_with_fresh_market_data(self):
        with fixtures.pinned_synthetic_evidence(self.feed, self.now):
            result = self.qualified()
            clock = self.now + timedelta(seconds=11)
            feed = deepcopy(self.feed)
            feed["as_of"] = feed["risk_context"]["as_of"] = clock.isoformat()
            feed["quote"]["time"] = clock.isoformat()
            with patch.object(mtf_runtime, "_clock", return_value=clock):
                for text in (presentation.format_proposal(result), presentation.format_analysis(feed, result, clock),
                             presentation.status_text({"state": "blocked", "reason": "entry_window_expired"})):
                    self.assert_no_trade(text)
                    self.assertIn("10 ثوانٍ فقط بعد إغلاق شمعة M1", text)
                    self.assertIn("انتظر إشارة جديدة", text)

    def test_sell_and_automatic_flag_still_require_final_human_action(self):
        feed = candle_fixtures.MultiTimeframeTests().feed(sell=True)
        with fixtures.pinned_synthetic_evidence(feed, self.now):
            result = self.qualified(feed)
            text = presentation.format_proposal(result, execution_enabled=True, manual_ticket_enabled=True)
            self.assertIn("بيع SELL", text)
            self.assertIn("موافقتك اليدوية لكل صفقة", text)
            self.assertIn("بنفسك", text)
            self.assertNotIn("تنفيذ تلقائي", text)

    def test_high_risk_status_has_reason_without_trade(self):
        for reason in ("equity_risk_limit", "excessive_or_unknown_spread", "existing_exposure", "low_tick_activity"):
            with self.subTest(reason=reason):
                text = presentation.format_proposal({"state": "blocked", "reason": reason})
                self.assertIn("فرصة عالية المخاطر", text)
                self.assertIn("السبب:", text)
                self.assert_no_trade(text)

    def test_waiting_trend_missing_costs_warmup_and_stale_are_explicit(self):
        for state, reason, explanation in (("no_signal", "m5_pullback_not_confirmed", "M5"),
                                            ("no_signal", "m1_breakout_not_confirmed", "M1"),
                                            ("blocked", "unverified_costs", "غير موثّقة"),
                                            ("warmup", "insufficient_contiguous_history", "22 شمعة"),
                                            ("stale", "stale_quote", "10 ثوانٍ")):
            with self.subTest(reason=reason):
                text = presentation.format_proposal({"state": state, "reason": reason})
                self.assertIn("لا توجد فرصة مؤكدة الشروط", text)
                self.assertIn(explanation, text)
                self.assert_no_trade(text)

    def test_chart_observations_are_closed_causal_and_identify_all_three_roles(self):
        with fixtures.pinned_synthetic_evidence(self.feed, self.now):
            text = presentation.format_analysis(self.feed, mtf_runtime.blocked(), self.now, include_proposal=False)
            for marker in ("M15 — الاتجاه العام: ميل صاعد", "M5 — تأكيد الفرصة: ميل صاعد",
                           "M1 — توقيت الدخول: ميل صاعد", "آخر إغلاق", "UTC", "عمق السوق غير متاحين"):
                self.assertIn(marker, text)
            self.assert_no_trade(text)
            self.assertNotIn("اجتازت بوابة الأدلة", text)

    def test_chart_proposal_once_or_gated_signals_reference(self):
        with fixtures.pinned_synthetic_evidence(self.feed, self.now):
            result = self.qualified()
            full = presentation.format_analysis(self.feed, result, self.now)
            self.assertEqual(full.count("شراء BUY"), 1)
            reference = presentation.format_analysis(self.feed, result, self.now, include_proposal=False)
            self.assertIn("اجتازت بوابة الأدلة", reference)
            self.assertIn("/signals", reference)
            self.assert_no_trade(reference)
            unqualified = presentation.format_analysis(self.feed, self.candidate, self.now, include_proposal=False)
            self.assertNotIn("اجتازت بوابة الأدلة", unqualified)
            self.assertIn("لا توجد فرصة مؤكدة الشروط", unqualified)

    def test_current_risk_broker_cost_or_drift_invalidates_cached_proposal(self):
        with fixtures.pinned_synthetic_evidence(self.feed, self.now):
            result = self.qualified()
            for change in ("exposure", "broker", "cost", "drift"):
                with self.subTest(change=change):
                    feed = deepcopy(self.feed)
                    if change == "exposure": feed["risk_context"]["open_positions"] = 1
                    elif change == "broker": feed["risk_context"]["broker_fingerprint"] = "b" * 64
                    elif change == "cost": feed["risk_context"]["slippage_price"] = .004
                    else: feed["quote"].update(bid=result["entry"] + .2, ask=result["entry"] + .202)
                    self.assert_no_trade(presentation.format_analysis(feed, result, self.now))
                    self.assertNotIn("اجتازت بوابة الأدلة", presentation.format_analysis(feed, result, self.now, include_proposal=False))

    def test_stale_quote_receipt_and_m1_timing_suppress_all_levels(self):
        with fixtures.pinned_synthetic_evidence(self.feed, self.now):
            result = self.qualified()
            for quote_shift in (11, -6):
                feed = deepcopy(self.feed)
                feed["quote"]["time"] = (self.now - timedelta(seconds=quote_shift)).isoformat()
                self.assert_no_trade(presentation.format_analysis(feed, result, self.now))
            for receipt_shift in (31, -1):
                self.assert_no_trade(presentation.format_analysis(self.feed, result, self.now,
                                                                 received_at=self.now - timedelta(seconds=receipt_shift)))
            clock = self.now + timedelta(seconds=76)
            feed = deepcopy(self.feed)
            for container in (feed, feed["risk_context"]): container["as_of"] = clock.isoformat()
            feed["quote"]["time"] = clock.isoformat()
            with patch.object(mtf_runtime, "_clock", return_value=clock):
                self.assert_no_trade(presentation.format_analysis(feed, result, clock))

    def test_nonfinite_nonpositive_crossed_quote_cannot_accompany_trade(self):
        with fixtures.pinned_synthetic_evidence(self.feed, self.now):
            result = self.qualified()
            for bid, ask in ((float("nan"), 2000.003), (2000.001, float("inf")), (0, 2000.003),
                             (2000.004, 2000.003), (True, 2000.003)):
                with self.subTest(bid=bid, ask=ask):
                    feed = deepcopy(self.feed)
                    feed["quote"].update(bid=bid, ask=ask)
                    text = presentation.format_analysis(feed, result, self.now)
                    self.assert_no_trade(text)
                    self.assertNotRegex(text.lower(), r"\b(?:nan|inf)\b")

    def test_bad_or_lookahead_history_never_claims_completed_trend(self):
        for failure in ("forming", "duplicate", "nonfinite", "lookahead", "stale_higher"):
            with self.subTest(failure=failure):
                feed = deepcopy(self.feed)
                if failure == "forming": feed["timeframes"]["M15"][-1]["time"] = self.now.isoformat()
                elif failure == "duplicate": feed["timeframes"]["M15"][-1]["time"] = feed["timeframes"]["M15"][-2]["time"]
                elif failure == "nonfinite": feed["timeframes"]["M15"][-1]["close"] = float("nan")
                else:
                    key, delta = ("M1", timedelta(minutes=1)) if failure == "lookahead" else ("M15", timedelta(minutes=15))
                    for bar in feed["timeframes"][key]:
                        bar["time"] = (datetime.fromisoformat(bar["time"]) - delta).isoformat()
                text = presentation.format_analysis(feed, self.candidate, self.now)
                self.assertNotIn("M15 — الاتجاه العام: ميل", text)
                self.assert_no_trade(text)

    def test_gap_resets_observations_and_shows_warmup_without_fabrication(self):
        feed = deepcopy(self.feed)
        for bar in feed["timeframes"]["M5"][:-21]:
            bar["time"] = (datetime.fromisoformat(bar["time"]) - timedelta(minutes=5)).isoformat()
        result = multi_timeframe.analyze_multi_timeframe(feed, self.now)
        text = presentation.format_analysis(feed, result, self.now)
        self.assertIn("M5 — تأكيد الفرصة: تجهيز السجل (21/22", text)
        self.assertNotIn("M5 — تأكيد الفرصة: ميل", text)
        self.assert_no_trade(text)

    def test_legacy_provisional_and_wrong_fingerprint_never_become_public(self):
        with fixtures.pinned_synthetic_evidence(self.feed, self.now):
            original = self.qualified()
            for key, value in (("strategy_id", "ema9-21-atr14-v1"), ("provisional", True),
                               ("strategy_fingerprint", "f" * 64), ("display_timeframe", "M15")):
                result = {**original, key: value}
                self.assert_no_trade(presentation.format_proposal(result))
                self.assert_no_trade(presentation.format_analysis(self.feed, result, self.now))

    def test_bad_collapsed_or_missing_levels_and_nonfinite_reward_risk_cannot_display(self):
        with fixtures.pinned_synthetic_evidence(self.feed, self.now):
            original = self.qualified()
            for key, value in (("stop", float("nan")), ("target", original["entry"]), ("target2", original["entry"]),
                               ("price_digits", True), ("entry_zone_low", original["entry_zone_high"]),
                               ("nominal_reward_risk", float("nan")), ("effective_reward_risk", float("inf"))):
                self.assert_no_trade(presentation.format_proposal({**original, key: value}))
            for key in ("stop", "target", "bar_time", "symbol"):
                result = deepcopy(original)
                del result[key]
                self.assert_no_trade(presentation.format_proposal(result))


class ExperimentalMtfPresentationTests(unittest.TestCase):
    def setUp(self):
        self.feed, _, self.now = fixtures.synthetic_case()
        self.feed["risk_context"]["costs_verified"] = False
        mode = patch.dict(os.environ, {"MT5_SIGNAL_MODE": "experimental_demo"})
        mode.start()
        self.addCleanup(mode.stop)
        clock = patch.object(mtf_runtime, "_clock", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def proposal(self, feed=None):
        result = mtf_runtime.evaluate_feed(self.feed if feed is None else feed, self.now)
        self.assertEqual(result["state"], "signal", result)
        return result

    def assert_no_trade(self, text):
        self.assertNotRegex(text, r"(?i)\b(?:BUY|SELL)\b")
        for marker in ("الدخول المقترح:", "منطقة الدخول:", "SL:", "TP1:", "TP2:"):
            self.assertNotIn(marker, text)

    def test_experimental_proposal_discloses_unproven_performance_cost_values_and_manual_window(self):
        result = self.proposal()
        original = deepcopy(result)
        text = presentation.format_proposal(result, manual_ticket_enabled=True)
        for marker in ("إشارة Demo تجريبية — الأداء غير مثبت", "شراء BUY", "Demo فقط", "الحجم 0.01",
                       "حتى 1% من حقوق الحساب (Equity)", "افتراضات تقديرية غير موثّقة", "عملة الحساب", "لكل جهة",
                       "ارتداد M5 مؤكّد خلال آخر 3 شموع قبل شمعة التأكيد", "نافذة الدخول 30 ثانية من إغلاق M1",
                       "الإلغاء: مرور 30 ثانية بعد إغلاق M1", "التجهيز خلال 30 ثانية",
                       "Buy أو Sell بنفسك", "الوقف والهدف ثابتين"):
            self.assertIn(marker, text)
        for key in ("commission_round_turn", "slippage_price"):
            self.assertIn(f"{result['cost_assumptions'][key]:.8g}", text)
        for marker in ("إشارة مؤهلة", "اختبار خارج العينة", "95%", "النسبة الملاحظة", "تقدير تاريخي"):
            self.assertNotIn(marker, text)
        self.assertEqual(result, original)

    def test_sell_and_automatic_flag_keep_explicit_demo_human_action(self):
        feed = candle_fixtures.MultiTimeframeTests().feed(sell=True)
        feed["risk_context"]["costs_verified"] = False
        text = presentation.format_proposal(self.proposal(feed), execution_enabled=True, manual_ticket_enabled=True)
        self.assertIn("بيع SELL", text)
        self.assertIn("الأداء غير مثبت", text)
        self.assertIn("موافقتك اليدوية لكل صفقة", text)
        self.assertIn("بنفسك", text)
        self.assertNotIn("تنفيذ تلقائي", text)

    def test_experimental_analysis_reference_does_not_claim_evidence_qualification(self):
        result = self.proposal()
        full = presentation.format_analysis(self.feed, result, self.now)
        self.assertEqual(full.count("شراء BUY"), 1)
        reference = presentation.format_analysis(self.feed, result, self.now, include_proposal=False)
        for marker in ("إشارة Demo تجريبية", "الأداء غير مثبت", "التكاليف تقديرية", "/signals"):
            self.assertIn(marker, reference)
        self.assert_no_trade(reference)
        self.assertNotIn("بوابة الأدلة", reference)

    def test_expired_experimental_entry_window_keeps30second_explanation(self):
        result = self.proposal()
        clock = self.now + timedelta(seconds=31)
        feed = deepcopy(self.feed)
        feed["as_of"] = feed["risk_context"]["as_of"] = feed["quote"]["time"] = clock.isoformat()
        with patch.object(mtf_runtime, "_clock", return_value=clock):
            for text in (presentation.format_proposal(result), presentation.format_analysis(feed, result, clock)):
                self.assert_no_trade(text)
                self.assertIn("30 ثانية فقط بعد إغلاق شمعة M1", text)
                self.assertIn("انتظر إشارة جديدة", text)
                self.assertIn("الأداء غير مثبت", text)
                self.assertNotIn("10 ثوانٍ فقط بعد إغلاق شمعة M1", text)

    def test_experimental_waiting_and_stale_data_remain_identified(self):
        waiting = {"state": "no_signal", "reason": "m5_pullback_not_confirmed", "signal_mode": "experimental_demo",
                   "provisional": True, "entry_window_seconds": 30}
        text = presentation.status_text(waiting)
        self.assertIn("لا توجد إشارة Demo تجريبية مستوفية الشروط", text)
        self.assertIn("الأداء غير مثبت", text)
        self.assertIn("افتراضات تقديرية غير موثّقة", text)
        self.assert_no_trade(text)
        result = self.proposal()
        clock = self.now + timedelta(seconds=11)
        text = presentation.format_analysis(self.feed, result, clock)
        self.assertIn("الأداء غير مثبت", text)
        self.assertIn("بيانات MT5 غير حديثة", text)
        self.assert_no_trade(text)

    def test_corrupt_estimated_cost_metadata_suppresses_trade_and_any_evidence_claims(self):
        original = self.proposal()
        for change in ("missing", "nonfinite", "certification"):
            with self.subTest(change=change):
                result = deepcopy(original)
                if change == "missing":
                    del result["cost_assumptions"]
                elif change == "nonfinite":
                    result["cost_assumptions"]["commission_round_turn"] = float("nan")
                else:
                    result["qualification_id"] = "f" * 64
                    result["evidence_metrics"] = [{"scenario": "fabricated", "wins": 200, "trades": 200,
                                                   "win_rate": 1.0, "lower_95": .99}]
                text = presentation.format_proposal(result)
                self.assert_no_trade(text)
                self.assertIn("الأداء غير مثبت", text)
                self.assertNotIn("اختبار خارج العينة", text)
                self.assertNotIn("95%", text)


if __name__ == "__main__": unittest.main()
