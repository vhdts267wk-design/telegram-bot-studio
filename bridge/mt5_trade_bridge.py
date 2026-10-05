"""User-started DEMO-only MT5 bridge for explicitly accepted Telegram offers.

No terminal login, trading-setting changes, order retries, or background install.
Official request/return contracts: https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py
"""

from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import hashlib
import hmac
import importlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
import tempfile
import time
from urllib import request
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

try:
    from bridge import mt5_market_bridge as market
except ModuleNotFoundError:
    import mt5_market_bridge as market


MAX_JSON_BYTES = 64 * 1024
POLL_SECONDS = 10
FEED_SECONDS = 60
TERMINAL_WARNING_SECONDS = 60
# A verified live connection was about three seconds ahead after its fixed
# broker offset was removed. Bound that clock skew; keep stale age at 30 seconds.
EXECUTABLE_QUOTE_FUTURE_TOLERANCE_SECONDS = 5
MAX_DRIFT_R = Decimal("0.1")
DEMO_VOLUME = Decimal("0.01")
STRATEGY_ID = "ema9-21-atr14-v1"
# Explicit rejection codes are safe failures. Processing errors, placed/partial,
# timeout, locked, connection loss and unfamiliar codes remain uncertain.
REJECTION_CODES = frozenset({
    10004, 10006, 10007, 10013, 10014, 10015, 10016, 10017, 10018,
    10019, 10020, 10021, 10022, 10024, 10026, 10027, 10029, 10030,
    10032, 10033, 10034, 10035, 10036, 10038, 10039, 10040, 10041,
    10042, 10043, 10044, 10045, 10046,
})


class GuardError(RuntimeError):
    """Only fixed, non-private explanations may be used for this exception."""

    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = safe_integer(code)


@dataclass(frozen=True)
class Settings:
    market: market.Settings
    state_directory: Path
    account_mode: str
    volume: Decimal
    enable_orders: bool
    startup_report: Path | None = None


STARTUP_STAGES = {
    "configuration": "reading the local configuration",
    "terminal_process_check": "checking the existing terminal process",
    "sdk_load": "loading the terminal client",
    "lock_directory": "checking the helper state directory for the process lock",
    "lock_file_open": "opening the local process lock file",
    "lock_file_prepare": "preparing the local process lock file",
    "lock_acquire": "acquiring the exclusive local helper lock",
    "ledger_directory": "checking the helper state directory for the journal",
    "ledger_open": "opening the local journal database",
    "ledger_schema": "initializing the local journal tables",
    "ledger_identity": "initializing the local journal identity",
    "terminal_initialize": "connecting to the existing terminal",
    "account_check": "checking the confirmed local account",
    "ledger_binding": "checking the saved local account binding",
    "broker_settings": "checking the broker symbol and volume",
    "registration": "registering the local helper with the service",
    "ready": "waiting for approved requests",
    "first_feed": "sending the first market update",
    "bridge_loop": "running the helper",
}
DIAGNOSTIC_ERRORS = frozenset({
    "PermissionError", "FileNotFoundError", "NotADirectoryError", "OSError",
    "OperationalError", "DatabaseError", "GuardError", "HTTPError", "URLError",
    "TimeoutError", "RuntimeError", "ValueError", "TypeError", "AttributeError",
    "KeyboardInterrupt", "MarketDataError",
})


def diagnostic_error(error):
    name = type(error).__name__
    return name if name in DIAGNOSTIC_ERRORS else "Exception"


def expected_startup_report():
    # This optional local diagnostic belongs beside the human launcher, outside
    # the repository and private state directory. No user-supplied write target.
    task = Path(__file__).absolute().parent.parent.parent.parent
    return task / "outputs" / "MT5-Startup-Status.json"


def validated_startup_report(value, state_directory=None):
    if value is None:
        return None
    expected = expected_startup_report()
    if os.path.normcase(str(Path(value).absolute())) != os.path.normcase(str(expected)):
        raise GuardError("The optional startup report destination does not match the existing output file.")
    if not report_destination_is_safe(expected):
        raise GuardError("The optional startup report output destination could not be safely verified.")
    parent = expected.parent.resolve(strict=True)
    if state_directory is not None:
        state = Path(state_directory).expanduser().resolve()
        if parent == state or state in parent.parents:
            raise GuardError("The optional startup report cannot be written inside helper state.")
    return expected


