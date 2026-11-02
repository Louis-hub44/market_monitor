"""Phase 2 demo: bootstrap the €STR curve from the offline sample snapshot.

Run from the project root:  python examples/phase2_estr_curve.py
Writes examples/output/estr_curve.html (zero rates and 1M forwards, both interpolations).
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from rates_analytics.config import load_settings
from rates_analytics.curves import estr_curve_from_market
from rates_analytics.data import build_market_data_service
from rates_analytics.instruments import OvernightIndexedSwap

AS_OF = date(2026, 10, 7)
OUT = Path(__file__).resolve().parent / "output"


def main() -> None:
    # the static provider serves data/sample/history.csv (illustrative quotes, not real market data)
    svc = build_market_data_service(load_settings(overrides={"provider": "static", "fallback": []}))
    curves = {m: estr_curve_from_market(svc, AS_OF, interpolation=m) for m in ("log_linear", "monotone_convex")}

    res = curves["monotone_convex"]
    print(f"€STR curve {AS_OF} – monotone convex, {res.passes} passes, "
          f"max repricing error {res.max_abs_error_bp():.1e} bp\n")
    rep = res.report()[["instrument", "quote", "pillar", "discount", "zero_cc", "error_bp"]]
    print(rep.to_string(index=False, formatters={"quote": "{:.4%}".format, "zero_cc": "{:.4%}".format,
                                                 "discount": "{:.8f}".format, "error_bp": "{:.1e}".format}))

    swap = OvernightIndexedSwap.from_tenor(AS_OF, "7Y", fixed_rate=0.024, notional=10_000_000)
    for m, r in curves.items():
        print(f"\n7Y payer OIS @2.40% on {m}: par {swap.fair_rate(r.curve):.5%}, "
              f"NPV {swap.npv(r.curve):,.2f} EUR, N·A {swap.annuity(r.curve):,.0f} (DV01 ≈ {swap.annuity(r.curve) * 1e-4:,.0f} EUR/bp)")

    grid = [AS_OF + timedelta(days=k) for k in range(7, 365 * 50, 7)]
    frame = pd.DataFrame({"date": grid})
    for m, r in curves.items():
        frame[f"zero {m}"] = [r.curve.zero_rate(d) for d in grid]
        frame[f"1M fwd {m}"] = [r.curve.forward_rate(d, d + timedelta(days=30)) for d in grid]
    try:
        import plotly.graph_objects as go
    except ImportError:
        return
    fig = go.Figure()
    for col in frame.columns[1:]:
        fig.add_scatter(x=frame["date"], y=frame[col] * 100, name=col, mode="lines",
                        line={"dash": "dot" if "log" in col else "solid"})
    fig.update_layout(title=f"€STR curve {AS_OF} (illustrative data)", yaxis_title="%", template="plotly_white")
    OUT.mkdir(exist_ok=True)
    fig.write_html(OUT / "estr_curve.html")
    print(f"\nChart written to {OUT / 'estr_curve.html'}")


if __name__ == "__main__":
    main()
