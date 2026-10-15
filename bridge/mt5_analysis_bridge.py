"""Analysis-only MT5 feed and candle-panel helper for managed Windows startup.

The managed startup wrapper must enforce a single instance. This module only
uploads market snapshots and writes expiring waiting/readiness display rows.
It never polls or claims offers, prepares tickets, or calls trading APIs.
The wrapper supplies private configuration through the process environment.
"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib
import os
import sys
import time

try:
    from bridge import mt5_market_bridge as market
    from bridge import mt5_chart_overlay as chart
except ModuleNotFoundError:
    import mt5_market_bridge as market
    import mt5_chart_overlay as chart


class BindingChanged(market.MarketDataError):
    """Stop after an account change; the managed launcher must not retry exit 2."""


def execution_metadata(symbol):
    """Read the broker's price grid; no trading permissions are needed."""
    if symbol is None:
        raise market.MarketDataError("Broker price metadata is unavailable.")
    digits = getattr(symbol, "digits", None)
    stops = getattr(symbol, "trade_stops_level", None)
    if type(digits) is not int or not 0 <= digits <= 10:
        raise market.MarketDataError("Invalid broker price precision.")
    if type(stops) is not int or not 0 <= stops <= 1000000:
        raise market.MarketDataError("Invalid broker stop-distance metadata.")
    return {
        "tick_size": market.positive_price(getattr(symbol, "trade_tick_size", None)),
        "point": market.positive_price(getattr(symbol, "point", None)),
        "digits": digits,
        "stops_level": stops,
    }


def account_binding(mt5, settings, expected=None):
    """Keep raw account identity in memory and require the same Demo account."""
    market.check_terminal(mt5, settings.terminal)
    account = mt5.account_info()
    if account is None:
        raise market.MarketDataError("Demo account data is unavailable.")
    mode = getattr(account, "trade_mode", None)
    login = getattr(account, "login", None)
    server = getattr(account, "server", None)
    currency = getattr(account, "currency", None)
    if (type(mode) is not int or mode != getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
        or type(login) is not int or not 0 < login < 2**63
        or type(server) is not str or not 0 < len(server) <= 256
        or type(currency) is not str or not 0 < len(currency) <= 16):
        raise BindingChanged("The selected Demo account is unavailable or changed.")
    binding = (mode, login, server, currency)
    if expected is not None and binding != expected:
        raise BindingChanged("The selected Demo account changed.")
    return binding, account


def _clear_display(exporter, clock):
    if exporter is not None:
        try:
            exporter.clear(clock())
        except Exception:
            # Every prior row expires independently if its destination is locked.
            pass


def run_bridge(mt5, settings, *, once=False, post=None, clock=None,
               sleep=None, exporter_factory=None):
    """Send only /api/market/feed; accepted updates may refresh waiting counts."""
    post = market.post_payload if post is None else post
    clock = (lambda: datetime.now(timezone.utc)) if clock is None else clock
    sleep = time.sleep if sleep is None else sleep
    exporter_factory = chart.ChartExporter.from_terminal_info if exporter_factory is None else exporter_factory
    exporter = None
    outage = False
    announced = False
    try:
        if not mt5.initialize(str(settings.terminal), timeout=market.REQUEST_TIMEOUT * 1000):
            raise market.MarketDataError("Unable to connect to the selected terminal.")
        binding, account = account_binding(mt5, settings)
        exporter = exporter_factory(mt5.terminal_info(), settings, account)
        exporter.clear(clock())
        while True:
            started = time.monotonic()
            try:
                account_binding(mt5, settings, binding)
                payload = market.build_payload(mt5, settings, clock())
                payload["execution"] = execution_metadata(mt5.symbol_info(settings.symbol))
                payload["analysis_only"] = True
                account_binding(mt5, settings, binding)
                post(settings, payload)
                # Network latency must not let an account switch renew its display.
                account_binding(mt5, settings, binding)
                exporter.publish(None, execution=payload["execution"], quote=payload["quote"],
                                 observed_at=clock(), feed=payload)
                if outage:
                    print("Analysis feed restored.", flush=True)
                elif once:
                    print("Analysis update accepted.", flush=True)
                elif not announced:
                    print("Analysis feed ready.", flush=True)
                announced = True
                outage = False
                result = 0
            except BindingChanged:
                raise
            except market.MarketDataError as error:
                if error.reason_code == "terminal_installation_mismatch":
                    raise
                _clear_display(exporter, clock)
                if not outage:
                    market.report_error(error)
                outage = True
                result = 1
            except Exception as error:
                _clear_display(exporter, clock)
                if not outage:
                    market.report_error(error)
                outage = True
                result = 1
            if once:
                return result
            sleep(max(0.0, chart.FEED_REFRESH_SECONDS - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("Analysis helper stopped.", flush=True)
        return 0
    except BindingChanged as error:
        market.report_error(error)
        return 2
    except Exception as error:
        market.report_error(error)
        return 1
    finally:
        _clear_display(exporter, clock)
        try:
            mt5.shutdown()
        except Exception as error:
            market.report_error(error)


def main(argv=None):
    parser = market.PrivateArgumentParser(description=__doc__)
    parser.add_argument("--terminal", required=True, help="Selected existing MT5 terminal executable")
    parser.add_argument("--symbol", required=True, help="Exact broker XAU/USD symbol")
    parser.add_argument("--once", action="store_true", help="Send one analysis update, then exit")
    args = parser.parse_args(argv)
    try:
        if os.name != "nt":
            raise market.ConfigurationError("The official MT5 bridge requires Windows.")
        settings = market.load_settings(args)
        mt5 = importlib.import_module("MetaTrader5")
    except Exception as error:
        market.report_error(error)
        return 1
    return run_bridge(mt5, settings, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
