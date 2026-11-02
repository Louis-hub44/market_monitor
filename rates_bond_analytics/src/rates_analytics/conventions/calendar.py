"""Business-day calendars and business-day conventions.

* :class:`TargetCalendar` – TARGET2 (Eurosystem RTGS) closing days, the settlement
  calendar of EUR money market, €STR, EURIBOR, EUR swaps and EGB settlement.
* :class:`WeekendsOnly`, :class:`NullCalendar`, :class:`JointCalendar`.

Business-day adjustment (:class:`BusinessDayConvention`) and date rolling
(:meth:`Calendar.advance`) follow ISDA 2006 §4.12 (identical semantics to QuantLib).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import date, timedelta
from enum import Enum

from rates_analytics.conventions.dateutils import add_months, easter_sunday
from rates_analytics.conventions.dateutils import end_of_month as _calendar_eom
from rates_analytics.conventions.dateutils import is_end_of_month as _is_calendar_eom
from rates_analytics.conventions.period import Period, TimeUnit
from rates_analytics.exceptions import ConventionError

_ONE_DAY = timedelta(days=1)


class BusinessDayConvention(str, Enum):
    """How a date falling on a non-business day is rolled."""

    UNADJUSTED = "Unadjusted"
    FOLLOWING = "Following"
    MODIFIED_FOLLOWING = "ModifiedFollowing"
    PRECEDING = "Preceding"
    MODIFIED_PRECEDING = "ModifiedPreceding"
    HALF_MONTH_MODIFIED_FOLLOWING = "HalfMonthModifiedFollowing"
    NEAREST = "Nearest"

    @classmethod
    def parse(cls, value: str | BusinessDayConvention) -> BusinessDayConvention:
        """Parse ``"MF"``, ``"Modified Following"``, ``"F"`` ..."""
        if isinstance(value, BusinessDayConvention):
            return value
        key = str(value).replace(" ", "").replace("_", "").upper()
        aliases = {
            "U": cls.UNADJUSTED, "NONE": cls.UNADJUSTED, "UNADJUSTED": cls.UNADJUSTED,
            "F": cls.FOLLOWING, "FOLLOWING": cls.FOLLOWING,
            "MF": cls.MODIFIED_FOLLOWING, "MODFOLLOWING": cls.MODIFIED_FOLLOWING,
            "MODIFIEDFOLLOWING": cls.MODIFIED_FOLLOWING,
            "P": cls.PRECEDING, "PRECEDING": cls.PRECEDING,
            "MP": cls.MODIFIED_PRECEDING, "MODIFIEDPRECEDING": cls.MODIFIED_PRECEDING,
            "HMMF": cls.HALF_MONTH_MODIFIED_FOLLOWING,
            "HALFMONTHMODIFIEDFOLLOWING": cls.HALF_MONTH_MODIFIED_FOLLOWING,
            "NEAREST": cls.NEAREST,
        }
        try:
            return aliases[key]
        except KeyError as exc:
            raise ConventionError(f"Unknown business day convention {value!r}") from exc


BDC = BusinessDayConvention


class Calendar(ABC):
    """Abstract business-day calendar (holidays cached per year)."""

    name: str = "abstract"

    def __init__(self) -> None:
        self._cache: dict[int, frozenset[date]] = {}

    # ---------------------------------------------------------------- holidays
    @abstractmethod
    def _holidays_in_year(self, year: int) -> frozenset[date]:
        """Non-weekend holidays of ``year`` (weekend days are handled separately)."""

    def _weekend(self, d: date) -> bool:
        return d.weekday() >= 5

    def _holiday_set(self, year: int) -> frozenset[date]:
        hs = self._cache.get(year)
        if hs is None:
            hs = self._holidays_in_year(year)
            self._cache[year] = hs
        return hs

    def is_business_day(self, d: date) -> bool:
        """True if markets settle on ``d``."""
        return not (self._weekend(d) or d in self._holiday_set(d.year))

    def is_holiday(self, d: date) -> bool:
        """True if ``d`` is a weekend day or a holiday."""
        return not self.is_business_day(d)

    def holidays(self, start: date, end: date, include_weekends: bool = False) -> list[date]:
        """Holidays in ``[start, end]`` (optionally including weekends)."""
        out, d = [], start
        while d <= end:
            if self.is_holiday(d) and (include_weekends or not self._weekend(d)):
                out.append(d)
            d += _ONE_DAY
        return out

    # ------------------------------------------------------------- adjustments
    def adjust(self, d: date, bdc: BusinessDayConvention | str = BDC.FOLLOWING) -> date:
        """Roll ``d`` to a business day according to ``bdc``."""
        bdc = BusinessDayConvention.parse(bdc)
        if bdc is BDC.UNADJUSTED:
            return d
        if bdc in (BDC.FOLLOWING, BDC.MODIFIED_FOLLOWING, BDC.HALF_MONTH_MODIFIED_FOLLOWING):
            d1 = d
            while self.is_holiday(d1):
                d1 += _ONE_DAY
            if bdc is not BDC.FOLLOWING:
                if d1.month != d.month:
                    return self.adjust(d, BDC.PRECEDING)
                if bdc is BDC.HALF_MONTH_MODIFIED_FOLLOWING and d.day <= 15 < d1.day:
                    return self.adjust(d, BDC.PRECEDING)
            return d1
        if bdc in (BDC.PRECEDING, BDC.MODIFIED_PRECEDING):
            d1 = d
            while self.is_holiday(d1):
                d1 -= _ONE_DAY
            if bdc is BDC.MODIFIED_PRECEDING and d1.month != d.month:
                return self.adjust(d, BDC.FOLLOWING)
            return d1
        # NEAREST: closest business day, Following on ties
        fwd, bwd = d, d
        while self.is_holiday(fwd) and self.is_holiday(bwd):
            fwd += _ONE_DAY
            bwd -= _ONE_DAY
        return fwd if self.is_business_day(fwd) else bwd

    def end_of_month(self, d: date) -> date:
        """Last *business* day of the month of ``d``."""
        return self.adjust(_calendar_eom(d), BDC.PRECEDING)

    def is_end_of_month(self, d: date) -> bool:
        """True if ``d`` is on or after the last business day of its month."""
        return d.month != self.adjust(d + _ONE_DAY, BDC.FOLLOWING).month

    # ---------------------------------------------------------------- rolling
    def advance(
        self,
        d: date,
        period: Period | str | int,
        bdc: BusinessDayConvention | str = BDC.FOLLOWING,
        end_of_month: bool = False,
    ) -> date:
        """Roll ``d`` by a tenor.

        * ``D`` units move by *business* days (``bdc`` only used when ``n == 0``);
        * ``W`` units move by calendar weeks then adjust;
        * ``M``/``Y`` units add calendar months; with ``end_of_month=True`` and ``d``
          the last business day of its month, the result is the last business
          day of the target month (EOM rule), otherwise it is adjusted with ``bdc``.

        An ``int`` is interpreted as a number of business days.
        """
        p = Period(period, TimeUnit.DAYS) if isinstance(period, int) else Period.parse(period)
        bdc = BusinessDayConvention.parse(bdc)
        if p.n == 0:
            return self.adjust(d, bdc)
        if p.unit is TimeUnit.DAYS:
            return self._advance_business_days(d, p.n)
        if p.unit is TimeUnit.WEEKS:
            return self.adjust(d + timedelta(weeks=p.n), bdc)
        d1 = add_months(d, p.total_months or 0)
        if end_of_month:
            if bdc is BDC.UNADJUSTED and _is_calendar_eom(d):
                return _calendar_eom(d1)
            if bdc is not BDC.UNADJUSTED and self.is_end_of_month(d):
                return self.end_of_month(d1)
        return self.adjust(d1, bdc)

    def _advance_business_days(self, d: date, n: int) -> date:
        step = _ONE_DAY if n > 0 else -_ONE_DAY
        remaining = abs(n)
        d1 = d
        while remaining > 0:
            d1 += step
            while self.is_holiday(d1):
                d1 += step
            remaining -= 1
        return d1

    def business_days_between(
        self, d1: date, d2: date, include_first: bool = True, include_last: bool = False
    ) -> int:
        """Count business days between two dates (signed if ``d1 > d2``)."""
        if d1 > d2:
            return -self.business_days_between(d2, d1, include_last, include_first)
        n, d = 0, d1
        while d <= d2:
            if self.is_business_day(d) and (d != d1 or include_first) and (d != d2 or include_last):
                n += 1
            d += _ONE_DAY
        return n

    def business_days(self, start: date, end: date) -> list[date]:
        """All business days in ``[start, end]``."""
        out, d = [], start
        while d <= end:
            if self.is_business_day(d):
                out.append(d)
            d += _ONE_DAY
        return out

    def __repr__(self) -> str:
        return f"<Calendar {self.name}>"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Calendar) and other.name == self.name

    def __hash__(self) -> int:
        return hash(self.name)


class NullCalendar(Calendar):
    """Every day is a business day (used for unadjusted date rolling)."""

    name = "Null"

    def _weekend(self, d: date) -> bool:
        return False

    def _holidays_in_year(self, year: int) -> frozenset[date]:
        return frozenset()


class WeekendsOnly(Calendar):
    """Only Saturdays and Sundays are holidays."""

    name = "WeekendsOnly"

    def _holidays_in_year(self, year: int) -> frozenset[date]:
        return frozenset()


class TargetCalendar(Calendar):
    """TARGET2 closing days.

    Since 2000: New Year's Day, Good Friday, Easter Monday, Labour Day (1 May),
    Christmas Day and 26 December, plus weekends. 31 December was also a closing
    day in 1998, 1999 and 2001. Before 2000 only 1 Jan and 25 Dec are holidays.
    """

    name = "TARGET"

    def _holidays_in_year(self, year: int) -> frozenset[date]:
        hs = {date(year, 1, 1), date(year, 12, 25)}
        if year >= 2000:
            easter = easter_sunday(year)
            hs |= {easter - timedelta(days=2), easter + timedelta(days=1),
                   date(year, 5, 1), date(year, 12, 26)}
        if year in (1998, 1999, 2001):
            hs.add(date(year, 12, 31))
        return frozenset(hs)


class JointCalendar(Calendar):
    """A day is a business day only if it is one in *every* member calendar."""

    def __init__(self, calendars: Iterable[Calendar]) -> None:
        super().__init__()
        self.calendars = tuple(calendars)
        if not self.calendars:
            raise ConventionError("JointCalendar needs at least one calendar")
        self.name = "+".join(c.name for c in self.calendars)

    def _weekend(self, d: date) -> bool:
        return any(c._weekend(d) for c in self.calendars)

    def _holidays_in_year(self, year: int) -> frozenset[date]:
        return frozenset().union(*(c._holiday_set(year) for c in self.calendars))


TARGET = TargetCalendar()
NULL_CALENDAR = NullCalendar()
WEEKENDS_ONLY = WeekendsOnly()

_REGISTRY: dict[str, Calendar] = {
    "TARGET": TARGET, "TARGET2": TARGET, "TE": TARGET, "EUR": TARGET,
    "NULL": NULL_CALENDAR, "NONE": NULL_CALENDAR,
    "WEEKENDSONLY": WEEKENDS_ONLY, "WEEKENDS": WEEKENDS_ONLY,
}


def get_calendar(name: str | Calendar) -> Calendar:
    """Calendar by name; ``"TARGET+WEEKENDS"`` builds a :class:`JointCalendar`."""
    if isinstance(name, Calendar):
        return name
    parts = [p.strip().upper().replace(" ", "") for p in str(name).split("+")]
    try:
        cals = [_REGISTRY[p] for p in parts]
    except KeyError as exc:
        raise ConventionError(f"Unknown calendar {name!r}") from exc
    return cals[0] if len(cals) == 1 else JointCalendar(cals)
