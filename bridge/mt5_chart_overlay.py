"""Display-only MT5 proposal export; no SDK, UI, claims or order operations."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import hashlib
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from uuid import UUID


DISPLAY_TTL_SECONDS = 25
FEED_REFRESH_SECONDS = 5
STATES = frozenset({"offered", "requested", "preparing", "prepared"})
FIELDS = frozenset({
    "version", "workflow", "offer_id", "status", "symbol", "timeframe", "direction",
    "entry", "entry_zone_low", "entry_zone_high", "stop", "target", "price_digits",
    "execution", "bar_time", "expires_at",
    "strategy_id", "strategy_version", "policy_id", "horizon_seconds", "strategy_fingerprint",
    "qualification_id", "direction_bar_time", "confirmation_bar_time",
})


class OverlayError(ValueError):
    """A fixed, non-private display validation failure."""


def utc(value):
    if type(value) is str and len(value) <= 40:
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise OverlayError("Invalid chart timestamp") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OverlayError("Invalid chart timestamp")
    return value.astimezone(timezone.utc)


def positive(value):
    if type(value) not in (int, float) or not 0 < value <= 1_000_000_000_000 or not math.isfinite(value):
        raise OverlayError("Invalid chart price")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise OverlayError("Invalid chart price") from None


def safe_text(value, *, limit=128):
    if type(value) is not str or not 0 < len(value) <= limit:
        raise OverlayError("Invalid chart text")
    if any(not 32 <= ord(char) <= 126 or char in ';"' for char in value):
        raise OverlayError("Invalid chart text")
    return value


def metadata(value):
    if type(value) is not dict or set(value) != {"tick_size", "point", "digits", "stops_level"}:
        raise OverlayError("Invalid chart precision")
    digits, stops = value["digits"], value["stops_level"]
    if type(digits) is not int or not 0 <= digits <= 8 or type(stops) is not int or not 0 <= stops <= 1_000_000:
        raise OverlayError("Invalid chart precision")
    tick, point = positive(value["tick_size"]), positive(value["point"])
    quantum = Decimal(1).scaleb(-digits)
    if tick < quantum or tick % quantum:
        raise OverlayError("Invalid chart precision")
    return tick, point, digits, stops


@dataclass(frozen=True)
class DisplayProposal:
    direction: str
    entry: Decimal
    zone_low: Decimal
    zone_high: Decimal
    stop: Decimal
    target: Decimal
    digits: int
    bar: datetime
    expires: datetime


def fresh_quote(value, now):
    if type(value) is not dict or not {"time", "bid", "ask"} <= set(value):
        raise OverlayError("Invalid chart quote")
    bid, ask = positive(value["bid"]), positive(value["ask"])
    stamp = utc(value["time"])
    if ask < bid or not timedelta(seconds=-5) <= now - stamp <= timedelta(seconds=10):
        raise OverlayError("Stale chart quote")
    return stamp


def validate_proposal(value, *, symbol, execution, quote, observed_at):
    """Validate the separate chart DTO without fabricating a claim or draft."""
    now = utc(observed_at)
    fresh_quote(quote, now)
    if type(value) is not dict or set(value) != FIELDS:
        raise OverlayError("Invalid chart proposal")
    if (
        type(value["version"]) is not int or value["version"] != 2
        or type(value["workflow"]) is not str or value["workflow"] != "chart_overlay"
        or type(value["status"]) is not str or value["status"] not in STATES
        or value["symbol"] != symbol or value["timeframe"] != "M1"
        or type(value["direction"]) is not str or value["direction"] not in ("BUY", "SELL")
    ):
        raise OverlayError("Incompatible chart proposal")
    if (
        value["strategy_id"] != "mtf-ema-pullback-60m-v1"
        or type(value["strategy_version"]) is not int or value["strategy_version"] != 1
        or value["policy_id"] != "mtf-manual-demo-cost-risk-v1"
        or type(value["horizon_seconds"]) is not int or value["horizon_seconds"] != 3600
        or any(type(value[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None for key in ("strategy_fingerprint", "qualification_id"))
    ):
        raise OverlayError("Unqualified chart proposal")
    try:
        if type(value["offer_id"]) is not str or str(UUID(value["offer_id"])) != value["offer_id"]:
            raise ValueError
    except (ValueError, AttributeError):
        raise OverlayError("Invalid chart reference") from None
    tick, _, digits, _ = metadata(value["execution"])
    if metadata(execution) != metadata(value["execution"]) or type(value["price_digits"]) is not int or value["price_digits"] != digits:
        raise OverlayError("Changed chart precision")
    entry, low, high, stop, target = (positive(value[key]) for key in (
        "entry", "entry_zone_low", "entry_zone_high", "stop", "target",
    ))
    if any(price % tick for price in (entry, low, high, stop, target)):
        raise OverlayError("Off-grid chart levels")
    ordered = stop < low <= entry <= high < target if value["direction"] == "BUY" else target < low <= entry <= high < stop
    if not ordered or low >= high:
        raise OverlayError("Unordered chart levels")
    allowance = abs(entry - stop) * Decimal("0.1")
    expected_low = ((entry - allowance) / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    expected_high = ((entry + allowance) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    if low != expected_low or high != expected_high:
        raise OverlayError("Changed chart entry zone")
    bar, expires = utc(value["bar_time"]), utc(value["expires_at"])
    direction, confirmation = utc(value["direction_bar_time"]), utc(value["confirmation_bar_time"])
    trigger = bar.timestamp()
    if (
        any(stamp.microsecond for stamp in (bar, direction, confirmation)) or trigger % 60
        or not trigger + 60 <= now.timestamp() <= trigger + 360
        or direction.timestamp() != int((trigger + 60) // 900) * 900 - 900
        or confirmation.timestamp() != int((trigger + 60) // 300) * 300 - 300
    ):
        raise OverlayError("Invalid chart reference candle")
    if not now < expires <= min(now + timedelta(minutes=5), bar + timedelta(minutes=6)):
        raise OverlayError("Expired chart proposal")
    return DisplayProposal(value["direction"], entry, low, high, stop, target, digits, bar, expires)


def terminal_key(data_path):
    path = Path(data_path)
    if not path.is_absolute() or not path.is_dir():
        raise OverlayError("Invalid terminal data directory")
    key = safe_text(path.resolve(strict=True).name.lower(), limit=80)
    if not re.fullmatch(r"[a-z0-9_-]+", key):
        raise OverlayError("Invalid terminal directory marker")
    return key


class ChartExporter:
    """Write one bounded, expiring ASCII row in the verified common Files."""

    def __init__(self, common_data_path, data_path, *, symbol, broker_offset_minutes, account, nonce=None):
        self.symbol = safe_text(symbol, limit=64)
        if any(char.isspace() for char in self.symbol):
            raise OverlayError("Invalid chart symbol")
        self.terminal_key = terminal_key(data_path)
        if type(broker_offset_minutes) is not int or not -720 <= broker_offset_minutes <= 840 or broker_offset_minutes % 15:
            raise OverlayError("Invalid chart broker offset")
        self.offset = broker_offset_minutes
        common = Path(common_data_path)
        if not common.is_absolute() or not common.is_dir():
            raise OverlayError("Invalid MT5 common directory")
        self.common = common.resolve(strict=True)
        self.directory = self.common / "Files" / "MT5Bot"
        if not self.directory.resolve(strict=False).is_relative_to(self.common):
            raise OverlayError("Chart directory escapes MT5 common Files")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / ("levels_" + self.terminal_key + ".csv")
        self._check_destination()
        self.nonce = nonce if nonce is not None else secrets.token_hex(16)
        if type(self.nonce) is not str or re.fullmatch(r"[0-9a-f]{32}", self.nonce) is None:
            raise OverlayError("Invalid local chart nonce")
        server, login = getattr(account, "server", None), getattr(account, "login", None)
        if type(server) is not str or not 0 < len(server) <= 256 or type(login) is not int or not 0 < login < 2**63:
            raise OverlayError("Invalid local chart account binding")
        material = self.nonce + "\n" + self.terminal_key + "\n" + server + "\n" + str(login)
        self.binding_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()

    @classmethod
    def from_terminal_info(cls, info, settings, account):
        common, data = getattr(info, "commondata_path", None), getattr(info, "data_path", None)
        if type(common) is not str or type(data) is not str:
            raise OverlayError("MT5 chart directories unavailable")
        return cls(common, data, symbol=settings.symbol,
                   broker_offset_minutes=settings.broker_utc_offset_minutes, account=account)

    def _check_destination(self):
        if not self.directory.resolve(strict=True).is_relative_to(self.common) or not self.directory.is_dir():
            raise OverlayError("Chart directory escapes MT5 common Files")
        if self.path.is_symlink() or not self.path.resolve(strict=False).is_relative_to(self.common):
            raise OverlayError("Invalid chart export destination")
        if self.path.exists() and not self.path.is_file():
            raise OverlayError("Invalid chart export destination")

    def _write(self, fields):
        self._check_destination()
        row = ";".join(str(field) for field in fields)
        encoded = row.encode("ascii")
        if len(fields) != 18 or len(encoded) > 2048 or any(char in row for char in '\r\n"'):
            raise OverlayError("Invalid chart row")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=self.directory, prefix=".levels-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(4):
                try:
                    os.replace(temporary, self.path)
                    break
                except OSError as error:
                    if not isinstance(error, PermissionError) and getattr(error, "winerror", None) not in (32, 33):
                        raise
                    if attempt == 3:
                        raise OverlayError("Chart export update unavailable") from None
                    time.sleep(0.05)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def clear(self, observed_at):
        now = utc(observed_at)
        observed = int(now.timestamp())
        self._write([2, "waiting", self.symbol, "M1", "NONE", 0, 0, 0, 0, 0, 2,
                     observed, observed + DISPLAY_TTL_SECONDS, 0, self.offset,
                     self.terminal_key, self.nonce, self.binding_hash])

    def publish(self, value, *, execution, quote, observed_at):
        if value is None:
            self.clear(observed_at)
            return
        proposal = validate_proposal(value, symbol=self.symbol, execution=execution, quote=quote, observed_at=observed_at)
        now = utc(observed_at)
        deadline = min(proposal.expires, now + timedelta(seconds=10),
                       fresh_quote(quote, now) + timedelta(seconds=10))
        if int(deadline.timestamp()) <= int(now.timestamp()):
            raise OverlayError("Chart freshness elapsed")
        prices = [format(price, f".{proposal.digits}f") for price in (
            proposal.entry, proposal.zone_low, proposal.zone_high, proposal.stop, proposal.target,
        )]
        self._write([2, "active", self.symbol, "M1", proposal.direction, *prices, proposal.digits,
                     int(now.timestamp()), int(deadline.timestamp()), int(proposal.bar.timestamp()),
                     self.offset, self.terminal_key, self.nonce, self.binding_hash])
