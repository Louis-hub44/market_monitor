"""Curve scenarios vs QuantLib (oracle only).

* zero mode   <-> ``PiecewiseZeroSpreadedTermStructure`` (linear spread in time, flat outside,
  continuous compounding) on the same base curve;
* quotes mode <-> QuantLib re-bootstrap (€STR + EURIBOR 6M dual curve) from the shocked quotes;
  swap NPVs after the scenario vs ``MakeVanillaSwap``.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

ql = pytest.importorskip("QuantLib")

from rates_analytics.curves import build_eur_curves  # noqa: E402
from rates_analytics.instruments import VanillaSwap  # noqa: E402
from rates_analytics.scenarios import (  # noqa: E402
    apply_scenario,
    butterfly,
    custom,
    irrbb_scenarios,
    shift_curve,
    shocked_quotes,
    steepener,
)
from tests.oracle.ql_helpers import to_ql  # noqa: E402
from tests.oracle.test_euribor_curve_vs_quantlib import AS_OF, ql_euribor_curve, sample_quotes  # noqa: E402

pytestmark = pytest.mark.oracle


@pytest.fixture(scope="module")
def base():
    quotes = sample_quotes()
    return build_eur_curves(AS_OF, quotes), quotes


def test_zero_shift_matches_piecewise_zero_spreaded(base):
    cs, quotes = base
    ql_disc, _ = ql_euribor_curve(quotes, "log_linear")
    dates = [AS_OF + timedelta(days=d) for d in (200, 900, 2500, 5000, 11000)]
    spreads_bp = [12.0, -5.0, 20.0, 3.0, -10.0]
    sc = custom({(d - AS_OF).days / 365.0: s for d, s in zip(dates, spreads_bp)})
    mine = shift_curve(cs.discount_curve, sc)
    ref = ql.PiecewiseZeroSpreadedTermStructure(
        ql.YieldTermStructureHandle(ql_disc), [ql.QuoteHandle(ql.SimpleQuote(s * 1e-4)) for s in spreads_bp],
        [to_ql(d) for d in dates], ql.Continuous, ql.NoFrequency, ql.Actual365Fixed())
    ref.enableExtrapolation()
    grid = [AS_OF + timedelta(days=k) for k in range(1, 365 * 45, 13)]
    assert max(abs(mine.discount(d) - ref.discount(to_ql(d))) for d in grid) < 1e-11


@pytest.mark.parametrize("scenario", [steepener(25), butterfly(-15), irrbb_scenarios()[3]],
                         ids=lambda s: s.name)
def test_quote_scenario_matches_quantlib_rebootstrap(base, scenario):
    cs, _ = base
    shocked = shocked_quotes(cs, scenario)
    mine = apply_scenario(cs, scenario, mode="quotes")
    ql_disc, ql_fwd = ql_euribor_curve(shocked, "log_linear")
    grid = [AS_OF + timedelta(days=k) for k in range(1, 365 * 49, 17)]
    for my_curve, ref in ((mine.discount_curve, ql_disc), (mine.forward_curve("EURIBOR6M"), ql_fwd)):
        assert max(abs(my_curve.discount(d) - ref.discount(to_ql(d))) for d in grid) < 1e-11
    e6 = ql.Euribor6M(ql.YieldTermStructureHandle(ql_fwd))
    for tenor in ("3Y", "12Y", "33Y"):
        swap = VanillaSwap.from_tenor(AS_OF, tenor, 0.026, notional=1e7)
        ref = ql.MakeVanillaSwap(ql.Period(tenor), e6, 0.026, ql.Period(0, ql.Days),
                                 effectiveDate=to_ql(swap.start), terminationDate=to_ql(swap.maturity),
                                 fixedLegTenor=ql.Period(1, ql.Years),
                                 fixedLegDayCount=ql.Thirty360(ql.Thirty360.BondBasis), nominal=1e7,
                                 fixedLegEndOfMonth=True, floatingLegEndOfMonth=True,
                                 discountingTermStructure=ql.YieldTermStructureHandle(ql_disc))
        assert swap.npv(mine.discount_curve, mine.forward_curve("EURIBOR6M")) == pytest.approx(ref.NPV(), abs=1e-5)
    assert np.isfinite(list(shocked.values())).all()
