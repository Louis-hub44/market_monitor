"""Unit tests for phase 7: fixings, seasoned swaps, SWPM valuation and bucketed risk."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from rates_analytics.conventions import ACT_360, TARGET
from rates_analytics.curves import CurveError, build_eur_curves
from rates_analytics.data import MarketDataService, StaticProvider
from rates_analytics.instruments import (
    Fixings,
    MissingFixingError,
    OvernightIndexedSwap,
    VanillaSwap,
    fixings_from_market,
)
from rates_analytics.swaps import bucketed_risk, parallel_dv01, swap_npv, swpm, value_swap

AS_OF = date(2026, 10, 6)
SAMPLE = Path(__file__).resolve().parents[2] / "data" / "sample"


@pytest.fixture(scope="module")
def quotes() -> dict[str, float]:
    return pd.read_csv(SAMPLE / "history.csv", index_col=0).loc[AS_OF.isoformat()].dropna().to_dict()


@pytest.fixture(scope="module")
def curves(quotes):
    return build_eur_curves(AS_OF, quotes)


@pytest.fixture(scope="module")
def market_fixings():
    svc = MarketDataService([StaticProvider.from_directory(SAMPLE)], today=lambda: AS_OF)
    return (fixings_from_market(svc, "EUR.EURIBOR6M.FIX", date(2024, 1, 1), AS_OF),
            fixings_from_market(svc, "EUR.ESTR.FIX", date(2024, 1, 1), AS_OF))


# ----------------------------------------------------------------- fixings
def test_fixings_container(market_fixings):
    e6, estr = market_fixings
    assert len(e6) > 600 and date(2026, 10, 6) in estr
    f = Fixings(pd.Series([0.02, np.nan], index=pd.to_datetime(["2026-01-05", "2026-01-06"])), "X")
    assert f.require(date(2026, 1, 5)) == 0.02 and date(2026, 1, 6) not in f
    with pytest.raises(MissingFixingError):
        f.require(date(2026, 1, 6))


# ---------------------------------------------------------- seasoned swaps
def test_seasoned_irs_uses_fixings_and_drops_paid_periods(curves, market_fixings):
    e6, _ = market_fixings
    swap = VanillaSwap(date(2025, 3, 12), date(2032, 3, 12), 0.0245, notional=1e7)
    fwd = curves.forward_curve("EURIBOR6M")
    rates, known = swap.float_rates(fwd, e6)
    paid = [p.payment_date <= AS_OF for p in swap.float_leg.periods]
    assert np.isnan(rates[paid]).all()
    current = next(i for i, p in enumerate(swap.float_leg.periods) if p.start <= AS_OF < p.end)
    assert known[current] and rates[current] == e6[swap.float_leg.periods[current].fixing_date]
    assert not known[current + 1]


def test_fixing_on_curve_date_is_used_if_published(curves):
    swap = VanillaSwap.from_tenor(AS_OF, "2Y")                 # first fixing date = AS_OF
    fwd = curves.forward_curve("EURIBOR6M")
    projected, k0 = swap.float_rates(fwd, {})
    fixed, k1 = swap.float_rates(fwd, {AS_OF: 0.03})
    assert not k0[0] and k1[0] and fixed[0] == 0.03 and projected[0] != 0.03


def test_seasoned_ois_compounding_by_hand(curves):
    start = TARGET.advance(AS_OF, -3)                          # three past business days
    swap = OvernightIndexedSwap(start, start.replace(year=start.year + 1), 0.02)
    fixings = {start: 0.019, TARGET.advance(start, 1): 0.0191, TARGET.advance(start, 2): 0.0192}
    growth, to_date = swap.compounded_growth(curves.discount_curve, fixings)
    factor, d = 1.0, start
    for r in fixings.values():
        nxt = TARGET.advance(d, 1)
        factor *= 1 + r * ACT_360.year_fraction(d, nxt)
        d = nxt
    assert d == AS_OF and to_date[0] == pytest.approx(factor - 1)
    end = swap.float_leg.periods[0].end
    expected = factor * curves.discount_curve.discount(AS_OF) / curves.discount_curve.discount(end) - 1
    assert growth[0] == pytest.approx(expected)
    with pytest.raises(MissingFixingError):
        swap.compounded_growth(curves.discount_curve, {})


# ----------------------------------------------------------------- valuation
def test_spot_par_swap_valuation(curves):
    k = VanillaSwap.from_tenor(AS_OF, "7Y").fair_rate(curves.discount_curve, curves.forward_curve("EURIBOR6M"))
    v = value_swap(VanillaSwap.from_tenor(AS_OF, "7Y", k, notional=1e8), curves)
    assert v.npv == pytest.approx(0.0, abs=1e-4)
    assert v.accrued == 0.0 and v.clean_npv == v.npv
    assert v.par_rate == pytest.approx(k) and v.par_spread == pytest.approx(0.0, abs=1e-12)
    assert v.fixed_leg.pv + v.float_leg.pv == pytest.approx(v.npv, abs=1e-6)
    assert v.pv01 == pytest.approx(VanillaSwap.from_tenor(AS_OF, "7Y", notional=1e8).annuity(curves.discount_curve) * 1e-4)


def test_seasoned_swap_accrued_and_clean(curves, market_fixings):
    e6, estr = market_fixings
    irs = VanillaSwap(date(2025, 3, 12), date(2032, 3, 12), 0.0245, notional=25e6, pay_fixed=False)
    v = value_swap(irs, curves, e6)
    fixed_days = 30 * 6 + 24                                    # 12-Mar -> 06-Oct, 30/360
    fixed_acc = 25e6 * 0.0245 * fixed_days / 360
    cur = next(p for p in irs.float_leg.periods if p.start <= AS_OF < p.end)
    float_acc = 25e6 * e6[cur.fixing_date] * (AS_OF - cur.start).days / 360
    assert v.accrued == pytest.approx(fixed_acc - float_acc)
    assert v.clean_npv == pytest.approx(v.npv - v.accrued)
    assert set(v.float_leg.cashflows["status"]) == {"fixed", "projected"}
    ois = OvernightIndexedSwap(date(2025, 11, 5), date(2027, 11, 5), 0.0195, notional=5e7)
    vo = value_swap(ois, curves, estr)
    assert vo.float_leg.cashflows["status"].iloc[0] == "partially fixed"
    assert vo.fixed_leg.pv + vo.float_leg.pv == pytest.approx(vo.npv, abs=1e-6)


# --------------------------------------------------------------------- risk
def test_bucketed_risk_concentrates_on_the_matching_pillar(curves):
    swap = VanillaSwap.from_tenor(AS_OF, "10Y", 0.027, notional=1e7)
    v = swpm(swap, curves)
    t = v.bucketed.table.set_index("market_id")["dv01"]
    assert t["EUR.EURIBOR6M.SWAP.10Y"] == pytest.approx(v.dv01, rel=0.01)
    assert t.drop("EUR.EURIBOR6M.SWAP.10Y").abs().max() < 0.01 * abs(v.dv01)
    ois = OvernightIndexedSwap.from_tenor(AS_OF, "5Y", 0.0228, notional=1e7)
    r = swpm(ois, curves).bucketed
    assert r.by_curve()["EURIBOR6M"] == pytest.approx(0.0, abs=1e-6)
    assert r.table.set_index("market_id")["dv01"].abs().idxmax() == "EUR.ESTR.OIS.5Y"


@pytest.mark.parametrize("interp", ["log_linear", "monotone_convex"])
def test_jacobian_matches_rebuild_and_parallel(quotes, interp):
    cs = build_eur_curves(AS_OF, quotes, interp)
    swap = VanillaSwap(date(2025, 3, 12), date(2032, 3, 12), 0.0245, notional=1e7)
    fixings = {p.fixing_date: 0.021 for p in swap.float_leg.periods if p.fixing_date < AS_OF}

    def pricer(c):
        return swap_npv(swap, c, fixings)

    jac = bucketed_risk(cs, pricer).table.set_index("market_id")["dv01"]
    ids = ["EUR.EURIBOR6M.SWAP.5Y", "EUR.EURIBOR6M.SWAP.6Y", "EUR.EURIBOR6M.FIX", "EUR.ESTR.OIS.5Y"]
    # monotone convex is only piecewise smooth: differentiate the rebuild with a small quote bump
    h = 1e-4 if interp == "log_linear" else 1e-6
    reb = bucketed_risk(cs, pricer, method="rebuild", market_ids=ids, quote_bump=h)
    reb = reb.table.set_index("market_id")["dv01"]
    for mid in ids:
        assert jac[mid] == pytest.approx(reb[mid], abs=1e-3 * jac.abs().max())
    central = (parallel_dv01(cs, pricer) - parallel_dv01(cs, pricer, -1e-4)) / 2
    assert jac.sum() == pytest.approx(central, rel=1e-4)


def test_risk_errors(curves):
    with pytest.raises(CurveError):
        bucketed_risk(curves, lambda c: 0.0, method="magic")
    with pytest.raises(CurveError):
        bucketed_risk(curves, lambda c: 0.0, bump=0.0)
