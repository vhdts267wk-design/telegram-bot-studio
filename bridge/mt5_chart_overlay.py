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
    "context_bar_times", "timeframe_context", "broker_utc_offset_minutes",
})
EXPERIMENTAL_FIELDS = (FIELDS - {"qualification_id"}) | {
    "signal_mode", "provisional", "entry_window_seconds", "cost_assumptions",
}
COST_ASSUMPTION_FIELDS = frozenset({
    "verified", "method", "commission_round_turn", "slippage_price", "spread_price", "tick_size",
})
FRAME_SECONDS = {"H4": 14400, "H1": 3600, "M15": 900, "M5": 300, "M1": 60}


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


def validate_cost_assumptions(value):
    """Explicit Demo estimates are never treated as certified broker costs."""
    if (type(value) is not dict or set(value) != COST_ASSUMPTION_FIELDS
        or value["verified"] is not False or value["method"] != "spread_tick_floor_v1"):
        raise OverlayError("Invalid experimental cost assumptions")
    parsed = {}
    for key in ("commission_round_turn", "slippage_price", "spread_price", "tick_size"):
        amount = value[key]
        if type(amount) not in (int, float) or not math.isfinite(amount) or not 0 <= amount <= 1_000_000_000_000:
            raise OverlayError("Invalid experimental cost assumptions")
        parsed[key] = Decimal(str(amount))
    if (parsed["commission_round_turn"] <= 0 or parsed["spread_price"] <= 0 or parsed["tick_size"] <= 0
        or parsed["slippage_price"] < max(parsed["spread_price"] / 2, 2 * parsed["tick_size"])):
        raise OverlayError("Understated experimental cost assumptions")
    return parsed


def validate_broker_offset(value):
    if type(value) is not int or not -720 <= value <= 840 or value % 15:
        raise OverlayError("Invalid context broker offset")
    return value


