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


def roll_days(rule: str, start: date | pd.Timestamp, end: date | pd.Timestamp,
              ahead: int = 0) -> pd.DatetimeIndex:
    """Effective roll days (plus tolerance) of ``rule`` between ``start`` and ``end``.

    ``ahead`` > 0 (series rolled that many business days before expiry, see
    :func:`front_contract`): the early switch days *and* the expiry roll days - an older
    window may have been served by the generic, which jumps at expiry.
    """
    if rule not in RULES:
        raise ValueError(f"unknown roll rule {rule!r} (known: {sorted(RULES)})")
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    days: list[pd.Timestamp] = []
    for month in pd.period_range(first - pd.DateOffset(months=1), last + pd.DateOffset(months=1), freq="M"):
        roll = RULES[rule](month.year, month.month)
        days += [roll + k * BDAY for k in range(TOLERANCE_BDAYS + 1)]
    if ahead:
        for month in pd.period_range(first - pd.DateOffset(months=1), last + pd.DateOffset(months=4),
                                     freq="M"):
            switch = switch_day(rule, month.year, month.month, ahead)
            days += [switch + k * BDAY for k in range(TOLERANCE_BDAYS + 1)]
    index = pd.DatetimeIndex(sorted(set(days)), name="date")
    return index[(index >= first) & (index <= last)]


def rolled_between(rule: str | None, after: pd.Timestamp, until: pd.Timestamp, ahead: int = 0) -> bool:
    """True when a roll window intersects ``(after, until]``."""
    if rule is None or until <= after:
        return False
    return len(roll_days(rule, after + pd.Timedelta(days=1), until, ahead)) > 0


# ------------------------------------------------------------ contract months
MONTH_CODES = "FGHJKMNQUVXZ"


def _shift(year: int, month: int, months: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + months
    return index // 12, index % 12 + 1


def last_trading_day(rule: str, year: int, month: int) -> pd.Timestamp:
    """Last trading day of the contract for delivery month ``month`` / ``year``."""
    if rule == "brent":  # last business day of the second month preceding the contract month
        return _last_bday(*_shift(year, month, -2))
    if rule == "wti":  # 3 business days before the 25th of the preceding month
        y, m = _shift(year, month, -1)
        d25 = pd.Timestamp(y, m, 25)
        anchor = d25 if d25.dayofweek < 5 else d25 - BDAY
        return anchor - 3 * BDAY
    if rule == "ttf":  # 2 business days before the delivery month
        return _last_bday(*_shift(year, month, -1)) - BDAY
    raise ValueError(f"unknown roll rule {rule!r} (known: {sorted(RULES)})")


def front_contract(rule: str, day: date | pd.Timestamp, ahead: int = 0) -> tuple[int, int]:
    """Contract month quoted on ``day``: the nearest one, left ``ahead`` business days
    before its last trading day (the "most active" convention of Investing & co)."""
    stamp = pd.Timestamp(day).normalize()
    for k in range(0, 6):
        year, month = _shift(stamp.year, stamp.month, k)
        if stamp <= last_trading_day(rule, year, month) - ahead * BDAY:
            return year, month
    raise ValueError(f"no contract found for {day}")  # pragma: no cover - k = 6 always fits


def switch_day(rule: str, year: int, month: int, ahead: int = 0) -> pd.Timestamp:
    """First day the contract after ``month`` / ``year`` is quoted (early roll ``ahead``)."""
    return last_trading_day(rule, year, month) - ahead * BDAY + BDAY


def contract_symbol(pattern: str, year: int, month: int) -> str:
    """``"BZ{code}{yy}.NYM"`` -> ``"BZF27.NYM"`` (January 2027)."""
    return pattern.replace("{code}", MONTH_CODES[month - 1]).replace("{yy}", f"{year % 100:02d}")
