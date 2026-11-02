"""YAS analytics vs QuantLib ``BondFunctions`` (oracle only).

* street convention  <-> QuantLib with unadjusted payment dates;
* true yield         <-> QuantLib with Following payment dates (QuantLib's native convention);
* Macaulay / modified duration, convexity, BPV.

QuantLib's ``basisPointValue`` adds a second-order term
(:math:`\\Delta = -D^{mod}P\\,10^{-4} + \\tfrac12 \\tfrac{C}{100} P\\,10^{-8}`); the first-order
DV01 is recovered from it before comparison.
"""

from __future__ import annotations

from datetime import date

import pytest

ql = pytest.importorskip("QuantLib")

from rates_analytics.bonds.analytics import (  # noqa: E402
    YieldConvention,
    dirty_price,
    yield_risk,
    yield_to_maturity,
)
from tests.oracle.ql_helpers import to_ql  # noqa: E402
from tests.oracle.test_sovereign_vs_quantlib import AS_OF, QL_FREQ, ql_bond, random_bond  # noqa: E402

pytestmark = pytest.mark.oracle
ISMA = ql.ActualActual(ql.ActualActual.ISMA)


@pytest.mark.parametrize("convention,payment", [(YieldConvention.STREET, ql.Unadjusted),
                                                (YieldConvention.TRUE, ql.Following)])
def test_yield_and_risk_match_quantlib(rng, convention, payment):
    ql.Settings.instance().evaluationDate = to_ql(AS_OF)
    skipped = []
    for _ in range(250):
        b = random_bond(rng)
        settle = b.settlement_date(AS_OF)
        qb = ql_bond(b, payment)
        freq = QL_FREQ[int(b.convention.yield_frequency)]
        y = rng.uniform(-0.005, 0.08)
        clean = dirty_price(b, y, settle, convention) - b.accrued_interest(settle)
        try:
            ref_y = ql.BondFunctions.bondYield(qb, ql.BondPrice(clean, ql.BondPrice.Clean), ISMA, ql.Compounded,
                                               freq, to_ql(settle), 1e-15, 500)
        except RuntimeError:
            # QuantLib's ISMA day counter rejects a long first coupon whose payment date is pushed past
            # its period end (we split at the period end instead); nothing to compare against.
            assert convention is YieldConvention.TRUE and b.first_coupon_date is not None
            skipped.append(b)
            continue
        assert yield_to_maturity(b, clean, settle, convention) == pytest.approx(ref_y, abs=1e-12)
        rate = ql.InterestRate(y, ISMA, ql.Compounded, freq)
        r = yield_risk(b, y, settle, convention)
        d = to_ql(settle)
        assert r["macaulay"] == pytest.approx(ql.BondFunctions.duration(qb, rate, ql.Duration.Macaulay, d), rel=1e-11)
        assert r["modified"] == pytest.approx(ql.BondFunctions.duration(qb, rate, ql.Duration.Modified, d), rel=1e-11)
        conv = ql.BondFunctions.convexity(qb, rate, d)
        assert r["convexity"] == pytest.approx(conv, rel=1e-10)
        bpv = ql.BondFunctions.basisPointValue(qb, rate, d)
        first_order = -(bpv - 0.5 * conv / 100.0 * r["dirty"] * 1e-8)
        assert r["dv01"] == pytest.approx(first_order, rel=1e-9)
    assert len(skipped) < 25          # ~6 % of the random bonds (long first coupon + shifted payment)


def test_true_and_street_yields_differ_only_by_payment_shifts():
    from rates_analytics.bonds import FixedRateBond
    from rates_analytics.conventions import get_convention

    # coupon on 15-Feb: 2031 (Sat) and 2032 (Sun) are paid on Monday
    b = FixedRateBond("B", 0.025, date(2035, 2, 15), date(2025, 1, 10), get_convention("BUND"),
                      first_coupon_date=date(2026, 2, 15))
    s = b.settlement_date(AS_OF)
    street = yield_to_maturity(b, 98.5, s, YieldConvention.STREET)
    true_y = yield_to_maturity(b, 98.5, s, YieldConvention.TRUE)
    assert 0 < (street - true_y) * 1e4 < 0.2
