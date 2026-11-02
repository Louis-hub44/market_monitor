"""Unit tests for phase 3: vanilla swap, FRA / swap helpers, EURIBOR 6M dual curve, CurveSet."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from rates_analytics.conventions import ACT_365F, THIRTY_360
from rates_analytics.conventions.market import EUR_ESTR_OIS, EUR_EURIBOR_3M_SWAP, EURIBOR_6M
from rates_analytics.curves import (
    CurveError,
    CurveSet,
    FRAHelper,
    InterpolatedCurve,
    SwapHelper,
    bootstrap,
    build_eur_curves,
    build_ibor_curve,
    eur_curves_from_market,
)
from rates_analytics.curves.helpers import DepositHelper, deposit_convention_for
from rates_analytics.data import MarketDataService, StaticProvider
from rates_analytics.instruments import InstrumentError, VanillaSwap

AS_OF = date(2026, 10, 6)
SAMPLE_DIR = Path(__file__).resolve().parents[2] / "data" / "sample"


@pytest.fixture(scope="module")
def quotes() -> dict[str, float]:
    return pd.read_csv(SAMPLE_DIR / "history.csv", index_col=0).loc[AS_OF.isoformat()].to_dict()


@pytest.fixture(scope="module")
def curves(quotes) -> CurveSet:
    return build_eur_curves(AS_OF, quotes)


def flat_curve(rate: float) -> InterpolatedCurve:
    end = date(2090, 1, 1)
    return InterpolatedCurve(AS_OF, [end], [np.exp(-rate * ACT_365F.year_fraction(AS_OF, end))])


# ----------------------------------------------------------------- swap
def test_vanilla_swap_structure():
    s = VanillaSwap.from_tenor(AS_OF, "10Y", 0.027)
    assert s.start == date(2026, 10, 8) and s.maturity == date(2036, 10, 8)
    assert len(s.fixed_leg) == 10 and len(s.float_leg) == 20
    p0 = s.float_leg.periods[0]
    assert p0.fixing_date == AS_OF and p0.end == date(2027, 4, 8)
    fx0 = s.fixed_leg.periods[0]
    assert fx0.year_fraction == pytest.approx(THIRTY_360.year_fraction(fx0.start, fx0.end))


def test_vanilla_swap_valuation(curves):
    disc, fwd = curves.discount_curve, curves.forward_curve("EURIBOR6M")
    s = VanillaSwap.from_tenor(AS_OF, "7Y")
    k = s.fair_rate(disc, fwd)
    par = VanillaSwap(s.start, s.maturity, k, notional=1e8)
    assert par.npv(disc, fwd) == pytest.approx(0.0, abs=1e-5)
    off = VanillaSwap(s.start, s.maturity, k + 0.0010, notional=1e8)
    assert off.npv(disc, fwd) == pytest.approx(-0.0010 * off.annuity(disc), rel=1e-12)
    assert VanillaSwap(s.start, s.maturity, k + 0.001, pay_fixed=False).npv(disc, fwd) > 0
    spread = off.fair_spread(disc, fwd)
    shifted = VanillaSwap(s.start, s.maturity, k + 0.0010, notional=1e8, spread=spread)
    assert shifted.npv(disc, fwd) == pytest.approx(0.0, abs=1e-5)


def test_indexed_vs_par_coupons_are_close(curves):
    disc, fwd = curves.discount_curve, curves.forward_curve("EURIBOR6M")
    par = VanillaSwap.from_tenor(AS_OF, "10Y").fair_rate(disc, fwd)
    idx = VanillaSwap.from_tenor(AS_OF, "10Y", indexed_coupons=True).fair_rate(disc, fwd)
    assert abs(par - idx) * 1e4 < 0.05


def test_vanilla_swap_validation(curves):
    with pytest.raises(InstrumentError):
        VanillaSwap(date(2030, 1, 1), date(2029, 1, 1))
    with pytest.raises(InstrumentError):
        VanillaSwap(date(2027, 1, 4), date(2030, 1, 4), convention=EUR_ESTR_OIS)
    seasoned = VanillaSwap(date(2026, 3, 2), date(2031, 3, 3))
    with pytest.raises(InstrumentError):
        seasoned.fair_rate(curves.discount_curve)


# --------------------------------------------------------------- helpers
def test_fra_helper_dates():
    h = FRAHelper(AS_OF, 6, 0.0217)
    assert h.label == "FRA 6X12"
    assert (h.start, h.end, h.fixing_date) == (date(2027, 4, 8), date(2027, 10, 8), date(2027, 4, 6))
    with pytest.raises(CurveError):
        FRAHelper(AS_OF, 0, 0.02)


def test_euribor_deposit_helper_is_the_fixing():
    h = DepositHelper(AS_OF, "6M", 0.0214, deposit_convention_for(EURIBOR_6M))
    assert (h.start, h.end) == (date(2026, 10, 8), date(2027, 4, 8))


# ------------------------------------------------------------ bootstrap
@pytest.mark.parametrize("method", ["log_linear", "monotone_convex"])
def test_dual_curve_reprices_all_instruments(quotes, method):
    cs = build_eur_curves(AS_OF, quotes, method)
    res = cs.forwards["EURIBOR6M"]
    assert len(res.helpers) == 25 and not res.skipped
    assert res.max_abs_error_bp() < 1e-8
    assert cs.discount.max_abs_error_bp() < 1e-8


def test_dual_curve_recovers_known_flat_curves():
    disc, true6 = flat_curve(0.020), flat_curve(0.022)
    helpers = [FRAHelper(AS_OF, m, FRAHelper(AS_OF, m, 0).implied_quote(true6)) for m in (1, 3, 6, 12)]
    for t in ("2Y", "5Y", "10Y", "30Y"):
        rate = VanillaSwap.from_tenor(AS_OF, t).fair_rate(disc, true6)
        helpers.append(SwapHelper(AS_OF, t, rate, discount_curve=disc))
    res = bootstrap(AS_OF, helpers)
    grid = [AS_OF + timedelta(days=k) for k in range(1, 365 * 30, 13)]
    assert max(abs(res.curve.discount(d) - true6.discount(d)) for d in grid) < 1e-12


def test_single_curve_differs_from_dual_curve(quotes, curves):
    single = build_eur_curves(AS_OF, quotes, dual_curve=False)
    dual_fwd, single_fwd = curves.forward_curve("EURIBOR6M"), single.forward_curve("EURIBOR6M")
    swap = VanillaSwap.from_tenor(AS_OF, "30Y")
    # each curve reprices the market in its own framework...
    assert swap.fair_rate(single_fwd) == pytest.approx(quotes["EUR.EURIBOR6M.SWAP.30Y"], abs=1e-12)
    assert swap.fair_rate(curves.discount_curve, dual_fwd) == pytest.approx(quotes["EUR.EURIBOR6M.SWAP.30Y"],
                                                                            abs=1e-12)
    # ...but the projected EURIBOR forwards are not the same (discounting matters for long swaps)
    d = date(2050, 10, 6)
    gap_bp = abs(dual_fwd.forward_rate(d, d + timedelta(days=182)) - single_fwd.forward_rate(d, d + timedelta(days=182))) * 1e4
    assert 0.001 < gap_bp < 5


def test_ibor_builder_errors(quotes, curves):
    with pytest.raises(CurveError):
        build_ibor_curve(AS_OF + timedelta(days=1), quotes, curves.discount_curve)
    with pytest.raises(CurveError):
        build_ibor_curve(AS_OF, quotes, curves.discount_curve, swap_convention=EUR_EURIBOR_3M_SWAP)
    with pytest.raises(CurveError):
        build_ibor_curve(AS_OF, {"EUR.EURIBOR6M.FIX": 0.021}, curves.discount_curve)


def test_ibor_builder_skips_missing_quotes(quotes, curves):
    partial = {k: v for k, v in quotes.items() if not k.endswith(("FRA.4X10", "SWAP.12Y"))}
    res = build_ibor_curve(AS_OF, partial, curves.discount_curve)
    assert set(res.skipped) == {"EUR.EURIBOR6M.FRA.4X10", "EUR.EURIBOR6M.SWAP.12Y"}


# -------------------------------------------------------------- curve set
def test_curve_set_access(curves):
    assert curves.forward_curve("ESTR") is curves.discount_curve
    assert curves.forward_curve(EURIBOR_6M) is curves.forwards["EURIBOR6M"].curve
    with pytest.raises(CurveError):
        curves.forward_curve("EURIBOR3M")


def test_basis_spreads(curves):
    b = curves.basis_spreads()
    assert list(b["tenor"]) == ["2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y", "50Y"]
    assert (b["basis_bp"] > 0).all() and b["basis_bp"].is_monotonic_decreasing
    assert (b["irs_par"] > b["ois_par"]).all()


def test_basis_is_zero_when_projection_equals_discounting(curves):
    same = CurveSet(AS_OF, curves.discount, {"EURIBOR6M": curves.discount})
    assert same.basis_spreads()["basis_bp"].abs().max() < 0.05   # only payment-lag effects remain


def test_forward_comparison(curves):
    fc = curves.forward_comparison(horizon_years=5)
    assert len(fc) == 10 and (fc["spread_bp"] > 0).all()


def test_eur_curves_from_market_service():
    svc = MarketDataService([StaticProvider.from_directory(SAMPLE_DIR)], today=lambda: AS_OF)
    cs = eur_curves_from_market(svc, AS_OF)
    assert svc.last_report.ok and len(cs.forwards["EURIBOR6M"].helpers) == 25
