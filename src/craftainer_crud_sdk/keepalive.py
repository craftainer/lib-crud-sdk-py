"""Clamp for the manifest's event-stream `keepalive_seconds`."""

from __future__ import annotations

import math

__all__ = [
    "DEFAULT_KEEPALIVE",
    "MAX_KEEPALIVE",
    "MIN_KEEPALIVE",
    "clamp_keepalive",
]

DEFAULT_KEEPALIVE = 15.0
MIN_KEEPALIVE = 1.0
MAX_KEEPALIVE = 60.0


def clamp_keepalive(value: object) -> float:
    """The keep-alive interval to use for a manifest `keepalive_seconds`.

    Missing, non-numeric, ``NaN``, infinite, too large to be a float or non-positive values give
    `DEFAULT_KEEPALIVE`; any other value is clamped to [`MIN_KEEPALIVE`, `MAX_KEEPALIVE`]. The
    read timeout is three intervals, so a huge value cannot switch dead-stream detection off.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return DEFAULT_KEEPALIVE
    try:
        seconds = float(value)
    except OverflowError:  # an int with hundreds of digits
        return DEFAULT_KEEPALIVE
    if not math.isfinite(seconds) or seconds <= 0:
        return DEFAULT_KEEPALIVE
    return min(max(seconds, MIN_KEEPALIVE), MAX_KEEPALIVE)
