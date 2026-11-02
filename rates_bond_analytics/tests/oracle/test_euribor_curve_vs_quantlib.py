"""EURIBOR 6M dual-curve bootstrap vs QuantLib (oracle only).

QuantLib set-up: €STR curve (phase 2 helpers), then ``Euribor6M`` projection
curve from ``DepositRateHelper`` + ``FraRateHelper`` + ``SwapRateHelper`` with the
€STR curve as ``discountingCurve`` (par coupons, EOM).
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ql = pytest.importorskip("QuantLib")

from rates_analytics.curves import (  # noqa: E402
    EURIBOR6M_CURVE_INSTRUMENTS,
    build_eur_curves,
)
from rates_analytics.instruments import VanillaSwap  # noqa: E402
from tests.oracle.ql_helpers import from_ql, to_ql  # noqa: E402
from tests.oracle.test_estr_curve_vs_quantlib import ql_curve as ql_estr_curve  # noqa: E402

pytestmark = pytest.mark.oracle
SAMPLE = Path(__file__).resolve().parents[2] / "data" / "sample" / "history.csv"
QUOTE_DATE = "2026-10-06"
AS_OF = date(2026, 10, 7)   # quotes of the previous close, as in phase 2


def sample_quotes() -> dict[str, float]:
    return pd.read_csv(SAMPLE, index_col=0).loc[QUOTE_DATE].to_dict()


def _maturity_years(market_id: str) -> float:
    last = market_id.split(".")[-1]
    if last in ("FIX",):
        return 0.5 if "EURIBOR6M" in market_id else 1 / 365
    if "X" in last:                                   # FRA mXn
        return int(last.split("X")[1]) / 12
    n, u = int(last[:-1]), last[-1]
    return n * {"W": 7 / 365, "M": 1 / 12, "Y": 1.0}[u]


def perturbed_quotes(seed: int) -> dict[str, float]:
    """Sample quotes moved by a random level / slope / basis change plus 0.5bp noise."""
    r = random.Random(seed)
    shift, slope, basis = r.uniform(-0.02, 0.01), r.uniform(-0.01, 0.01), r.uniform(-0.001, 0.002)
    out = {}
    for k, v in sample_quotes().items():
        t = _maturity_years(k)
        move = shift + slope * (1 - np.exp(-t / 5)) + (basis * np.exp(-t / 10) if "EURIBOR" in k else 0.0)
        out[k] = v + move + r.gauss(0, 0.00005)
    return out


def ql_euribor_curve(quotes: dict[str, float], method: str, dual: bool = True):
    estr_curve, _ = ql_estr_curve(quotes, method)
    disc = ql.YieldTermStructureHandle(estr_curve)
    e6 = ql.Euribor6M()
    helpers = []
    for ins in EURIBOR6M_CURVE_INSTRUMENTS:
        q = ql.QuoteHandle(ql.SimpleQuote(quotes[ins.market_id]))
        if ins.kind == "deposit":
            helpers.append(ql.DepositRateHelper(q, e6))
        elif ins.kind == "fra":
            helpers.append(ql.FraRateHelper(q, int(ins.tenor.split("X")[0]), e6))
        else:
            helpers.append(ql.SwapRateHelper(q, ql.Period(ins.tenor), ql.TARGET(), ql.Annual, ql.ModifiedFollowing,
                                             ql.Thirty360(ql.Thirty360.BondBasis), e6, ql.QuoteHandle(),
                                             ql.Period(0, ql.Days), disc if dual else ql.YieldTermStructureHandle(),
                                             2, ql.Pillar.LastRelevantDate, ql.Date(), True))
    if method == "log_linear":
        fwd = ql.PiecewiseLogLinearDiscount(to_ql(AS_OF), helpers, ql.Actual365Fixed())
    else:
        fwd = ql.PiecewiseConvexMonotoneForward(to_ql(AS_OF), helpers, ql.Actual365Fixed(),
                                                ql.IterativeBootstrap(), ql.ConvexMonotone(0.0, 1.0, False))
    fwd.enableExtrapolation()
    return estr_curve, fwd


@pytest.mark.parametrize("method", ["log_linear", "monotone_convex"])
@pytest.mark.parametrize("seed", [None, 1, 2, 3])
def test_euribor_curve_matches_quantlib(method, seed):
    quotes = sample_quotes() if seed is None else perturbed_quotes(seed)
    mine = build_eur_curves(AS_OF, quotes, method).forward_curve("EURIBOR6M")
    _, ref = ql_euribor_curve(quotes, method)
    assert [n.date for n in mine.nodes()] == [from_ql(d) for d in ref.dates()][1:]
    grid = [AS_OF + timedelta(days=k) for k in range(1, (mine.dates[-1] - AS_OF).days, 5)]
    ql_df = np.array([ref.discount(to_ql(d)) for d in grid])
    assert np.max(np.abs(mine.discount(np.array(grid, dtype="datetime64[D]")) - ql_df)) < 1e-11


def test_single_curve_mode_matches_quantlib():
    quotes = sample_quotes()
    mine = build_eur_curves(AS_OF, quotes, dual_curve=False).forward_curve("EURIBOR6M")
    _, ref = ql_euribor_curve(quotes, "log_linear", dual=False)
    grid = [AS_OF + timedelta(days=k) for k in range(1, 365 * 49, 11)]
    assert max(abs(mine.discount(d) - ref.discount(to_ql(d))) for d in grid) < 1e-11


@pytest.mark.parametrize("method", ["log_linear", "monotone_convex"])
def test_swap_par_rates_match_quantlib(method):
    quotes = sample_quotes()
    cs = build_eur_curves(AS_OF, quotes, method)
    disc, fwd = cs.discount_curve, cs.forward_curve("EURIBOR6M")
    ql_disc, ql_fwd = ql_euribor_curve(quotes, method)
    e6 = ql.Euribor6M(ql.YieldTermStructureHandle(ql_fwd))
    rng = random.Random(3)
    for _ in range(30):
        tenor = rng.choice(["2Y", "3Y", "7Y", "11Y", "23Y", "45Y"])
        fwd_start = rng.choice(["0D", "1M", "1Y", "5Y"])
        swap = VanillaSwap.from_tenor(AS_OF, tenor, forward_start=fwd_start)
        ref = ql.MakeVanillaSwap(ql.Period(tenor), e6, 0.0, ql.Period(fwd_start),
                                 effectiveDate=to_ql(swap.start), terminationDate=to_ql(swap.maturity),
                                 fixedLegTenor=ql.Period(1, ql.Years),
                                 fixedLegDayCount=ql.Thirty360(ql.Thirty360.BondBasis),
                                 fixedLegEndOfMonth=True, floatingLegEndOfMonth=True,
                                 discountingTermStructure=ql.YieldTermStructureHandle(ql_disc))
        assert swap.fair_rate(disc, fwd) == pytest.approx(ref.fairRate(), abs=1e-11), (tenor, fwd_start)
        expected_fwds = [ql.as_floating_rate_coupon(c).indexFixing() for c in ref.floatingLeg()]
        assert np.allclose(swap.forwards(fwd), expected_fwds, atol=1e-12)
