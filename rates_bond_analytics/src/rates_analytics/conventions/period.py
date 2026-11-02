"""Tenors (``Period``) and coupon frequencies (``Frequency``).

A tenor is a signed integer length in a time unit: ``3M``, ``10Y``, ``1W``, ``2D``.
Market shorthands ``ON`` / ``O/N`` are parsed as ``1D``; compound tenors such as
``1Y6M`` are normalised to months (``18M``). ``TN`` and ``SN`` are money-market
*start* conventions, not lengths: they are handled by
:func:`rates_analytics.conventions.dates.money_market_dates`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, IntEnum

from rates_analytics.exceptions import ConventionError


class TimeUnit(str, Enum):
    """Unit of a tenor."""

    DAYS = "D"
    WEEKS = "W"
    MONTHS = "M"
    YEARS = "Y"


_TENOR_TOKEN = re.compile(r"(\d+)\s*([DWMY])", re.IGNORECASE)
_OVERNIGHT_ALIASES = {"ON", "O/N", "1BD"}


@dataclass(frozen=True, order=False)
class Period:
    """A tenor ``n`` x ``unit`` (``n`` may be negative)."""

    n: int
    unit: TimeUnit

    def __post_init__(self) -> None:
        if not isinstance(self.n, int) or isinstance(self.n, bool):
            raise ConventionError(f"Period length must be an int, got {self.n!r}")
        if not isinstance(self.unit, TimeUnit):
            object.__setattr__(self, "unit", TimeUnit(self.unit))

    # ------------------------------------------------------------------ parsing
    @classmethod
    def parse(cls, text: str | Period) -> Period:
        """Parse a tenor string (``"6M"``, ``"10Y"``, ``"1Y6M"``, ``"ON"``)."""
        if isinstance(text, Period):
            return text
        if not isinstance(text, str) or not text.strip():
            raise ConventionError(f"Invalid tenor {text!r}")
        s = text.strip().upper()
        if s in _OVERNIGHT_ALIASES:
            return cls(1, TimeUnit.DAYS)
        sign = -1 if s.startswith("-") else 1
        body = s.lstrip("+-")
        tokens = _TENOR_TOKEN.findall(body)
        if not tokens or "".join(n + u for n, u in tokens) != body.replace(" ", ""):
            raise ConventionError(f"Invalid tenor {text!r}")
        if len(tokens) == 1:
            n, u = tokens[0]
            return cls(sign * int(n), TimeUnit(u))
        units = {u for _, u in tokens}
        if units <= {"Y", "M"}:
            months = sum(int(n) * (12 if u == "Y" else 1) for n, u in tokens)
            return cls(sign * months, TimeUnit.MONTHS)
        if units <= {"W", "D"}:
            days = sum(int(n) * (7 if u == "W" else 1) for n, u in tokens)
            return cls(sign * days, TimeUnit.DAYS)
        raise ConventionError(f"Cannot mix day/week and month/year units in {text!r}")

    # --------------------------------------------------------------- arithmetic
    def __neg__(self) -> Period:
        return Period(-self.n, self.unit)

    def __mul__(self, k: int) -> Period:
        return Period(self.n * int(k), self.unit)

    __rmul__ = __mul__

    def __str__(self) -> str:
        return f"{self.n}{self.unit.value}"

    def __repr__(self) -> str:
        return f"Period('{self}')"

    @property
    def is_zero(self) -> bool:
        return self.n == 0

    @property
    def total_months(self) -> int | None:
        """Length in months for M/Y units, ``None`` for D/W units."""
        if self.unit is TimeUnit.MONTHS:
            return self.n
        if self.unit is TimeUnit.YEARS:
            return 12 * self.n
        return None

    @property
    def years(self) -> float:
        """Approximate length in years (D/365, W*7/365, M/12, Y)."""
        factor = {TimeUnit.DAYS: 1 / 365.0, TimeUnit.WEEKS: 7 / 365.0,
                  TimeUnit.MONTHS: 1 / 12.0, TimeUnit.YEARS: 1.0}[self.unit]
        return self.n * factor

    def normalized(self) -> Period:
        """Canonical form (``12M`` -> ``1Y``, ``14D`` -> ``2W``)."""
        if self.unit is TimeUnit.MONTHS and self.n % 12 == 0 and self.n != 0:
            return Period(self.n // 12, TimeUnit.YEARS)
        if self.unit is TimeUnit.DAYS and self.n % 7 == 0 and self.n != 0:
            return Period(self.n // 7, TimeUnit.WEEKS)
        return self

    def sort_key(self) -> float:
        """Key for sorting curve pillars by length."""
        return self.years


class Frequency(IntEnum):
    """Number of payments per year."""

    ONCE = 0
    ANNUAL = 1
    SEMIANNUAL = 2
    QUARTERLY = 4
    MONTHLY = 12

    @property
    def period(self) -> Period:
        """Tenor between two payments (``ONCE`` -> zero period)."""
        if self is Frequency.ONCE:
            return Period(0, TimeUnit.YEARS)
        return Period(12 // int(self), TimeUnit.MONTHS).normalized()

    @classmethod
    def from_period(cls, p: Period | str) -> Frequency:
        """Frequency matching a coupon tenor (``6M`` -> ``SEMIANNUAL``)."""
        p = Period.parse(p)
        months = p.total_months
        if p.n == 0:
            return cls.ONCE
        if months is None or months <= 0 or 12 % months != 0:
            raise ConventionError(f"Tenor {p} does not map to a regular frequency")
        return cls(12 // months)

    @classmethod
    def parse(cls, value: str | int | Frequency) -> Frequency:
        """Parse ``"annual"``, ``"S"``, ``2``, ``"6M"`` ..."""
        if isinstance(value, Frequency):
            return value
        if isinstance(value, int):
            return cls(value)
        aliases = {"A": 1, "ANNUAL": 1, "Y": 1, "S": 2, "SA": 2, "SEMIANNUAL": 2,
                   "SEMI-ANNUAL": 2, "Q": 4, "QUARTERLY": 4, "M": 12, "MONTHLY": 12,
                   "ONCE": 0, "ZC": 0, "Z": 0}
        key = str(value).strip().upper()
        if key in aliases:
            return cls(aliases[key])
        try:
            return cls.from_period(key)
        except ConventionError as exc:
            raise ConventionError(f"Unknown frequency {value!r}") from exc
