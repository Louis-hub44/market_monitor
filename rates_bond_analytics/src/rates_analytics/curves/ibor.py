r"""IBOR forwarding curves in the dual-curve framework (EURIBOR 6M).

Since the 2008 basis blow-out, EURIBOR rates can no longer be read off the
discounting curve: each tenor has its own *projection* curve :math:`P_{6M}`,
while all collateralised cash flows are discounted on €STR (:math:`P_d`).

Instruments for EURIBOR 6M (all spot T+2 TARGET):

* the 6M fixing = 6M deposit from spot (ACT/360, MF, EOM);
* FRAs 1x7 ... 12x18: :math:`F = \frac{1}{\tau}\left(\frac{P_{6M}(s)}{P_{6M}(e)} - 1\right)`;
* swaps 2Y ... 50Y, fixed annual 30/360 vs 6M ACT/360, priced dual curve:

.. math:: K^{par} = \frac{\sum_j F_j \tau_j P_d(p_j)}{\sum_i \tau^{30/360}_i P_d(p_i)}

The bootstrap solves the nodes of :math:`P_{6M}` with :math:`P_d` frozen. With
``discount_curve=None`` the swaps are discounted on the curve being built
(the pre-2008 single-curve method, kept for comparison).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import date

import pandas as pd

from rates_analytics.conventions.market import EUR_EURIBOR_6M_SWAP, EURIBOR_6M, RateIndex, SwapConvention
from rates_analytics.curves.bootstrap import BootstrapResult, bootstrap
from rates_analytics.curves.helpers import (
    DepositHelper,
    FRAHelper,
    RateHelper,
    SwapHelper,
    deposit_convention_for,
)
from rates_analytics.curves.interpolation import CurveError, InterpolationMethod
from rates_analytics.curves.templates import CurveInstrument, check_enough, make_helpers
from rates_analytics.curves.yield_curve import YieldCurve

logger = logging.getLogger(__name__)

EURIBOR6M_FRAS = ("1X7", "2X8", "3X9", "4X10", "5X11", "6X12", "9X15", "12X18")
EURIBOR6M_SWAP_TENORS = ("2Y", "3Y", "4Y", "5Y", "6Y", "7Y", "8Y", "9Y", "10Y", "12Y", "15Y", "20Y", "25Y",
                         "30Y", "40Y", "50Y")

EURIBOR6M_CURVE_INSTRUMENTS: tuple[CurveInstrument, ...] = (
    CurveInstrument("EUR.EURIBOR6M.FIX", "deposit", "6M"),
    *(CurveInstrument(f"EUR.EURIBOR6M.FRA.{f}", "fra", f) for f in EURIBOR6M_FRAS),
    *(CurveInstrument(f"EUR.EURIBOR6M.SWAP.{t}", "swap", t) for t in EURIBOR6M_SWAP_TENORS),
)


def _fra_start_months(tenor: str) -> int:
    try:
        a, _ = tenor.upper().split("X")
        return int(a)
    except ValueError as exc:
        raise CurveError(f"Invalid FRA tenor {tenor!r} (expected e.g. '6X12')") from exc


def make_ibor_helpers(
    as_of: date,
    quotes,
    discount_curve: YieldCurve | None,
    index: RateIndex = EURIBOR_6M,
    swap_convention: SwapConvention = EUR_EURIBOR_6M_SWAP,
    instruments: Sequence[CurveInstrument] = EURIBOR6M_CURVE_INSTRUMENTS,
) -> tuple[list[RateHelper], dict[str, str]]:
    """Helpers of an IBOR forwarding curve; returns (helpers, skipped)."""
    if swap_convention.index != index:
        raise CurveError(f"Swap convention {swap_convention.name} is not on {index.name}")
    dep_conv = deposit_convention_for(index)

    def factory(ins: CurveInstrument, q: float) -> RateHelper:
        if ins.kind == "deposit":
            return DepositHelper(as_of, ins.tenor, q, dep_conv, ins.market_id)
        if ins.kind == "fra":
            return FRAHelper(as_of, _fra_start_months(ins.tenor), q, index, ins.market_id)
        if ins.kind == "swap":
            return SwapHelper(as_of, ins.tenor, q, swap_convention, discount_curve, ins.market_id)
        raise CurveError(f"Unknown instrument kind {ins.kind!r} for {ins.market_id}")

    return make_helpers(instruments, quotes, factory)


def build_ibor_curve(
    as_of: date,
    quotes: Mapping[str, float] | pd.Series | pd.DataFrame,
    discount_curve: YieldCurve | None,
    index: RateIndex = EURIBOR_6M,
    swap_convention: SwapConvention = EUR_EURIBOR_6M_SWAP,
    instruments: Sequence[CurveInstrument] = EURIBOR6M_CURVE_INSTRUMENTS,
    interpolation: InterpolationMethod | str = InterpolationMethod.LOG_LINEAR,
    min_instruments: int = 3,
) -> BootstrapResult:
    """Bootstrap an IBOR projection curve (dual curve if ``discount_curve`` is given)."""
    if discount_curve is not None and discount_curve.reference_date != as_of:
        raise CurveError(f"Discount curve is as of {discount_curve.reference_date}, not {as_of}")
    helpers, skipped = make_ibor_helpers(as_of, quotes, discount_curve, index, swap_convention, instruments)
    name = f"EUR-{index.name}"
    check_enough(helpers, min_instruments, name)
    if skipped:
        logger.info("%s curve %s: skipped %s", name, as_of, skipped)
    res = bootstrap(as_of, helpers, interpolation, index.calendar, name=name)
    res.skipped = skipped
    return res
