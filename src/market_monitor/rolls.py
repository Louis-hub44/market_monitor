r"""Contract-roll calendar of generic front-month futures (Brent, WTI, TTF).

A generic front-month series (Bloomberg ``CO1 Comdty``, Yahoo ``BZ=F``...) jumps to the
next contract when the front one expires. The jump is the calendar spread, not a market
move: on that day the 1D change, its z-score and the alerts are meaningless.

Expiry rules (exchange rulebooks, Monday-Friday business days, exchange holidays ignored -
hence a two-day window around the computed date):

* ``brent`` (ICE Brent): trading ceases on the **last business day of the second month
  preceding** the contract month -> the generic rolls on the first business day of every
  month;
* ``wti`` (NYMEX CL): trading terminates **3 business days before the 25th** calendar day of
  the month preceding the contract month (4 when the 25th is not a business day);
* ``ttf`` (ICE Endex Dutch TTF): trading ceases **2 business days before the first calendar
  day of the delivery month** -> roll on the last business day of every month.

``roll_days`` returns the *effective* roll days - the first close of the new contract - plus
the following business day (holiday tolerance).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pandas as pd

BDAY = pd.offsets.BDay()
TOLERANCE_BDAYS = 1


def _last_bday(year: int, month: int) -> pd.Timestamp:
    return pd.Timestamp(year, month, 1) + pd.offsets.BMonthEnd(0)


def _brent(year: int, month: int) -> pd.Timestamp:
    """First business day of the month (day after the last business day of the previous one)."""
    return _last_bday(*_previous(year, month)) + BDAY


def _wti(year: int, month: int) -> pd.Timestamp:
    """Business day after the CL last trading day falling in ``month``."""
    d25 = pd.Timestamp(year, month, 25)
    anchor = d25 if d25.dayofweek < 5 else d25 - BDAY  # last business day before the 25th
    last_trading = anchor - 3 * BDAY
    return last_trading + BDAY


def _ttf(year: int, month: int) -> pd.Timestamp:
    """Last business day of the month (expiry is the second-to-last one)."""
    return _last_bday(year, month)


def _previous(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


RULES: dict[str, Callable[[int, int], pd.Timestamp]] = {"brent": _brent, "wti": _wti, "ttf": _ttf}


def roll_days(rule: str, start: date | pd.Timestamp, end: date | pd.Timestamp) -> pd.DatetimeIndex:
    """Effective roll days (plus tolerance) of ``rule`` between ``start`` and ``end``."""
    if rule not in RULES:
        raise ValueError(f"unknown roll rule {rule!r} (known: {sorted(RULES)})")
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    days: list[pd.Timestamp] = []
    for month in pd.period_range(first - pd.DateOffset(months=1), last + pd.DateOffset(months=1), freq="M"):
        roll = RULES[rule](month.year, month.month)
        days += [roll + k * BDAY for k in range(TOLERANCE_BDAYS + 1)]
    index = pd.DatetimeIndex(sorted(set(days)), name="date")
    return index[(index >= first) & (index <= last)]


def rolled_between(rule: str | None, after: pd.Timestamp, until: pd.Timestamp) -> bool:
    """True when a roll window intersects ``(after, until]``."""
    if rule is None or until <= after:
        return False
    return len(roll_days(rule, after + pd.Timedelta(days=1), until)) > 0
