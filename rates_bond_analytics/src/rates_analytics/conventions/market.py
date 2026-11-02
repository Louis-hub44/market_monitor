"""Market conventions registry (EUR rates and euro-area government bonds).

Conventions are market facts, not user parameters, so they live in code as
immutable objects. Other terminal modules (Pricing Lab, CVA, Portfolio
Intelligence) should reference them by name through :func:`get_convention`.

Sources: ISDA 2006 definitions / 2021 €STR fallbacks, LCH SwapClear product
specs, EMMI EURIBOR rules, Finanzagentur, Agence France Trésor, MEF Tesoro.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rates_analytics.conventions.calendar import TARGET, BusinessDayConvention, Calendar
from rates_analytics.conventions.daycount import (
    ACT_360,
    ACT_ACT_ICMA,
    THIRTY_360,
    DayCounter,
)
from rates_analytics.conventions.period import Frequency, Period
from rates_analytics.exceptions import ConventionError

BDC = BusinessDayConvention


@dataclass(frozen=True)
class RateIndex:
    """Floating rate index.

    Attributes:
        name: e.g. ``"ESTR"``, ``"EURIBOR6M"``.
        tenor: ``1D`` for overnight indices.
        fixing_lag: business days between fixing and value date (EURIBOR: 2).
        publication_lag: business days between the rate date and publication
            (€STR is published at 08:00 CET on T+1).
    """

    name: str
    currency: str
    tenor: Period
    day_counter: DayCounter
    calendar: Calendar
    fixing_lag: int
    bdc: BusinessDayConvention
    end_of_month: bool
    is_overnight: bool
    publication_lag: int = 0


@dataclass(frozen=True)
class DepositConvention:
    """Unsecured deposit / money-market convention."""

    name: str
    day_counter: DayCounter
    calendar: Calendar
    spot_lag: int
    bdc: BusinessDayConvention
    end_of_month: bool


@dataclass(frozen=True)
class SwapLegConvention:
    """One leg of a swap."""

    frequency: Frequency
    day_counter: DayCounter
    bdc: BusinessDayConvention
    payment_lag: int = 0

    @property
    def tenor(self) -> Period:
        return self.frequency.period


@dataclass(frozen=True)
class SwapConvention:
    """Fixed-vs-floating swap convention.

    Attributes:
        single_period_below_1y: OIS shorter than one year pay a single
            (compounded) coupon at maturity.
    """

    name: str
    index: RateIndex
    calendar: Calendar
    spot_lag: int
    end_of_month: bool
    fixed_leg: SwapLegConvention
    float_leg: SwapLegConvention
    single_period_below_1y: bool = False


@dataclass(frozen=True)
class BondConvention:
    """Government bond convention (coupon roll, accrual, settlement, yield)."""

    name: str
    issuer: str
    calendar: Calendar
    settlement_lag: int
    day_counter: DayCounter
    frequency: Frequency
    accrual_bdc: BusinessDayConvention
    payment_bdc: BusinessDayConvention
    yield_frequency: Frequency
    end_of_month: bool = False
    ex_coupon_days: int = 0


# ------------------------------------------------------------------- indices
ESTR = RateIndex("ESTR", "EUR", Period.parse("1D"), ACT_360, TARGET, 0,
                 BDC.FOLLOWING, False, True, publication_lag=1)
EURIBOR_1M = RateIndex("EURIBOR1M", "EUR", Period.parse("1M"), ACT_360, TARGET, 2,
                       BDC.MODIFIED_FOLLOWING, True, False)
EURIBOR_3M = RateIndex("EURIBOR3M", "EUR", Period.parse("3M"), ACT_360, TARGET, 2,
                       BDC.MODIFIED_FOLLOWING, True, False)
EURIBOR_6M = RateIndex("EURIBOR6M", "EUR", Period.parse("6M"), ACT_360, TARGET, 2,
                       BDC.MODIFIED_FOLLOWING, True, False)
EURIBOR_12M = RateIndex("EURIBOR12M", "EUR", Period.parse("12M"), ACT_360, TARGET, 2,
                        BDC.MODIFIED_FOLLOWING, True, False)

# ------------------------------------------------------------------ deposits
EUR_DEPOSIT = DepositConvention("EUR_DEPOSIT", ACT_360, TARGET, 2, BDC.MODIFIED_FOLLOWING, True)

# --------------------------------------------------------------------- swaps
EUR_ESTR_OIS = SwapConvention(
    name="EUR_ESTR_OIS", index=ESTR, calendar=TARGET, spot_lag=2, end_of_month=True,
    fixed_leg=SwapLegConvention(Frequency.ANNUAL, ACT_360, BDC.MODIFIED_FOLLOWING, payment_lag=1),
    float_leg=SwapLegConvention(Frequency.ANNUAL, ACT_360, BDC.MODIFIED_FOLLOWING, payment_lag=1),
    single_period_below_1y=True,
)
EUR_EURIBOR_6M_SWAP = SwapConvention(
    name="EUR_EURIBOR_6M_SWAP", index=EURIBOR_6M, calendar=TARGET, spot_lag=2, end_of_month=True,
    fixed_leg=SwapLegConvention(Frequency.ANNUAL, THIRTY_360, BDC.MODIFIED_FOLLOWING),
    float_leg=SwapLegConvention(Frequency.SEMIANNUAL, ACT_360, BDC.MODIFIED_FOLLOWING),
)
EUR_EURIBOR_3M_SWAP = SwapConvention(
    name="EUR_EURIBOR_3M_SWAP", index=EURIBOR_3M, calendar=TARGET, spot_lag=2, end_of_month=True,
    fixed_leg=SwapLegConvention(Frequency.ANNUAL, THIRTY_360, BDC.MODIFIED_FOLLOWING),
    float_leg=SwapLegConvention(Frequency.QUARTERLY, ACT_360, BDC.MODIFIED_FOLLOWING),
)

# --------------------------------------------------------------------- bonds
_EGB: dict[str, Any] = dict(calendar=TARGET, settlement_lag=2, day_counter=ACT_ACT_ICMA,
            accrual_bdc=BDC.UNADJUSTED, payment_bdc=BDC.FOLLOWING)
BUND = BondConvention("BUND", "DE", frequency=Frequency.ANNUAL, yield_frequency=Frequency.ANNUAL, **_EGB)
OAT = BondConvention("OAT", "FR", frequency=Frequency.ANNUAL, yield_frequency=Frequency.ANNUAL, **_EGB)
BTP = BondConvention("BTP", "IT", frequency=Frequency.SEMIANNUAL, yield_frequency=Frequency.SEMIANNUAL, **_EGB)
BONO = BondConvention("BONO", "ES", frequency=Frequency.ANNUAL, yield_frequency=Frequency.ANNUAL, **_EGB)

_REGISTRY = {
    c.name: c
    for c in (ESTR, EURIBOR_1M, EURIBOR_3M, EURIBOR_6M, EURIBOR_12M, EUR_DEPOSIT,
              EUR_ESTR_OIS, EUR_EURIBOR_6M_SWAP, EUR_EURIBOR_3M_SWAP, BUND, OAT, BTP, BONO)
}


def get_convention(name: str):
    """Return a registered convention object by name (case-insensitive)."""
    try:
        return _REGISTRY[name.strip().upper()]
    except KeyError as exc:
        raise ConventionError(f"Unknown convention {name!r}; known: {sorted(_REGISTRY)}") from exc


def list_conventions() -> list[str]:
    """Names of all registered conventions."""
    return sorted(_REGISTRY)
