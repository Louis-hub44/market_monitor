"""Phase 4 demo: Bund, OAT and BTP curves fitted in Nelson-Siegel-Svensson on the sample universe.

Run from the project root:  python examples/phase4_sovereign_curves.py
"""

from __future__ import annotations

from datetime import date

from rates_analytics.config import load_settings
from rates_analytics.curves import eur_curves_from_market, sovereign_curve_from_market, zero_spreads
from rates_analytics.curves.sovereign import resolve_universe
from rates_analytics.data import build_market_data_service

AS_OF = date(2026, 10, 6)


def main() -> None:
    settings = load_settings(overrides={"provider": "static", "fallback": []})
    svc = build_market_data_service(settings)
    fits = {}
    for name in ("BUND", "OAT", "BTP"):
        isins, kwargs = resolve_universe(settings, name, svc)
        fits[name] = fit = sovereign_curve_from_market(svc, AS_OF, isins, name=name, **kwargs)
        p = fit.params
        print(f"{name}: {fit.params.model.value.upper()} on {fit.n_used}/{len(fit.bonds)} bonds, "
              f"RMSE {fit.rmse_bp:.2f} bp | b0 {p.beta0:.4%} b1 {p.beta1:.4%} b2 {p.beta2:.4%} "
              f"b3 {p.beta3:.4%} tau1 {p.tau1:.2f} tau2 {p.tau2:.2f}")
        for isin, why in fit.excluded.items():
            print(f"    excluded {isin}: {why}")
        print("    par yields  " + "  ".join(f"{int(t)}Y {y:.3%}" for t, y in fit.par_yields().items()))

    print("\nZero spreads vs Bund (bp):")
    print(zero_spreads(fits, "BUND").round(1).to_string())

    swaps = eur_curves_from_market(svc, AS_OF)
    print("\nBund zero minus €STR zero (bp):")
    for t in (2, 5, 10, 30):
        gap = fits["BUND"].curve.zero_rate(float(t)) - swaps.discount_curve.zero_rate(float(t))
        print(f"  {t:>2}Y {gap * 1e4:+.1f}")

    print("\nBTP rich / cheap vs fitted curve (top 5 cheap):")
    print(fits["BTP"].rich_cheap().head(5).to_string(formatters={
        "coupon": "{:.3%}".format, "ytm_mkt": "{:.3%}".format, "ytm_model": "{:.3%}".format,
        "rich_cheap_bp": "{:+.1f}".format}))


if __name__ == "__main__":
    main()
