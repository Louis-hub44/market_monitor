"""€STR bootstrap vs QuantLib (oracle only).

* log-linear  <-> ``PiecewiseLogLinearDiscount``
* monotone convex <-> ``PiecewiseConvexMonotoneForward`` with
  ``ConvexMonotone(quadraticity=0, monotonicity=1, forcePositive=False)``,
  i.e. the basic Hagan-West scheme.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ql = pytest.importorskip("QuantLib")

from rates_analytics.curves import ESTR_CURVE_INSTRUMENTS, build_estr_curve  # noqa: E402
from rates_analytics.instruments import OvernightIndexedSwap  # noqa: E402
from tests.oracle.ql_helpers import from_ql, to_ql  # noqa: E402

pytestmark = pytest.mark.oracle
AS_OF = date(2026, 10, 7)
SAMPLE = Path(__file__).resolve().parents[2] / "data" / "sample" / "history.csv"


def sample_quotes() -> dict[str, float]:
    return pd.read_csv(SAMPLE, index_col=0).loc["2026-10-06"].to_dict()


def random_quotes(seed: int) -> dict[str, float]:
    """Random but plausible curves: level, slope, hump – including negative-rate regimes."""
    r = random.Random(seed)
    level, slope, hump = r.uniform(-0.007, 0.05), r.uniform(-0.015, 0.025), r.uniform(-0.01, 0.01)
    out = {}
    for ins in ESTR_CURVE_INSTRUMENTS:
        t = 1 / 365 if ins.tenor == "ON" else _tenor_years(ins.tenor)
        x = 1 - np.exp(-t / 4)
        out[ins.market_id] = level + slope * x + hump * x * np.exp(-t / 6) + r.gauss(0, 0.0003)
    return out


def _tenor_years(t: str) -> float:
    n, u = int(t[:-1]), t[-1]
    return n * {"W": 7 / 365, "M": 1 / 12, "Y": 1.0}[u]


def ql_curve(quotes: dict[str, float], method: str):
    ql.Settings.instance().evaluationDate = to_ql(AS_OF)
    estr = ql.Estr()
    helpers = []
    for ins in ESTR_CURVE_INSTRUMENTS:
        q = ql.QuoteHandle(ql.SimpleQuote(quotes[ins.market_id]))
        if ins.kind == "deposit":
            helpers.append(ql.DepositRateHelper(q, ql.Period(1, ql.Days), 0, ql.TARGET(),
                                                ql.ModifiedFollowing, False, ql.Actual360()))
        else:
            helpers.append(ql.OISRateHelper(2, ql.Period(ins.tenor), q, estr, paymentLag=1,
                                            paymentConvention=ql.ModifiedFollowing, endOfMonth=True))
    if method == "log_linear":
        c = ql.PiecewiseLogLinearDiscount(to_ql(AS_OF), helpers, ql.Actual365Fixed())
    else:
        c = ql.PiecewiseConvexMonotoneForward(to_ql(AS_OF), helpers, ql.Actual365Fixed(),
                                              ql.IterativeBootstrap(), ql.ConvexMonotone(0.0, 1.0, False))
    c.enableExtrapolation()
    return c, estr


GRID = [AS_OF + timedelta(days=k) for k in range(1, 365 * 52, 5)]


@pytest.mark.parametrize("method", ["log_linear", "monotone_convex"])
@pytest.mark.parametrize("seed", [None, 1, 2, 3, 4, 5])
def test_curve_matches_quantlib(method, seed):
    quotes = sample_quotes() if seed is None else random_quotes(seed)
    mine = build_estr_curve(AS_OF, quotes, method).curve
    ref, _ = ql_curve(quotes, method)
    assert [n.date for n in mine.nodes()] == [from_ql(d) for d in ref.dates()][1:]
    # extrapolation beyond the last pillar is a convention choice: compare inside the curve only
    grid = [d for d in GRID if d <= mine.dates[-1] - timedelta(days=1)]
    ql_df = np.array([ref.discount(to_ql(d)) for d in grid])
    assert np.max(np.abs(mine.discount(np.array(grid, dtype="datetime64[D]")) - ql_df)) < 1e-11
    ql_fwd = np.array([ref.forwardRate(to_ql(d), to_ql(d + timedelta(days=1)), ql.Actual365Fixed(),
                                       ql.Continuous).rate() for d in grid[::7]])
    my_fwd = np.array([np.log(mine.discount(d) / mine.discount(d + timedelta(days=1))) * 365
                       for d in grid[::7]])
    assert np.max(np.abs(my_fwd - ql_fwd)) * 1e4 < 1e-5      # bp


@pytest.mark.parametrize("method", ["log_linear", "monotone_convex"])
def test_ois_par_rates_match_quantlib(method):
    quotes = sample_quotes()
    mine = build_estr_curve(AS_OF, quotes, method).curve
    ref, estr = ql_curve(quotes, method)
    handle = ql.YieldTermStructureHandle(ref)
    estr = ql.Estr(handle)
    rng = random.Random(11)
    for _ in range(40):
        tenor = rng.choice(["3M", "11M", "15M", "2Y", "7Y", "13Y", "33Y"])
        fwd = rng.choice(["0D", "1M", "6M", "2Y", "5Y"])
        swap = OvernightIndexedSwap.from_tenor(AS_OF, tenor, forward_start=fwd)
        ql_swap = ql.MakeOIS(ql.Period(tenor), estr, 0.0, ql.Period(fwd), paymentLag=1, endOfMonth=True,
                             discountingTermStructure=handle,
                             paymentAdjustmentConvention=ql.ModifiedFollowing)
        assert swap.fair_rate(mine) == pytest.approx(ql_swap.fairRate(), abs=1e-11), (tenor, fwd)
