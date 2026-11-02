"""Bond spreads vs QuantLib (oracle only).

* Z-spread       <-> ``BondFunctions.zSpread`` (continuous, ACT/365F) on the €STR curve;
* ASW par-par    <-> ``AssetSwap(parAssetSwap=True).fairSpread()`` – EURIBOR 6M projection,
  €STR discounting;
* I-spread swap  <-> ``MakeVanillaSwap`` with explicit effective / termination dates.
"""

from __future__ import annotations

import random

import pytest

ql = pytest.importorskip("QuantLib")

from rates_analytics.bonds import asw_par_par, swap_rate_to_maturity, z_spread  # noqa: E402
from rates_analytics.conventions import TARGET  # noqa: E402
from rates_analytics.curves import build_eur_curves  # noqa: E402
from tests.oracle.ql_helpers import to_ql  # noqa: E402
from tests.oracle.test_euribor_curve_vs_quantlib import AS_OF, ql_euribor_curve, sample_quotes  # noqa: E402
from tests.oracle.test_sovereign_vs_quantlib import ql_bond, random_bond  # noqa: E402

pytestmark = pytest.mark.oracle


@pytest.fixture(scope="module")
def curves():
    quotes = sample_quotes()
    mine = build_eur_curves(AS_OF, quotes)
    ql_disc, ql_fwd = ql_euribor_curve(quotes, "log_linear")
    return mine, ql_disc, ql_fwd


def _bonds(n: int, seed: int):
    rng = random.Random(seed)
    out = []
    while len(out) < n:
        b = random_bond(rng)
        if TARGET.is_business_day(b.maturity) and b.residual_years(AS_OF) > 0.6:
            out.append(b)
    return out


def test_z_spread_matches_quantlib(curves):
    mine, ql_disc, _ = curves
    ql.Settings.instance().evaluationDate = to_ql(AS_OF)
    rng = random.Random(1)
    for b in _bonds(80, 2):
        settle = b.settlement_date(AS_OF)
        clean = rng.uniform(80, 115)
        ref = ql.BondFunctions.zSpread(ql_bond(b), ql.BondPrice(clean, ql.BondPrice.Clean), ql_disc,
                                       ql.Actual365Fixed(), ql.Continuous, ql.Annual, to_ql(settle), 1e-14, 500, 0.0)
        assert z_spread(b, clean, settle, mine.discount_curve) == pytest.approx(ref, abs=1e-10)


def test_asw_par_par_matches_quantlib(curves):
    mine, ql_disc, ql_fwd = curves
    ql.Settings.instance().evaluationDate = to_ql(AS_OF)
    disc_h, fwd_h = ql.YieldTermStructureHandle(ql_disc), ql.YieldTermStructureHandle(ql_fwd)
    e6 = ql.Euribor6M(fwd_h)
    engine = ql.DiscountingSwapEngine(disc_h)
    rng = random.Random(3)
    for b in _bonds(60, 4):
        settle = b.settlement_date(AS_OF)
        clean = rng.uniform(85, 112)
        qb = ql_bond(b)
        qb.setPricingEngine(ql.DiscountingBondEngine(disc_h))
        float_sched = ql.Schedule(to_ql(settle), to_ql(b.maturity), ql.Period(6, ql.Months), ql.TARGET(),
                                  ql.ModifiedFollowing, ql.ModifiedFollowing, ql.DateGeneration.Backward, True)
        asw = ql.AssetSwap(True, qb, clean, e6, 0.0, float_sched, ql.Actual360(), True)
        asw.setPricingEngine(engine)
        res = asw_par_par(b, clean, settle, mine.discount_curve, mine.forward_curve("EURIBOR6M"))
        assert res.spread == pytest.approx(asw.fairSpread(), abs=1e-10), b


def test_swap_rate_to_bond_maturity_matches_quantlib(curves):
    mine, ql_disc, ql_fwd = curves
    e6 = ql.Euribor6M(ql.YieldTermStructureHandle(ql_fwd))
    for b in _bonds(40, 6):
        settle = b.settlement_date(AS_OF)
        ref = ql.MakeVanillaSwap(ql.Period(1, ql.Years), e6, 0.0, ql.Period(0, ql.Days),
                                 effectiveDate=to_ql(settle), terminationDate=to_ql(b.maturity),
                                 fixedLegTenor=ql.Period(1, ql.Years),
                                 fixedLegDayCount=ql.Thirty360(ql.Thirty360.BondBasis),
                                 fixedLegEndOfMonth=True, floatingLegEndOfMonth=True,
                                 discountingTermStructure=ql.YieldTermStructureHandle(ql_disc))
        rate = swap_rate_to_maturity(b, settle, mine.discount_curve, mine.forward_curve("EURIBOR6M"))
        assert rate == pytest.approx(ref.fairRate(), abs=1e-11)

