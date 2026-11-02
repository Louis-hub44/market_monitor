"""Coupon / reset schedule generation.

:func:`generate_schedule` rolls a date grid between an effective and a
termination date and returns both the *unadjusted* roll dates (needed by bonds,
which accrue on unadjusted dates, and by ACT/ACT ICMA reference periods) and the
*adjusted* dates (swaps accrue on adjusted dates).

Rules (ISDA / market practice, same semantics as QuantLib ``Schedule``):

* ``BACKWARD`` – roll back from the termination date; any stub is at the front
  (market standard for swaps and bonds).
* ``FORWARD``  – roll forward from the effective date; any stub is at the back.
* ``ZERO``     – single period.

Stubs are short by default; ``long_stub=True`` merges the stub with the adjacent
regular period. Explicit ``first_date`` / ``next_to_last_date`` handle bonds with
an irregular first or last coupon. With ``end_of_month=True`` and a seed date on
the last business day of its month, every roll date is moved to month end.

:func:`build_accrual_periods` turns a schedule into accrual periods with payment
dates, fixing dates, ICMA reference periods and year fractions.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum

from rates_analytics.conventions.calendar import (
    NULL_CALENDAR,
    BusinessDayConvention,
    Calendar,
)
from rates_analytics.conventions.dateutils import end_of_month as calendar_eom
from rates_analytics.conventions.daycount import DayCounter
from rates_analytics.conventions.period import Period, TimeUnit
from rates_analytics.exceptions import ScheduleError

BDC = BusinessDayConvention


class DateGenerationRule(str, Enum):
    """Direction of the roll."""

    BACKWARD = "Backward"
    FORWARD = "Forward"
    ZERO = "Zero"


@dataclass(frozen=True)
class Schedule:
    """Generated schedule.

    Attributes:
        unadjusted: roll dates before business-day adjustment.
        adjusted: business-day adjusted dates (termination date adjusted with
            ``termination_bdc``).
        is_regular: one flag per period, ``False`` for stubs.
        tenor, calendar, bdc, termination_bdc, end_of_month, rule: generation inputs.
    """

    unadjusted: tuple[date, ...]
    adjusted: tuple[date, ...]
    is_regular: tuple[bool, ...]
    tenor: Period
    calendar: Calendar
    bdc: BusinessDayConvention
    termination_bdc: BusinessDayConvention
    end_of_month: bool
    rule: DateGenerationRule

    def __len__(self) -> int:
        return len(self.adjusted)

    @property
    def n_periods(self) -> int:
        return len(self.adjusted) - 1

    @property
    def start_date(self) -> date:
        return self.adjusted[0]

    @property
    def end_date(self) -> date:
        return self.adjusted[-1]

    def periods(self, adjusted: bool = True) -> Iterator[tuple[date, date]]:
        """Iterate over (start, end) pairs."""
        ds = self.adjusted if adjusted else self.unadjusted
        return zip(ds[:-1], ds[1:])

    def to_frame(self):
        """Schedule as a ``pandas.DataFrame`` (one row per period)."""
        import pandas as pd

        return pd.DataFrame(
            {
                "unadj_start": self.unadjusted[:-1],
                "unadj_end": self.unadjusted[1:],
                "start": self.adjusted[:-1],
                "end": self.adjusted[1:],
                "regular": self.is_regular,
            }
        )


def _validate(effective: date, termination: date, tenor: Period,
              first_date: date | None, next_to_last_date: date | None) -> None:
    if not effective < termination:
        raise ScheduleError(f"Effective date {effective} must precede termination {termination}")
    if tenor.n < 0:
        raise ScheduleError(f"Schedule tenor must be positive, got {tenor}")
    if first_date is not None and not effective < first_date <= termination:
        raise ScheduleError(f"first_date {first_date} outside ({effective}, {termination}]")
    if next_to_last_date is not None and not effective <= next_to_last_date < termination:
        raise ScheduleError(f"next_to_last_date {next_to_last_date} outside [{effective}, {termination})")
    if first_date and next_to_last_date and first_date > next_to_last_date:
        raise ScheduleError("first_date must not be after next_to_last_date")


def _roll_backward(effective, termination, tenor, calendar, bdc, eom, first_date, next_to_last_date):
    dates, regular = [termination], []
    seed = termination
    if next_to_last_date is not None:
        dates.append(next_to_last_date)
        regular.append(NULL_CALENDAR.advance(seed, -tenor, bdc, eom) == next_to_last_date)
        seed = next_to_last_date
    exit_date = first_date if first_date is not None else effective
    k = 1
    while True:
        temp = NULL_CALENDAR.advance(seed, -(tenor * k), bdc, eom)
        if temp < exit_date:
            if first_date is not None and calendar.adjust(dates[-1], bdc) != calendar.adjust(first_date, bdc):
                dates.append(first_date)
                regular.append(NULL_CALENDAR.advance(dates[-2], -tenor, bdc, eom) == first_date)
            break
        if calendar.adjust(dates[-1], bdc) != calendar.adjust(temp, bdc):
            dates.append(temp)
            regular.append(True)
        k += 1
    if calendar.adjust(dates[-1], bdc) != calendar.adjust(effective, bdc):
        dates.append(effective)
        regular.append(NULL_CALENDAR.advance(dates[-2], -tenor, bdc, eom) == effective)
    return dates[::-1], regular[::-1], seed


def _roll_forward(effective, termination, tenor, calendar, bdc, term_bdc, eom, first_date, next_to_last_date):
    dates, regular = [effective], []
    seed = effective
    if first_date is not None:
        dates.append(first_date)
        regular.append(NULL_CALENDAR.advance(seed, tenor, bdc, eom) == first_date)
        seed = first_date
    exit_date = next_to_last_date if next_to_last_date is not None else termination
    k = 1
    while True:
        temp = NULL_CALENDAR.advance(seed, tenor * k, bdc, eom)
        if temp > exit_date:
            if next_to_last_date is not None and calendar.adjust(dates[-1], bdc) != calendar.adjust(next_to_last_date, bdc):
                dates.append(next_to_last_date)
                regular.append(NULL_CALENDAR.advance(dates[-2], tenor, bdc, eom) == next_to_last_date)
            break
        if calendar.adjust(dates[-1], bdc) != calendar.adjust(temp, bdc):
            dates.append(temp)
            regular.append(True)
        k += 1
    if calendar.adjust(dates[-1], term_bdc) != calendar.adjust(termination, term_bdc):
        dates.append(termination)
        regular.append(False)
    return dates, regular, seed


def _apply_adjustments(raw, calendar, bdc, term_bdc, eom_active):
    """Return (unadjusted, adjusted) date lists.

    End points: the effective date is adjusted with ``bdc``, the termination date
    with ``term_bdc`` (ISDA: unadjusted unless the confirmation says otherwise).
    Intermediate dates are moved to calendar month end first when the EOM rule
    is active, then adjusted with ``bdc``.
    """
    unadj, adj = list(raw), list(raw)
    n = len(raw)
    adj[0] = calendar.adjust(raw[0], bdc)
    adj[-1] = calendar.adjust(raw[-1], term_bdc)
    for i in range(1, n - 1):
        if eom_active:
            unadj[i] = calendar_eom(raw[i])
        adj[i] = calendar.adjust(unadj[i], bdc)
    return unadj, adj


def _remove_degenerate(unadj, adj, regular):
    """Drop roll dates that collapse onto the end points after adjustment."""
    if len(adj) >= 2 and adj[-2] >= adj[-1]:
        if len(regular) >= 2:
            regular[-2] = adj[-2] == adj[-1]
        adj[-2], unadj[-2] = adj[-1], unadj[-1]
        adj.pop(), unadj.pop(), regular.pop()
    if len(adj) >= 2 and adj[1] <= adj[0]:
        if len(regular) >= 2:
            regular[1] = adj[1] == adj[0]
        adj[1], unadj[1] = adj[0], unadj[0]
        del adj[0], unadj[0], regular[0]
    if len(adj) < 2:
        raise ScheduleError("Degenerate single-date schedule")


def generate_schedule(
    effective: date,
    termination: date,
    tenor: Period | str,
    calendar: Calendar,
    bdc: BusinessDayConvention | str = BDC.MODIFIED_FOLLOWING,
    termination_bdc: BusinessDayConvention | str | None = None,
    rule: DateGenerationRule | str = DateGenerationRule.BACKWARD,
    end_of_month: bool = False,
    first_date: date | None = None,
    next_to_last_date: date | None = None,
    long_stub: bool = False,
) -> Schedule:
    """Generate a schedule.

    Args:
        effective: start of the first accrual period (unadjusted).
        termination: maturity (unadjusted).
        tenor: roll period (``"6M"``, ``"1Y"``); a zero tenor gives a single period.
        calendar: business-day calendar.
        bdc: convention for intermediate dates.
        termination_bdc: convention for the termination date (default ``bdc``).
        rule: ``BACKWARD`` (front stub) or ``FORWARD`` (back stub) or ``ZERO``.
        end_of_month: apply the EOM roll rule (ignored for tenors below 1M).
        first_date: explicit end of an irregular first period.
        next_to_last_date: explicit start of an irregular last period.
        long_stub: merge a generated stub into its neighbouring regular period.

    Returns:
        A :class:`Schedule`.

    Raises:
        ScheduleError: on inconsistent dates.
    """
    tenor = Period.parse(tenor)
    bdc = BusinessDayConvention.parse(bdc)
    term_bdc = bdc if termination_bdc is None else BusinessDayConvention.parse(termination_bdc)
    rule = DateGenerationRule(rule)
    _validate(effective, termination, tenor, first_date, next_to_last_date)
    if tenor.is_zero:
        rule = DateGenerationRule.ZERO
    if tenor.unit in (TimeUnit.DAYS, TimeUnit.WEEKS):
        end_of_month = False

    if rule is DateGenerationRule.ZERO:
        raw, regular, seed = [effective, termination], [True], termination
        eom_active = False
    elif rule is DateGenerationRule.BACKWARD:
        raw, regular, seed = _roll_backward(effective, termination, tenor, calendar, bdc,
                                            end_of_month, first_date, next_to_last_date)
        eom_active = end_of_month
    else:
        raw, regular, seed = _roll_forward(effective, termination, tenor, calendar, bdc, term_bdc,
                                           end_of_month, first_date, next_to_last_date)
        eom_active = end_of_month

    unadj, adj = _apply_adjustments(raw, calendar, bdc, term_bdc,
                                    eom_active and calendar.is_end_of_month(seed))
    _remove_degenerate(unadj, adj, regular)

    if long_stub and len(adj) > 2:
        if rule is DateGenerationRule.BACKWARD and not regular[0]:
            del unadj[1], adj[1], regular[0]
            regular[0] = False
        elif rule is DateGenerationRule.FORWARD and not regular[-1]:
            del unadj[-2], adj[-2], regular[-1]
            regular[-1] = False

    return Schedule(tuple(unadj), tuple(adj), tuple(regular), tenor, calendar, bdc,
                    term_bdc, end_of_month, rule)


# ------------------------------------------------------------------ accruals
@dataclass(frozen=True)
class AccrualPeriod:
    """One accrual period of a leg.

    Attributes:
        start, end: accrual dates actually used for the year fraction.
        unadj_start, unadj_end / adj_start, adj_end: both date flavours.
        payment_date: cash-flow date.
        fixing_date: rate fixing date (``None`` for fixed legs).
        ref_start, ref_end: ICMA reference (notional regular) period.
        year_fraction: accrual factor per the leg day count.
        is_regular: ``False`` for stubs.
    """

    start: date
    end: date
    unadj_start: date
    unadj_end: date
    adj_start: date
    adj_end: date
    payment_date: date
    fixing_date: date | None
    ref_start: date
    ref_end: date
    year_fraction: float
    is_regular: bool


def build_accrual_periods(
    schedule: Schedule,
    day_counter: DayCounter,
    *,
    accrue_on_adjusted: bool = True,
    payment_calendar: Calendar | None = None,
    payment_bdc: BusinessDayConvention | str = BDC.FOLLOWING,
    payment_lag: int = 0,
    fixing_calendar: Calendar | None = None,
    fixing_lag: int | None = None,
    fixing_in_arrears: bool = False,
) -> list[AccrualPeriod]:
    """Build accrual periods from a schedule.

    Args:
        schedule: the roll schedule.
        day_counter: leg day count.
        accrue_on_adjusted: ``True`` for swaps, ``False`` for bonds (accrual on
            unadjusted coupon dates, payment on the adjusted date).
        payment_calendar / payment_bdc / payment_lag: payment date = accrual end
            (adjusted) adjusted with ``payment_bdc`` then moved ``payment_lag``
            business days (EUR €STR OIS: lag 1).
        fixing_calendar / fixing_lag: fixing date = ``fixing_lag`` business days
            before the accrual start (EURIBOR: 2 TARGET days); ``None`` for fixed legs.
        fixing_in_arrears: fix relative to the accrual end instead of the start.

    Reference periods for irregular first/last periods are the notional regular
    periods ``(end - tenor, end)`` / ``(start, start + tenor)`` (ICMA Rule 251).
    """
    pay_cal = payment_calendar or schedule.calendar
    fix_cal = fixing_calendar or schedule.calendar
    payment_bdc = BusinessDayConvention.parse(payment_bdc)
    ds = schedule.adjusted if accrue_on_adjusted else schedule.unadjusted
    ref_cal = schedule.calendar if accrue_on_adjusted else NULL_CALENDAR
    ref_bdc = schedule.bdc if accrue_on_adjusted else BDC.UNADJUSTED
    n = schedule.n_periods
    tenor = schedule.tenor
    out: list[AccrualPeriod] = []
    for i in range(n):
        start, end = ds[i], ds[i + 1]
        ref_start, ref_end = start, end
        regular = schedule.is_regular[i]
        if not regular and not tenor.is_zero:
            if i == 0:
                ref_start = ref_cal.advance(end, -tenor, ref_bdc, schedule.end_of_month)
            elif i == n - 1:
                ref_end = ref_cal.advance(start, tenor, ref_bdc, schedule.end_of_month)
        pay = pay_cal.adjust(schedule.adjusted[i + 1], payment_bdc)
        if payment_lag:
            pay = pay_cal.advance(pay, payment_lag)
        fixing = None
        if fixing_lag is not None:
            anchor = schedule.adjusted[i + 1] if fixing_in_arrears else schedule.adjusted[i]
            fixing = fix_cal.advance(anchor, -fixing_lag)
        out.append(
            AccrualPeriod(
                start=start, end=end,
                unadj_start=schedule.unadjusted[i], unadj_end=schedule.unadjusted[i + 1],
                adj_start=schedule.adjusted[i], adj_end=schedule.adjusted[i + 1],
                payment_date=pay, fixing_date=fixing,
                ref_start=ref_start, ref_end=ref_end,
                year_fraction=day_counter.year_fraction(start, end, ref_start, ref_end),
                is_regular=regular,
            )
        )
    return out


def periods_to_frame(periods: Sequence[AccrualPeriod]):
    """Accrual periods as a ``pandas.DataFrame`` (for display / debugging)."""
    from dataclasses import asdict

    import pandas as pd

    return pd.DataFrame([asdict(p) for p in periods])
