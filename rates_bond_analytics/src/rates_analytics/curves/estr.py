"""€STR discounting curve (EUR OIS curve): instrument set and builders.

Market practice for the EUR risk-free curve:

* short end: the overnight deposit at €STR (``EUR.ESTR.FIX``, from the trade
  date to the next TARGET day). On the morning of T only the fixing for T-1 is
  published (08:00 CET on T); it is used as the ON quote;
* then spot-starting €STR OIS from 1W to 50Y (annual ACT/360 both legs,
  single coupon below 1Y, payment lag 1 day).

Quotes are logical ids of the market-data layer, so the same template builds
the curve from Bloomberg, a static snapshot or any other provider.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import date

import pandas as pd

from rates_analytics.conventions.market import EUR_DEPOSIT, EUR_ESTR_OIS
from rates_analytics.curves.bootstrap import BootstrapResult, bootstrap
from rates_analytics.curves.helpers import DepositHelper, OISHelper, RateHelper
from rates_analytics.curves.interpolation import CurveError, InterpolationMethod
from rates_analytics.curves.templates import CurveInstrument, check_enough, make_helpers

logger = logging.getLogger(__name__)

ESTR_FIXING_ID = "EUR.ESTR.FIX"
ESTR_OIS_TENORS = ("1W", "2W", "1M", "2M", "3M", "4M", "5M", "6M", "9M", "1Y", "18M", "2Y", "3Y", "4Y",
                   "5Y", "6Y", "7Y", "8Y", "9Y", "10Y", "12Y", "15Y", "20Y", "25Y", "30Y", "40Y", "50Y")

ESTR_CURVE_INSTRUMENTS: tuple[CurveInstrument, ...] = (
    CurveInstrument(ESTR_FIXING_ID, "deposit", "ON"),
    *(CurveInstrument(f"EUR.ESTR.OIS.{t}", "ois", t) for t in ESTR_OIS_TENORS),
)


def make_estr_helpers(as_of: date, quotes, instruments: Sequence[CurveInstrument] = ESTR_CURVE_INSTRUMENTS
                      ) -> tuple[list[RateHelper], dict[str, str]]:
    """Helpers for the available quotes; returns (helpers, skipped id -> reason)."""
    def factory(ins: CurveInstrument, q: float) -> RateHelper:
        if ins.kind == "deposit":
            return DepositHelper(as_of, ins.tenor, q, EUR_DEPOSIT, ins.market_id)
        if ins.kind == "ois":
            return OISHelper(as_of, ins.tenor, q, EUR_ESTR_OIS, ins.market_id)
        raise CurveError(f"Unknown instrument kind {ins.kind!r} for {ins.market_id}")
    return make_helpers(instruments, quotes, factory)


def build_estr_curve(
    as_of: date,
    quotes: Mapping[str, float] | pd.Series | pd.DataFrame,
    interpolation: InterpolationMethod | str = InterpolationMethod.LOG_LINEAR,
    instruments: Sequence[CurveInstrument] = ESTR_CURVE_INSTRUMENTS,
    min_instruments: int = 3,
    positivity: bool = False,
) -> BootstrapResult:
    """Bootstrap the €STR curve from quotes keyed by logical id (decimal rates).

    Missing quotes are skipped (listed in ``result.skipped``); fewer than
    ``min_instruments`` usable quotes raises :class:`CurveError`.
    """
    helpers, skipped = make_estr_helpers(as_of, quotes, instruments)
    check_enough(helpers, min_instruments, "€STR curve")
    if skipped:
        logger.info("€STR curve %s: skipped %s", as_of, skipped)
    res = bootstrap(as_of, helpers, interpolation, EUR_ESTR_OIS.calendar, name="EUR-ESTR",
                    positivity=positivity)
    res.skipped = skipped
    return res


def estr_curve_from_market(service, as_of: date, **kwargs) -> BootstrapResult:
    """Fetch quotes through a :class:`~rates_analytics.data.MarketDataService` and bootstrap."""
    instruments = kwargs.pop("instruments", ESTR_CURVE_INSTRUMENTS)
    quotes = service.quotes([i.market_id for i in instruments], as_of)
    return build_estr_curve(as_of, quotes, instruments=instruments, **kwargs)
