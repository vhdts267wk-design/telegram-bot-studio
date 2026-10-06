"""Production indicator interoperability and capability checks, without MT5 or UI."""

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest


from bridge import mt5_chart_overlay as writer


SOURCE = Path(__file__).resolve().parents[1] / "bridge" / "MT5BotLevels.mq5"


class IndicatorContractTests(unittest.TestCase):
    def setUp(self):
        self.source = SOURCE.read_text(encoding="utf-8")

    def test_indicator_has_only_display_capabilities(self):
        self.assertIn("#property indicator_chart_window", self.source)
        forbidden = r"\b(?:OrderSend|OrderSendAsync|OrderCheck|CTrade|WebRequest|SocketCreate|ShellExecuteW|FileWrite|FileDelete|FileMove|FileCopy|GlobalVariableSet|OBJ_BUTTON)\b|#(?:include|import)\b"
        self.assertIsNone(re.search(forbidden, self.source))
        self.assertIn("FILE_READ|FILE_BIN|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE", self.source)
        self.assertNotIn("FILE_WRITE", self.source)
        self.assertIn("EventSetTimer(1)", self.source)
        self.assertIn("EventKillTimer()", self.source)

    def test_every_delete_is_limited_to_owned_objects(self):
        self.assertEqual(self.source.count("ObjectsDeleteAll("), 1)
        self.assertIn("ObjectsDeleteAll(0,g_prefix,0,-1)", self.source)
        self.assertIn("if(StringLen(g_prefix)>0)", self.source)
        for call in re.findall(r"\bObjectDelete\(([^;]+)\);", self.source):
            self.assertIn("g_prefix+", call)
        self.assertIn("IntegerToString(ChartID())", self.source)
        self.assertIn('ObjectFind(0,g_prefix+"status")', self.source)

    def test_guards_and_coordinates_are_explicit(self):
        expected = (
            "OVERLAY_FIELD_COUNT 18", "OVERLAY_TTL_SECONDS 25", "OVERLAY_MAX_BYTES   2048",
            "bytes[i]<33", "bytes[i]>126", "copied!=size", "fields[15]!=g_terminal_key",
            "AccountBindingMatches(fields[16],fields[17])", 'fields[2]!="XAUUSD"', 'fields[3]!="M1"',
            "ACCOUNT_TRADE_MODE_DEMO", "TERMINAL_CONNECTED", "MQL_TESTER", "g_account_changed=true",
            "MathIsValidNumber(value)", "OnTickGrid(p.entry", "OnTickGrid(p.zone_low", "OnTickGrid(p.zone_high",
            "OnTickGrid(p.stop", "OnTickGrid(p.target", "p.stop<p.zone_low", "p.zone_high<p.target",
            "p.target<p.zone_low", "p.zone_high<p.stop", "bar+60>observed", "bar%60!=0",
            "iBarShift(_Symbol,PERIOD_M1,chart_bar,true)<1", "observed>(long)now",
            "valid_until-observed>OVERLAY_TTL_SECONDS", "(long)now>=valid_until",
            "TimeGMT()>=p.valid_until", "(long)p.bar+(long)p.offset_minutes*60",
            "(long)p.valid_until+(long)p.offset_minutes*60", "OBJ_RECTANGLE", "OBJ_HLINE",
            "ObjectGetDouble", "ObjectGetInteger", "ChartRedraw(0)", "ShowWaiting(message)",
        )
        for text in expected:
            with self.subTest(text=text):
                self.assertIn(text, self.source)

    def test_mql_binding_matches_python_utf8_material(self):
        self.assertIn('nonce+"\\n"+g_terminal_key+"\\n"+server+"\\n"+IntegerToString(login)', self.source)
        self.assertIn("StringToCharArray(material,data,0,WHOLE_ARRAY,CP_UTF8)", self.source)
        self.assertIn("ArrayResize(data,copied-1)", self.source)
        self.assertIn("CryptEncode(CRYPT_HASH_SHA256,data,key,digest)!=32", self.source)
        self.assertIn('StringFormat("%02x",(int)digest[i])', self.source)
        # Neither status messages nor logs reveal identity or binding material.
        for display in re.findall(r"(?:Print|SetLabel)\([^;]+\);", self.source):
            self.assertNotRegex(display, r"ACCOUNT_LOGIN|ACCOUNT_SERVER|material|binding|nonce")

    def make_exporter(self, temporary, login=12345678):
        common = Path(temporary) / "common"
        data = Path(temporary) / "A12345ABCDEF"
        common.mkdir(exist_ok=True)
        data.mkdir(exist_ok=True)
        return writer.ChartExporter(
            common, data, symbol="XAUUSD", broker_offset_minutes=180,
            account=SimpleNamespace(login=login, server="Synthetic Démo"), nonce="ab" * 16,
        )

    def test_waiting_row_exact_18_fields_and_anonymous_binding(self):
        now = datetime(2026, 10, 5, 16, 22, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory(prefix="indicator-contract-") as temporary:
            exporter = self.make_exporter(temporary)
            exporter.clear(now)
            encoded = exporter.path.read_bytes()
            fields = encoded.decode("ascii").split(";")
            self.assertEqual(len(fields), 18)
            self.assertEqual(fields[:11], ["2", "waiting", "XAUUSD", "M1", "NONE", "0", "0", "0", "0", "0", "2"])
            self.assertEqual([int(value) for value in fields[11:15]], [int(now.timestamp()), int(now.timestamp()) + 25, 0, 180])
            self.assertEqual(fields[15:17], ["a12345abcdef", "ab" * 16])
            expected = hashlib.sha256(("ab" * 16 + "\na12345abcdef\nSynthetic Démo\n12345678").encode("utf-8")).hexdigest()
            self.assertEqual(fields[17], expected)
            self.assertNotIn(b"12345678", encoded)
            self.assertNotIn(b"Synthetic", encoded)
            self.assertRegex(exporter.path.name, r"^levels_a12345abcdef\.csv$")
            self.assertTrue(all(33 <= byte <= 126 and byte != 34 for byte in encoded))

    def test_active_row_coordinates_levels_and_original_expiry(self):
        now = datetime(2026, 10, 5, 16, 22, 0, tzinfo=timezone.utc)
        execution = {"tick_size": 0.01, "point": 0.01, "digits": 2, "stops_level": 0}
        proposal = {
            "version": 2, "workflow": "chart_overlay",
            "offer_id": "00000000-0000-4000-8000-000000000001", "status": "offered",
            "symbol": "XAUUSD", "timeframe": "M1", "direction": "BUY",
            "entry": 4000.00, "entry_zone_low": 3999.00, "entry_zone_high": 4001.00,
            "stop": 3990.00, "target": 4020.00, "price_digits": 2, "execution": execution,
            "bar_time": "2026-10-05T16:21:00Z", "expires_at": "2026-10-05T16:22:10Z",
            "strategy_id": "mtf-ema-pullback-60m-v1", "strategy_version": 1,
            "policy_id": "mtf-manual-demo-cost-risk-v1", "horizon_seconds": 3600,
            "strategy_fingerprint": "a" * 64, "qualification_id": "b" * 64,
            "direction_bar_time": "2026-10-05T16:00:00Z",
            "confirmation_bar_time": "2026-10-05T16:15:00Z",
        }
        quote = {"time": now.isoformat(), "bid": 3999.99, "ask": 4000.01}
        with tempfile.TemporaryDirectory(prefix="indicator-contract-") as temporary:
            exporter = self.make_exporter(temporary)
            exporter.publish(proposal, execution=execution, quote=quote, observed_at=now)
            fields = exporter.path.read_text(encoding="ascii").split(";")
            self.assertEqual(len(fields), 18)
            self.assertEqual(fields[1:11], ["active", "XAUUSD", "M1", "BUY", "4000.00", "3999.00", "4001.00", "3990.00", "4020.00", "2"])
            self.assertEqual(int(fields[12]), int(now.timestamp()) + 10)
            chart_bar = datetime.fromtimestamp(int(fields[13]) + int(fields[14]) * 60, timezone.utc)
            self.assertEqual(chart_bar.hour, 19)
            self.assertEqual(chart_bar.minute, 21)
            # Re-publication does not extend the original proposal expiry.
            exporter.publish(proposal, execution=execution, quote=quote, observed_at=now + timedelta(seconds=5))
            self.assertEqual(exporter.path.read_text(encoding="ascii").split(";")[12], fields[12])

    def test_account_switch_changes_binding_without_raw_identity(self):
        with tempfile.TemporaryDirectory(prefix="indicator-contract-") as temporary:
            before = self.make_exporter(temporary, login=12345678)
            after = self.make_exporter(temporary, login=23456789)
            self.assertNotEqual(before.binding_hash, after.binding_hash)
            self.assertEqual(before.path, after.path)

if __name__ == "__main__":
    unittest.main(verbosity=2)