def report_destination_is_safe(path):
    try:
        for parent in path.parents:
            info = os.lstat(parent)
            if getattr(info, "st_file_attributes", 0) & 0x400 or not stat.S_ISDIR(info.st_mode):
                return False
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return True
        return (not getattr(info, "st_file_attributes", 0) & 0x400
                and stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    except OSError:
        return False


class StartupDiagnostics:
    """Fixed fields only: never paths, keys, pairing codes or account details."""

    def __init__(self, path=None):
        self.path = validated_startup_report(path)
        self.stage = "configuration"
        self.original_error = None
        self.record = {
            "report_version": 1, "kind": "mt5_bridge_startup",
            "attempt_id": uuid4().hex, "started_utc": iso_date(now_utc()),
            "updated_utc": None, "status": "starting", "stage": self.stage,
            "error_class": None, "registration_succeeded": None,
            "bridge_ready": False, "already_paired": None,
            "first_feed_accepted": None,
        }

    def set_stage(self, stage):
        self.stage = stage if stage in STARTUP_STAGES else "configuration"

    def save(self, status, error=None):
        if self.path is None:
            return False
        self.record.update(
            status=status if status in ("starting", "ready", "stopped", "interrupted") else "stopped",
            stage=self.stage, updated_utc=iso_date(now_utc()),
            error_class=self.original_error or (diagnostic_error(error) if error is not None else None),
        )
        try:
            # The existing outputs directory must already exist. No mkdir,
            # ledger access, source data or exception strings are used here.
            if not report_destination_is_safe(self.path):
                return False
            temporary = None
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                                 prefix="mt5-startup-", suffix=".tmp", delete=False) as file:
                    temporary = Path(file.name)
                    json.dump(self.record, file, indent=2)
                    file.write("\n")
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return True
        except Exception:
            return False


def startup_stage(diagnostics, stage):
    if diagnostics is not None:
        diagnostics.set_stage(stage)


