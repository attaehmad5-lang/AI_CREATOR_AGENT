"""Safe numeric normalization helpers for untrusted provider/API values."""
from __future__ import annotations

import math
from typing import Any


def finite_number(
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    """Return a finite float within optional bounds, or None for any invalid value."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number
