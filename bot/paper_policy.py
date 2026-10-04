"""Experimental risk cutoff after three adequately sampled observed stops.

This fixed, bounded pause is not an optimizer, evidence of profitability, or a
change to the underlying EMA/ATR signal rule. It makes no network requests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot import paper_journal


MAX_INPUT_TRADES = 100
REQUIRED_STOPS = 3
PAUSE_MINUTES = 45
POLICY_ID = "three-complete-observed-stops-pause45-v1"


def _identifier(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128 and value.strip() == value and not any(
        char in value for char in "\r\n\x00"
    )


def _clock(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware policy timestamp is required.")
    return value.astimezone(timezone.utc)


def _fingerprint(trade: dict) -> tuple:
    """Treat contradictory copies of one outcome as untrusted input."""
    return tuple(trade.get(key) for key in (
        "source_identity", "strategy_id", "signal_bar_time", "direction",
        "entry", "stop", "target", "opened_at", "deadline", "duration_minutes",
        "status", "closed_at", "exit_price", "gross_r", "coverage_complete",
        "last_observation_at", "last_observation_price", "max_gap_seconds",
    ))


def pause_until(trades: list, source_identity: str, strategy_id: str, now: datetime) -> datetime | None:
    """Return an active 45-minute cutoff, or None when the evidence is inadequate.

    Matching open rows and future outcomes are ignored. The three most recent
    unique, valid terminal outcomes must all be coverage-complete observed
    stops. Every other terminal outcome, including inconclusive or partially
    covered stops, breaks the streak. Malformed matching records cannot be
    silently discarded to manufacture a loss streak.
    """
    if (
        not isinstance(trades, list) or len(trades) > MAX_INPUT_TRADES
        or not _identifier(source_identity) or not _identifier(strategy_id)
        or not isinstance(now, datetime)
    ):
        return None
    try:
        clock = _clock(now)
        outcomes = {}
        for trade in trades:
            if not isinstance(trade, dict) or not _identifier(trade.get("source_identity")):
                return None
            if trade["source_identity"] != source_identity:
                continue
            if not _identifier(trade.get("strategy_id")):
                return None
            if trade["strategy_id"] != strategy_id:
                continue
            status = trade.get("status")
            if status == "open":
                continue
            if status not in tuple(paper_journal.TERMINAL_STATUSES):
                return None
            closed = _clock(trade.get("closed_at"))
            if closed > clock:
                continue
            if not _identifier(trade.get("id")):
                return None
            # The public journal engine validates the complete frozen record,
            # its declared deadline, prices, coverage flag and resolved state.
            checked = paper_journal.advance_trade(trade, [], clock)
            if checked["coverage_complete"] and checked["max_gap_seconds"] > paper_journal.MAX_GAP_SECONDS:
                return None
            if status == "stop_observed":
                exit_price = checked["exit_price"]
                stop_crossed = exit_price <= checked["stop"] if checked["direction"] == "BUY" else exit_price >= checked["stop"]
                if not stop_crossed or checked["last_observation_price"] != exit_price:
                    return None
                sign = 1 if checked["direction"] == "BUY" else -1
                observed_r = round(sign * (exit_price - checked["entry"]) / abs(checked["entry"] - checked["stop"]), 6)
                if checked["gross_r"] != observed_r:
                    return None
            signal_id = checked["id"]
            previous = outcomes.get(signal_id)
            if previous is not None and _fingerprint(previous[1]) != _fingerprint(checked):
                return None
            outcomes[signal_id] = (closed, checked)
        ordered = sorted(outcomes.values(), key=lambda item: (item[0], item[1]["id"]), reverse=True)
        recent = ordered[:REQUIRED_STOPS]
        if len(recent) != REQUIRED_STOPS or any(
            trade["status"] != "stop_observed" or trade["coverage_complete"] is not True
            for _, trade in recent
        ):
            return None
        # A contradictory outcome at the same boundary time has no trustworthy
        # ordering, so do not infer a streak from a lexical identifier tie.
        boundary = recent[-1][0]
        if any(
            closed == boundary and (trade["status"] != "stop_observed" or trade["coverage_complete"] is not True)
            for closed, trade in ordered[REQUIRED_STOPS:]
        ):
            return None
        deadline = recent[0][0] + timedelta(minutes=PAUSE_MINUTES)
        return deadline if deadline > clock else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
