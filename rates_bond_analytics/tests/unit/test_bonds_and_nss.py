"""Unit tests for phase 4: bonds, NS/NSS curves, sovereign fits."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from rates_analytics.bonds import FixedRateBond, bond_from_static, bonds_from_static, convention_for_issuer
from rates_analytics.conventions import get_convention
from rates_analytics.curves import (
    CurveError,
    InterpolatedCurve,
    NelsonSiegelSvenssonCurve,
    NSSParams,
    ParametricModel,
    fit_sovereign_curve,
    sovereign_curve_from_market,
    zero_spreads,
)
from rates_analytics.data import MarketDataService, StaticProvider
from rates_analytics.exceptions import ConventionError
from rates_analytics.instruments import InstrumentError

AS_OF = date(2026, 10, 6)
SAMPLE = Path(__file__).resolve().parents[2] / "data" / "sample"
BUND, BTP = get_convention("BUND"), get_convention("BTP")
TRUE = NSSParams(0.031, -0.0115, -0.0185, 1.8, beta3=0.006, tau2=10.0)


# ------------------------------------------------------------------- bonds
def test_bund_accrued_and_cashflows():
    b = FixedRateBond("B1", 0.025, date(2035, 2, 15), date(2005, 2, 15), BUND)
    settle = b.settlement_date(date(2026, 10, 7))
    assert settle == date(2026, 10, 9)
    assert b.accrued_interest(settle) == pytest.approx(100 * 0.025 * 236 / 365)
    cfs = b.cashflows(settle)
    assert [cf.amount for cf in cfs[:3]] == pytest.approx([2.5, 2.5, 2.5])
    assert cfs[-1].kind == "redemption" and cfs[-1].amount == 100.0
    # 15-Feb-2031 is a Saturday: coupon accrues to the 15th, paid on Monday 17th
    feb31 = next(cf for cf in cfs if cf.accrual and cf.accrual.end == date(2031, 2, 15))
    assert feb31.payment_date == date(2031, 2, 17)


def test_btp_semiannual_and_coupon_date_settlement():
    b = FixedRateBond("I1", 0.025, date(2030, 4, 1), date(2025, 4, 1), BTP)
    assert b.periods[0].year_fraction == pytest.approx(0.5)
    assert b.coupon_amount(b.periods[0]) == pytest.approx(1.25)
    assert b.accrued_interest(date(2026, 10, 1)) == 0.0          # settlement on the coupon date
    assert all(cf.accrual.end > date(2026, 10, 1) for cf in b.cashflows(date(2026, 10, 1))[:-1])


def test_irregular_first_coupons():
    short = FixedRateBond("S", 0.03, date(2036, 2, 15), date(2025, 11, 10), BUND, first_coupon_date=date(2026, 2, 15))
    long_ = FixedRateBond("L", 0.03, date(2036, 2, 15), date(2025, 1, 10), BUND, first_coupon_date=date(2026, 2, 15))
    assert short.coupon_amount(short.periods[0]) == pytest.approx(3.0 * 97 / 365)
    assert long_.coupon_amount(long_.periods[0]) > 3.0
    assert not short.periods[0].is_regular and short.periods[1].is_regular


def test_yield_price_roundtrip_and_zero_coupon():
    b = FixedRateBond("B", 0.021, date(2034, 8, 15), date(2024, 8, 15), BUND)
    s = b.settlement_date(AS_OF)
    for y in (-0.004, 0.0, 0.027, 0.09):
        assert b.yield_from_clean_price(b.clean_price_from_yield(y, s), s) == pytest.approx(y, abs=1e-12)
    zc = FixedRateBond("Z", 0.0, date(2031, 8, 15), date(2021, 8, 15), BUND)
    s = zc.settlement_date(AS_OF)
    w = (date(2027, 8, 15) - s).days / 365 + 4
    assert zc.dirty_price_from_yield(0.025, s) == pytest.approx(100 / 1.025 ** w)


def test_bond_validation():
    with pytest.raises(InstrumentError):
        FixedRateBond("X", 0.5, date(2030, 1, 1), date(2020, 1, 1), BUND)
    with pytest.raises(InstrumentError):
        FixedRateBond("X", 0.02, date(2020, 1, 1), date(2030, 1, 1), BUND)
    with pytest.raises(InstrumentError):
        FixedRateBond("X", 0.02, date(2030, 1, 1), date(2020, 1, 1), BUND, first_coupon_date=date(2031, 1, 1))
    b = FixedRateBond("X", 0.02, date(2027, 1, 15), date(2020, 1, 15), BUND)
    with pytest.raises(InstrumentError):
        b.accrued_interest(date(2027, 1, 15))
    with pytest.raises(InstrumentError):
        b.yield_from_clean_price(-5.0, date(2026, 10, 8))


def test_bonds_from_static():
    row = {"name": "DBR", "issuer": "DE", "coupon": 0.025, "frequency": 1, "day_count": "ACT/ACT",
           "issue_date": "2025-01-10", "accrual_start": None, "first_coupon_date": "2026-02-15",
           "maturity": "2035-02-15"}
    b = bond_from_static("ID1", row)
    assert b.convention.name == "BUND" and b.first_coupon_date == date(2026, 2, 15)
    table = pd.DataFrame([row, {**row, "maturity": None}], index=["OK", "BAD"])
    bonds, rejected = bonds_from_static(table)
    assert [x.isin for x in bonds] == ["OK"] and "BAD" in rejected
    assert convention_for_issuer("it").name == "BTP"
    with pytest.raises(ConventionError):
        convention_for_issuer("JP")


def test_premium_bond_on_low_flat_curve():
    r = 0.03
    flat = InterpolatedCurve(AS_OF, [date(2080, 1, 1)], [np.exp(-r * (date(2080, 1, 1) - AS_OF).days / 365)])
    b = FixedRateBond("B", 0.04, date(2036, 10, 6), date(2026, 1, 6), get_convention("OAT"))
    s = b.settlement_date(AS_OF)
    assert b.dirty_price_from_curve(flat, s) > b.redemption


# --------------------------------------------------------------------- NSS
def test_nss_formulas():
    c = NelsonSiegelSvenssonCurve(AS_OF, TRUE)
    assert c.zero_rate_cc(0.0) == pytest.approx(TRUE.beta0 + TRUE.beta1)
    assert c.zero_rate_cc(1e6) == pytest.approx(TRUE.beta0, abs=1e-6)
    # z(t) t = integral of the instantaneous forward
    grid = np.linspace(0, 20, 200001)
    assert np.trapezoid(c.instantaneous_forward(grid), grid) == pytest.approx(c.zero_rate_cc(20.0) * 20.0, abs=1e-9)
    t = np.linspace(0.01, 40, 400)
    ns = NSSParams(0.03, -0.01, 0.005, 2.0)
    assert ns.model is ParametricModel.NS
    assert np.allclose(ns.zero_rate(t), NSSParams(0.03, -0.01, 0.005, 2.0, beta3=0.0, tau2=5.0).zero_rate(t))


def test_nss_gradient_matches_finite_differences():
    t = np.array([0.0, 0.5, 2.0, 10.0, 35.0])
    g = TRUE.zero_rate_gradient(t)
    x = TRUE.to_array()
    for j in range(6):
        h = 1e-7 * max(1.0, abs(x[j]))
        up, dn = x.copy(), x.copy()
        up[j] += h
        dn[j] -= h
        num = (NSSParams.from_array("nss", up).zero_rate(t) - NSSParams.from_array("nss", dn).zero_rate(t)) / (2 * h)
        assert np.allclose(g[:, j], num, atol=1e-8)


@pytest.mark.parametrize("bad", [
    dict(beta0=0.03, beta1=0, beta2=0, tau1=-1.0), dict(beta0=0.03, beta1=0, beta2=0, tau1=1.0, beta3=0.01),
    dict(beta0=np.nan, beta1=0, beta2=0, tau1=1.0),
])
def test_nss_param_validation(bad):
    with pytest.raises(CurveError):
        NSSParams(**bad)


# --------------------------------------------------------------------- fits
def _universe(n: int = 25) -> list[FixedRateBond]:
    out = []
    for k in range(n):
        mat = date(2027 + k, 2 if k % 2 else 8, 15) if k < 12 else date(2027 + 2 * k - 10, 8, 15)
        out.append(FixedRateBond(f"U{k:02d}", 0.005 + 0.001 * (k % 30), mat, date(2025, mat.month, 15), BUND))
    return out


def _prices(bonds, params, bump: dict | None = None) -> dict[str, float]:
    curve = NelsonSiegelSvenssonCurve(AS_OF, params)
    out = {}
    for b in bonds:
        s = b.settlement_date(AS_OF)
        p = b.clean_price_from_curve(curve, s)
        if bump and b.isin in bump:
            p = b.clean_price_from_yield(b.yield_from_clean_price(p, s) + bump[b.isin], s)
        out[b.isin] = p
    return out


def test_fit_recovers_exact_nss_curve():
    bonds = _universe()
    fit = fit_sovereign_curve(AS_OF, bonds, _prices(bonds, TRUE), model="nss", max_outliers=0)
    assert fit.rmse_bp < 1e-4 and fit.n_used == len(bonds)
    t = np.array([1, 2, 5, 10, 20, 30.0])
    assert np.max(np.abs(fit.zero_rates(t).to_numpy() - TRUE.zero_rate(t))) * 1e4 < 0.01


def test_fit_flags_outlier_and_short_bonds():
    bonds = _universe() + [FixedRateBond("SHORT", 0.02, date(2026, 12, 15), date(2024, 12, 15), BUND)]
    prices = _prices(bonds, TRUE, bump={"U07": 0.0025})
    fit = fit_sovereign_curve(AS_OF, bonds, prices)
    assert fit.excluded["SHORT"].startswith("residual maturity")
    assert fit.excluded["U07"].startswith("outlier")
    assert fit.rich_cheap().index[0] == "U07"                  # cheapest bond first
    assert fit.bonds.loc["U07", "rich_cheap_bp"] == pytest.approx(25, abs=0.5)
    assert fit.rmse_bp < 0.05


def test_fit_auto_model_and_errors():
    few = _universe(8)
    fit = fit_sovereign_curve(AS_OF, few, _prices(few, TRUE), model="auto")
    assert fit.params.model is ParametricModel.NS
    with pytest.raises(CurveError):
        fit_sovereign_curve(AS_OF, few[:4], _prices(few[:4], TRUE), model="nss")
    with pytest.raises(CurveError):
        fit_sovereign_curve(AS_OF, few, {}, model="ns")


def test_par_yields_on_flat_curve():
    flat = NSSParams(0.03, 0.0, 0.0, 1.0)
    bonds = _universe(10)
    fit = fit_sovereign_curve(AS_OF, bonds, _prices(bonds, flat), model="ns", max_outliers=0)
    assert np.allclose(fit.par_yields((2, 10, 30)).to_numpy(), np.exp(0.03) - 1, atol=1e-6)


def test_sample_curves_and_spreads():
    svc = MarketDataService([StaticProvider.from_directory(SAMPLE)], today=lambda: AS_OF)
    static = pd.read_csv(SAMPLE / "bond_static.csv")
    fits = {nm: sovereign_curve_from_market(svc, AS_OF, static.loc[static.issuer == iss, "isin"].tolist(), name=nm)
            for iss, nm in (("DE", "BUND"), ("FR", "OAT"), ("IT", "BTP"))}
    for f in fits.values():
        assert f.rmse_bp < 2.5 and len([v for v in f.excluded.values() if v.startswith("outlier")]) == 1
    spreads = zero_spreads(fits, "BUND", (2, 10, 30))
    assert (spreads["BTP"] > spreads["OAT"]).all() and (spreads > 0).all().all()
    assert 60 < spreads.loc[10, "OAT"] < 85
    with pytest.raises(CurveError):
        zero_spreads(fits, "SPGB")


def test_resolve_universe_from_config(tmp_path):
    from rates_analytics.config import load_settings
    from rates_analytics.curves.sovereign import resolve_universe

    settings = load_settings(overrides={"provider": "static", "fallback": []})
    svc = MarketDataService([StaticProvider.from_directory(SAMPLE)], today=lambda: AS_OF)
    isins, kwargs = resolve_universe(settings, "BTP", svc)
    assert len(isins) == 49 and all(i.startswith("SMPL-IT") for i in isins)
    assert kwargs["model"] == "auto"
    with pytest.raises(CurveError):
        resolve_universe(settings, "JGB", svc)
