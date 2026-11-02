"""Bonds and NSS sovereign fits vs QuantLib (oracle only).

* accrued interest, ICMA yield <-> price: ``FixedRateBond`` + ``BondFunctions``;
  QuantLib discounts yields on *payment* dates whereas the street (ICMA)
  convention uses the theoretical coupon dates, so the yield comparison uses
  unadjusted payments (identical cash-flow dates);
* dirty price from a curve: ``DiscountingBondEngine`` (Following payments);
* NSS discount function: ``FittedBondDiscountCurve`` built from given parameters;
* fit optimality: QuantLib's ``SvenssonFitting`` (same duration weights, started
  from our solution) must not find a materially lower cost.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ql = pytest.importorskip("QuantLib")

from rates_analytics.bonds import FixedRateBond, bonds_from_static  # noqa: E402
from rates_analytics.conventions import get_convention  # noqa: E402
from rates_analytics.conventions.dateutils import add_months  # noqa: E402
from rates_analytics.curves import NelsonSiegelSvenssonCurve, NSSParams, fit_sovereign_curve  # noqa: E402
from rates_analytics.curves.sovereign import _modified_duration  # noqa: E402
from tests.oracle.ql_helpers import to_ql  # noqa: E402

pytestmark = pytest.mark.oracle
AS_OF = date(2026, 10, 6)
SAMPLE = Path(__file__).resolve().parents[2] / "data" / "sample"
QL_FREQ = {1: ql.Annual, 2: ql.Semiannual}


def ql_schedule(b: FixedRateBond):
    return ql.Schedule(to_ql(b.issue_date), to_ql(b.maturity), ql.Period(12 // int(b.frequency), ql.Months),
                       ql.TARGET(), ql.Unadjusted, ql.Unadjusted, ql.DateGeneration.Backward, False,
                       to_ql(b.first_coupon_date) if b.first_coupon_date else ql.Date())


def ql_bond(b: FixedRateBond, payment=ql.Following):
    return ql.FixedRateBond(2, 100.0, ql_schedule(b), [b.coupon], ql.ActualActual(ql.ActualActual.ISMA), payment,
                            100.0, to_ql(b.issue_date))


def random_bond(rng: random.Random) -> FixedRateBond:
    conv = get_convention(rng.choice(["BUND", "OAT", "BTP"]))
    maturity = AS_OF + timedelta(days=rng.randint(200, 365 * 40))
    issue = maturity - timedelta(days=rng.randint(400, 365 * 30))
    issue = min(issue, AS_OF - timedelta(days=rng.randint(1, 300)))
    months = 12 // int(conv.frequency)
    first = maturity
    while add_months(first, -months) > issue:
        first = add_months(first, -months)
    if rng.random() < 0.3 and first < maturity:          # long first coupon
        first = add_months(first, months)
    return FixedRateBond("RND", round(rng.uniform(0, 0.06), 4), maturity, issue, conv, first)


def test_accrued_and_yield_match_quantlib(rng):
    ql.Settings.instance().evaluationDate = to_ql(AS_OF)
    for _ in range(300):
        b = random_bond(rng)
        settle = b.settlement_date(AS_OF)
        qb = ql_bond(b, ql.Unadjusted)
        assert b.accrued_interest(settle) == pytest.approx(qb.accruedAmount(to_ql(settle)), abs=1e-10)
        y = rng.uniform(-0.006, 0.08)
        clean = b.clean_price_from_yield(y, settle)
        fy = QL_FREQ[int(b.convention.yield_frequency)]
        rate = ql.InterestRate(y, ql.ActualActual(ql.ActualActual.ISMA), ql.Compounded, fy)
        assert clean == pytest.approx(ql.BondFunctions.cleanPrice(qb, rate, to_ql(settle)), abs=1e-9)
        ref_y = ql.BondFunctions.bondYield(qb, ql.BondPrice(clean, ql.BondPrice.Clean),
                                           ql.ActualActual(ql.ActualActual.ISMA), ql.Compounded, fy,
                                           to_ql(settle), 1e-14, 500)
        assert b.yield_from_clean_price(clean, settle) == pytest.approx(ref_y, abs=1e-11)


def test_dirty_price_from_nss_curve_matches_quantlib(rng):
    ql.Settings.instance().evaluationDate = to_ql(AS_OF)
    params = NSSParams(0.031, -0.0115, -0.0185, 1.8, beta3=0.006, tau2=10.0)
    curve = NelsonSiegelSvenssonCurve(AS_OF, params)
    x = ql.Array([params.beta0, params.beta1, params.beta2, params.beta3, 1 / params.tau1, 1 / params.tau2])
    ql_curve = ql.FittedBondDiscountCurve(to_ql(AS_OF), ql.SvenssonFitting(), x, ql.Date(1, 1, 2090),
                                          ql.Actual365Fixed())
    grid = [AS_OF + timedelta(days=k) for k in range(1, 365 * 60, 37)]
    assert max(abs(curve.discount(d) - ql_curve.discount(to_ql(d))) for d in grid) < 1e-12
    engine = ql.DiscountingBondEngine(ql.YieldTermStructureHandle(ql_curve))
    for _ in range(100):
        b = random_bond(rng)
        qb = ql_bond(b)
        qb.setPricingEngine(engine)
        assert b.dirty_price_from_curve(curve, b.settlement_date(AS_OF)) == pytest.approx(qb.dirtyPrice(), abs=1e-9)


@pytest.mark.parametrize("issuer", ["DE", "FR", "IT"])
def test_nss_fit_is_optimal_for_quantlib(issuer):
    static = pd.read_csv(SAMPLE / "bond_static.csv", index_col="isin")
    prices = pd.read_csv(SAMPLE / "bond_prices.csv", parse_dates=["date"])
    prices = prices[prices["date"] == pd.Timestamp(AS_OF)].set_index("isin")["mid"]
    bonds, _ = bonds_from_static(static[static["issuer"] == issuer])
    fit = fit_sovereign_curve(AS_OF, bonds, prices.to_dict(), model="nss")
    used = [b for b in bonds if b.isin in fit.bonds.index and fit.bonds.loc[b.isin, "used"]]

    ql.Settings.instance().evaluationDate = to_ql(AS_OF)
    helpers, weights, mkt = [], [], []
    for b in used:
        helpers.append(ql.FixedRateBondHelper(ql.QuoteHandle(ql.SimpleQuote(prices[b.isin])), 2, 100.0,
                                              ql_schedule(b), [b.coupon], ql.ActualActual(ql.ActualActual.ISMA),
                                              ql.Following))
        s = b.settlement_date(AS_OF)
        weights.append(1.0 / _modified_duration(b, b.yield_from_clean_price(prices[b.isin], s), s))
        mkt.append(prices[b.isin])
    weights, mkt = np.array(weights), np.array(mkt)
    p = fit.params
    guess = ql.Array([p.beta0, p.beta1, p.beta2, p.beta3, 1 / p.tau1, 1 / p.tau2])
    ours = 2 * fit.cost / 1e8          # ours: 0.5 * sum((w * err * 1e4)^2); QuantLib: sum((w * err)^2)

    # 1) QuantLib's own pricer at our parameters reproduces our objective
    at_ours = ql.FittedBondDiscountCurve(to_ql(AS_OF), ql.SvenssonFitting(), guess, ql.Date(1, 1, 2090),
                                         ql.Actual365Fixed())
    engine = ql.DiscountingBondEngine(ql.YieldTermStructureHandle(at_ours))
    model = []
    for b in used:
        qb = ql_bond(b)
        qb.setPricingEngine(engine)
        model.append(qb.cleanPrice())
    assert np.sum((weights * (np.array(model) - mkt)) ** 2) == pytest.approx(ours, rel=1e-8)

    # 2) QuantLib's optimiser started from our solution finds nothing materially better
    ql_curve = ql.FittedBondDiscountCurve(to_ql(AS_OF), helpers, ql.Actual365Fixed(),
                                          ql.SvenssonFitting(ql.Array(list(weights))), 1e-12, 5000, guess)
    ql_curve.enableExtrapolation()
    assert ql_curve.fitResults().minimumCostValue() >= ours * (1 - 1e-6)
    grid = [AS_OF + timedelta(days=k) for k in range(30, 365 * 30, 91)]
    gap = max(abs(fit.curve.zero_rate(d) - ql_curve.zeroRate(to_ql(d), ql.Actual365Fixed(), ql.Continuous).rate())
              for d in grid)
    assert gap * 1e4 < 0.5
