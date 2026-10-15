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

    def function_source(self, declaration):
        """Select a whole MQL function without depending on adjacent helpers."""
        start = self.source.index(declaration)
        opening = self.source.index("{", start)
        depth = 1
        for index in range(opening + 1, len(self.source)):
            depth += (self.source[index] == "{") - (self.source[index] == "}")
            if depth == 0:
                return self.source[start:index + 1]
        self.fail(f"Unclosed function: {declaration}")

    def test_indicator_has_only_display_and_bound_readonly_broker_metadata_capabilities(self):
        self.assertIn("#property indicator_chart_window", self.source)
        forbidden = r"\b(?:OrderSend|OrderSendAsync|OrderCheck|CTrade|WebRequest|SocketCreate|ShellExecuteW|FileWrite|FileDelete|FileMove|FileCopy|GlobalVariableSet|OBJ_BUTTON)\b|#import\b"
        self.assertIsNone(re.search(forbidden, self.source))
        self.assertEqual(re.findall(r"#include\s+([^\r\n]+)", self.source), [r"<Canvas\Canvas.mqh>"])
        self.assertIn("FILE_READ|FILE_BIN|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE", self.source)
        publisher = self.function_source("void PublishTradeSession()")
        remainder = self.source.replace(publisher, "")
        self.assertNotIn("FILE_WRITE", remainder)
        self.assertNotRegex(remainder, r"\bFileWrite\w*\s*\(")
        self.assertEqual(self.source.count("FileWriteArray("), 1)
        self.assertIn("length>0 && length<=2048", publisher)
        self.assertIn("AccountBindingMatches(fields[6],fields[7])", publisher)
        self.assertIn("(long)now>=expires", publisher)
        self.assertIn('string target="MT5Bot\\\\sessions_"+g_terminal_key+".csv"', publisher)
        schedule = self.function_source("bool CurrentTradeSession(")
        self.assertEqual(schedule.count("SymbolInfoSessionTrade("), 1)
        self.assertNotIn("SymbolInfoSessionQuote(", self.source)
        self.assertEqual(self.source.count("FileOpen("), 4)
        self.assertIn("g_panel.CreateBitmapLabel(", self.source)
        self.assertIn('ObjectSetString(0,name,OBJPROP_TOOLTIP,"\\n")', self.source)
        self.assertIn("EventSetTimer(1)", self.source)
        self.assertIn("EventKillTimer()", self.source)

    def test_experimental_state_is_distinct_and_labels_never_claim_certified_performance(self):
        self.assertIn('fields[1]!="experimental"', self.source)
        self.assertIn('p.experimental=(fields[1]=="experimental")', self.source)
        self.assertIn('Demo experimental | ', self.source)
        self.assertIn('Estimated costs | No certified win rate', self.source)
        self.assertIn('Performance unproven | No certified win rate', self.source)
        self.assertIn('p.experimental && (observed>bar+90 || valid_until>bar+90)', self.source)
        self.assertIn('ObjectSetString(0,name,OBJPROP_TEXT,text)', self.source)

    def test_chart_view_timeframe_is_independent_of_closed_m1_signal_validation(self):
        # A valid M1 opportunity must be visible while viewing XAUUSD M15/M30.
        # Changing the chart view never changes the incoming signal timeframe.
        parser = self.source.split("bool ReadProposal(", 1)[1].split("bool PrepareObject(", 1)[0]
        self.assertNotRegex(parser, r"\b_Period\b|\bPERIOD_CURRENT\b")
        self.assertIn('if(_Symbol!="XAUUSD")', self.source)
        self.assertIn('fields[3]!="M1"', self.source)
        self.assertIn("iBarShift(_Symbol,PERIOD_M1,chart_bar,true)<1", self.source)
        self.assertIn("bar+60>observed", self.source)

    def test_notice_can_only_follow_fresh_validated_draw_and_is_informational(self):
        update = self.source.split("void UpdateOverlay()", 1)[1].split("int OnInit()", 1)[0]
        notice = self.source.split("void NotifyOpportunity(", 1)[1].split("bool ParseInteger(", 1)[0]
        self.assertLess(update.index("ReadProposal(p,message)"), update.index("DrawProposal(p)"))
        self.assertLess(update.index("DrawProposal(p)"), update.index("NotifyOpportunity(p)"))
        self.assertLess(update.rindex("TimeGMT()>=p.valid_until"), update.index("NotifyOpportunity(p)"))
        self.assertEqual(self.source.count("Alert("), 1)
        self.assertEqual(notice.count("TimeGMT()>=p.valid_until"), 2)
        for guard in ("ACCOUNT_LOGIN", "ACCOUNT_SERVER", "ACCOUNT_TRADE_MODE_DEMO", "TERMINAL_CONNECTED"):
            self.assertEqual(notice.count(guard), 2)
        for detail in ("Demo experimental | ", "Entry ", "Zone ", "SL ", "TP ", "Expires UTC ",
                       "Estimated costs | No certified win rate", "Telegram Accept requests one guarded Demo attempt",
                       "wait for MT5 confirmation"):
            self.assertIn(detail, notice)
        self.assertLess(notice.index("ClaimOpportunityNotice(p)"), notice.index("Alert(notice)"))
        self.assertNotRegex(self.source, r"\b(?:SendNotification|SendMail|ChartApplyTemplate)\b")
        self.assertNotIn("ReadReadiness", notice)
        self.assertNotIn("g_waiting_snapshot", notice)

    def test_four_frame_panel_has_only_view_actions_and_visible_version(self):
        panel = self.source.split("bool DrawPanel(", 1)[1].split("void HandlePanelClick(", 1)[0]
        click = self.source.split("void HandlePanelClick(", 1)[1].split("void ClearLevels()", 1)[0]
        self.assertIn('#property version   "2.17"', self.source)
        self.assertIn('PanelText(168,20,"2.17"', panel)
        self.assertIn('INDICATOR_SHORTNAME,"MT5 Bot Panel 2.17"', self.source)
        self.assertIn('"M30 / M15 / M5 / M1"', panel)
        self.assertIn('string readiness_frames[]={"M30","M15","M5","M1"}', panel)
        self.assertIn('string frames[]={"M1","M5","M15","M30"}', panel)
        self.assertEqual(re.findall(r'hit.action=="(view_[^"]+)"', click), [
            "view_M1", "view_M5", "view_M15", "view_M30",
        ])
        self.assertNotRegex(self.source, r"\bPERIOD_(?:H1|H4)\b|view_(?:H1|H4)")
        self.assertEqual(self.source.count("ChartSetSymbolPeriod("), 1)
        self.assertIn("ChartSetSymbolPeriod(0,_Symbol,period)", click)
        self.assertIn('hit.action=="refresh") { UpdateOverlay(); return; }', click)
        self.assertIn('"Refresh rereads the latest local update."', panel)
        self.assertNotRegex(click, r"\b(?:ReadProposal|ReadReadiness|Alert|FileOpen|GlobalVariable\w+)\s*\(")

    def test_existing_proposal_parser_is_unchanged_from_installed_canvas_version(self):
        # Readiness must remain a presentation channel. Pin the full existing
        # 2.12 parser, including byte limits, identity, TTL, price and M1 guards.
        parser = "bool ReadProposal(" + self.source.split("bool ReadProposal(", 1)[1].split("bool PrepareObject(", 1)[0]
        self.assertEqual(hashlib.sha256(parser.encode("utf-8")).hexdigest(),
                         "c8c34f50fd4874ccd08df462782d3eb15d9e5219a6bfb22d268fbe6edd036181")

    def test_readiness_has_strict_bounded_readonly_anonymous_contract(self):
        reader = self.function_source("bool ReadReadiness(")
        self.assertIn("READINESS_FIELD_COUNT 10", self.source)
        self.assertIn("READINESS_TTL_SECONDS 25", self.source)
        self.assertIn("READINESS_MAX_BYTES   2048", self.source)
        for text in (
            "size>READINESS_MAX_BYTES", "FileSize(handle)==size && FileTell(handle)==size",
            "copied!=size || !unchanged", 'bytes[i]<33 || bytes[i]>126 || bytes[i]==\'"\'',
            "StringSplit(row,';',fields)!=READINESS_FIELD_COUNT", 'fields[0]!="2"',
            "fields[7]!=g_terminal_key", "AccountBindingMatches(fields[8],fields[9])",
            "ParseInteger(fields[1],observed)", "ParseInteger(fields[2],valid_until)",
            "ParseInteger(fields[i+3],count)", "count<0 || count>64", "observed<946684800",
            "observed>(long)now", "valid_until<=observed", "valid_until-observed>READINESS_TTL_SECONDS",
            "(long)now-observed>READINESS_TTL_SECONDS", "(long)now>=valid_until", "MQL_TESTER",
            'g_terminal_key==""', '_Symbol!="XAUUSD"',
        ):
            with self.subTest(text=text):
                self.assertIn(text, reader)
        for guard in ("g_account_changed", "ACCOUNT_LOGIN", "ACCOUNT_SERVER", "ACCOUNT_TRADE_MODE_DEMO", "TERMINAL_CONNECTED"):
            self.assertEqual(reader.count(guard), 2)
        self.assertIn('g_readiness_file="MT5Bot\\\\readiness_"+g_terminal_key+".csv"', self.source)
        self.assertNotRegex(reader, r"\b(?:Proposal|Alert|NotifyOpportunity|GlobalVariable\w*|PanelText\w*|Print|FileWrite\w*|OrderSend\w*)\b")
        self.assertNotIn("g_snapshot_", reader)

    def test_readiness_cannot_mask_invalid_main_row_or_gate_active_proposal(self):
        update = self.source.split("void UpdateOverlay()", 1)[1].split("int OnInit()", 1)[0]
        panel = self.source.split("bool DrawPanel(", 1)[1].split("void HandlePanelClick(", 1)[0]
        self.assertLess(update.index("g_waiting_snapshot=false"), update.index("MQL_TESTER"))
        self.assertLess(update.index("ReadProposal(p,message)"), update.index("g_waiting_snapshot=(!accepted"))
        self.assertIn('message=="No current proposal: waiting for market and risk conditions"', update)
        self.assertIn("p.observed>0 && TimeGMT()<p.valid_until", update)
        self.assertIn("if(!accepted) { ShowWaiting(message); return; }", update)
        self.assertNotIn("ReadReadiness", update)
        self.assertIn("bool telemetry=ReadReadiness(readiness) && main_fresh", panel)
        self.assertIn("(active && now<g_panel_proposal.valid_until)", panel)
        self.assertIn("(g_waiting_snapshot && now<g_snapshot_deadline)", panel)
        self.assertIn("g_waiting_snapshot && main_fresh && telemetry && warmup_frame>=0", panel)
        self.assertIn("string reason=message", panel)
        self.assertIn('" warmup: "+IntegerToString(readiness.counts[warmup_frame])+"/22 closed candles"', panel)
        self.assertIn("MathMin(22,displayed_count)", panel)
        self.assertIn('telemetry ? "LIVE"', panel)
        self.assertIn('"All four frames ready. Waiting for market and risk conditions."', panel)
        self.assertIn('"Ready candles do not guarantee a signal."', panel)
        self.assertIn(' : "--/22"', panel)

    def test_native_history_reads_closed_bars_independently_without_bridge_capabilities(self):
        counter = self.function_source("int CountClosedContiguous(")
        history_counter = self.function_source("int CountClosedHistory(")
        reader = self.function_source("bool ReadLocalCandleCounts(")
        compact = re.sub(r"\s+", "", reader)
        self.assertIn("ArraySetAsSeries(rates,false)", compact)
        self.assertIn("CopyRates(_Symbol,frames[i],1,64,rates)", compact)
        self.assertIn("PERIOD_M30,PERIOD_M15,PERIOD_M5,PERIOD_M1", compact)
        self.assertIn("PeriodSeconds(frames[i])", compact)
        self.assertIn("ArrayInitialize(counts,-1)", compact)
        self.assertEqual(self.source.count("ReadLocalCandleCounts("), 2)
        self.assertEqual(self.source.count("CountClosedContiguous("), 2)
        self.assertEqual(self.source.count("CountClosedHistory("), 2)
        self.assertIn("CountClosedHistory(rates,copied,PeriodSeconds(frames[i]),broker_now)", compact)
        self.assertIn("CountClosedContiguous(rates,copied,seconds,broker_now)", history_counter)
        self.assertIn("return(contiguous<0 ? -1 : copied)", history_counter)
        self.assertNotRegex(history_counter, r"\b(?:for|while|ArrayResize|ArrayCopy)\s*\(")
        self.assertEqual(hashlib.sha256(counter.encode("utf-8")).hexdigest(),
                         "6d1fca7049ea3ec5930661fc3c327eaeaf1b3950e8be68ef237eb5af553e544e")
        for guard in ("g_account_changed", "ACCOUNT_LOGIN", "ACCOUNT_SERVER", "ACCOUNT_TRADE_MODE_DEMO", "TERMINAL_CONNECTED"):
            self.assertEqual(reader.count(guard), 2)
        self.assertIn("MQL_TESTER", reader)
        self.assertIn("return(-1)", re.sub(r"\s+", "", counter))
        # Source constraints verify that the fallback is display-only. The
        # pure counter's arithmetic is exercised by the native compiler probe.
        self.assertNotRegex(counter + history_counter + reader, r"\b(?:ReadProposal|ReadReadiness|NotifyOpportunity|DrawProposal|Alert|WebRequest|FileOpen|FileWrite\w*|GlobalVariable\w*|OrderSend\w*|ChartSetSymbolPeriod)\s*\(")
        self.assertNotRegex(counter + history_counter + reader, r"\bg_(?:snapshot_\w*|waiting_snapshot|panel_proposal)\b")

    def test_native_counts_do_not_authorize_signals_or_turn_missing_feed_live(self):
        panel = self.function_source("bool DrawPanel(")
        for declaration in ("bool ReadProposal(", "void UpdateOverlay()", "void NotifyOpportunity("):
            with self.subTest(declaration=declaration):
                function = self.function_source(declaration)
                self.assertNotIn("ReadLocalCandleCounts", function)
                self.assertNotIn("CountClosedContiguous", function)
                self.assertNotIn("CountClosedHistory", function)
                self.assertNotIn("local_history", function)
        self.assertRegex(panel, r"if\(!telemetry\)\s*local_history=ReadLocalCandleCounts\(local_counts\)")
        self.assertIn('"LOCAL HISTORY"', panel)
        self.assertIn('telemetry ? "LIVE"', panel)
        self.assertIn("bool telemetry=ReadReadiness(readiness) && main_fresh", panel)
        self.assertIn("telemetry ? readiness.counts[i] : local_counts[i]", panel)
        self.assertIn('"SIGNAL: UNAVAILABLE"', panel)
        ready = re.search(r"bool ready=([^;]+);", panel)
        self.assertIsNotNone(ready)
        self.assertIn("telemetry", ready.group(1))
        self.assertNotIn("local_history", ready.group(1))
        self.assertIn("g_waiting_snapshot && main_fresh && telemetry && warmup_frame>=0", panel)
        self.assertIn("g_waiting_snapshot && main_fresh && telemetry)", panel)

    def test_notice_dedup_is_shared_atomic_anonymous_and_survives_waiting_or_reload(self):
        claim = self.source.split("bool ClaimOpportunityNotice(", 1)[1].split("void NotifyOpportunity(", 1)[0]
        key = self.source.split("string OpportunityNoticeKey()", 1)[1].split("bool ClaimOpportunityNotice(", 1)[0]
        self.assertIn("GlobalVariableTemp(g_notice_key)", claim)
        self.assertIn("GlobalVariableSetOnCondition(g_notice_key,token,previous)", claim)
        self.assertIn("(long)p.bar<last_bar", claim)
        self.assertIn("(mask & direction_bit)!=0", claim)
        self.assertIn("mask | direction_bit", claim)
        self.assertIn("MT5BotOpportunity/v1", key)
        self.assertIn("g_terminal_key", key)
        self.assertIn("g_session_server", key)
        self.assertIn("g_session_login", key)
        self.assertIn("CRYPT_HASH_SHA256", key)
        self.assertNotRegex(key, r"\b(?:nonce|ChartID|_Period|GetTickCount64)\b")
        # Only this namespaced temporary display state may be written. There is
        # one bounded value per binding, never a variable per signal or chart.
        calls = re.findall(r"\b(GlobalVariable\w*)\(([^()]*)\)", self.source)
        self.assertEqual({name for name, _ in calls}, {
            "GlobalVariableCheck", "GlobalVariableTemp", "GlobalVariableGet", "GlobalVariableSetOnCondition",
        })
        for _, arguments in calls:
            self.assertTrue(arguments.startswith("g_notice_key"))
        self.assertNotRegex(self.source, r"\b(?:GlobalVariableDel|GlobalVariablesDeleteAll|GlobalVariableSet)\b")

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
            "strategy_id": "mtf-ema-pullback-60m-v8", "strategy_version": 8,
            "policy_id": "mtf-manual-demo-cost-risk-v8", "horizon_seconds": 3600,
            "strategy_fingerprint": "a" * 64, "qualification_id": "b" * 64,
            "direction_bar_time": "2026-10-05T16:00:00Z",
            "confirmation_bar_time": "2026-10-05T16:15:00Z",
            "broker_utc_offset_minutes": 180,
            "context_bar_times": {"M30": "2026-10-05T15:30:00Z"},
            "timeframe_context": {"trends": {key: "BUY" for key in ("M1", "M5", "M15", "M30")},
                                  "alignment": "aligned", "confidence": "aligned", "counter_trend": False,
                                  "levels_timeframe": "M30", "support": 3900, "resistance": 4100},
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
