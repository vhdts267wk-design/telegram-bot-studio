"""Shared, deterministic closed-OHLCV provenance for Demo approval claims."""

from datetime import datetime
import hashlib
import json
import math


FRAMES = tuple(sorted(("M1", "M5", "M15", "M30")))


def closed_history_sha256(feed):
    """Hash actual bar order with UTC epochs and JSON float OHLC values."""
    try:
        frames = feed["timeframes"]
        if type(frames) is not dict or set(frames) != set(FRAMES):
            raise ValueError
        canonical = []
        for frame in FRAMES:
            bars = frames[frame]
            if type(bars) is not list or not 1 <= len(bars) <= 64:
                raise ValueError
            rows = []
            previous = None
            for bar in bars:
                if type(bar) is not dict or type(bar["time"]) is not str:
                    raise ValueError
                instant = datetime.fromisoformat(bar["time"].replace("Z", "+00:00"))
                if instant.tzinfo is None or instant.utcoffset() is None or instant.microsecond:
                    raise ValueError
                epoch = instant.timestamp()
                if not math.isfinite(epoch) or epoch != int(epoch) or (previous is not None and epoch <= previous):
                    raise ValueError
                prices = []
                for key in ("open", "high", "low", "close"):
                    value = bar[key]
                    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                        raise ValueError
                    prices.append(float(value))
                volume = bar["tick_volume"]
                if type(volume) is not int or not 0 <= volume <= 10**12:
                    raise ValueError
                rows.append([int(epoch), *prices, volume])
                previous = epoch
            canonical.append([frame, rows])
        encoded = json.dumps(canonical, separators=(",", ":"), allow_nan=False).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise ValueError("Complete closed UTC OHLCV histories are required.") from None