def positive_decimal(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise GuardError("Invalid numeric trade parameter.")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise GuardError("Invalid numeric trade parameter.") from None
    if not number.is_finite() or number <= 0:
        raise GuardError("Trade parameters must be positive and finite.")
    return number


def safe_integer(value) -> int:
    return value if type(value) is int and 0 <= value <= (1 << 63) - 1 else 0


def uuid_text(value) -> str:
    if type(value) is not str:
        raise GuardError("Invalid trade identity.")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise GuardError("Invalid trade identity.") from None
    if str(parsed) != value.lower():
        raise GuardError("Invalid trade identity.")
    return str(parsed)


def utc_date(value) -> datetime:
    if type(value) is not str or len(value) > 64:
        raise GuardError("Invalid trade timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise GuardError("Invalid trade timestamp.") from None
    if parsed.utcoffset() is None:
        raise GuardError("Trade timestamps must include a timezone.")
    return parsed.astimezone(timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_date(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def load_settings(args, environ=None) -> Settings:
    if not args.enable_orders or args.account_mode != "demo" or positive_decimal(args.volume) != DEMO_VOLUME:
        raise GuardError("This bridge requires explicit DEMO order opt-in and exactly 0.01 lots.")
    configured = market.load_settings(args, environ)
    if len(configured.key) < 32:
        raise GuardError("The private bridge key must contain at least 32 characters.")
    report = getattr(args, "startup_report", None)
    state = Path(args.state_directory).expanduser().resolve()
    return Settings(configured, state, "demo", DEMO_VOLUME, True,
                    validated_startup_report(report, state))


def terminal_is_running(terminal: Path) -> bool:
    """Read process executable paths; never start the terminal ourselves."""
    if os.name != "nt":
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = kernel.Process32FirstW.argtypes
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        return False
    expected = str(terminal.resolve()).casefold()
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        valid = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while valid:
            if entry.szExeFile.casefold() == terminal.name.casefold():
                handle = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
                if handle:
                    try:
                        buffer = ctypes.create_unicode_buffer(32768)
                        length = wintypes.DWORD(len(buffer))
                        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                            if str(Path(buffer.value).resolve()).casefold() == expected:
                                return True
                    finally:
                        kernel.CloseHandle(handle)
            valid = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        return False
    finally:
        kernel.CloseHandle(snapshot)


class Ledger:
    """Durably reserve each offer before any broker call can send an order."""

    def __init__(self, directory: Path, diagnostics=None):
        self.diagnostics = diagnostics
        startup_stage(diagnostics, "ledger_directory")
        directory.mkdir(parents=True, exist_ok=True)
        startup_stage(diagnostics, "ledger_open")
        self.db = sqlite3.connect(directory / "trade-ledger.sqlite3", timeout=10)
        startup_stage(diagnostics, "ledger_schema")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS offers (
            id TEXT PRIMARY KEY, claim_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
            result TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0)""")
        self.db.commit()
        startup_stage(diagnostics, "ledger_identity")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('device_id', ?)", (str(uuid4()),))
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('binding_salt', ?)", (secrets.token_hex(32),))

    def close(self):
        self.db.close()

    def value(self, name: str) -> str | None:
        row = self.db.execute("SELECT value FROM metadata WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    @property
    def device_id(self):
        return self.value("device_id")

    def bind(self, binding: str):
        startup_stage(self.diagnostics, "ledger_binding")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('binding', ?)", (binding,))
            if self.value("binding") != binding:
                raise GuardError("The terminal/account or saved local configuration changed. Orders are blocked.")

    def reserve(self, offer: dict) -> tuple[bool, dict]:
        offer_id, claim_id = uuid_text(offer.get("id")), uuid_text(offer.get("claim_id"))
        encoded = json.dumps(offer.get("payload"), sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
            raise GuardError("Trade payload is too large.")
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        initial = {"status": "unknown", "code": 0}
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO offers (id,claim_id,payload_hash,result) VALUES (?,?,?,?)",
                (offer_id, claim_id, digest, json.dumps(initial)),
            )
            row = self.db.execute("SELECT payload_hash,result FROM offers WHERE id=?", (offer_id,)).fetchone()
            if row[0] != digest:
                raise GuardError("An existing trade identity was changed. Orders are blocked.")
            # A new server lease can receive the persisted outcome, never a retry.
            self.db.execute("UPDATE offers SET claim_id=?,acknowledged=0 WHERE id=?", (claim_id, offer_id))
        return cursor.rowcount == 1, json.loads(row[1])

    def complete(self, offer_id: str, result: dict):
        with self.db:
            self.db.execute("UPDATE offers SET result=?,acknowledged=0 WHERE id=?", (json.dumps(result), offer_id))

    def pending(self):
        rows = self.db.execute("SELECT id,claim_id,result FROM offers WHERE acknowledged=0 ORDER BY rowid LIMIT 100").fetchall()
        return [{"offer_id": row[0], "claim_id": row[1], "result": json.loads(row[2])} for row in rows]

    def acknowledge(self, offer_id: str, claim_id: str):
        with self.db:
            self.db.execute("UPDATE offers SET acknowledged=1 WHERE id=? AND claim_id=?", (offer_id, claim_id))


class ProcessLock:
    """Keep one local bridge alive; OS releases the lock if the process dies."""

    def __init__(self, directory: Path, diagnostics=None):
        startup_stage(diagnostics, "lock_directory")
        directory.mkdir(parents=True, exist_ok=True)
        startup_stage(diagnostics, "lock_file_open")
        self.handle = open(directory / "bridge.lock", "a+b")
        try:
            startup_stage(diagnostics, "lock_file_prepare")
            if self.handle.seek(0, os.SEEK_END) == 0:
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            startup_stage(diagnostics, "lock_acquire")
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # Permits synthetic CI tests; main still requires Windows.
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if diagnostics is not None:
                diagnostics.original_error = diagnostic_error(error)
            self.handle.close()
            raise GuardError("Another bridge is already running for this local configuration.") from None

    def close(self):
        # Closing releases the OS lock; the file stays for the next user start.
        self.handle.close()


def check_bound_account(mt5, settings: Settings, ledger: Ledger):
    if not settings.enable_orders or settings.account_mode != "demo" or settings.volume != DEMO_VOLUME:
        raise GuardError("Only explicitly enabled DEMO orders of 0.01 lots are allowed.")
    market.check_terminal(mt5, settings.market.terminal)
    terminal = mt5.terminal_info()
    account = mt5.account_info()
    if (
        terminal is None or not getattr(terminal, "trade_allowed", False)
        or getattr(terminal, "tradeapi_disabled", True)
        or account is None or not getattr(account, "trade_allowed", False)
        or not getattr(account, "trade_expert", False)
    ):
        raise GuardError("MT5 must explicitly permit external Python trading on the selected demo account.")
    mode = getattr(account, "trade_mode", None)
    if type(mode) is not int or mode != getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0):
        raise GuardError("The connected MT5 account is not DEMO. Orders are blocked.")
    login, server = getattr(account, "login", None), getattr(account, "server", None)
    if type(login) is not int or login <= 0 or type(server) is not str or not server or len(server) > 256:
        raise GuardError("The local account identity could not be verified.")
    private_identity = json.dumps([server, login], separators=(",", ":")).encode("utf-8")
    identity_hash = hmac.new(bytes.fromhex(ledger.value("binding_salt")), private_identity, hashlib.sha256).hexdigest()
    bound = json.dumps({
        "account_hash": identity_hash, "terminal": str(settings.market.terminal).casefold(),
        "symbol": settings.market.symbol, "mode": settings.account_mode,
        "volume": str(settings.volume), "origin": urlsplit(settings.market.url).netloc,
    }, sort_keys=True)
    ledger.bind(bound)
    return account


def validate_offer(offer: dict, settings: Settings, now: datetime):
    if type(offer) is not dict or type(offer.get("payload")) is not dict:
        raise GuardError("Invalid accepted trade.")
    uuid_text(offer.get("id"))
    uuid_text(offer.get("claim_id"))
    payload = offer["payload"]
    if (
        payload.get("symbol") != settings.market.symbol
        or payload.get("account_mode") != settings.account_mode
        or positive_decimal(payload.get("volume")) != settings.volume
        or payload.get("direction") not in ("BUY", "SELL")
        or payload.get("strategy_id") != STRATEGY_ID
    ):
        raise GuardError("The accepted trade does not match the confirmed local settings.")
    expires = utc_date(offer.get("expires_at"))
    bar = utc_date(payload.get("bar_time"))
    if expires <= now or expires > now + timedelta(minutes=30):
        raise GuardError("The accepted trade has expired or has an invalid deadline.")
    if bar.timestamp() % 900 or bar + timedelta(minutes=15) > now or now - bar > timedelta(minutes=45):
        raise GuardError("The accepted trade is not from a recent completed M15 candle.")
    entry, stop, target = (positive_decimal(payload.get(name)) for name in ("entry", "stop", "target"))
    if payload["direction"] == "BUY" and not stop < entry < target:
        raise GuardError("Invalid accepted BUY protection levels.")
    if payload["direction"] == "SELL" and not target < entry < stop:
        raise GuardError("Invalid accepted SELL protection levels.")
    if "max_drift_r" in payload and positive_decimal(payload["max_drift_r"]) != MAX_DRIFT_R:
        raise GuardError("The accepted trade has an unsupported price-drift limit.")
    return payload, entry, stop, target


def prepare_order(mt5, settings: Settings, ledger: Ledger, offer: dict, now: datetime):
    payload, entry, stop, target = validate_offer(offer, settings, now)
    account = check_bound_account(mt5, settings, ledger)
    symbol = mt5.symbol_info(settings.market.symbol)
    if symbol is None or getattr(symbol, "currency_base", None) != "XAU" or getattr(symbol, "currency_profit", None) != "USD":
        raise GuardError("The broker symbol must explicitly identify XAU in USD.")
    direction = payload["direction"]
    symbol_mode = getattr(symbol, "trade_mode", None)
    if type(symbol_mode) is not int or (symbol_mode != 4 and symbol_mode != (1 if direction == "BUY" else 2)):
        raise GuardError("This symbol does not permit the accepted direction.")
    order_modes = getattr(symbol, "order_mode", 0)
    if type(order_modes) is not int or (order_modes & 49) != 49:
        raise GuardError("The symbol must support market orders with attached stop loss and take profit.")
    minimum, maximum, step = (positive_decimal(getattr(symbol, name, None)) for name in ("volume_min", "volume_max", "volume_step"))
    if not minimum <= settings.volume <= maximum or settings.volume % step:
        raise GuardError("The confirmed 0.01 lots do not match this broker's volume limits/step.")
    tick_size = positive_decimal(getattr(symbol, "trade_tick_size", None))
    point = positive_decimal(getattr(symbol, "point", None))
    if stop % tick_size or target % tick_size:
        raise GuardError("Accepted protection levels do not match the broker price grid.")
    # Netting orders can reduce or change an existing position. Do not do that.
    margin_mode = getattr(account, "margin_mode", None)
    if type(margin_mode) is not int or margin_mode not in (0, 1, 2):
        raise GuardError("The account position model could not be verified.")
    if margin_mode != 2:
        positions = mt5.positions_get(symbol=settings.market.symbol)
        orders = mt5.orders_get(symbol=settings.market.symbol)
        if positions is None or orders is None or len(positions) or len(orders):
            raise GuardError("A netting account must have no existing positions or pending orders for this symbol.")
    tick = mt5.symbol_info_tick(settings.market.symbol)
    if tick is None:
        raise GuardError("A fresh executable broker quote is required.")
    stamp = market.broker_timestamp_utc(getattr(tick, "time", None), settings.market)
    bid, ask = positive_decimal(getattr(tick, "bid", None)), positive_decimal(getattr(tick, "ask", None))
    if ask < bid or not -EXECUTABLE_QUOTE_FUTURE_TOLERANCE_SECONDS <= now.timestamp() - stamp <= 30:
        raise GuardError("The executable broker quote is stale, future-dated, or crossed.")
    price = ask if direction == "BUY" else bid
    risk = abs(entry - stop)
    used_guard = ask - bid + abs(price - entry)
    allowance = risk * MAX_DRIFT_R
    if used_guard > allowance:
        raise GuardError("Spread plus entry drift exceeds 0.1 of the original stop distance.")
    if direction == "BUY" and not stop < price < target:
        raise GuardError("BUY protection levels no longer surround the executable price.")
    if direction == "SELL" and not target < price < stop:
        raise GuardError("SELL protection levels no longer surround the executable price.")
    stops_level = getattr(symbol, "trade_stops_level", None)
    if type(stops_level) is not int or stops_level < 0:
        raise GuardError("Broker stop-distance restrictions could not be verified.")
    distance = point * stops_level
    if direction == "BUY" and (bid - stop < distance or target - bid < distance):
        raise GuardError("Accepted BUY stops are too close for this broker.")
    if direction == "SELL" and (stop - ask < distance or ask - target < distance):
        raise GuardError("Accepted SELL stops are too close for this broker.")
    filling = getattr(symbol, "filling_mode", None)
    execution = getattr(symbol, "trade_exemode", None)
    if type(filling) is not int or type(execution) is not int or execution not in (0, 1, 2, 3):
        raise GuardError("The broker's execution policy could not be verified.")
    if execution in (0, 1) or filling & 1:
        policy = mt5.ORDER_FILLING_FOK
    elif filling & 2:
        policy = mt5.ORDER_FILLING_IOC
    else:
        raise GuardError("The broker does not support a cancellable market filling policy.")
    digest = hashlib.sha256(offer["id"].encode("ascii")).hexdigest()
    result = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": settings.market.symbol,
        "volume": float(settings.volume), "type": mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL,
        "sl": float(stop), "tp": float(target),
        "deviation": int(((allowance - used_guard) / point).to_integral_value(rounding=ROUND_FLOOR)),
        "magic": int(digest[:8], 16), "comment": "tg:" + digest[:20],
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": policy,
    }
    # MetaQuotes documents that market-execution deals do not set price. Such
    # brokers may ignore deviation; the local guard is a pre-send check only.
    if execution != 2:
        result["price"] = float(price)
    return result


def execute_offer(mt5, settings: Settings, ledger: Ledger, offer: dict, clock=now_utc):
    created, recorded = ledger.reserve(offer)
    if not created:
        return recorded
    sent = False
    result = {"status": "failed", "code": 0}
    try:
        checked_request = prepare_order(mt5, settings, ledger, offer, clock())
        checked = mt5.order_check(checked_request)
        if checked is None or type(getattr(checked, "retcode", None)) is not int or checked.retcode != 0:
            raise GuardError("MT5 rejected the trade preflight.", getattr(checked, "retcode", 0))
        # Recheck account, expiry, volume, symbol and current quote after preflight.
        final_request = prepare_order(mt5, settings, ledger, offer, clock())
        # Price/deviation can be refreshed inside the same fixed risk guard.
        # A change to any other checked request field requires a new offer.
        fixed_checked = {key: value for key, value in checked_request.items() if key not in ("price", "deviation")}
        fixed_final = {key: value for key, value in final_request.items() if key not in ("price", "deviation")}
        if fixed_checked != fixed_final:
            raise GuardError("The broker execution policy changed after preflight.")
        sent = True
        response = mt5.order_send(final_request)  # The only execution call; NEVER retry it.
        code = safe_integer(getattr(response, "retcode", None))
        ticket = safe_integer(getattr(response, "order", None))
        result = {"status": "unknown", "code": code}
        if code == getattr(mt5, "TRADE_RETCODE_DONE", 10009):
            if ticket and positive_decimal(getattr(response, "volume", None)) == settings.volume:
                result = {"status": "filled", "code": code, "executed_at": iso_date(clock())}
        elif code in REJECTION_CODES:
            result = {"status": "failed", "code": code}
        if ticket:
            result["order_ticket"] = ticket
    except Exception as error:
        code = result.get("code", 0) if sent else (error.code if isinstance(error, GuardError) else 0)
        result = {"status": "unknown" if sent else "failed", "code": safe_integer(code)}
    # If saving fails after execution, the initial committed UNKNOWN survives.
    ledger.complete(uuid_text(offer["id"]), result)
    return result


def api_post(settings: Settings, route: str, payload: dict):
    if route not in ("register", "poll", "result", "market"):
        raise GuardError("Invalid bridge route.")
    market.validate_feed_url(settings.market.url)
    parts = urlsplit(settings.market.url)
    path = "/api/market/feed" if route == "market" else "/api/mt5/" + route
    url = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_JSON_BYTES:
        raise GuardError("Bridge request is too large.")
    req = request.Request(url, data=body, method="POST", headers={
        "Authorization": "Bearer " + settings.market.key, "Content-Type": "application/json",
    })
    opener = request.build_opener(market.NoRedirect())
    with opener.open(req, timeout=15) as response:
        if response.status not in (200, 201, 202, 204):
            raise GuardError("The bridge request was not accepted.")
        content = response.read(MAX_JSON_BYTES + 1)
        if len(content) > MAX_JSON_BYTES:
            raise GuardError("Bridge response is too large.")
        if not content:
            return {}
        result = json.loads(content)
        if type(result) is not dict:
            raise GuardError("Invalid bridge response.")
        return result


def pairing_registration(settings: Settings, ledger: Ledger):
    code = base64.b32encode(secrets.token_bytes(10)).decode("ascii").rstrip("=")
    return code, {
        "device_id": ledger.device_id, "symbol": settings.market.symbol,
        "account_mode": settings.account_mode, "volume": float(settings.volume),
        "pair_code_hash": hashlib.sha256(code.encode("ascii")).hexdigest(),
    }


def flush_results(settings: Settings, ledger: Ledger, post=api_post):
    for pending in ledger.pending():
        try:
            post(settings, "result", {"device_id": ledger.device_id, **pending})
        except HTTPError as error:
            if error.code != 409:
                raise
            # A closed claim cannot accept a late outcome. Keep its local
            # outcome for inspection, stop delivery retries, never resend order.
            ledger.acknowledge(pending["offer_id"], pending["claim_id"])
            print("Trade result claim is closed on the server. Check MT5 if Telegram shows unknown; the order is never retried.", file=sys.stderr, flush=True)
            continue
        ledger.acknowledge(pending["offer_id"], pending["claim_id"])


def execution_metadata(symbol) -> dict:
    """Publish broker grid restrictions so levels are fixed before Accept."""
    if symbol is None:
        raise GuardError("Broker execution metadata is unavailable.")
    tick_size = market.positive_price(getattr(symbol, "trade_tick_size", None))
    point = market.positive_price(getattr(symbol, "point", None))
    digits = getattr(symbol, "digits", None)
    stops_level = getattr(symbol, "trade_stops_level", None)
    if type(digits) is not int or not 0 <= digits <= 10:
        raise GuardError("Broker price digits are invalid.")
    if type(stops_level) is not int or not 0 <= stops_level <= 1000000:
        raise GuardError("Broker stop-distance restrictions are invalid.")
    return {"tick_size": tick_size, "point": point, "digits": digits, "stops_level": stops_level}


def run_bridge(mt5, settings: Settings, *, post=api_post, clock=now_utc, sleep=time.sleep, diagnostics=None):
    ledger, process_lock = None, None
    initialized = False
    diagnostics = diagnostics or StartupDiagnostics(settings.startup_report)
    try:
        validated_startup_report(diagnostics.path, settings.state_directory)
        process_lock = ProcessLock(settings.state_directory, diagnostics=diagnostics)
        ledger = Ledger(settings.state_directory, diagnostics=diagnostics)
        diagnostics.set_stage("terminal_process_check")
        if not terminal_is_running(settings.market.terminal):
            raise GuardError("Open and connect the selected MT5 demo terminal before starting the bridge.")
        diagnostics.set_stage("terminal_initialize")
        if not mt5.initialize(str(settings.market.terminal), timeout=15000):
            raise GuardError("The existing MT5 terminal could not be connected.")
        initialized = True
        diagnostics.set_stage("account_check")
        check_bound_account(mt5, settings, ledger)
        # Verify broker volume and metadata before exposing pairing. A synthetic
        # order is never sent here; volume/metadata are checked without order_check.
        diagnostics.set_stage("broker_settings")
        symbol = mt5.symbol_info(settings.market.symbol)
        if symbol is None or getattr(symbol, "currency_base", None) != "XAU" or getattr(symbol, "currency_profit", None) != "USD":
            raise GuardError("Choose the broker's exact XAU/USD symbol.")
        minimum, maximum, step = (positive_decimal(getattr(symbol, name, None)) for name in ("volume_min", "volume_max", "volume_step"))
        if not minimum <= settings.volume <= maximum or settings.volume % step:
            raise GuardError("This broker does not accept exactly 0.01 lots.")
        code, registration = pairing_registration(settings, ledger)
        diagnostics.set_stage("registration")
        diagnostics.record["registration_succeeded"] = False
        registered = post(settings, "register", registration)
        diagnostics.record["registration_succeeded"] = True
        paired = registered.get("paired")
        diagnostics.record["already_paired"] = paired if type(paired) is bool else None
        diagnostics.record["bridge_ready"] = True
        diagnostics.set_stage("ready")
        diagnostics.save("ready")
        print("DEMO bridge ready. Each Telegram Accept can submit one 0.01-lot market order.", flush=True)
        if registered.get("paired") is True:
            print("This local demo bridge is already paired with your private bot chat.", flush=True)
        else:
            print("In your private bot chat, send: /connect_mt5 " + code, flush=True)
        next_feed = 0.0
        terminal_failure = None
        last_terminal_warning = 0.0
        while terminal_is_running(settings.market.terminal):
            try:
                check_bound_account(mt5, settings, ledger)
                if terminal_failure is not None:
                    print("MT5 connection recovered. The selected demo account is verified; bridge processing resumed.", flush=True)
                    terminal_failure = None
                flush_results(settings, ledger, post)
                if time.monotonic() >= next_feed:
                    next_feed = time.monotonic() + FEED_SECONDS
                    try:
                        first_feed = diagnostics.record["first_feed_accepted"] is not True
                        if first_feed:
                            diagnostics.set_stage("first_feed")
                        payload = market.build_payload(mt5, settings.market, clock())
                        payload["device_id"] = ledger.device_id
                        payload["execution"] = execution_metadata(mt5.symbol_info(settings.market.symbol))
                        post(settings, "market", payload)
                        if first_feed:
                            diagnostics.record["first_feed_accepted"] = True
                            diagnostics.save("ready")
                    except Exception as error:
                        if diagnostics.record["first_feed_accepted"] is None:
                            diagnostics.record["first_feed_accepted"] = False
                            diagnostics.save("ready", error)
                        # An unchanged/stale market upload must not disable the
                        # outcome outbox or polling for accepted decisions.
                        print("Market update unavailable (" + type(error).__name__ + ").", file=sys.stderr, flush=True)
                diagnostics.set_stage("bridge_loop")
                reply = post(settings, "poll", {"device_id": ledger.device_id})
                offer = reply.get("trade")
                if offer is not None:
                    execute_offer(mt5, settings, ledger, offer, clock)
                    flush_results(settings, ledger, post)
            except GuardError:
                raise  # Changes to the bound terminal/account require a fresh user start.
            except market.MarketDataError as error:
                reason = getattr(error, "reason_code", None)
                message = market.TERMINAL_FAILURE_MESSAGES.get(reason)
                if message is None:
                    print("Bridge update unavailable (MarketDataError).", file=sys.stderr, flush=True)
                else:
                    observed = time.monotonic()
                    if reason != terminal_failure or observed - last_terminal_warning >= TERMINAL_WARNING_SECONDS:
                        print("Bridge paused (" + reason + "): " + message
                              + " Orders remain blocked until the selected terminal and demo account are verified.",
                              file=sys.stderr, flush=True)
                        last_terminal_warning = observed
                    terminal_failure = reason
            except Exception as error:
                print("Bridge update unavailable (" + type(error).__name__ + ").", file=sys.stderr, flush=True)
            sleep(POLL_SECONDS)
        print("MT5 terminal closed; bridge stopped.", flush=True)
        diagnostics.save("stopped")
        return 0
    except KeyboardInterrupt:
        print("Bridge stopped. Uncertain trades must be checked in MT5; they are never retried.", flush=True)
        diagnostics.save("interrupted", KeyboardInterrupt())
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, GuardError) else type(error).__name__
        print("Bridge stopped: " + message + " (startup stage: " + STARTUP_STAGES[diagnostics.stage] + ").", file=sys.stderr, flush=True)
        if diagnostics.save("stopped", error):
            print("A sanitized startup report was saved beside the launcher.", file=sys.stderr, flush=True)
        elif diagnostics.path is not None:
            print("The sanitized startup report could not be saved.", file=sys.stderr, flush=True)
        return 1
    finally:
        if initialized:
            try:
                mt5.shutdown()
            except Exception:
                pass
        if ledger is not None:
            ledger.close()
        if process_lock is not None:
            process_lock.close()


def main(argv=None):
    parser = market.PrivateArgumentParser(description=__doc__)
    parser.add_argument("--terminal", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--account-mode", required=True, choices=("demo",))
    parser.add_argument("--volume", required=True)
    parser.add_argument("--state-directory", required=True)
    parser.add_argument("--enable-orders", action="store_true")
    parser.add_argument("--startup-report", help="Optional sanitized startup JSON in an existing output directory")
    args = parser.parse_args(argv)
    diagnostics = StartupDiagnostics()
    try:
        diagnostics.path = validated_startup_report(args.startup_report, args.state_directory)
        diagnostics.save("starting")
        if os.name != "nt":
            raise GuardError("The local trade bridge requires Windows.")
        settings = load_settings(args)
        diagnostics.set_stage("terminal_process_check")
        if not terminal_is_running(settings.market.terminal):
            raise GuardError("Start the selected MT5 demo terminal yourself first.")
        diagnostics.set_stage("sdk_load")
        mt5 = importlib.import_module("MetaTrader5")
    except Exception as error:
        message = str(error) if isinstance(error, GuardError) else type(error).__name__
        print("Bridge unavailable: " + message, file=sys.stderr)
        diagnostics.save("stopped", error)
        return 1
    return run_bridge(mt5, settings, diagnostics=diagnostics)


if __name__ == "__main__":
    raise SystemExit(main())
