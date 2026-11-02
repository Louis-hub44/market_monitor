"""Phase 10 demo: how the other terminal modules consume Rates & Bond Analytics.

Run from the project root:  python examples/phase10_integration.py
(sample data; drop the overrides to use the provider of config/config.yaml)
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from rates_analytics import api
from rates_analytics.config import load_settings

AS_OF = date(2026, 10, 6)
OUT = Path(__file__).resolve().parent / "output"


def main() -> None:
    snap = api.market_snapshot(AS_OF, load_settings(overrides={"provider": "static", "fallback": []}))
    print(f"Snapshot {AS_OF}: {len(snap.bonds)} bonds, sovereigns {sorted(snap.sovereigns)}, warnings {snap.warnings}")

    # Pricing Lab / Valuation Screener: discount factors
    print("\n€STR discount factors:", api.discount_factors(snap.curves, [1.0, 5.0, 10.0, 30.0]).round(6).to_dict())

    # CVA Calculator: monthly DF / zero / 6M forward grid + JSON curve snapshot
    OUT.mkdir(exist_ok=True)
    grid = api.curve_export(snap.curves)
    grid.to_csv(OUT / "curve_grid.csv", index=False)
    (OUT / "curves.json").write_text(json.dumps(api.curves_to_dict(snap.curves), indent=1))
    print(f"\nCurve grid {grid.shape} and curves.json written to {OUT}")
    print(grid.iloc[[11, 59, 119, 359]][["date", "df_estr", "zero_estr", "fwd_euribor6m"]].to_string(index=False))

    # Portfolio Intelligence / PTF Event Radar: one analytics line per bond
    table = api.bond_table(snap, snap.bonds)
    cols = ["name", "ytm", "modified", "dv01", "g_spread", "z_spread_swap", "asw_euribor"]
    print("\nBond table (first lines):")
    print(table[cols].head(5).to_string())

    # Pricing Lab / CVA: swap risk as plain numbers
    r = api.swap_risk(snap, "IRS", date(2025, 3, 12), date(2032, 3, 12), 0.0245, 25e6, pay_fixed=False)
    print(f"\nReceiver IRS 25m: NPV {r['npv']:,.0f} EUR, par {r['par_rate']:.4%}, DV01 {r['dv01']:,.0f} EUR/bp")


if __name__ == "__main__":
    main()
