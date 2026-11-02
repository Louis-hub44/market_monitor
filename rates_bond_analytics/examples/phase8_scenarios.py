"""Phase 8 demo: scenario P&L of a small rates book (swaps + BTP + Bund).

Run from the project root:  python examples/phase8_scenarios.py
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from rates_analytics.bonds import bonds_from_static
from rates_analytics.config import load_settings
from rates_analytics.curves import eur_curves_from_market, sovereign_curve_from_market
from rates_analytics.curves.sovereign import resolve_universe
from rates_analytics.data import build_market_data_service
from rates_analytics.instruments import OvernightIndexedSwap, VanillaSwap, fixings_from_market
from rates_analytics.scenarios import (
    CurveEnvironment,
    Position,
    apply_scenario,
    butterfly,
    custom,
    flattener,
    irrbb_scenarios,
    par_rate_table,
    parallel,
    scenario_pnl,
    steepener,
)

AS_OF = date(2026, 10, 6)


def main() -> None:
    settings = load_settings(overrides={"provider": "static", "fallback": []})
    svc = build_market_data_service(settings)
    curves = eur_curves_from_market(svc, AS_OF)
    gov = {}
    for name in ("BUND", "BTP"):
        isins, kw = resolve_universe(settings, name, svc)
        gov[name] = sovereign_curve_from_market(svc, AS_OF, isins, name=name, **kw).curve
    e6 = fixings_from_market(svc, "EUR.EURIBOR6M.FIX", date(2024, 1, 1), AS_OF)

    def bond(isin):
        b = bonds_from_static(svc.bond_static([isin]))[0][0]
        return b, float(svc.bond_prices([isin], AS_OF).loc[isin, "mid"])

    btp, btp_px = bond("SMPL-IT-20350401")
    bund, bund_px = bond("SMPL-DE-20340815")
    book = [
        Position("Rec IRS 2.45% 2032 (seasoned)", VanillaSwap(date(2025, 3, 12), date(2032, 3, 12), 0.0245,
                                                               notional=25e6, pay_fixed=False), fixings=e6),
        Position("Pay IRS 10Y 2.70%", VanillaSwap.from_tenor(AS_OF, "10Y", 0.027, notional=20e6)),
        Position("Rec OIS 2Y 2.05%", OvernightIndexedSwap.from_tenor(AS_OF, "2Y", 0.0205, notional=50e6,
                                                                     pay_fixed=False)),
        Position(f"Long {btp}", btp, nominal=10e6, clean_price=btp_px, curve="BTP"),
        Position(f"Short {bund}", bund, nominal=-10e6, clean_price=bund_px, curve="BUND"),
    ]
    base = CurveEnvironment(curves, gov)
    scenarios = [parallel(25), parallel(-25), steepener(25, kind="bear"), flattener(25, kind="bull"),
                 butterfly(15), custom({"2Y": -10, "5Y": 0, "30Y": 20}, name="Custom 2Y-10 / 30Y+20")]

    pd.set_option("display.width", 220)
    for mode in ("quotes", "zero"):
        pnl = scenario_pnl(book, base, scenarios, mode=mode)
        print(f"\nScenario P&L (EUR) – shocks applied to {'market quotes + re-bootstrap' if mode == 'quotes' else 'zero rates'}")
        print(pnl.drop(columns="base_value").round(0).to_string())

    shocked = {s.name: apply_scenario(curves, s, "quotes") for s in scenarios[:3]}
    print("\nPar swap rates (base, %) and changes (bp):")
    t = par_rate_table(curves, shocked)
    t["base"] = t["base"] * 100
    print(t.round(3).to_string())

    print("\nIRRBB (BCBS d368, EUR) – zero-rate shocks:")
    pnl = scenario_pnl(book, base, irrbb_scenarios("EUR"), mode="zero")
    print(pnl.loc[["TOTAL"]].drop(columns="base_value").round(0).to_string())


if __name__ == "__main__":
    main()
