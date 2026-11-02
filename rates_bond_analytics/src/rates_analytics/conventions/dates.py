"""Spot lag, settlement and money-market date helpers."""

from __future__ import annotations

from datetime import date

from rates_analytics.conventions.calendar import BusinessDayConvention, Calendar
from rates_analytics.conventions.dateutils import add_months
from rates_analytics.conventions.dateutils import end_of_month as calendar_eom
from rates_analytics.conventions.period import Period
from rates_analytics.exceptions import ConventionError

BDC = BusinessDayConvention


def spot_date(trade_date: date, calendar: Calendar, spot_lag: int = 2) -> date:
    """Spot (value) date: ``spot_lag`` business days after the trade date.

    EUR money market, €STR OIS, EURIBOR swaps and EGB cash all settle T+2 TARGET.
    If ``trade_date`` is itself a holiday, it is first rolled to the next business
    day (a trade cannot be booked on a TARGET closing day).
    """
    if spot_lag < 0:
        raise ConventionError(f"spot_lag must be >= 0, got {spot_lag}")
    t = calendar.adjust(trade_date, BDC.FOLLOWING)
    return calendar.advance(t, spot_lag) if spot_lag else t


def settlement_date(trade_date: date, calendar: Calendar, settlement_lag: int = 2) -> date:
    """Alias of :func:`spot_date` for bonds (T+``settlement_lag``)."""
    return spot_date(trade_date, calendar, settlement_lag)


def money_market_dates(
    trade_date: date,
    tenor: str,
    calendar: Calendar,
    spot_lag: int = 2,
    bdc: BusinessDayConvention | str = BDC.MODIFIED_FOLLOWING,
    end_of_month: bool = True,
) -> tuple[date, date]:
    """Start and end dates of a deposit / OIS money-market instrument.

    * ``ON`` (overnight): trade date -> +1 business day;
    * ``TN`` (tom-next): +1 -> +2 business days;
    * ``SN`` (spot-next): spot -> spot + 1 business day;
    * ``SW``: spot -> spot + 1 week;
    * any other tenor: spot -> spot + tenor (``bdc``, EOM rule for M/Y tenors).
    """
    t = calendar.adjust(trade_date, BDC.FOLLOWING)
    key = tenor.strip().upper()
    if key in {"ON", "O/N"}:
        return t, calendar.advance(t, 1)
    if key in {"TN", "T/N"}:
        start = calendar.advance(t, 1)
        return start, calendar.advance(start, 1)
    spot = spot_date(t, calendar, spot_lag)
    if key in {"SN", "S/N"}:
        return spot, calendar.advance(spot, 1)
    if key == "SW":
        key = "1W"
    p = Period.parse(key)
    if p.n <= 0:
        raise ConventionError(f"Money-market tenor must be positive, got {tenor!r}")
    return spot, calendar.advance(spot, p, bdc, end_of_month)


def maturity_from_tenor(
    start: date,
    tenor: Period | str,
    calendar: Calendar,
    bdc: BusinessDayConvention | str = BDC.MODIFIED_FOLLOWING,
    end_of_month: bool = False,
    adjust: bool = False,
) -> date:
    """Maturity ``start + tenor``.

    Swaps quote an *unadjusted* maturity rolled by calendar months (the
    termination date is adjusted later by the schedule); with ``adjust=True`` the
    business-day-adjusted maturity is returned instead.
    """
    p = Period.parse(tenor)
    months = p.total_months
    if adjust or months is None:
        return calendar.advance(start, p, bdc, end_of_month)
    unadj = add_months(start, months)
    if end_of_month and calendar.is_end_of_month(start):
        unadj = calendar_eom(unadj)
    return unadj
