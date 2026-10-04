"""Manually run a read-only Windows MT5 candle bridge for the Railway bot."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import importlib
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib import request
from urllib.parse import urlsplit


BAR_COUNT = 64
BAR_SECONDS = 15 * 60
POLL_SECONDS = 60
REQUEST_TIMEOUT = 15
MAX_QUOTE_AGE_SECONDS = 10 * 24 * 60 * 60
FUTURE_TOLERANCE_SECONDS = 30


class ConfigurationError(ValueError):
    """The manual bridge configuration is incomplete or unsafe."""


class MarketDataError(ValueError):
    """The terminal or its market data cannot be used safely."""


class TransportError(RuntimeError):
    """The feed endpoint did not accept the market update."""


@dataclass(frozen=True)
class Settings:
    terminal: Path
    symbol: str
    url: str
    key: str = field(repr=False)


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's default error repeats unknown arguments, which might be private.
        self.print_usage(sys.stderr)
        self.exit(2, "Invalid bridge arguments. Use --help for setup instructions.\n")


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the Authorization header to a redirected endpoint.
        return None


def validate_feed_url(value: str) -> str:
    if not value or value != value.strip() or any(ord(c) <= 32 for c in value):
        raise ConfigurationError("Set a clean HTTPS feed URL.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("Invalid feed URL.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
        or "?" in value
        or "#" in value
        or parsed.path != "/api/market/feed"
        or (port is not None and port < 1)
    ):
        raise ConfigurationError("Use HTTPS /api/market/feed without credentials or parameters.")
    return value


def load_settings(args: argparse.Namespace, environ: Any = None) -> Settings:
    environ = os.environ if environ is None else environ
    url = validate_feed_url(environ.get("MARKET_BRIDGE_URL", ""))
    key = environ.get("MARKET_BRIDGE_KEY", "")
    if not key or key != key.strip() or any(ord(c) <= 32 or ord(c) >= 127 for c in key):
        raise ConfigurationError("Set the bridge key in the environment.")
    terminal = Path(args.terminal).expanduser().resolve()
    if terminal.suffix.lower() != ".exe" or not terminal.is_file():
        raise ConfigurationError("Choose the existing MT5 terminal executable.")
    symbol = args.symbol
    if not symbol or len(symbol) > 64 or symbol != symbol.strip() or any(ord(c) < 32 for c in symbol):
        raise ConfigurationError("Choose the exact broker gold symbol.")
    return Settings(terminal=terminal, symbol=symbol, url=url, key=key)


def check_terminal(mt5: Any, terminal: Path) -> None:
    info = mt5.terminal_info()
    if info is None or not info.connected:
        raise MarketDataError("Start and connect the selected MT5 terminal first.")
    actual_path = getattr(info, "path", None)
    if not actual_path or str(Path(actual_path).resolve()).casefold() != str(terminal.parent).casefold():
        raise MarketDataError("The connected terminal does not match the selected installation.")


def positive_price(value: Any) -> float:
    if isinstance(value, bool):
        raise MarketDataError("Invalid market price.")
    try:
        price = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarketDataError("Invalid market price.") from exc
    if not math.isfinite(price) or price <= 0:
        raise MarketDataError("Invalid market price.")
    return price


def integer_value(value: Any) -> int:
    if isinstance(value, bool):
        raise MarketDataError("Invalid market timestamp or volume.")
    try:
        number = float(value)
        if not math.isfinite(number) or not number.is_integer():
            raise ValueError
        return int(number)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarketDataError("Invalid market timestamp or volume.") from exc


def utc_timestamp(value: int) -> str:
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError) as exc:
        raise MarketDataError("Invalid market timestamp.") from exc


def build_payload(mt5: Any, settings: Settings, now: datetime | None = None) -> dict:
    now = datetime.now(timezone.utc) if now is None else now
    if now.tzinfo is None or now.utcoffset() is None:
        raise MarketDataError("Use a timezone-aware clock.")
    now_seconds = now.timestamp()
    check_terminal(mt5, settings.terminal)
    symbol = mt5.symbol_info(settings.symbol)
    if (
        symbol is None
        or getattr(symbol, "currency_base", "") != "XAU"
        or getattr(symbol, "currency_profit", "") != "USD"
    ):
        raise MarketDataError("The broker must explicitly identify this symbol as gold in US dollars.")
    tick = mt5.symbol_info_tick(settings.symbol)
    if tick is None:
        raise MarketDataError("A fresh broker quote is required.")
    bid, ask = positive_price(tick.bid), positive_price(tick.ask)
    quote_time = integer_value(tick.time)
    if (
        ask < bid
        or quote_time <= 0
        or quote_time > now_seconds + FUTURE_TOLERANCE_SECONDS
        or now_seconds - quote_time > MAX_QUOTE_AGE_SECONDS
    ):
        raise MarketDataError("A fresh, valid broker quote is required.")
    # Index zero is the forming bar. Do not include it in educational analysis.
    rates = mt5.copy_rates_from_pos(settings.symbol, mt5.TIMEFRAME_M15, 1, BAR_COUNT)
    if rates is None or not 4 <= len(rates) <= BAR_COUNT:
        raise MarketDataError("Load at least four completed M15 bars in MT5.")
    candles = []
    seen_times = set()
    for rate in rates:
        try:
            bar_time = integer_value(rate["time"])
            prices = {name: positive_price(rate[name]) for name in ("open", "high", "low", "close")}
            volume = integer_value(rate["tick_volume"])
        except (KeyError, IndexError, TypeError) as exc:
            raise MarketDataError("Incomplete broker candle.") from exc
        if (
            bar_time <= 0
            or bar_time % BAR_SECONDS
            or bar_time + BAR_SECONDS > now_seconds + FUTURE_TOLERANCE_SECONDS
            or bar_time in seen_times
            or volume < 0
            or prices["high"] < max(prices["open"], prices["close"], prices["low"])
            or prices["low"] > min(prices["open"], prices["close"], prices["high"])
        ):
            raise MarketDataError("Invalid or unfinished broker candle.")
        seen_times.add(bar_time)
        candles.append({"time": utc_timestamp(bar_time), **prices, "tick_volume": volume})
    candles.sort(key=lambda candle: candle["time"])
    return {
        "symbol": settings.symbol,
        "timeframe": "M15",
        "source": "MetaTrader 5",
        "quote": {"bid": bid, "ask": ask, "time": utc_timestamp(quote_time)},
        "candles": candles,
    }


def post_payload(settings: Settings, payload: dict) -> None:
    # Validate again at the transport boundary, even for callers bypassing the CLI.
    validate_feed_url(settings.url)
    body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
    req = request.Request(
        settings.url,
        data=body,
        headers={"Authorization": f"Bearer {settings.key}", "Content-Type": "application/json"},
        method="POST",
    )
    opener = request.build_opener(NoRedirect())
    with opener.open(req, timeout=REQUEST_TIMEOUT) as response:
        if response.status not in (200, 201, 202, 204):
            raise TransportError("The market update was rejected.")
        # Response bodies and errors may contain private diagnostics; do not print them.


def report_error(exc: Exception) -> None:
    print(f"Bridge unavailable ({type(exc).__name__}).", file=sys.stderr)


def run_bridge(mt5: Any, settings: Settings, *, once: bool = False) -> int:
    try:
        if not mt5.initialize(str(settings.terminal), timeout=REQUEST_TIMEOUT * 1000):
            raise MarketDataError("Unable to connect to the selected terminal.")
        check_terminal(mt5, settings.terminal)
        while True:
            started = time.monotonic()
            try:
                post_payload(settings, build_payload(mt5, settings))
                print("Market update sent.", flush=True)
                result = 0
            except Exception as exc:
                report_error(exc)
                result = 1
            if once:
                return result
            time.sleep(max(0.0, POLL_SECONDS - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("Bridge stopped.", flush=True)
        return 0
    except Exception as exc:
        report_error(exc)
        return 1
    finally:
        try:
            mt5.shutdown()
        except Exception as exc:
            report_error(exc)


def main(argv: list[str] | None = None) -> int:
    parser = PrivateArgumentParser(description=__doc__)
    parser.add_argument("--terminal", required=True, help="Existing, already connected MT5 terminal .exe path")
    parser.add_argument("--symbol", required=True, help="Exact broker XAU/USD symbol, including its suffix")
    parser.add_argument("--once", action="store_true", help="Send one update, then exit; useful for manual setup")
    args = parser.parse_args(argv)
    try:
        if os.name != "nt":
            raise ConfigurationError("The official MT5 bridge requires Windows.")
        settings = load_settings(args)
        # The server and tests can import this module without the Windows-only SDK.
        mt5 = importlib.import_module("MetaTrader5")
    except Exception as exc:
        report_error(exc)
        return 1
    return run_bridge(mt5, settings, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
