"""One UTC policy for entry expiry, maximum holding time and broker sessions.

Session hours must come from the paired terminal's verified symbol schedule.
The existing 06:00–19:00 UTC weekday window further limits that schedule; it
does not stand in for the broker's opening or closing hours.
"""

from datetime import datetime, timedelta, timezone


POLICY_ID = "mt5-session-60m-v1"
SESSION_SOURCE = "mt5_symbol_info_session_trade"
MAX_HOLD_SECONDS = 3600
EXIT_BUFFER_SECONDS = 900
SESSION_MAX_AGE_SECONDS = 30
SESSION_FIELDS = frozenset({"source", "verified", "as_of", "session_open", "session_close"})
TIMING_FIELDS = frozenset({
    "policy_id", "evaluated_at", "signal_at", "entry_deadline", "session_open",
    "session_close", "session_exit_at", "latest_exit_at", "max_hold_seconds",
    "exit_buffer_seconds", "session_source",
})


class TradeTimingError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _utc(value):
    try:
        if type(value) is str and len(value) <= 40:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError
        return value.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise TradeTimingError("invalid_broker_session") from None


def build_trade_timing(session_context, decision, signal_at, entry_window_seconds):
    """Build a fixed plan only when the full permitted holding time is available.

    The actual exit deadline is actual fill time + 3600 seconds, capped by
    session_exit_at. latest_exit_at is the outer bound assuming the last
    permissible fill; it must never extend past the broker safety cutoff.
    """
    clock, signal = _utc(decision), _utc(signal_at)
    if type(entry_window_seconds) is not int or entry_window_seconds not in (10, 30):
        raise TradeTimingError("invalid_broker_session")
    if type(session_context) is not dict or session_context.get("verified") is not True:
        raise TradeTimingError("broker_session_unavailable")
    if set(session_context) != SESSION_FIELDS or session_context.get("source") != SESSION_SOURCE:
        raise TradeTimingError("invalid_broker_session")
    captured = _utc(session_context["as_of"])
    opened, closed = (_utc(session_context[key]) for key in ("session_open", "session_close"))
    if not timedelta(0) < closed - opened <= timedelta(days=7):
        raise TradeTimingError("invalid_broker_session")
    if not timedelta(0) <= clock - captured <= timedelta(seconds=SESSION_MAX_AGE_SECONDS):
        raise TradeTimingError("stale_broker_session")
    if not opened <= clock < closed:
        raise TradeTimingError("outside_broker_session")
    entry_deadline = signal + timedelta(seconds=entry_window_seconds)
    if signal.microsecond or signal.timestamp() % 60 or not signal <= clock <= entry_deadline:
        raise TradeTimingError("entry_window_expired")
    conservative_open = clock.replace(hour=6, minute=0, second=0, microsecond=0)
    conservative_close = clock.replace(hour=19, minute=0, second=0, microsecond=0)
    session_exit = min(closed - timedelta(seconds=EXIT_BUFFER_SECONDS), conservative_close)
    latest_exit = entry_deadline + timedelta(seconds=MAX_HOLD_SECONDS)
    if (clock.weekday() >= 5 or clock < conservative_open
            or latest_exit > session_exit):
        raise TradeTimingError("outside_full_horizon_session")
    return {
        "policy_id": POLICY_ID, "evaluated_at": clock.isoformat(), "signal_at": signal.isoformat(),
        "entry_deadline": entry_deadline.isoformat(), "session_open": opened.isoformat(),
        "session_close": closed.isoformat(), "session_exit_at": session_exit.isoformat(),
        "latest_exit_at": latest_exit.isoformat(), "max_hold_seconds": MAX_HOLD_SECONDS,
        "exit_buffer_seconds": EXIT_BUFFER_SECONDS, "session_source": SESSION_SOURCE,
    }


def validate_trade_timing(timing, now=None, bar_time=None, *, require_entry_open=True):
    """Recompute a frozen plan; never accept a changed deadline or policy.

    Entry consumers retain require_entry_open=True. Reports and exit handlers
    can validate its shape after expiry by selecting False explicitly.
    """
    try:
        if (type(timing) is not dict or set(timing) != TIMING_FIELDS
                or type(require_entry_open) is not bool
                or type(timing["max_hold_seconds"]) is not int
                or type(timing["exit_buffer_seconds"]) is not int):
            return False
        evaluated, signal, deadline = (_utc(timing[key]) for key in (
            "evaluated_at", "signal_at", "entry_deadline"))
        window = (deadline - signal).total_seconds()
        if window not in (10, 30):
            return False
        expected = build_trade_timing({
            "source": timing["session_source"], "verified": True, "as_of": evaluated,
            "session_open": timing["session_open"], "session_close": timing["session_close"],
        }, evaluated, signal, int(window))
        if expected != timing:
            return False
        if bar_time is not None and _utc(bar_time) + timedelta(minutes=1) != signal:
            return False
        if require_entry_open:
            clock = _utc(datetime.now(timezone.utc) if now is None else now)
            if not evaluated <= clock <= deadline:
                return False
        return True
    except (TradeTimingError, ValueError, TypeError, KeyError, OverflowError):
        return False


def actual_exit_deadline(timing, entry_time):
    """Return the deadline for a confirmed fill without extending its lifetime."""
    if not validate_trade_timing(timing, require_entry_open=False):
        raise TradeTimingError("invalid_broker_session")
    entered = _utc(entry_time)
    if not _utc(timing["evaluated_at"]) <= entered <= _utc(timing["entry_deadline"]):
        raise TradeTimingError("entry_window_expired")
    return min(entered + timedelta(seconds=MAX_HOLD_SECONDS), _utc(timing["session_exit_at"]))
