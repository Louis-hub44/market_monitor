"""Phase 7 demo: SWPM screen for a seasoned EURIBOR swap and a seasoned €STR OIS.

Run from the project root:  python examples/phase7_swpm.py
"""

from __future__ import annotations

from datetime import date

from rates_analytics.config import load_settings
from rates_analytics.curves import eur_curves_from_market
from rates_analytics.data import build_market_data_service
from rates_analytics.instruments import OvernightIndexedSwap, VanillaSwap, fixings_from_market
from rates_analytics.swaps import swpm

AS_OF = date(2026, 10, 6)


def show(title: str, val) -> None:
    s = val.summary()
    print(f"\n=== {title} ===")
    print(f"  NPV {s['npv']:,.0f}   accrued {s['accrued']:,.0f}   clean {s['clean_npv']:,.0f}   "
          f"(fixed leg {s['fixed_leg_pv']:,.0f} / float leg {s['float_leg_pv']:,.0f})")
    print(f"  par rate {s['par_rate']:.5%}   par spread {s['par_spread_bp']:+.2f} bp   PV01 {s['pv01']:,.0f}   "
          f"DV01 {s['dv01']:,.0f} EUR/bp")
    t = val.bucketed.table
    big = t[t["dv01"].abs() >= 0.5].sort_values("pillar")
    print("  Bucketed DV01 (EUR / +1bp on each quote):")
    for _, r in big.iterrows():
        print(f"    {r['curve']:<9} {r['instrument']:<9} {r['quote']:.4%}  {r['dv01']:>12,.0f}")
    print(f"    {'total':<29}{val.bucketed.total:>12,.0f}")


def main() -> None:
    svc = build_market_data_service(load_settings(overrides={"provider": "static", "fallback": []}))
    curves = eur_curves_from_market(svc, AS_OF)
    e6 = fixings_from_market(svc, "EUR.EURIBOR6M.FIX", date(2024, 1, 1), AS_OF)
    estr = fixings_from_market(svc, "EUR.ESTR.FIX", date(2024, 1, 1), AS_OF)

    irs = VanillaSwap(date(2025, 3, 12), date(2032, 3, 12), fixed_rate=0.0245, notional=25_000_000, pay_fixed=False)
    val = swpm(irs, curves, e6, rebuild_parallel=True)
    show("Receiver IRS 2.45% vs EURIBOR 6M, 12/03/2025 -> 12/03/2032, 25m", val)
    print("  Floating leg (next periods):")
    print(val.float_leg.cashflows.head(3)[["accrual_start", "accrual_end", "fixing_date", "rate", "status",
                                           "amount", "pv"]].to_string(index=False))

    ois = OvernightIndexedSwap(date(2025, 11, 5), date(2027, 11, 5), fixed_rate=0.0195, notional=50_000_000)
    show("Payer €STR OIS 1.95%, 05/11/2025 -> 05/11/2027, 50m", swpm(ois, curves, estr))


if __name__ == "__main__":
    main()
