"""Bound read-only MQL trade-session snapshots to this terminal and Demo account.

The official Python SDK has no SymbolInfoSessionTrade call. The chart indicator
answers a short-lived, account-bound request in MT5's shared Files directory.
Missing/stale responses omit session evidence; they never invent trading hours.
"""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import secrets
import tempfile

SOURCE = 'mt5_symbol_info_session_trade'
_exchanges = {}


class SessionExchange:
    def __init__(self, info, account, symbol, offset, nonce=None):
        data, common = Path(info.data_path), Path(info.commondata_path)
        if not data.is_absolute() or not data.is_dir() or not common.is_absolute() or not common.is_dir():
            raise ValueError('Verified terminal directories required')
        self.terminal = data.resolve(strict=True).name.lower()
        if re.fullmatch(r'[a-z0-9_-]{1,80}', self.terminal) is None:
            raise ValueError('Invalid terminal marker')
        if re.fullmatch(r'(?:XAUUSD|GOLD)[A-Za-z0-9._#-]{0,24}', symbol, re.I) is None:
            raise ValueError('Invalid gold symbol')
        if type(offset) is not int or not -720 <= offset <= 840 or offset % 15:
            raise ValueError('Invalid verified broker offset')
        if account.trade_mode != 0 or type(account.login) is not int or account.login <= 0 or not account.server:
            raise ValueError('Demo binding required')
        self.common = common.resolve(strict=True)
        self.directory = self.common / 'Files' / 'MT5Bot'
        if not self.directory.resolve(strict=False).is_relative_to(self.common):
            raise ValueError('Session directory escaped terminal common Files')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.symbol, self.offset = symbol, offset
        self.nonce = secrets.token_hex(16) if nonce is None else nonce
        if re.fullmatch(r'[0-9a-f]{32}', self.nonce) is None:
            raise ValueError('Invalid session binding nonce')
        material = '\n'.join((self.nonce, self.terminal, account.server, str(account.login)))
        self.binding = hashlib.sha256(material.encode('utf-8')).hexdigest()
        self.request_path = self.directory / ('session_request_' + self.terminal + '.csv')
        self.response_path = self.directory / ('sessions_' + self.terminal + '.csv')

    def _safe_path(self, path):
        if (self.directory.resolve(strict=True).parent.parent != self.common
                or path.resolve(strict=False).parent != self.directory.resolve(strict=True)
                or (path.exists() and not path.is_file())):
            raise ValueError('Invalid session sidecar destination')

    def request(self, now):
        self._safe_path(self.request_path)
        stamp = int(now.timestamp())
        row = ';'.join(map(str, (1,self.symbol,stamp,stamp+20,self.offset,
                                  self.terminal,self.nonce,self.binding))).encode('ascii')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory,prefix='.sessions-',suffix='.tmp',delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(row)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary,self.request_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def read(self, now):
        self._safe_path(self.response_path)
        with self.response_path.open('rb') as stream:
            raw = stream.read(2049)
        if not raw or len(raw)>2048 or any(c<32 or c>126 for c in raw):
            raise ValueError('Invalid native session row')
        fields = raw.decode('ascii').split(';')
        if len(fields)!=10:
            raise ValueError('Incomplete native session row')
        version,symbol,observed,expires,offset,opened,closed,terminal,nonce,binding = fields
        if (version!='1' or symbol!=self.symbol or terminal!=self.terminal
                or nonce!=self.nonce or binding!=self.binding or str(self.offset)!=offset
                or any(re.fullmatch(r'[0-9]{1,12}', value) is None for value in (observed,expires,opened,closed))):
            raise ValueError('Foreign native session row')
        observed,expires,opened,closed=map(int,(observed,expires,opened,closed))
        stamp=now.timestamp()
        if (not 0<=stamp-observed<=15 or not observed<expires<=observed+20
                or stamp>=expires or not 0<opened<=stamp<closed
                or closed-opened>7*86400):
            raise ValueError('Unavailable or stale current trade session')
        iso=lambda value: datetime.fromtimestamp(value,timezone.utc).isoformat()
        return {'source':SOURCE,'verified':True,'as_of':iso(observed),
                'session_open':iso(opened),'session_close':iso(closed)}


def capture_session_context(mt5, settings, now):
    """A missing response blocks proposals downstream, while feed counts remain live."""
    try:
        if now.tzinfo is None or now.utcoffset() is None:
            return None
        info,account=mt5.terminal_info(),mt5.account_info()
        if info is None or account is None or not info.connected:
            return None
        key=(str(Path(info.data_path).resolve()),account.login,account.server,
             account.trade_mode,settings.symbol,settings.broker_utc_offset_minutes)
        exchange=_exchanges.get(key)
        if exchange is None:
            _exchanges.clear()
            exchange=SessionExchange(info,account,settings.symbol,settings.broker_utc_offset_minutes)
            _exchanges[key]=exchange
        exchange.request(now)
        return exchange.read(now)
    except (OSError,ValueError,TypeError,AttributeError,OverflowError):
        return None
