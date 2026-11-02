"""Randomised comparison of the conventions layer against QuantLib.

QuantLib is a *test oracle only*: the engine never imports it.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

ql = pytest.importorskip("QuantLib")

from rates_analytics.conventions import (  # noqa: E402
    ACT_360,
    ACT_365F,
    ACT_ACT_ICMA,
    ACT_ACT_ISDA,
    TARGET,
    THIRTY_360,
    THIRTY_360_US,
    THIRTY_E_360,
    BusinessDayConvention,
    DateGenerationRule,
    Period,
    Thirty360,
    Thirty360Variant,
    build_accrual_periods,
    generate_schedule,
)
from tests.conftest import random_date  # noqa: E402
from tests.oracle.ql_helpers import QL_BDC, QL_RULE, from_ql, ql_period, to_ql  # noqa: E402

pytestmark = pytest.mark.oracle
BDC = BusinessDayConvention
QL_TARGET = ql.TARGET()


# ------------------------------------------------------------------ calendar
def test_target_business_days_1999_2060():
    d, end = date(1999, 1, 1), date(2060, 12, 31)
    mismatches = []
    while d <= end:
        if TARGET.is_business_day(d) != QL_TARGET.isBusinessDay(to_ql(d)):
            mismatches.append(d)
        d += timedelta(days=1)
    assert not mismatches, mismatches[:10]


@pytest.mark.parametrize("bdc", list(BDC))
def test_adjust_matches_quantlib(bdc):
    d, end = date(2018, 1, 1), date(2032, 12, 31)
    while d <= end:
        assert TARGET.adjust(d, bdc) == from_ql(QL_TARGET.adjust(to_ql(d), QL_BDC[bdc])), (d, bdc)
        d += timedelta(days=1)


def test_advance_matches_quantlib(rng):
    units = ["D", "W", "M", "Y"]
    bdcs = [BDC.FOLLOWING, BDC.MODIFIED_FOLLOWING, BDC.PRECEDING, BDC.UNADJUSTED]
    for _ in range(4000):
        d = random_date(rng, date(2005, 1, 1), date(2050, 12, 31))
        p = Period(rng.randint(-40, 40), units[rng.randrange(4)])
        bdc = bdcs[rng.randrange(4)]
        eom = rng.random() < 0.5
        mine = TARGET.advance(d, p, bdc, eom)
        ref = from_ql(QL_TARGET.advance(to_ql(d), ql_period(p), QL_BDC[bdc], eom))
        assert mine == ref, (d, p, bdc, eom)


def test_business_days_between_matches_quantlib(rng):
    for _ in range(500):
        d1 = random_date(rng, date(2020, 1, 1), date(2030, 12, 31))
        d2 = d1 + timedelta(days=rng.randint(-60, 60))
        f, l_ = rng.random() < 0.5, rng.random() < 0.5
        assert TARGET.business_days_between(d1, d2, f, l_) == QL_TARGET.businessDaysBetween(
            to_ql(d1), to_ql(d2), f, l_), (d1, d2, f, l_)


# ----------------------------------------------------------------- day counts
_SIMPLE_DC = [
    (ACT_360, ql.Actual360()),
    (ACT_365F, ql.Actual365Fixed()),
    (THIRTY_360, ql.Thirty360(ql.Thirty360.BondBasis)),
    (THIRTY_E_360, ql.Thirty360(ql.Thirty360.European)),
    (THIRTY_360_US, ql.Thirty360(ql.Thirty360.USA)),
    (ACT_ACT_ISDA, ql.ActualActual(ql.ActualActual.ISDA)),
]


@pytest.mark.parametrize("mine,ref", _SIMPLE_DC, ids=lambda x: getattr(x, "name", ""))
def test_day_counts_match_quantlib(mine, ref, rng):
    for _ in range(5000):
        d1 = random_date(rng)
        d2 = d1 + timedelta(days=rng.randint(-800, 4000))
        assert mine.day_count(d1, d2) == ref.dayCount(to_ql(d1), to_ql(d2)), (d1, d2)
        assert mine.year_fraction(d1, d2) == pytest.approx(
            ref.yearFraction(to_ql(d1), to_ql(d2)), abs=1e-14), (d1, d2)


def test_thirty_e_360_isda_matches_quantlib(rng):
    for _ in range(3000):
        d1 = random_date(rng)
        d2 = d1 + timedelta(days=rng.randint(1, 4000))
        term = d2 if rng.random() < 0.5 else d2 + timedelta(days=rng.randint(1, 400))
        mine = Thirty360(Thirty360Variant.ISDA, term)
        ref = ql.Thirty360(ql.Thirty360.ISDA, to_ql(term))
        assert mine.day_count(d1, d2) == ref.dayCount(to_ql(d1), to_ql(d2)), (d1, d2, term)


def test_act_act_icma_with_reference_periods(rng):
    ref = ql.ActualActual(ql.ActualActual.ISMA)
    for _ in range(3000):
        months = rng.choice([3, 6, 12])
        rs = random_date(rng, date(2001, 1, 1), date(2055, 1, 1))
        re_ = from_ql(to_ql(rs) + ql.Period(months, ql.Months))
        case = rng.randrange(3)
        if case == 0:      # regular sub-period
            d1 = rs + timedelta(days=rng.randint(0, (re_ - rs).days - 1))
            d2 = d1 + timedelta(days=rng.randint(1, (re_ - d1).days))
        elif case == 1:    # long first coupon
            d1 = rs - timedelta(days=rng.randint(1, 500))
            d2 = re_ - timedelta(days=rng.randint(0, (re_ - rs).days - 1))
        else:              # long last coupon
            d1 = rs + timedelta(days=rng.randint(0, (re_ - rs).days - 1))
            d2 = re_ + timedelta(days=rng.randint(1, 700))
        mine = ACT_ACT_ICMA.year_fraction(d1, d2, rs, re_)
        exp = ref.yearFraction(to_ql(d1), to_ql(d2), to_ql(rs), to_ql(re_))
        assert mine == pytest.approx(exp, abs=1e-13), (d1, d2, rs, re_)


# ------------------------------------------------------------------ schedules
def _ql_schedule(eff, term, tenor, bdc, term_bdc, rule, eom, first=None, ntl=None):
    return ql.Schedule(to_ql(eff), to_ql(term), ql_period(tenor), QL_TARGET, QL_BDC[bdc],
                       QL_BDC[term_bdc], QL_RULE[rule], eom,
                       to_ql(first) if first else ql.Date(), to_ql(ntl) if ntl else ql.Date())


def test_schedule_matches_quantlib(rng):
    tenors = [Period.parse(t) for t in ("1M", "3M", "6M", "1Y")]
    bdcs = [BDC.MODIFIED_FOLLOWING, BDC.FOLLOWING, BDC.UNADJUSTED, BDC.PRECEDING]
    rules = [DateGenerationRule.BACKWARD, DateGenerationRule.FORWARD]
    checked = 0
    for _ in range(3000):
        eff = random_date(rng, date(2005, 1, 1), date(2040, 12, 31))
        if rng.random() < 0.3:  # force month-end starts to exercise the EOM rule
            eff = TARGET.end_of_month(eff)
        term = eff + timedelta(days=rng.randint(20, 365 * 31))
        if rng.random() < 0.3:
            term = TARGET.end_of_month(term)
        tenor = rng.choice(tenors)
        bdc, term_bdc = rng.choice(bdcs), rng.choice(bdcs)
        rule, eom = rng.choice(rules), rng.random() < 0.5
        mine = generate_schedule(eff, term, tenor, TARGET, bdc, term_bdc, rule, eom)
        ref = _ql_schedule(eff, term, tenor, bdc, term_bdc, rule, eom)
        ref_dates = [from_ql(x) for x in ref]
        assert list(mine.adjusted) == ref_dates, (eff, term, tenor, bdc, term_bdc, rule, eom)
        assert list(mine.is_regular) == [ref.isRegular(i + 1) for i in range(len(ref_dates) - 1)]
        checked += 1
    assert checked == 3000


def test_schedule_with_explicit_stub_dates_matches_quantlib(rng):
    for _ in range(1500):
        eff = random_date(rng, date(2005, 1, 1), date(2035, 12, 31))
        term = eff + timedelta(days=rng.randint(800, 365 * 20))
        tenor = Period.parse(rng.choice(["6M", "1Y"]))
        rule = rng.choice([DateGenerationRule.BACKWARD, DateGenerationRule.FORWARD])
        first = eff + timedelta(days=rng.randint(30, 300)) if rng.random() < 0.6 else None
        ntl = term - timedelta(days=rng.randint(30, 300)) if rng.random() < 0.6 else None
        mine = generate_schedule(eff, term, tenor, TARGET, BDC.UNADJUSTED, BDC.UNADJUSTED, rule,
                                 False, first, ntl)
        ref = _ql_schedule(eff, term, tenor, BDC.UNADJUSTED, BDC.UNADJUSTED, rule, False, first, ntl)
        assert list(mine.adjusted) == [from_ql(x) for x in ref], (eff, term, tenor, rule, first, ntl)


def test_bond_coupon_accruals_match_quantlib_fixed_rate_bond(rng):
    """Irregular first coupons + ACT/ACT ICMA, as in a newly issued Bund/BTP."""
    for _ in range(600):
        months = rng.choice([6, 12])
        tenor = Period(months, "M")
        maturity = random_date(rng, date(2028, 1, 1), date(2056, 12, 31))
        issue = maturity - timedelta(days=rng.randint(400, 365 * 30))
        sched = generate_schedule(issue, maturity, tenor, TARGET, BDC.UNADJUSTED, BDC.UNADJUSTED,
                                  DateGenerationRule.BACKWARD, False)
        periods = build_accrual_periods(sched, ACT_ACT_ICMA, accrue_on_adjusted=False,
                                        payment_bdc=BDC.FOLLOWING)
        ql_sched = _ql_schedule(issue, maturity, tenor, BDC.UNADJUSTED, BDC.UNADJUSTED,
                                DateGenerationRule.BACKWARD, False)
        bond = ql.FixedRateBond(2, 100.0, ql_sched, [0.025], ql.ActualActual(ql.ActualActual.ISMA),
                                ql.Following)
        coupons = [ql.as_fixed_rate_coupon(c) for c in bond.cashflows()
                   if ql.as_fixed_rate_coupon(c) is not None]
        assert len(coupons) == len(periods)
        for p, c in zip(periods, coupons):
            assert p.payment_date == from_ql(c.date())
            assert p.ref_start == from_ql(c.referencePeriodStart())
            assert p.ref_end == from_ql(c.referencePeriodEnd())
            assert p.year_fraction == pytest.approx(c.accrualPeriod(), abs=1e-13)
