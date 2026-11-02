"""Trading calendars of the listing places (``exchange_calendars``, offline).

A member's last close is compared with the last session of *its* market: a stock that did
not trade on a session its market held is flagged (suspended, delisted, missing print) and
not counted; on a local holiday (London open, Paris closed...) nothing is expected.
"""

from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

#: market of a member whose listing code is unknown, by currency
DEFAULT_MIC_BY_CURRENCY = {"EUR": "XPAR", "USD": "XNYS", "GBP": "XLON", "GBX": "XLON", "CHF": "XSWX",
                           "SEK": "XSTO", "DKK": "XCSE", "NOK": "XOSL", "PLN": "XWAR"}
CALENDAR_START = "2015-01-01"


@lru_cache(maxsize=32)
def calendar(mic: str) -> Any | None:
    """``exchange_calendars`` calendar of ``mic`` (``None`` if unknown or library missing)."""
    try:
        import exchange_calendars as xcals
    except ImportError:  # pragma: no cover - declared dependency
        return None
    try:
        return xcals.get_calendar(mic, start=CALENDAR_START)
    except Exception:  # noqa: BLE001 - unknown code: no calendar, the caller falls back
        return None


def market_of(mic: str, currency: str = "") -> str:
    return mic or DEFAULT_MIC_BY_CURRENCY.get(currency.upper(), "XPAR")


def is_session(mic: str, day: date) -> bool:
    cal = calendar(mic)
    if cal is None:
        return day.weekday() < 5
    return bool(cal.is_session(pd.Timestamp(day)))


def last_closed_session(mic: str, now: datetime) -> date:
    """Last session of ``mic`` whose close is past at ``now`` (tz-aware)."""
    cal = calendar(mic)
    if cal is None:
        day = pd.Timestamp(now.date())
        while day.weekday() >= 5:
            day -= pd.Timedelta(days=1)
        return day.date()
    utc = pd.Timestamp(now).tz_convert("UTC")
    session = cal.date_to_session(pd.Timestamp(now.astimezone(ZoneInfo("UTC")).date()), direction="previous")
    if cal.session_close(session) > utc:  # today's session still open: the previous one
        session = cal.previous_session(session)
    return session.date()


def previous_session(mic: str, session: date) -> date:
    cal = calendar(mic)
    if cal is None:
        return (pd.Timestamp(session) - pd.offsets.BDay()).date()
    return cal.previous_session(cal.date_to_session(pd.Timestamp(session), direction="previous")).date()


def session_on_or_before(mic: str, day: date) -> date:
    """``day`` if ``mic`` holds a session on it, else the session before."""
    cal = calendar(mic)
    if cal is None:
        stamp = pd.Timestamp(day)
        while stamp.weekday() >= 5:
            stamp -= pd.Timedelta(days=1)
        return stamp.date()
    return cal.date_to_session(pd.Timestamp(day), direction="previous").date()
