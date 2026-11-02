"""Phase 6 demo: G / I / Z / ASW spreads of the sample OAT and BTP universes.

Run from the project root:  python examples/phase6_spreads.py
"""

from __future__ import annotations

from datetime import date

from rates_analytics.bonds import asw_par_par, bonds_from_static, spread_analytics, spread_table, yas
from rates_analytics.config import load_settings
from rates_analytics.curves import eur_curves_from_market, sovereign_curve_from_market
from rates_analytics.curves.sovereign import resolve_universe
from rates_analytics.data import build_market_data_service

AS_OF = date(2026, 10, 6)
COLS = ["maturity", "ytm", "g_spread", "i_spread", "z_spread_swap", "z_spread_ois", "asw_euribor", "asw_estr"]


def main() -> None:
    settings = load_settings(overrides={"provider": "static", "fallback": []})
    svc = build_market_data_service(settings)
    curves = eur_curves_from_market(svc, AS_OF)
    isins, kw = resolve_universe(settings, "BUND", svc)
    bund = sovereign_curve_from_market(svc, AS_OF, isins, name="BUND", **kw)

    for name in ("OAT", "BTP"):
        isins, _ = resolve_universe(settings, name, svc)
        bonds, _ = bonds_from_static(svc.bond_static(isins))
        mids = svc.bond_prices(isins, AS_OF)["mid"].dropna().to_dict()
        tab = spread_table(bonds, mids, AS_OF, curves, bund.curve)
        print(f"\n{name} spreads (bp) – G vs Bund par curve, I / Z / ASW vs EURIBOR 6M and €STR")
        print(tab[COLS].iloc[::5].to_string(formatters={"ytm": "{:.3%}".format, **dict.fromkeys(COLS[2:], "{:.1f}".format)}))

    isin = "SMPL-IT-20350401"
    bond = bonds_from_static(svc.bond_static([isin]))[0][0]
    clean = float(svc.bond_prices([isin], AS_OF).loc[isin, "mid"])
    s = spread_analytics(bond, AS_OF, clean, curves, bund.curve)
    a = asw_par_par(bond, clean, bond.settlement_date(AS_OF), curves.discount_curve, curves.forward_curve("EURIBOR6M"))
    print(f"\n{bond}  clean {clean}")
    print(f"  G {s.g_spread * 1e4:.1f} bp (Bund par yield {s.gov_benchmark_yield:.3%})   "
          f"I {s.i_spread * 1e4:.1f} bp (swap {s.swap_rate:.3%})")
    print(f"  Z vs 6M {s.z_spread_swap * 1e4:.1f} bp   Z vs €STR {s.z_spread_ois * 1e4:.1f} bp   "
          f"Z vs Bund {s.z_spread_gov * 1e4:.1f} bp")
    print(f"  ASW par-par 6M {a.spread * 1e4:.1f} bp = (upfront {a.upfront:.4f} + PV coupons {a.pv_coupons:.4f}"
          f" - PV float {a.pv_float:.4f}) / BPV {a.bpv_float:.4f}")
    rep = yas(bond, AS_OF, clean=clean, curve=curves.forward_curve("EURIBOR6M"), spread="z", nominal=10_000_000)
    kr = rep.curve.krd_dv01(10_000_000)
    print("  Market-consistent KRD DV01 on the swap curve (+Z): "
          + "  ".join(f"{k:g}Y {v:,.0f}" for k, v in kr.items() if abs(v) > 1) + f"   total {kr.sum():,.0f} EUR")


if __name__ == "__main__":
    main()
