"""Phase 5 demo: YAS screen on a sample BTP, priced off the fitted BTP NSS curve.

Run from the project root:  python examples/phase5_yas.py
"""

from __future__ import annotations

from datetime import date

from rates_analytics.bonds import bonds_from_static, yas
from rates_analytics.config import load_settings
from rates_analytics.curves import sovereign_curve_from_market
from rates_analytics.curves.sovereign import resolve_universe
from rates_analytics.data import build_market_data_service

AS_OF = date(2026, 10, 6)
ISIN = "SMPL-IT-20350401"   # BTP 3.40 % 01/04/2035 (synthetic sample bond)


def main() -> None:
    settings = load_settings(overrides={"provider": "static", "fallback": []})
    svc = build_market_data_service(settings)
    isins, kw = resolve_universe(settings, "BTP", svc)
    fit = sovereign_curve_from_market(svc, AS_OF, isins, name="BTP", **kw)
    bond = bonds_from_static(svc.bond_static([ISIN]))[0][0]
    clean = float(svc.bond_prices([ISIN], AS_OF).loc[ISIN, "mid"])

    rep = yas(bond, AS_OF, clean=clean, curve=fit.curve, nominal=10_000_000)
    a = rep.analytics
    print(f"YAS  {a.name}   settle {a.settlement}   nominal 10,000,000")
    print(f"  Price {a.clean:.3f}   Accrued {a.accrued:.6f} ({a.accrued_days} days)   Dirty {a.dirty:.6f}")
    print(f"  Yield (street, s.a.) {a.ytm:.5%}   true {a.true_yield:.5%}   annual equiv. {a.annual_equivalent_yield:.5%}")
    print(f"  Macaulay {a.macaulay:.4f}   Modified {a.modified:.4f}   Convexity {a.convexity:.4f}   Risk {a.risk:.4f}")
    am = rep.amounts
    print(f"  Principal {am['principal']:,.2f}   Accrued {am['accrued']:,.2f}   Total {am['total']:,.2f}"
          f"   DV01 {am['dv01']:,.2f} EUR")
    c = rep.curve
    print(f"\nOn the fitted BTP curve: model dirty {c.model_dirty:.4f} (market {a.dirty:.4f}), "
          f"effective duration {c.effective_duration:.4f}, effective convexity {c.effective_convexity:.2f}")
    print("Key-rate DV01 (EUR / bp on 10m):")
    print("  " + "  ".join(f"{k:g}Y {v:,.0f}" for k, v in c.krd_dv01(10_000_000).items()))
    print(f"  sum {c.krd_dv01(10_000_000).sum():,.0f} EUR  vs  parallel {c.effective_duration * c.model_dirty * 1e-4 * 1e5:,.0f} EUR")
    print("\nNext cash flows:")
    print(rep.cashflows.head(4)[["payment_date", "type", "amount", "time", "pv_yield", "pv_curve"]].to_string(index=False))


if __name__ == "__main__":
    main()
