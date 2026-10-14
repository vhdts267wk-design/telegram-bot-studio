"""Display validation and local-file tests; no SDK, UI or network access."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bridge import mt5_chart_overlay as chart


NOW = datetime(2026, 10, 5, 12, 30, tzinfo=timezone.utc)
EXECUTION = {"tick_size": 0.01, "point": 0.01, "digits": 2, "stops_level": 10}
QUOTE = {"bid": 2499.9, "ask": 2500.1, "time": NOW.isoformat()}


def proposal():
    return {
        "version": 2, "workflow": "chart_overlay",
        "offer_id": "11111111-2222-4333-8444-555555555555", "status": "offered",
        "symbol": "XAUUSD", "timeframe": "M1", "direction": "BUY",
        "entry": 2500.0, "entry_zone_low": 2499.0, "entry_zone_high": 2501.0,
        "stop": 2490.0, "target": 2520.0, "price_digits": 2,
        "execution": deepcopy(EXECUTION), "bar_time": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=4)).isoformat(),
        "strategy_id": "mtf-ema-pullback-60m-v6", "strategy_version": 6,
        "policy_id": "mtf-manual-demo-cost-risk-v6", "horizon_seconds": 3600,
        "strategy_fingerprint": "a" * 64, "qualification_id": "b" * 64,
        "direction_bar_time": (NOW - timedelta(minutes=15)).isoformat(),
        "confirmation_bar_time": (NOW - timedelta(minutes=5)).isoformat(),
        "broker_utc_offset_minutes": 180,
        "context_bar_times": {frame: datetime.fromtimestamp(chart.latest_closed_reference(
            NOW.timestamp(), seconds, 180), timezone.utc).isoformat() for frame, seconds in (("M30", 1800),)},
        "timeframe_context": {"trends": {frame: "BUY" for frame in chart.FRAME_SECONDS}, "alignment": "aligned",
                              "confidence": "aligned", "counter_trend": False, "levels_timeframe": "M30",
                              "support": 2490.0, "resistance": 2520.0},
    }


def experimental_proposal():
    value = proposal()
    value.pop("qualification_id")
    value.update(strategy_id="mtf-ema-pullback-60m-demo-v7", strategy_version=7,
                 policy_id="mtf-manual-demo-estimated-cost-risk-v7", signal_mode="experimental_demo",
                 provisional=True, entry_window_seconds=30, expires_at=(NOW + timedelta(seconds=30)).isoformat(),
                 cost_assumptions={"verified": False, "method": "spread_tick_floor_v1", "commission_round_turn": 0.2,
                                   "slippage_price": 0.1, "spread_price": 0.2, "tick_size": 0.01})
    return value


def readiness_feed(captured=NOW):
    frames = {}
    for frame, seconds in chart.FRAME_SECONDS.items():
        latest = chart.latest_closed_reference(captured.timestamp(), seconds, 180)
        frames[frame] = [{"time": datetime.fromtimestamp(latest - (63 - index) * seconds, timezone.utc).isoformat(),
                          "open": 2500.0, "high": 2501.0, "low": 2499.0, "close": 2500.5,
                          "tick_volume": 100} for index in range(64)]
    return {"source": "MetaTrader 5", "schema_version": 3, "symbol": "XAUUSD", "as_of": captured.isoformat(),
            "broker_utc_offset_minutes": 180, "execution": deepcopy(EXECUTION),
            "quote": dict(QUOTE, time=captured.isoformat()), "timeframes": frames}


class ReadinessValidationTests(unittest.TestCase):
    def validate(self, feed, *, now=NOW):
        return chart.feed_readiness(feed, symbol="XAUUSD", broker_offset_minutes=180, observed_at=now)

    def test_counts_are_actual_closed_contiguous_suffixes_and_empty_frame_is_zero(self):
        feed = readiness_feed()
        for row in feed["timeframes"]["M30"][:-10]:
            row["time"] = (chart.utc(row["time"]) - timedelta(hours=1)).isoformat()
        feed["timeframes"]["M15"] = []
        counts, deadline = self.validate(feed)
        self.assertEqual(counts, [10, 0, 64, 64])
        self.assertEqual(deadline, NOW + timedelta(seconds=10))

    def test_capture_boundary_rather_than_later_receipt_defines_closed_history(self):
        feed = readiness_feed(NOW - timedelta(seconds=5))
        self.assertEqual(self.validate(feed)[0], [64] * 4)
        # A candle closed after capture is lookahead even though receipt is later.
        feed["timeframes"]["M1"][-1] = readiness_feed()["timeframes"]["M1"][-1]
        with self.assertRaises(chart.OverlayError):
            self.validate(feed)

    def test_source_quote_and_capture_bound_expiry_without_receipt_renewal(self):
        feed = readiness_feed(NOW - timedelta(seconds=4))
        feed["quote"]["time"] = (NOW - timedelta(seconds=3)).isoformat()
        self.assertEqual(self.validate(feed)[1], NOW + timedelta(seconds=6))
        self.assertEqual(self.validate(feed, now=NOW + timedelta(seconds=2))[1], NOW + timedelta(seconds=6))
        feed = readiness_feed()
        feed["quote"]["time"] = (NOW + timedelta(seconds=5)).isoformat()
        self.assertEqual(self.validate(feed)[1], NOW + timedelta(seconds=10))

    def test_future_or_stale_capture_quote_and_naive_timestamps_fail_closed(self):
        for field, offset in (("as_of", 0.001), ("as_of", -10), ("quote", -10), ("quote", 5.001)):
            feed = readiness_feed()
            stamp = (NOW + timedelta(seconds=offset)).isoformat()
            if field == "quote":
                feed["quote"]["time"] = stamp
            else:
                feed[field] = stamp
            with self.subTest(field=field, offset=offset), self.assertRaises(chart.OverlayError):
                self.validate(feed)
        feed = readiness_feed()
        feed["as_of"] = NOW.replace(tzinfo=None).isoformat()
        with self.assertRaises(chart.OverlayError):
            self.validate(feed)

    def test_malformed_feed_or_history_never_becomes_readiness(self):
        mutations = [
            lambda f: f.update(source="reference"),
            lambda f: f.update(schema_version=True),
            lambda f: f.update(schema_version=2),
            lambda f: f.update(symbol="GOLD"),
            lambda f: f.update(broker_utc_offset_minutes=0),
            lambda f: f["timeframes"].update(H4=[]),
            lambda f: f["timeframes"].update(H1=f["timeframes"].pop("M30")),
            lambda f: f["timeframes"].update(M1=f["timeframes"]["M1"] * 2),
            lambda f: f["timeframes"]["M1"][-1].update(time=f["timeframes"]["M1"][-2]["time"]),
            lambda f: f["timeframes"]["M1"][-1].update(time=(NOW - timedelta(seconds=59)).isoformat()),
            lambda f: f["timeframes"]["M30"].pop(),
            lambda f: f["timeframes"]["M5"][-1].update(tick_volume=True),
            lambda f: f["timeframes"]["M15"][-1].update(high=2498.0),
            lambda f: f["timeframes"]["M1"][-1].update(close=float("nan")),
            lambda f: f["quote"].update(bid=2501.0),
            lambda f: f["execution"].update(tick_size=0),
        ]
        for index, mutate in enumerate(mutations):
            feed = readiness_feed()
            mutate(feed)
            with self.subTest(index=index), self.assertRaises(chart.OverlayError):
                self.validate(feed)


class ValidationTests(unittest.TestCase):
    def validate(self, value=None, *, execution=None, quote=None, now=NOW):
        return chart.validate_proposal(value if value is not None else proposal(), symbol="XAUUSD",
                                       execution=execution or EXECUTION, quote=quote or QUOTE, observed_at=now,
                                       broker_offset_minutes=180)

    def test_experimental_profile_requires_distinct_ids_explicit_estimates_and_window(self):
        value = experimental_proposal()
        later = NOW + timedelta(seconds=20)
        self.assertTrue(self.validate(value, quote=dict(QUOTE, time=later.isoformat()), now=later).experimental)
        self.assertFalse(self.validate().experimental)
        for field, changed in (("strategy_id", "mtf-ema-pullback-60m-v6"), ("provisional", False),
                               ("strategy_version", 1), ("entry_window_seconds", 10),
                               ("qualification_id", "b" * 64), ("cost_assumptions", None),
                               ("expires_at", (NOW + timedelta(seconds=31)).isoformat())):
            invalid = experimental_proposal()
            invalid[field] = changed
            with self.subTest(field=field), self.assertRaises(chart.OverlayError):
                self.validate(invalid)
        for field, changed in (("verified", True), ("method", "verified"), ("slippage_price", 0), ("tick_size", 0.02)):
            invalid = experimental_proposal()
            invalid["cost_assumptions"][field] = changed
            with self.subTest(field=field), self.assertRaises(chart.OverlayError):
                self.validate(invalid)
        late = NOW + timedelta(seconds=31)
        with self.assertRaises(chart.OverlayError):
            self.validate(value, quote=dict(QUOTE, time=late.isoformat()), now=late)

    def test_buy_sell_and_all_active_states_preserve_reference_levels(self):
        for status in chart.STATES:
            value = proposal()
            value["status"] = status
            self.assertEqual(self.validate(value).entry, Decimal("2500"))
        value = proposal()
        value.update(direction="SELL", stop=2510.0, target=2480.0)
        value["timeframe_context"]["trends"] = {frame: "SELL" for frame in chart.FRAME_SECONDS}
        result = self.validate(value)
        self.assertEqual((result.direction, result.stop, result.target), ("SELL", Decimal("2510"), Decimal("2480")))

    def test_workflow_symbol_state_and_unknown_fields_are_rejected(self):
        for field, value in (("version", True), ("workflow", "manual_ticket"), ("status", "failed"),
                             ("status", []), ("symbol", "OTHER"), ("timeframe", "M15"),
                             ("direction", "CLOSE"), ("offer_id", "not-a-uuid"), ("claim_id", "never-required")):
            changed = proposal()
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(chart.OverlayError):
                self.validate(changed)

    def test_legacy_or_unqualified_proposals_are_not_displayed(self):
        for field, value in (("version", 1), ("strategy_id", "old-ema-cross"),
                             ("strategy_version", True), ("policy_id", "other-policy"),
                             ("horizon_seconds", 900), ("strategy_fingerprint", "A" * 64),
                             ("qualification_id", ""), ("qualification_id", "b" * 63),
                             ("direction_bar_time", NOW.isoformat()),
                             ("confirmation_bar_time", NOW.isoformat())):
            changed = proposal()
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(chart.OverlayError):
                self.validate(changed)
        changed = proposal()
        del changed["qualification_id"]
        with self.assertRaises(chart.OverlayError):
            self.validate(changed)

    def test_four_frame_context_and_offset_causal_m30_reference_are_required(self):
        mutations = [
            lambda value: value.pop("context_bar_times"),
            lambda value: value["context_bar_times"].pop("M30"),
            lambda value: value["context_bar_times"].update(M30=NOW.isoformat()),
            lambda value: value["context_bar_times"].update(H4=NOW.replace(hour=8, minute=0).isoformat()),
            lambda value: value.update(broker_utc_offset_minutes=0),
            lambda value: value.update(broker_utc_offset_minutes=True),
            lambda value: value["timeframe_context"]["trends"].pop("M30"),
            lambda value: value["timeframe_context"]["trends"].update(H4="SELL"),
            lambda value: value["timeframe_context"]["trends"].update(M30="NEUTRAL"),
            lambda value: value["timeframe_context"].pop("levels_timeframe"),
            lambda value: value["timeframe_context"].update(levels_timeframe="H4"),
            lambda value: value["timeframe_context"].update(alignment="counter_trend", confidence="reduced", counter_trend=True),
            lambda value: value["timeframe_context"].update(support=float("nan")),
            lambda value: value["timeframe_context"].update(resistance=True),
            lambda value: value.update(strategy_id="mtf-ema-pullback-60m-v1", strategy_version=1),
        ]
        for mutate in mutations:
            value = proposal()
            mutate(value)
            with self.subTest(mutation=mutate), self.assertRaises(chart.OverlayError):
                self.validate(value)
        self.assertEqual(set(proposal()["context_bar_times"]), {"M30"})
        self.assertEqual(chart.utc(proposal()["context_bar_times"]["M30"]).hour, 12)
        self.validate()

    def test_old_five_frame_strategy_profiles_are_rejected_even_with_current_context(self):
        for experimental in (False, True):
            value = experimental_proposal() if experimental else proposal()
            value.update(strategy_id="mtf-ema-pullback-60m-demo-v3" if experimental else "mtf-ema-pullback-60m-v2",
                         strategy_version=3 if experimental else 2,
                         policy_id="mtf-manual-demo-estimated-cost-risk-v3" if experimental else "mtf-manual-demo-cost-risk-v2")
            with self.subTest(experimental=experimental), self.assertRaises(chart.OverlayError):
                self.validate(value)

    def test_prior_h1_profiles_and_context_are_rejected_even_with_fresh_quotes(self):
        for experimental in (False, True):
            value = experimental_proposal() if experimental else proposal()
            value.update(strategy_id="mtf-ema-pullback-60m-demo-v5" if experimental else "mtf-ema-pullback-60m-v4",
                         strategy_version=5 if experimental else 4,
                         policy_id="mtf-manual-demo-estimated-cost-risk-v5" if experimental else "mtf-manual-demo-cost-risk-v4")
            with self.subTest(experimental=experimental), self.assertRaises(chart.OverlayError):
                self.validate(value)
        for mutation in ("reference", "trend", "levels"):
            value = proposal()
            if mutation == "reference":
                value["context_bar_times"]["H1"] = value["context_bar_times"].pop("M30")
            elif mutation == "trend":
                value["timeframe_context"]["trends"]["H1"] = value["timeframe_context"]["trends"].pop("M30")
            else:
                value["timeframe_context"]["levels_timeframe"] = "H1"
            with self.subTest(mutation=mutation), self.assertRaises(chart.OverlayError):
                self.validate(value)

    def test_local_context_uses_only_four_histories_and_m30_levels(self):
        frames = {}
        for frame, seconds in chart.FRAME_SECONDS.items():
            last = chart.latest_closed_reference(NOW.timestamp(), seconds, 180)
            frames[frame] = [
                {"time": datetime.fromtimestamp(last - (63 - index) * seconds, timezone.utc).isoformat(),
                 "open": 2493.7 + index * .1, "high": 2494.2 + index * .1,
                 "low": 2493.2 + index * .1, "close": 2493.7 + index * .1, "tick_volume": 100}
                for index in range(64)
            ]
        feed = {"schema_version": 3, "timeframes": frames, "quote": deepcopy(QUOTE), "broker_utc_offset_minutes": 180}
        result = chart.current_timeframe_context(feed, observed_at=NOW, broker_offset_minutes=180)
        self.assertEqual(set(result["trends"]), {"M30", "M15", "M5", "M1"})
        self.assertEqual(result["alignment"], "aligned")
        self.assertEqual(result["levels_timeframe"], "M30")
        self.assertEqual(result["support"], min(row["low"] for row in frames["M30"][-20:]))
        self.assertEqual(result["resistance"], max(row["high"] for row in frames["M30"][-20:]))
        self.assertEqual(chart.FRAME_SECONDS["M30"], 1800)
        self.assertEqual(chart.utc(proposal()["context_bar_times"]["M30"]).minute, 0)
        old_schema = dict(feed, schema_version=2)
        with self.assertRaises(chart.OverlayError):
            chart.current_timeframe_context(old_schema, observed_at=NOW, broker_offset_minutes=180)
        old_frames = deepcopy(feed)
        old_frames["timeframes"]["H1"] = old_frames["timeframes"].pop("M30")
        with self.assertRaises(chart.OverlayError):
            chart.current_timeframe_context(old_frames, observed_at=NOW, broker_offset_minutes=180)
        # Reject an old five-frame envelope rather than depending on its H4 history.
        frames["H4"] = []
        with self.assertRaises(chart.OverlayError):
            chart.current_timeframe_context(feed, observed_at=NOW, broker_offset_minutes=180)

    def test_original_reference_bars_remain_fixed_until_the_original_expiry(self):
        later = NOW + timedelta(minutes=2)
        quote = dict(QUOTE, time=later.isoformat())
        value = proposal()
        self.assertEqual(self.validate(value, quote=quote, now=later).bar, NOW - timedelta(minutes=1))
        # Updating a reference to a different timeframe bar cannot renew a
        # frozen proposal, even when the updated bar has since completed.
        value["confirmation_bar_time"] = (NOW - timedelta(minutes=10)).isoformat()
        with self.assertRaises(chart.OverlayError):
            self.validate(value, quote=quote, now=later)

    def test_invalid_prices_changed_metadata_and_unsupported_precision_are_rejected(self):
        for field, value in (("entry", True), ("stop", float("nan")), ("target", float("inf")),
                             ("entry_zone_low", -1), ("entry", 2500.005), ("entry_zone_high", 2501.1),
                             ("stop", 2521), ("price_digits", 3)):
            changed = proposal()
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(chart.OverlayError):
                self.validate(changed)
        changed = deepcopy(EXECUTION)
        changed["stops_level"] += 1
        with self.assertRaises(chart.OverlayError):
            self.validate(execution=changed)
        for digits in (9, 10, True):
            changed = proposal()
            changed["execution"]["digits"] = digits
            with self.subTest(digits=digits), self.assertRaises(chart.OverlayError):
                self.validate(changed)

    def test_entry_zone_is_exact_inward_tick_rounding_and_never_collapsed(self):
        value = proposal()
        value.update(stop=2490.05, entry_zone_low=2499.01, entry_zone_high=2500.99)
        self.assertEqual(self.validate(value).zone_low, Decimal("2499.01"))
        value["entry_zone_low"] = 2499.0
        with self.assertRaises(chart.OverlayError):
            self.validate(value)
        value.update(stop=2499.99, entry_zone_low=2500.0, entry_zone_high=2500.0)
        with self.assertRaises(chart.OverlayError):
            self.validate(value)

    def test_reference_band_remains_visible_when_price_is_outside_it(self):
        quote = {"bid": 2510.0, "ask": 2510.2, "time": NOW.isoformat()}
        self.assertEqual(self.validate(quote=quote).entry, Decimal("2500"))

    def test_quote_source_freshness_and_crossed_prices_are_checked(self):
        for seconds in (-11, 6):
            quote = dict(QUOTE, time=(NOW + timedelta(seconds=seconds)).isoformat())
            with self.subTest(seconds=seconds), self.assertRaises(chart.OverlayError):
                self.validate(quote=quote)
        self.validate(quote=dict(QUOTE, time=(NOW + timedelta(seconds=5)).isoformat()))
        with self.assertRaises(chart.OverlayError):
            self.validate(quote=dict(QUOTE, bid=2501))

    def test_original_expiry_and_completed_m1_bar_cannot_be_renewed(self):
        for field, value in (("expires_at", NOW.isoformat()),
                             ("expires_at", (NOW + timedelta(minutes=6)).isoformat()),
                             ("bar_time", NOW.isoformat()),
                             ("bar_time", (NOW - timedelta(minutes=60)).isoformat()),
                             ("bar_time", (NOW - timedelta(minutes=15, seconds=1)).isoformat()),
                             ("bar_time", "2026-10-05T12:15:00")):
            changed = proposal()
            changed[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(chart.OverlayError):
                self.validate(changed)


class ExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.common, self.data = self.base / "Common", self.base / ("A" * 32)
        self.common.mkdir()
        self.data.mkdir()
        self.account = SimpleNamespace(server="Synthetic-Demo-Ω", login=98765432101234)
        self.exporter = chart.ChartExporter(self.common, self.data, symbol="XAUUSD", broker_offset_minutes=180,
                                            account=self.account, nonce="12" * 16)

    def publish(self, value=None, *, quote=None, now=NOW):
        self.exporter.publish(value if value is not None else proposal(), execution=EXECUTION,
                              quote=quote or QUOTE, observed_at=now)
        return self.exporter.path.read_bytes()

    def test_active_row_has_exact_18_fields_safe_path_and_unchanged_prices(self):
        raw = self.publish()
        self.assertNotIn(b"\n", raw)
        self.assertNotIn(b"\r", raw)
        self.assertNotIn(b'"', raw)
        fields = raw.decode("ascii").split(";")
        self.assertEqual(len(fields), 18)
        self.assertEqual(fields[:11], ["2", "active", "XAUUSD", "M1", "BUY", "2500.00", "2499.00", "2501.00", "2490.00", "2520.00", "2"])
        self.assertEqual(fields[14:16], ["180", "a" * 32])
        self.assertEqual(self.exporter.path, self.common / "Files" / "MT5Bot" / ("levels_" + "a" * 32 + ".csv"))
        self.assertNotIn(proposal()["offer_id"], raw.decode("ascii"))
        self.assertNotIn(str(self.account.login), raw.decode("ascii"))
        self.assertNotIn(self.account.server.encode("utf-8"), raw)

    def test_experimental_row_keeps_eighteen_fields_and_is_explicitly_marked(self):
        value = experimental_proposal()
        fields = self.publish(value).decode("ascii").split(";")
        self.assertEqual(len(fields), 18)
        self.assertEqual(fields[:5], ["2", "experimental", "XAUUSD", "M1", "BUY"])
        self.assertEqual(fields[5:10], ["2500.00", "2499.00", "2501.00", "2490.00", "2520.00"])
        late = NOW + timedelta(seconds=25)
        fields = self.publish(value, quote=dict(QUOTE, time=late.isoformat()), now=late).decode("ascii").split(";")
        self.assertEqual(int(fields[12]), int(NOW.timestamp()) + 30)
        self.exporter.clear(late)
        self.assertEqual(self.exporter.path.read_text(encoding="ascii").split(";")[1], "waiting")

    def test_nonce_hash_uses_exact_utf8_account_binding_without_final_newline(self):
        fields = self.publish().decode("ascii").split(";")
        expected = hashlib.sha256(("12" * 16 + "\n" + "a" * 32 + "\n" + self.account.server + "\n" + str(self.account.login)).encode("utf-8")).hexdigest()
        self.assertEqual(fields[16:], ["12" * 16, expected])
        different = chart.ChartExporter(self.common, self.data, symbol="XAUUSD", broker_offset_minutes=180,
                                        account=SimpleNamespace(server=self.account.server, login=self.account.login + 1), nonce="12" * 16)
        self.assertNotEqual(different.binding_hash, self.exporter.binding_hash)

    def test_source_quote_and_original_expiry_cap_deadline_even_on_repeated_exports(self):
        value = proposal()
        value["expires_at"] = (NOW + timedelta(seconds=4)).isoformat()
        fields = self.publish(value).decode("ascii").split(";")
        self.assertEqual(int(fields[12]), int(NOW.timestamp()) + 4)
        fields = self.publish(now=NOW + timedelta(seconds=5)).decode("ascii").split(";")
        self.assertEqual(int(fields[12]), int(NOW.timestamp()) + 10)
        with self.assertRaises(chart.OverlayError):
            self.publish(now=NOW + timedelta(seconds=10))

    def test_active_observation_cap_prevents_future_quote_skew_extending_display(self):
        for quote_offset, lifetime in ((-4, 6), (0, 10), (5, 10)):
            quote = dict(QUOTE, time=(NOW + timedelta(seconds=quote_offset)).isoformat())
            with self.subTest(quote_offset=quote_offset):
                fields = self.publish(quote=quote).decode("ascii").split(";")
                self.assertEqual(int(fields[11]), int(NOW.timestamp()))
                self.assertEqual(int(fields[12]), int(NOW.timestamp()) + lifetime)

    def test_null_and_clear_replace_old_prices_with_waiting_row(self):
        self.publish()
        self.exporter.publish(None, execution=None, quote=None, observed_at=NOW)
        fields = self.exporter.path.read_text(encoding="ascii").split(";")
        self.assertEqual(len(fields), 18)
        self.assertEqual(fields[:5], ["2", "waiting", "XAUUSD", "M1", "NONE"])
        self.assertEqual(fields[5:10], ["0"] * 5)
        self.assertEqual(int(fields[12]), int(NOW.timestamp()) + 25)
        self.assertEqual(fields[13], "0")

    def test_null_proposal_writes_bound_readiness_without_inventing_active_levels(self):
        feed = readiness_feed()
        feed["timeframes"]["M30"] = feed["timeframes"]["M30"][-10:]
        self.exporter.publish(None, execution=EXECUTION, quote=QUOTE, observed_at=NOW, feed=feed)
        raw = self.exporter.readiness_path.read_bytes()
        self.assertLessEqual(len(raw), 2048)
        self.assertFalse(any(char in raw for char in (b"\r", b"\n", b'"')))
        fields = raw.decode("ascii").split(";")
        self.assertEqual(len(fields), 10)
        self.assertEqual(fields[:7], ["2", str(int(NOW.timestamp())), str(int(NOW.timestamp()) + 10), "10", "64", "64", "64"])
        self.assertEqual(fields[7:], [self.exporter.terminal_key, self.exporter.nonce, self.exporter.binding_hash])
        self.assertEqual(self.exporter.readiness_path.name, "readiness_" + "a" * 32 + ".csv")
        self.assertNotIn(str(self.account.login), raw.decode("ascii"))
        self.assertNotIn(self.account.server.encode("utf-8"), raw)
        self.assertEqual(self.exporter.path.read_text(encoding="ascii").split(";")[1], "waiting")

    def test_invalid_or_missing_readiness_cannot_suppress_or_authorize_proposals(self):
        feed = readiness_feed()
        self.exporter.publish(proposal(), execution=EXECUTION, quote=QUOTE, observed_at=NOW, feed=feed)
        original = self.exporter.path.read_bytes()
        self.assertTrue(self.exporter.readiness_path.is_file())
        feed["as_of"] = (NOW + timedelta(seconds=1)).isoformat()
        self.exporter.publish(proposal(), execution=EXECUTION, quote=QUOTE, observed_at=NOW, feed=feed)
        self.assertEqual(self.exporter.path.read_bytes(), original)
        self.assertFalse(self.exporter.readiness_path.exists())
        self.exporter.publish(None, execution=EXECUTION, quote=QUOTE, observed_at=NOW, feed=feed)
        self.assertEqual(self.exporter.path.read_text(encoding="ascii").split(";")[1], "waiting")
        self.exporter.publish(proposal(), execution=EXECUTION, quote=QUOTE, observed_at=NOW, feed=readiness_feed())
        self.publish()  # Legacy callers need not supply the optional feed.
        self.assertFalse(self.exporter.readiness_path.exists())
        invalid_proposal = proposal()
        invalid_proposal["stop"] = 2600
        with self.assertRaises(chart.OverlayError):
            self.exporter.publish(invalid_proposal, execution=EXECUTION, quote=QUOTE, observed_at=NOW, feed=readiness_feed())

    def test_sidecar_atomic_io_failure_is_isolated_from_complete_proposal_row(self):
        real_replace = chart.os.replace

        def replace(source, target):
            if Path(target) == self.exporter.readiness_path:
                raise PermissionError("synthetic sidecar reader lock")
            return real_replace(source, target)

        with patch.object(chart.os, "replace", side_effect=replace), patch.object(chart.time, "sleep"):
            self.exporter.publish(proposal(), execution=EXECUTION, quote=QUOTE, observed_at=NOW, feed=readiness_feed())
        self.assertEqual(self.exporter.path.read_text(encoding="ascii").split(";")[1], "active")
        self.assertFalse(self.exporter.readiness_path.exists())
        self.assertEqual(list(self.exporter.directory.glob(".readiness-*.tmp")), [])

    def test_clear_removes_owned_readiness_and_never_refreshes_zero_counts(self):
        self.exporter.publish(None, execution=EXECUTION, quote=QUOTE, observed_at=NOW, feed=readiness_feed())
        self.exporter.clear(NOW + timedelta(seconds=1))
        self.assertFalse(self.exporter.readiness_path.exists())
        self.assertEqual(self.exporter.path.read_text(encoding="ascii").split(";")[1], "waiting")

    def test_fresh_local_readiness_never_renews_waiting_or_expired_active_row(self):
        for value in (None, proposal()):
            with self.subTest(active=value is not None):
                self.exporter.publish(value, execution=EXECUTION, quote=QUOTE, observed_at=NOW, feed=readiness_feed())
                original = self.exporter.path.read_bytes()
                later = NOW + timedelta(seconds=26)
                self.exporter.refresh_readiness(readiness_feed(later), later)
                self.assertEqual(self.exporter.path.read_bytes(), original)
                self.assertLess(int(original.decode("ascii").split(";")[12]), int(later.timestamp()))
                fields = self.exporter.readiness_path.read_text(encoding="ascii").split(";")
                self.assertEqual(int(fields[2]), int(later.timestamp()) + 10)
                # Re-exporting the same source after its deadline cannot keep it LIVE.
                self.exporter.refresh_readiness(readiness_feed(later), later + timedelta(seconds=10))
                self.assertFalse(self.exporter.readiness_path.exists())
                self.assertEqual(self.exporter.path.read_bytes(), original)

    def test_invalid_readiness_destination_and_locked_clear_do_not_block_levels(self):
        self.exporter.readiness_path.mkdir()
        self.exporter.publish(proposal(), execution=EXECUTION, quote=QUOTE, observed_at=NOW, feed=readiness_feed())
        self.assertTrue(self.exporter.readiness_path.is_dir())
        self.assertEqual(self.exporter.path.read_text(encoding="ascii").split(";")[1], "active")
        with patch.object(self.exporter, "_clear_readiness", side_effect=PermissionError("synthetic sidecar lock")):
            self.exporter.clear(NOW)
        self.assertEqual(self.exporter.path.read_text(encoding="ascii").split(";")[1], "waiting")

    def test_failed_atomic_replace_preserves_old_complete_row_and_removes_temporary_file(self):
        original = self.publish()
        with patch.object(chart.os, "replace", side_effect=PermissionError("synthetic reader lock")) as replace, patch.object(chart.time, "sleep") as sleep:
            with self.assertRaisesRegex(chart.OverlayError, "^Chart export update unavailable$"):
                self.exporter.clear(NOW)
        self.assertEqual(replace.call_count, 4)
        self.assertEqual([call.args for call in sleep.call_args_list], [(0.05,)] * 3)
        self.assertEqual(self.exporter.path.read_bytes(), original)
        self.assertEqual(list(self.exporter.directory.glob(".levels-*.tmp")), [])

    def test_transient_reader_sharing_lock_retries_same_atomic_row(self):
        self.publish()
        real_replace = chart.os.replace
        attempts = []

        def replace(source, target):
            attempts.append((source, target))
            if len(attempts) == 1:
                error = OSError("synthetic Windows sharing violation")
                error.winerror = 32
                raise error
            if len(attempts) <= 2:
                raise PermissionError("synthetic short read")
            real_replace(source, target)

        with patch.object(chart.os, "replace", side_effect=replace), patch.object(chart.time, "sleep") as sleep:
            self.exporter.clear(NOW)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(attempts[1], attempts[2])
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(self.exporter.path.read_text(encoding="ascii").split(";")[1], "waiting")
        self.assertEqual(list(self.exporter.directory.glob(".levels-*.tmp")), [])

    def test_missing_relative_and_unsafe_markers_are_rejected(self):
        for common, data in ((Path("relative"), self.data), (self.base / "missing", self.data),
                             (self.common, self.base / "missing")):
            with self.subTest(common=common, data=data), self.assertRaises(chart.OverlayError):
                chart.ChartExporter(common, data, symbol="XAUUSD", broker_offset_minutes=180, account=self.account)
        for name in ("bad;marker", "bad marker", "bad.marker"):
            data = self.base / name
            data.mkdir()
            with self.subTest(name=name), self.assertRaises(chart.OverlayError):
                chart.ChartExporter(self.common, data, symbol="XAUUSD", broker_offset_minutes=180, account=self.account)

    def test_destination_directory_instead_of_file_is_rejected(self):
        self.exporter.path.mkdir()
        with self.assertRaises(chart.OverlayError):
            self.exporter.clear(NOW)

    def test_constructor_uses_verified_terminal_metadata_and_never_path_from_dto(self):
        info = SimpleNamespace(commondata_path=str(self.common), data_path=str(self.data))
        settings = SimpleNamespace(symbol="XAUUSD", broker_utc_offset_minutes=180)
        built = chart.ChartExporter.from_terminal_info(info, settings, self.account)
        self.assertEqual(built.path, self.exporter.path)
        info.commondata_path = None
        with self.assertRaises(chart.OverlayError):
            chart.ChartExporter.from_terminal_info(info, settings, self.account)


if __name__ == "__main__":
    unittest.main()
