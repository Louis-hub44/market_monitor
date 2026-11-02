r"""Day count conventions (ISDA 2006 §4.16 / ICMA Rule 251).

Each convention is an immutable :class:`DayCounter` exposing

* ``day_count(d1, d2)``   – the numerator (number of days per the convention),
* ``year_fraction(d1, d2, ref_start, ref_end)`` – the accrual factor :math:`\tau`.

Implemented:

=================  ==========================================================
``ACT/360``        :math:`\tau = (d_2-d_1)/360`  (money market, €STR, EURIBOR)
``ACT/365F``       :math:`\tau = (d_2-d_1)/365`
``30/360``         Bond Basis (ISDA 4.16(f)) – EUR swap fixed legs
``30E/360``        Eurobond basis (ISDA 4.16(g))
``30E/360 ISDA``   ISDA 4.16(h), needs the termination date
``30/360 US``      SIA / NASD bond basis with end-of-February rules
``ACT/ACT ICMA``   :math:`\tau = \frac{d_2-d_1}{F\,(E-S)}` on reference period
                   :math:`[S,E]` – government bonds (Bund, OAT, BTP)
``ACT/ACT ISDA``   split by calendar year, 365 or 366 denominators
=================  ==========================================================

The ACT/ACT ICMA treatment of irregular (short/long) periods follows the notional
reference-period decomposition of ICMA Rule 251 (identical to QuantLib's
``ActualActual::ISMA`` with explicit reference dates).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from rates_analytics.conventions.dateutils import (
    add_months,
    days_between,
    days_in_month,
    is_end_of_month,
    is_leap,
)
from rates_analytics.exceptions import ConventionError


class DayCounter(ABC):
    """Abstract day count convention."""

    name: str = "abstract"

    @abstractmethod
    def day_count(self, d1: date, d2: date) -> int:
        """Number of days between ``d1`` and ``d2`` under the convention."""

    @abstractmethod
    def year_fraction(
        self,
        d1: date,
        d2: date,
        ref_start: date | None = None,
        ref_end: date | None = None,
    ) -> float:
        """Accrual fraction from ``d1`` to ``d2``.

        ``ref_start`` / ``ref_end`` are only used by ACT/ACT ICMA (coupon reference
        period); other conventions ignore them.
        """

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"<DayCounter {self.name}>"


# --------------------------------------------------------------------- ACT/xxx
@dataclass(frozen=True, repr=False)
class _ActualFixed(DayCounter):
    denominator: float
    name: str

    def day_count(self, d1: date, d2: date) -> int:
        return days_between(d1, d2)

    def year_fraction(self, d1, d2, ref_start=None, ref_end=None) -> float:
        return days_between(d1, d2) / self.denominator


ACT_360 = _ActualFixed(360.0, "ACT/360")
ACT_365F = _ActualFixed(365.0, "ACT/365F")


# ----------------------------------------------------------------------- 30/360
class Thirty360Variant(str, Enum):
    """Supported 30/360 flavours."""

    BOND_BASIS = "30/360"          # ISDA 4.16(f) – "30/360", "360/360", "Bond Basis"
    EUROBOND = "30E/360"           # ISDA 4.16(g) – "Eurobond Basis"
    ISDA = "30E/360 ISDA"          # ISDA 4.16(h) – "German"
    US = "30/360 US"               # SIA / NASD


def _is_last_of_february(d: date) -> bool:
    return d.month == 2 and d.day == days_in_month(d.year, 2)


@dataclass(frozen=True, repr=False)
class Thirty360(DayCounter):
    r"""30/360 family:  :math:`\tau = \frac{360(Y_2-Y_1)+30(M_2-M_1)+(D_2-D_1)}{360}`.

    Attributes:
        variant: which rule set adjusts :math:`D_1, D_2`.
        termination_date: only for ``30E/360 ISDA`` – a ``D_2`` falling on the
            last day of February is *not* moved to 30 when it is the termination date.
    """

    variant: Thirty360Variant = Thirty360Variant.BOND_BASIS
    termination_date: date | None = None
    name: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "variant", Thirty360Variant(self.variant))
        object.__setattr__(self, "name", self.variant.value)

    def _adjusted_days(self, d1: date, d2: date) -> tuple[int, int]:
        dd1, dd2 = d1.day, d2.day
        v = self.variant
        if v is Thirty360Variant.BOND_BASIS:
            if dd1 == 31:
                dd1 = 30
            if dd2 == 31 and dd1 == 30:
                dd2 = 30
        elif v is Thirty360Variant.EUROBOND:
            dd1 = min(dd1, 30)
            dd2 = min(dd2, 30)
        elif v is Thirty360Variant.ISDA:
            if is_end_of_month(d1):
                dd1 = 30
            if is_end_of_month(d2) and not (
                d2.month == 2 and self.termination_date is not None and d2 == self.termination_date
            ):
                dd2 = 30
        else:  # US (SIA rule order: February rules first)
            if _is_last_of_february(d2) and _is_last_of_february(d1):
                dd2 = 30
            if _is_last_of_february(d1):
                dd1 = 30
            if dd2 == 31 and dd1 >= 30:
                dd2 = 30
            if dd1 == 31:
                dd1 = 30
        return dd1, dd2

    def day_count(self, d1: date, d2: date) -> int:
        dd1, dd2 = self._adjusted_days(d1, d2)
        return 360 * (d2.year - d1.year) + 30 * (d2.month - d1.month) + (dd2 - dd1)

    def year_fraction(self, d1, d2, ref_start=None, ref_end=None) -> float:
        return self.day_count(d1, d2) / 360.0


THIRTY_360 = Thirty360(Thirty360Variant.BOND_BASIS)
THIRTY_E_360 = Thirty360(Thirty360Variant.EUROBOND)
THIRTY_360_US = Thirty360(Thirty360Variant.US)


# ------------------------------------------------------------------ ACT/ACT ISDA
@dataclass(frozen=True, repr=False)
class ActActISDA(DayCounter):
    r""":math:`\tau = \sum_{y} \frac{\text{days in year } y}{365 \text{ or } 366}`."""

    name: str = "ACT/ACT ISDA"

    def day_count(self, d1: date, d2: date) -> int:
        return days_between(d1, d2)

    def year_fraction(self, d1, d2, ref_start=None, ref_end=None) -> float:
        if d1 == d2:
            return 0.0
        if d1 > d2:
            return -self.year_fraction(d2, d1)
        y1, y2 = d1.year, d2.year
        b1 = 366.0 if is_leap(y1) else 365.0
        b2 = 366.0 if is_leap(y2) else 365.0
        tau = float(y2 - y1 - 1)
        tau += days_between(d1, date(y1 + 1, 1, 1)) / b1
        tau += days_between(date(y2, 1, 1), d2) / b2
        return tau


ACT_ACT_ISDA = ActActISDA()


# ------------------------------------------------------------------ ACT/ACT ICMA
@dataclass(frozen=True, repr=False)
class ActActICMA(DayCounter):
    r"""ACT/ACT ICMA (ISMA, Rule 251) – government bond accrual.

    For an accrual from :math:`d_1` to :math:`d_2` inside the regular coupon period
    :math:`[S, E]` of a bond paying :math:`F` coupons per year:

    .. math:: \tau = \frac{1}{F}\,\frac{d_2 - d_1}{E - S}

    Irregular periods are decomposed on *notional* regular periods obtained by
    rolling the reference dates by the coupon tenor. Without reference dates, the
    accrual period itself is taken as reference (correct only for regular periods).
    """

    name: str = "ACT/ACT ICMA"

    def day_count(self, d1: date, d2: date) -> int:
        return days_between(d1, d2)

    def year_fraction(self, d1, d2, ref_start=None, ref_end=None) -> float:
        if d1 == d2:
            return 0.0
        if d1 > d2:
            return -self.year_fraction(d2, d1, ref_start, ref_end)
        rs = ref_start if ref_start is not None else d1
        re_ = ref_end if ref_end is not None else d2
        if not (re_ > rs and re_ > d1):
            raise ConventionError(
                f"ACT/ACT ICMA: invalid reference period [{rs}, {re_}] for accrual [{d1}, {d2}]"
            )
        months = round(12.0 * days_between(rs, re_) / 365.0)
        if months == 0:
            rs, re_, months = d1, add_months(d1, 12), 12
        period = months / 12.0

        if d2 <= re_:
            if d1 >= rs:
                # rs <= d1 <= d2 <= re : plain regular sub-period
                return period * days_between(d1, d2) / days_between(rs, re_)
            # long first coupon: d1 < rs < re, d2 <= re
            previous_ref = add_months(rs, -months)
            if d2 > rs:
                return self.year_fraction(d1, rs, previous_ref, rs) + self.year_fraction(
                    rs, d2, rs, re_
                )
            return self.year_fraction(d1, d2, previous_ref, rs)

        if rs > d1:
            # d1 < rs < re < d2: long first period running past its end (e.g. a payment
            # date shifted by a holiday). Split at re: long-first part + next notional period.
            # (QuantLib's ISMA implementation rejects this case.)
            return self.year_fraction(d1, re_, rs, re_) + self.year_fraction(re_, d2, re_, add_months(re_, months))
        # long last coupon: rs <= d1 < re < d2
        total = self.year_fraction(d1, re_, rs, re_)
        i = 0
        while True:
            new_start = add_months(re_, months * i)
            new_end = add_months(re_, months * (i + 1))
            if d2 < new_end:
                break
            total += period
            i += 1
        return total + self.year_fraction(new_start, d2, new_start, new_end)


ACT_ACT_ICMA = ActActICMA()


# --------------------------------------------------------------------- registry
_ALIASES: dict[str, DayCounter] = {
    "ACT/360": ACT_360, "A360": ACT_360, "ACTUAL/360": ACT_360, "A/360": ACT_360,
    "ACT/365F": ACT_365F, "ACT/365": ACT_365F, "A365F": ACT_365F, "ACT/365 FIXED": ACT_365F,
    "ACTUAL/365 FIXED": ACT_365F, "A/365F": ACT_365F,
    "30/360": THIRTY_360, "30/360 BOND BASIS": THIRTY_360, "BOND BASIS": THIRTY_360,
    "360/360": THIRTY_360, "30/360 ISDA": THIRTY_360, "ISMA-30/360": THIRTY_E_360,
    "30E/360": THIRTY_E_360, "EUROBOND BASIS": THIRTY_E_360, "30/360 EUROBOND": THIRTY_E_360,
    "30/360 US": THIRTY_360_US, "30U/360": THIRTY_360_US, "30/360 SIA": THIRTY_360_US,
    "ACT/ACT ICMA": ACT_ACT_ICMA, "ACT/ACT ISMA": ACT_ACT_ICMA, "ACT/ACT": ACT_ACT_ICMA,
    "ACTUAL/ACTUAL ICMA": ACT_ACT_ICMA, "ACT/ACT BOND": ACT_ACT_ICMA, "ISMA-ACT/ACT": ACT_ACT_ICMA,
    "ACT/ACT ISDA": ACT_ACT_ISDA, "ACTUAL/ACTUAL ISDA": ACT_ACT_ISDA, "ACT/ACT HISTORICAL": ACT_ACT_ISDA,
}


def get_day_counter(name: str | DayCounter, termination_date: date | None = None) -> DayCounter:
    """Return a day counter from its market name (case-insensitive, many aliases).

    ``"30E/360 ISDA"`` accepts an optional ``termination_date``.
    Note: a bare ``"ACT/ACT"`` maps to ICMA, the government bond market default
    (Bloomberg ``DAY_CNT_DES`` = ``ACT/ACT``).
    """
    if isinstance(name, DayCounter):
        return name
    key = " ".join(str(name).strip().upper().split())
    if key in {"30E/360 ISDA", "30E/360 GERMAN", "GERMAN"}:
        return Thirty360(Thirty360Variant.ISDA, termination_date)
    try:
        return _ALIASES[key]
    except KeyError as exc:
        raise ConventionError(f"Unknown day count convention {name!r}") from exc
