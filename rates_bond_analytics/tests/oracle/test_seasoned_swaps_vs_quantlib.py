"""Seasoned swaps (past fixings) vs QuantLib (oracle only).

* IRS vs EURIBOR 6M: ``MakeVanillaSwap`` with explicit dates, ``Euribor6M.addFixing`` for past
  fixings, €STR discounting;
* €STR OIS: ``MakeOIS`` with ``Estr.addFixing`` for every past business day, payment lag 1.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

ql = pytest.importorskip("QuantLib")

from rates_analytics.conventions import TARGET  # noqa: E402
from rates_analytics.curves import build_eur_curves  # noqa: E402
from rates_analytics.instruments import OvernightIndexedSwap, VanillaSwap  # noqa: E402
from rates_analytics.instruments.fixings import Fixings, MissingFixingError  # noqa: E402
from tests.oracle.ql_helpers import to_ql  # noqa: E402
from tests.oracle.test_euribor_curve_vs_quantlib import AS_OF, ql_euribor_curve, sample_quotes  # noqa: E402

pytestmark = pytest.mark.oracle


@pytest.fixture(scope="module")
def setup():
    quotes = sample_quotes()
    mine = build_eur_curves(AS_OF, quotes)
    ql_disc, ql_fwd = ql_euribor_curve(quotes, "log_linear")
    return mine, ql_disc, ql_fwd


def _euribor_fixings(swap: VanillaSwap, rng: random.Random) -> dict[date, float]:
    return {p.fixing_date: rng.uniform(0.015, 0.035) for p in swap.float_leg.periods if p.fixing_date < AS_OF}


def _estr_fixings(start: date, rng: random.Random) -> dict[date, float]:
    out, d = {}, start
    while d < AS_OF:
        out[d] = 0.0192 + rng.gauss(0, 0.0003)
        d = TARGET.advance(d, 1)
    return out


@pytest.mark.parametrize("seed", range(6))
def test_seasoned_irs_matches_quantlib(setup, seed):
    mine, ql_disc, ql_fwd = setup
    rng = random.Random(seed)
    start = TARGET.adjust(AS_OF - timedelta(days=rng.randint(30, 365 * 4)))
    maturity = start.replace(year=start.year + rng.choice([3, 5, 7, 10]))
    swap = VanillaSwap(start, maturity, fixed_rate=rng.uniform(0.0, 0.04), notional=1e7,
                       pay_fixed=rng.random() < 0.5, spread=rng.choice([0.0, 0.0015]))
    fixings = _euribor_fixings(swap, rng)

    ql.Settings.instance().evaluationDate = to_ql(AS_OF)
    e6 = ql.Euribor6M(ql.YieldTermStructureHandle(ql_fwd))
    e6.clearFixings()
    for d, r in fixings.items():
        e6.addFixing(to_ql(d), r)
    ref = ql.MakeVanillaSwap(ql.Period(1, ql.Years), e6, swap.fixed_rate, ql.Period(0, ql.Days),
                             effectiveDate=to_ql(swap.start), terminationDate=to_ql(swap.maturity),
                             fixedLegTenor=ql.Period(1, ql.Years),
                             fixedLegDayCount=ql.Thirty360(ql.Thirty360.BondBasis),
                             fixedLegEndOfMonth=True, floatingLegEndOfMonth=True, nominal=swap.notional,
                             floatingLegSpread=swap.spread,
                             swapType=ql.Swap.Payer if swap.pay_fixed else ql.Swap.Receiver,
                             discountingTermStructure=ql.YieldTermStructureHandle(ql_disc))
    disc, fwd = mine.discount_curve, mine.forward_curve("EURIBOR6M")
    assert swap.npv(disc, fwd, fixings) == pytest.approx(ref.NPV(), abs=1e-6)
    assert swap.fair_rate(disc, fwd, fixings) == pytest.approx(ref.fairRate(), abs=1e-12)
    assert swap.fair_spread(disc, fwd, fixings) == pytest.approx(ref.fairSpread(), abs=1e-12)


@pytest.mark.parametrize("seed", range(6))
def test_seasoned_ois_matches_quantlib(setup, seed):
    mine, ql_disc, _ = setup
    rng = random.Random(100 + seed)
    start = TARGET.adjust(AS_OF - timedelta(days=rng.randint(5, 365 * 3)))
    maturity = max(start.replace(year=start.year + rng.choice([1, 2, 5])),
                   AS_OF + timedelta(days=rng.randint(20, 400)))
    swap = OvernightIndexedSwap(start, maturity, fixed_rate=rng.uniform(0.0, 0.04), notional=1e7,
                                pay_fixed=rng.random() < 0.5, spread=rng.choice([0.0, 0.0005]))
    fixings = _estr_fixings(start, rng)

    ql.Settings.instance().evaluationDate = to_ql(AS_OF)
    handle = ql.YieldTermStructureHandle(ql_disc)
    estr = ql.Estr(handle)
    estr.clearFixings()
    estr.addFixings([to_ql(d) for d in fixings], list(fixings.values()))
    ref = ql.MakeOIS(ql.Period(1, ql.Years), estr, swap.fixed_rate, ql.Period(0, ql.Days),
                     effectiveDate=to_ql(swap.start), terminationDate=to_ql(swap.maturity), paymentLag=1,
                     endOfMonth=True, nominal=swap.notional, overnightLegSpread=swap.spread,
                     paymentAdjustmentConvention=ql.ModifiedFollowing,
                     swapType=ql.Swap.Payer if swap.pay_fixed else ql.Swap.Receiver,
                     discountingTermStructure=handle)
    disc = mine.discount_curve
    assert swap.npv(disc, fixings=fixings) == pytest.approx(ref.NPV(), abs=1e-6)
    assert swap.fair_rate(disc, fixings=fixings) == pytest.approx(ref.fairRate(), abs=1e-12)


def test_missing_fixing_is_reported(setup):
    mine, _, _ = setup
    swap = VanillaSwap(date(2025, 3, 10), date(2030, 3, 11))
    with pytest.raises(MissingFixingError):
        swap.npv(mine.discount_curve, mine.forward_curve("EURIBOR6M"), Fixings({}, "EURIBOR6M"))
