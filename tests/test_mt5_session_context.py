from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from bridge.mt5_session_context import SessionExchange, capture_session_context


class SessionExchangeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        root=Path(self.temp.name)
        data,common=root/'terminal_a',root/'Common'
        data.mkdir(); common.mkdir()
        self.info=SimpleNamespace(data_path=str(data),commondata_path=str(common),connected=True)
        self.account=SimpleNamespace(login=12345,server='Synthetic-Demo',trade_mode=0)
        self.now=datetime(2026,10,14,13,56,tzinfo=timezone.utc)
        self.exchange=SessionExchange(self.info,self.account,'XAUUSD',180,nonce='a'*32)
        self.settings=SimpleNamespace(symbol='XAUUSD',broker_utc_offset_minutes=180)
        self.addCleanup(self.temp.cleanup)

    def row(self,**changes):
        stamp=int(self.now.timestamp())
        fields={'version':'1','symbol':'XAUUSD','observed':str(stamp),'expires':str(stamp+15),
                'offset':'180','opened':str(stamp-3600),'closed':str(stamp+7200),
                'terminal':self.exchange.terminal,'nonce':self.exchange.nonce,
                'binding':self.exchange.binding}
        fields.update(changes)
        self.exchange.response_path.write_text(';'.join(fields.values()),encoding='ascii')

    def test_valid_native_session_is_normalized_and_account_identity_stays_off_disk(self):
        self.exchange.request(self.now)
        raw=self.exchange.request_path.read_text()
        self.assertNotIn('12345',raw)
        self.assertNotIn('Synthetic-Demo',raw)
        self.row()
        result=self.exchange.read(self.now)
        self.assertEqual(result['source'],'mt5_symbol_info_session_trade')
        self.assertIs(result['verified'],True)
        self.assertEqual(datetime.fromisoformat(result['session_close']),self.now+timedelta(hours=2))

    def test_foreign_binding_symbol_offset_and_corrupted_rows_fail_closed(self):
        for changed in ({'nonce':'b'*32},{'binding':'b'*64},{'terminal':'other'},
                        {'symbol':'GOLD'},{'offset':'120'},{'version':'2'},
                        {'closed':'not-a-time'},{'observed':'1.2'}):
            with self.subTest(changed=changed):
                self.row(**changed)
                with self.assertRaises(ValueError): self.exchange.read(self.now)

    def test_expired_future_and_closed_sessions_fail_closed(self):
        stamp=int(self.now.timestamp())
        for changed in ({'observed':str(stamp-16),'expires':str(stamp+1)},
                        {'observed':str(stamp+1)}, {'expires':str(stamp)},
                        {'closed':str(stamp)}, {'opened':str(stamp+1)},
                        {'opened':str(stamp-8*86400)}):
            with self.subTest(changed=changed):
                self.row(**changed)
                with self.assertRaises(ValueError): self.exchange.read(self.now)

    def test_missing_native_response_omits_evidence_instead_of_fabricating_hours(self):
        mt5=SimpleNamespace(terminal_info=lambda:self.info,account_info=lambda:self.account)
        self.assertIsNone(capture_session_context(mt5,self.settings,self.now))

    def test_account_switch_cannot_reuse_prior_native_response(self):
        self.row()
        changed=SimpleNamespace(login=67890,server='Synthetic-Demo',trade_mode=0)
        mt5=SimpleNamespace(terminal_info=lambda:self.info,account_info=lambda:changed)
        self.assertIsNone(capture_session_context(mt5,self.settings,self.now))

    def test_live_account_request_is_refused(self):
        self.account.trade_mode=2
        with self.assertRaises(ValueError):
            SessionExchange(self.info,self.account,'XAUUSD',180)

    def test_truncated_and_excess_rows_are_rejected(self):
        for raw in (b'1;XAUUSD',b'a'*2049,b'1;XAUUSD\n'):
            self.exchange.response_path.write_bytes(raw)
            with self.assertRaises(ValueError): self.exchange.read(self.now)


if __name__=='__main__':
    unittest.main()
