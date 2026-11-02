"""Integration API (contract for the other terminal modules) and cross-module invariants."""

from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import pytest

from rates_analytics import api
from rates_analytics.bonds import yield_to_maturity, z_spread
from rates_analytics.config import load_settings
from rates_analytics.scenarios import apply_scenario, parallel

AS_OF = date(2026, 10, 6)


@pytest.fixture(scope="module")
def snap():
    return api.market_snapshot(AS_OF, load_settings(overrides={"provider": "static", "fallback": []}))


# ---------------------------------------------------------------- contract
def test_snapshot_contents(snap):
    assert not snap.warnings and set(snap.sovereigns) == {"BUND", "OAT", "BTP"}
    assert len(snap.bonds) == len(snap.prices) == 109 and set(snap.fixings) == {"ESTR", "EURIBOR6M"}
    with pytest.raises(Exception, match="No sovereign curve"):
        snap.gov_curve("SPGB")
    with pytest.raises(TypeError):
        api.market_snapshot("2026-10-06")


def test_discount_factors_dates_and_times(snap):
    d = AS_OF + timedelta(days=3650)
    s = api.discount_factors(snap.curves, [d, 10.0])
    assert s.iloc[0] == pytest.approx(s.iloc[1], abs=1e-4) and 0.5 < s.iloc[0] < 1.0
    e6 = api.discount_factors(snap.curves, [10.0], index="EURIBOR6M")
    assert e6.name == "df_euribor6m" and e6.iloc[0] < s.iloc[1]   # projection curve sits above €STR


def test_curve_export_grid(snap):
    g = api.curve_export(snap.curves, horizon_years=30)
    assert len(g) == 360 and g["t"].is_monotonic_increasing and g["df_estr"].is_monotonic_decreasing
    assert np.allclose(g["df_estr"], np.exp(-g["zero_estr"] * g["t"]), atol=1e-12)
    assert (g["fwd_euribor6m"] > g["fwd_estr"]).all()                # positive basis everywhere
    with pytest.raises(ValueError):
        api.curve_export(snap.curves, horizon_years=0)


def test_curves_roundtrip_through_json(snap):
    payload = json.loads(json.dumps(api.curves_to_dict(snap.curves)))
    rebuilt = api.curves_from_dict(payload)
    assert set(rebuilt) == {"ESTR", "EURIBOR6M"} and payload["api_version"] == api.API_VERSION
    for name, c in rebuilt.items():
        ref = snap.curves.forward_curve(name)
        for d in (date(2027, 1, 15), date(2036, 6, 30), date(2075, 1, 2)):
            assert c.discount(d) == ref.discount(d)


def test_bond_table_and_swap_risk(snap):
    t = api.bond_table(snap, snap.bonds)
    assert len(t) == 109 and {"ytm", "modified", "z_spread_swap", "asw_euribor", "g_spread"} <= set(t.columns)
    btp, bund = t[t.index.str.startswith("SMPL-IT")], t[t.index.str.startswith("SMPL-DE")]
    assert btp["z_spread_swap"].median() > bund["z_spread_swap"].median()
    assert api.bond_table(snap, [], {}).empty
    r = api.swap_risk(snap, "IRS", date(2025, 3, 12), date(2032, 3, 12), 0.0245, 25e6, pay_fixed=False)
    assert r["npv"] == pytest.approx(253101.47, abs=1.0)
    assert r["buckets"]["dv01"].sum() == pytest.approx(r["dv01"], rel=1e-3)
    with pytest.raises(ValueError):
        api.make_swap("CDS", AS_OF, date(2030, 1, 1), 0.02, 1e6)


# -------------------------------------------------------------- invariants
@pytest.mark.parametrize("kind", ["IRS", "OIS"])
def test_par_swap_has_zero_npv_and_payer_receiver_symmetry(snap, kind):
    start, mat = AS_OF + timedelta(days=2), date(2036, 10, 8)
    k = api.swap_risk(snap, kind, start, mat, 0.02, 1e7)["par_rate"]
    payer = api.swap_risk(snap, kind, start, mat, k, 1e7, pay_fixed=True)
    receiver = api.swap_risk(snap, kind, start, mat, k, 1e7, pay_fixed=False)
    assert abs(payer["npv"]) < 1e-4 and payer["dv01"] == pytest.approx(-receiver["dv01"], rel=1e-9)
    assert payer["dv01"] > 0                                          # payer gains when rates rise


def test_parallel_scenario_moves_every_curve_and_npv_by_dv01(snap):
    start, mat = AS_OF + timedelta(days=2), date(2031, 10, 8)
    swap = api.make_swap("IRS", start, mat, 0.025, 1e7)
    base = api.swap_risk(snap, "IRS", start, mat, 0.025, 1e7)
    shocked = apply_scenario(snap.curves, parallel(1), mode="zero")
    for t in (1.0, 5.0, 20.0):
        for name in ("ESTR", "EURIBOR6M"):
            dz = shocked.forward_curve(name).zero_rate(t) - snap.curves.forward_curve(name).zero_rate(t)
            assert dz == pytest.approx(1e-4, abs=1e-10)
    from rates_analytics.swaps import value_swap

    pnl = value_swap(swap, shocked, snap.fixings["EURIBOR6M"]).npv - base["npv"]
    assert pnl == pytest.approx(base["dv01"], rel=0.05)              # zero shock vs quote shock: same order


def test_bond_price_yield_and_zspread_consistency(snap):
    disc = snap.curves.discount_curve
    for isin in list(snap.bonds)[::15]:
        b, px = snap.bonds[isin], snap.prices[isin]
        settle = b.settlement_date(AS_OF)
        y = yield_to_maturity(b, px, settle)
        assert b.clean_price_from_yield(y, settle) == pytest.approx(px, abs=1e-9)
        z = z_spread(b, px, settle, disc)
        lo, hi = z_spread(b, px + 0.5, settle, disc), z_spread(b, px - 0.5, settle, disc)
        assert lo < z < hi                                              # cheaper bond, wider spread


def test_fixing_published_on_curve_date_is_used_consistently(snap):
    """Regression (phase 10): a spot-starting IRS fixes today; NPV and SWPM leg tables must agree."""
    from rates_analytics.instruments import Fixings
    from rates_analytics.swaps import value_swap

    swap = api.make_swap("IRS", AS_OF + timedelta(days=2), date(2031, 10, 8), 0.025, 1e7)
    e6 = snap.curves.forward_curve("EURIBOR6M")
    projected = value_swap(swap, snap.curves).npv
    for fix in (0.020, 0.030):
        v = value_swap(swap, snap.curves, Fixings({AS_OF: fix}, "EURIBOR6M"))   # internal leg check passes
        first = v.float_leg.table.iloc[0] if hasattr(v.float_leg, "table") else None
        if first is not None:
            assert first["rate"] == fix and first["status"] == "fixed"
        assert swap.npv(snap.curves.discount_curve, e6, Fixings({AS_OF: fix}, "EURIBOR6M")) == pytest.approx(v.npv)
    lo = value_swap(swap, snap.curves, Fixings({AS_OF: 0.020}, "EURIBOR6M")).npv
    hi = value_swap(swap, snap.curves, Fixings({AS_OF: 0.030}, "EURIBOR6M")).npv
    assert lo < hi and lo != projected                                  # payer: higher first fixing, higher NPV
