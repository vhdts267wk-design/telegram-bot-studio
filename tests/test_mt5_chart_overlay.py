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
        "strategy_id": "mtf-ema-pullback-60m-v2", "strategy_version": 2,
        "policy_id": "mtf-manual-demo-cost-risk-v2", "horizon_seconds": 3600,
        "strategy_fingerprint": "a" * 64, "qualification_id": "b" * 64,
        "direction_bar_time": (NOW - timedelta(minutes=15)).isoformat(),
        "confirmation_bar_time": (NOW - timedelta(minutes=5)).isoformat(),
        "broker_utc_offset_minutes": 180,
        "context_bar_times": {frame: datetime.fromtimestamp(chart.latest_closed_reference(
            NOW.timestamp(), seconds, 180), timezone.utc).isoformat() for frame, seconds in (("H1", 3600), ("H4", 14400))},
        "timeframe_context": {"trends": {frame: "BUY" for frame in chart.FRAME_SECONDS}, "alignment": "aligned",
                              "confidence": "aligned", "counter_trend": False, "support": 2490.0, "resistance": 2520.0},
    }


def experimental_proposal():
    value = proposal()
    value.pop("qualification_id")
    value.update(strategy_id="mtf-ema-pullback-60m-demo-v3", strategy_version=3,
                 policy_id="mtf-manual-demo-estimated-cost-risk-v3", signal_mode="experimental_demo",
                 provisional=True, entry_window_seconds=30, expires_at=(NOW + timedelta(seconds=30)).isoformat(),
                 cost_assumptions={"verified": False, "method": "spread_tick_floor_v1", "commission_round_turn": 0.2,
                                   "slippage_price": 0.1, "spread_price": 0.2, "tick_size": 0.01})
    return value


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
        for field, changed in (("strategy_id", "mtf-ema-pullback-60m-v2"), ("provisional", False),
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

    def test_five_frame_context_and_offset_causal_higher_references_are_required(self):
        mutations = [
            lambda value: value.pop("context_bar_times"),
            lambda value: value["context_bar_times"].pop("H4"),
            lambda value: value["context_bar_times"].update(H1=NOW.isoformat()),
            lambda value: value["context_bar_times"].update(H4=NOW.replace(hour=8, minute=0).isoformat()),
            lambda value: value.update(broker_utc_offset_minutes=0),
            lambda value: value.update(broker_utc_offset_minutes=True),
            lambda value: value["timeframe_context"]["trends"].pop("H1"),
            lambda value: value["timeframe_context"]["trends"].update(H4="SELL"),
            lambda value: value["timeframe_context"]["trends"].update(H1="NEUTRAL"),
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
        # The configured +03:00 broker's last completed H4 began at 05:00 UTC,
        # whereas flooring UTC directly would incorrectly select 08:00 UTC.
        self.assertEqual(chart.utc(proposal()["context_bar_times"]["H4"]).hour, 5)
        self.validate()

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
