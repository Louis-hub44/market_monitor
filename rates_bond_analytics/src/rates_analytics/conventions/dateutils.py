"""Low-level calendar-day arithmetic (no business-day logic).

All engine dates are plain :class:`datetime.date` objects: hashable, immutable and
free of time-zone ambiguity. :func:`to_date` normalises the usual inputs
(``str``, ``datetime``, ``pandas.Timestamp``, ``numpy.datetime64``).
"""

from __future__ import annotations

import calendar as _pycal
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from rates_analytics.exceptions import ConventionError

DateLike = date | datetime | str | pd.Timestamp | np.datetime64


def to_date(value: Any) -> date:
    """Convert a date-like value to :class:`datetime.date`.

    Accepts ``date``, ``datetime``, ``pandas.Timestamp``, ``numpy.datetime64`` and
    ISO strings (``"2026-10-07"``).

    Raises:
        ConventionError: if the value cannot be interpreted as a date.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            raise ConventionError("NaT is not a valid date")
        return value.date()
    if isinstance(value, (np.datetime64, str)):
        try:
            ts = pd.Timestamp(value)
        except (ValueError, TypeError) as exc:
            raise ConventionError(f"Cannot parse date {value!r}") from exc
        if pd.isna(ts):
            raise ConventionError(f"Cannot parse date {value!r}")
        return ts.date()
    raise ConventionError(f"Unsupported date type {type(value).__name__}: {value!r}")


def is_leap(year: int) -> bool:
    """Gregorian leap-year test."""
    return _pycal.isleap(year)


def days_in_month(year: int, month: int) -> int:
    """Number of calendar days in ``month`` of ``year``."""
    return _pycal.monthrange(year, month)[1]


def end_of_month(d: date) -> date:
    """Last calendar day of the month of ``d``."""
    return date(d.year, d.month, days_in_month(d.year, d.month))


def is_end_of_month(d: date) -> bool:
    """True if ``d`` is the last calendar day of its month."""
    return d.day == days_in_month(d.year, d.month)


def add_months(d: date, months: int) -> date:
    """Add ``months`` calendar months, clipping the day to the target month length.

    This is the ISDA / QuantLib convention: 31-Jan + 1M = 28/29-Feb.
    """
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    if not 1 <= year <= 9999:
        raise ConventionError(f"Date out of range when adding {months}M to {d}")
    return date(year, month, min(d.day, days_in_month(year, month)))


def add_days(d: date, days: int) -> date:
    """Add calendar days."""
    return d + timedelta(days=days)


def days_between(d1: date, d2: date) -> int:
    """Actual number of calendar days from ``d1`` to ``d2`` (signed)."""
    return (d2 - d1).days


def easter_sunday(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian / Meeus-Jones-Butcher algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return date(year, month, day + 1)