def latest_closed_reference(boundary, seconds, broker_offset_minutes):
    offset = validate_broker_offset(broker_offset_minutes) * 60
    return int((boundary + offset) // seconds) * seconds - seconds - offset


def validate_timeframe_context(value, direction):
    """Only a complete, aligned five-frame context can accompany an offer."""
    if (type(value) is not dict or set(value) != {
        "trends", "alignment", "confidence", "counter_trend", "support", "resistance",
    } or type(value["trends"]) is not dict or set(value["trends"]) != set(FRAME_SECONDS)
        or direction not in ("BUY", "SELL")
        or any(type(trend) is not str or trend != direction for trend in value["trends"].values())
        or value["alignment"] != "aligned" or value["confidence"] != "aligned"
        or value["counter_trend"] is not False):
        raise OverlayError("Incomplete or unaligned five-timeframe context")
    support, resistance = value["support"], value["resistance"]
    for price in (support, resistance):
        if price is not None:
            positive(price)
    if support is not None and resistance is not None and support > resistance:
        raise OverlayError("Invalid higher-timeframe levels")
    return value


def validate_context_references(value, *, trigger, boundary, broker_offset_minutes):
    """Require the last closed H1/H4 bar at both signal and current boundary."""
    if type(value) is not dict or set(value) != {"H1", "H4"}:
        raise OverlayError("Incomplete higher-timeframe references")
    result = {}
    for frame in ("H1", "H4"):
        if type(value[frame]) is not str:
            raise OverlayError("Invalid higher-timeframe reference format")
        reference = utc(value[frame])
        seconds = FRAME_SECONDS[frame]
        if (reference.microsecond
            or reference.timestamp() != latest_closed_reference(trigger + 60, seconds, broker_offset_minutes)
            or reference.timestamp() != latest_closed_reference(boundary, seconds, broker_offset_minutes)):
            raise OverlayError("Stale or future higher-timeframe reference")
        result[frame] = reference
    return result


def current_timeframe_context(feed, *, observed_at, broker_offset_minutes):
    """Recompute the read-only five-frame trend contract from local closed bars.

    This standalone bridge cannot import the server. These EMA/ATR operations
    match its declared strategy, and never prepare or submit a ticket.
    """
    now = utc(observed_at)
    offset = validate_broker_offset(broker_offset_minutes)
    if type(feed) is not dict or type(feed.get("timeframes")) is not dict or set(feed["timeframes"]) != set(FRAME_SECONDS):
        raise OverlayError("Incomplete local five-timeframe history")
    if validate_broker_offset(feed.get("broker_utc_offset_minutes")) != offset:
        raise OverlayError("Changed local context broker offset")
    fresh_quote(feed.get("quote"), now)
    streams = {}
    for frame, seconds in FRAME_SECONDS.items():
        rows = feed["timeframes"][frame]
        if type(rows) is not list or not 22 <= len(rows) <= 64:
            raise OverlayError("Insufficient local five-timeframe history")
        parsed = []
        for row in rows:
            if type(row) is not dict:
                raise OverlayError("Invalid local context candle")
            stamp = utc(row.get("time"))
            prices = {key: float(positive(row.get(key))) for key in ("open", "high", "low", "close")}
            if (stamp.microsecond or (stamp.timestamp() + offset * 60) % seconds
                or stamp.timestamp() + seconds > now.timestamp()
                or (parsed and stamp <= parsed[-1]["time"])
                or prices["high"] < max(prices["open"], prices["close"], prices["low"])
                or prices["low"] > min(prices["open"], prices["close"], prices["high"])):
                raise OverlayError("Invalid local context candle")
            parsed.append({"time": stamp, **prices})
        if parsed[-1]["time"].timestamp() != latest_closed_reference(now.timestamp(), seconds, offset):
            raise OverlayError("Stale local context history")
        start = len(parsed) - 1
        while start > 0 and (parsed[start]["time"] - parsed[start - 1]["time"]).total_seconds() == seconds:
            start -= 1
        streams[frame] = parsed[start:]
        if len(streams[frame]) < 22:
            raise OverlayError("Insufficient contiguous context history")

    def ema(closes, period):
        current = math.fsum(closes[:period]) / period
        values = [None] * (period - 1) + [current]
        for close in closes[period:]:
            current += (2 / (period + 1)) * (close - current)
            values.append(current)
        return values

    trends = {}
    for frame, rows in streams.items():
        closes = [row["close"] for row in rows]
        fast, slow = ema(closes, 9), ema(closes, 21)
        separation, slope = fast[-1] - slow[-1], fast[-1] - fast[-2]
        if frame in ("M1", "M5"):
            trends[frame] = "BUY" if separation > 0 else "SELL" if separation < 0 else "NEUTRAL"
            continue
        ranges = [max(current["high"] - current["low"], abs(current["high"] - previous["close"]),
                      abs(current["low"] - previous["close"])) for previous, current in zip(rows, rows[1:])]
        atr = math.fsum(ranges[:14]) / 14
        for value in ranges[14:]:
            atr = (atr * 13 + value) / 14
        if not math.isfinite(atr) or atr <= 0 or abs(separation) < 0.05 * atr:
            trends[frame] = "NEUTRAL"
        else:
            trends[frame] = ("BUY" if separation > 0 and slope > 0 and closes[-1] > fast[-1]
                             else "SELL" if separation < 0 and slope < 0 and closes[-1] < fast[-1] else "NEUTRAL")
    direction = trends["M15"]
    aligned = direction != "NEUTRAL" and all(trend == direction for trend in trends.values())
    counter = direction != "NEUTRAL" and any(trends[frame] not in (direction, "NEUTRAL") for frame in ("H1", "H4"))
    midpoint = (positive(feed["quote"]["bid"]) + positive(feed["quote"]["ask"])) / 2
    support = min(row["low"] for row in streams["H4"][-20:])
    resistance = max(row["high"] for row in streams["H4"][-20:])
    return {"trends": trends, "alignment": "aligned" if aligned else "counter_trend" if counter else "unconfirmed",
            "confidence": "aligned" if aligned else "reduced" if counter else "unconfirmed", "counter_trend": counter,
            "support": support if Decimal(str(support)) <= midpoint else None,
            "resistance": resistance if Decimal(str(resistance)) >= midpoint else None}


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
    experimental: bool = False


def fresh_quote(value, now):
    if type(value) is not dict or not {"time", "bid", "ask"} <= set(value):
        raise OverlayError("Invalid chart quote")
    bid, ask = positive(value["bid"]), positive(value["ask"])
    stamp = utc(value["time"])
    if ask < bid or not timedelta(seconds=-5) <= now - stamp <= timedelta(seconds=10):
        raise OverlayError("Stale chart quote")
    return stamp


def validate_proposal(value, *, symbol, execution, quote, observed_at, broker_offset_minutes=0):
    """Validate the separate chart DTO without fabricating a claim or draft."""
    now = utc(observed_at)
    fresh_quote(quote, now)
    if type(value) is not dict:
        raise OverlayError("Invalid chart proposal")
    experimental = value.get("signal_mode") == "experimental_demo"
    expected = EXPERIMENTAL_FIELDS if experimental else FIELDS
    if set(value) != expected and not (experimental and set(value) == expected | {"qualification_id"} and value["qualification_id"] == ""):
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
        value["strategy_id"] != ("mtf-ema-pullback-60m-demo-v3" if experimental else "mtf-ema-pullback-60m-v2")
        or type(value["strategy_version"]) is not int or value["strategy_version"] != (3 if experimental else 2)
        or value["policy_id"] != ("mtf-manual-demo-estimated-cost-risk-v3" if experimental else "mtf-manual-demo-cost-risk-v2")
        or type(value["horizon_seconds"]) is not int or value["horizon_seconds"] != 3600
        or type(value["strategy_fingerprint"]) is not str or re.fullmatch(r"[0-9a-f]{64}", value["strategy_fingerprint"]) is None
        or (not experimental and (type(value["qualification_id"]) is not str or re.fullmatch(r"[0-9a-f]{64}", value["qualification_id"]) is None))
        or (experimental and (value["provisional"] is not True or type(value["entry_window_seconds"]) is not int or value["entry_window_seconds"] != 30))
    ):
        raise OverlayError("Unqualified chart proposal")
    if validate_broker_offset(value["broker_utc_offset_minutes"]) != validate_broker_offset(broker_offset_minutes):
        raise OverlayError("Changed chart broker offset")
    validate_timeframe_context(value["timeframe_context"], value["direction"])
    assumptions = validate_cost_assumptions(value["cost_assumptions"]) if experimental else None
    try:
        if type(value["offer_id"]) is not str or str(UUID(value["offer_id"])) != value["offer_id"]:
            raise ValueError
    except (ValueError, AttributeError):
        raise OverlayError("Invalid chart reference") from None
    tick, _, digits, _ = metadata(value["execution"])
    if metadata(execution) != metadata(value["execution"]) or type(value["price_digits"]) is not int or value["price_digits"] != digits:
        raise OverlayError("Changed chart precision")
    if experimental and assumptions["tick_size"] != tick:
        raise OverlayError("Changed experimental cost precision")
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
        or direction.timestamp() != latest_closed_reference(trigger + 60, 900, broker_offset_minutes)
        or confirmation.timestamp() != latest_closed_reference(trigger + 60, 300, broker_offset_minutes)
    ):
        raise OverlayError("Invalid chart reference candle")
    validate_context_references(value["context_bar_times"], trigger=trigger, boundary=now.timestamp(),
                                broker_offset_minutes=broker_offset_minutes)
    if not now < expires <= min(now + timedelta(minutes=5), bar + timedelta(minutes=6)):
        raise OverlayError("Expired chart proposal")
    if experimental and (now > bar + timedelta(seconds=90) or expires > bar + timedelta(seconds=90)):
        raise OverlayError("Experimental M1 entry window expired")
    return DisplayProposal(value["direction"], entry, low, high, stop, target, digits, bar, expires, experimental)


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
        proposal = validate_proposal(value, symbol=self.symbol, execution=execution, quote=quote, observed_at=observed_at,
                                     broker_offset_minutes=self.offset)
        now = utc(observed_at)
        deadline = min(proposal.expires, now + timedelta(seconds=10),
                       fresh_quote(quote, now) + timedelta(seconds=10))
        if int(deadline.timestamp()) <= int(now.timestamp()):
            raise OverlayError("Chart freshness elapsed")
        prices = [format(price, f".{proposal.digits}f") for price in (
            proposal.entry, proposal.zone_low, proposal.zone_high, proposal.stop, proposal.target,
        )]
        self._write([2, "experimental" if proposal.experimental else "active", self.symbol, "M1", proposal.direction, *prices, proposal.digits,
                     int(now.timestamp()), int(deadline.timestamp()), int(proposal.bar.timestamp()),
                     self.offset, self.terminal_key, self.nonce, self.binding_hash])
