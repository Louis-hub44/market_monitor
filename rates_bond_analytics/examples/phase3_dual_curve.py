"""Phase 3 demo: €STR discounting + EURIBOR 6M projection (dual curve) from the sample snapshot.

Run from the project root:  python examples/phase3_dual_curve.py
"""

from __future__ import annotations

from datetime import date, timedelta

from rates_analytics.config import load_settings
from rates_analytics.curves import eur_curves_from_market
from rates_analytics.data import build_market_data_service
from rates_analytics.instruments import VanillaSwap

AS_OF = date(2026, 10, 6)


def main() -> None:
    svc = build_market_data_service(load_settings(overrides={"provider": "static", "fallback": []}))
    dual = eur_curves_from_market(svc, AS_OF)
    single = eur_curves_from_market(svc, AS_OF, dual_curve=False)
    e6 = dual.forwards["EURIBOR6M"]
    print(f"EURIBOR 6M projection curve {AS_OF} – max repricing error {e6.max_abs_error_bp():.1e} bp\n")
    print(e6.report()[["instrument", "quote", "pillar", "discount", "zero_cc"]].to_string(
        index=False, formatters={"quote": "{:.4%}".format, "zero_cc": "{:.4%}".format,
                                 "discount": "{:.6f}".format}))

    print("\n€STR / EURIBOR 6M basis (spread on the €STR leg):")
    print(dual.basis_spreads().to_string(index=False, formatters={
        "ois_par": "{:.4%}".format, "irs_par": "{:.4%}".format, "basis_bp": "{:.2f}".format}))

    print("\n6M EURIBOR forwards – dual vs single curve:")
    for y in (2030, 2036, 2046, 2056):
        s = date(y, 10, 8)
        e = s + timedelta(days=182)
        fd = dual.forward_curve("EURIBOR6M").forward_rate(s, e)
        fs = single.forward_curve("EURIBOR6M").forward_rate(s, e)
        print(f"  {s}: dual {fd:.4%}  single {fs:.4%}  gap {(fd - fs) * 1e4:+.2f} bp")

    swap = VanillaSwap.from_tenor(AS_OF, "10Y", fixed_rate=0.025, notional=10_000_000)
    npv_dual = swap.npv(dual.discount_curve, dual.forward_curve("EURIBOR6M"))
    npv_single = swap.npv(single.forward_curve("EURIBOR6M"))
    print(f"\n10Y payer IRS @2.50% on 10m EUR: NPV dual {npv_dual:,.0f}  single {npv_single:,.0f}  "
          f"(discounting effect {npv_dual - npv_single:+,.0f} EUR)")


if __name__ == "__main__":
    main()
