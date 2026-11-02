"""Module Fixed Income : duration, convexité, KRD, courbes, scénarios."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from components.charts import bar_breakdown, heatmap, impact_bars, krd_bars, waterfall, yield_curve
from components.ui import (
    confidence_note,
    empty_state,
    fmt,
    kpi_row,
    page_header,
    quality_badge,
    risk_rail,
    warning_list,
)
from config.constants import KEY_RATE_TENORS
from models.enums import AssetClass
from services.fixed_income.curves import CURVE_SHAPES, CURVE_SHAPE_LABELS, interpolate_shape
from services.scenarios.library import credit_scenarios, rates_scenarios
from utils.state import curves, fixed_income_analytics, market_data, require_portfolio, scenario_engine

page_header("Fixed Income", "Sensibilité à la courbe des taux",
            "Quelle est la sensibilité du portefeuille, et que se passe-t-il si les taux bougent ?")

portfolio = require_portfolio()
if portfolio is None:
    st.stop()

bonds = portfolio.positions_of(AssetClass.GOV_BOND, AssetClass.CREDIT)
if not bonds:
    empty_state("Ce portefeuille ne contient pas de lignes obligataires identifiées.",
                "Vérifiez que le fichier comporte les colonnes coupon et maturité.")
    st.stop()

analytics = fixed_income_analytics(portfolio)
available_curves = curves()

kpi_row([
    {"label": "Poche obligataire", "value": fmt(analytics.sleeve_value / portfolio.nav, "pct0")},
    {"label": "Duration modifiée", "value": fmt(analytics.modified_duration, "num"),
     "foot": f"couverture {analytics.coverage:.0%}"},
    {"label": "Convexité", "value": fmt(analytics.convexity, "num", 1)},
    {"label": "Duration de spread", "value": fmt(analytics.spread_duration, "num")},
    {"label": "DV01", "value": fmt(analytics.dv01, "money") + f" {portfolio.base_currency}"},
])

if analytics.coverage < 0.99:
    st.warning(f"Duration calculée sur {analytics.coverage:.0%} de la poche obligataire seulement.")
warning_list(analytics.warnings)

tabs = st.tabs(["Duration", "Key Rate Duration", "Courbes", "Scénarios de taux", "Scénarios de courbe"])

# ------------------------------------------------------------------ #
with tabs[0]:
    rows = [
        {
            "identifiant": r.position.identifier,
            "libellé": r.position.name,
            "poids poche": r.weight_in_sleeve,
            "maturité (ans)": round(r.ttm, 2),
            "YTM": r.ytm,
            "duration modifiée": r.modified_duration,
            "convexité": r.convexity,
            "duration de spread": r.spread_duration,
            "contribution duration": r.duration_contribution,
            "DV01": round(r.dv01_contribution, 2),
            "qualité": r.quality.value,
        }
        for r in analytics.positions
    ]
    frame = pd.DataFrame(rows).sort_values("contribution duration", ascending=False)
    st.dataframe(frame, width="stretch", hide_index=True)
    st.download_button("Exporter en CSV", frame.to_csv(index=False).encode("utf-8"),
                       file_name="duration_positions.csv", mime="text/csv")

    c1, c2 = st.columns(2)
    with c1:
        st.plotly_chart(bar_breakdown(analytics.breakdown("country"), "Contribution à la duration par pays",
                                      percent=False), width="stretch")
    with c2:
        st.plotly_chart(bar_breakdown(analytics.breakdown("currency"), "Contribution à la duration par devise",
                                      percent=False), width="stretch")

# ------------------------------------------------------------------ #
with tabs[1]:
    krd = analytics.key_rate_durations
    if not any(abs(v) > 1e-9 for v in krd.values()):
        empty_state("Key rate durations non calculables : aucune courbe zéro-coupon disponible.",
                    "Elles nécessitent une courbe de marché (voir la page Qualité des données).")
    else:
        st.plotly_chart(krd_bars(krd), width="stretch")
        st.caption(f"Somme des KRD : {sum(krd.values()):.2f} — à comparer à la duration modifiée "
                   f"{analytics.modified_duration:.2f}.")

        matrix = pd.DataFrame(
            [[r.key_rate_durations.get(t, 0.0) * r.weight_in_sleeve for t in KEY_RATE_TENORS]
             for r in analytics.positions if r.key_rate_durations],
            index=[r.position.identifier for r in analytics.positions if r.key_rate_durations],
            columns=list(KEY_RATE_TENORS),
        )
        if not matrix.empty:
            st.plotly_chart(heatmap(matrix, "Contribution par position et par maturité", percent=False),
                            width="stretch")

# ------------------------------------------------------------------ #
with tabs[2]:
    if not available_curves:
        empty_state("Aucune courbe de marché disponible.",
                    "La courbe US Treasury est récupérée via FMP ; vérifiez la clé API et la connectivité.")
    else:
        display = {}
        for key, curve in available_curves.items():
            if key in ("DEFAULT",) or len(key) != 2:
                continue
            display[curve.label] = {f"{t:g}Y": curve.rate(t) for t in sorted(curve.nodes)}
        if display:
            st.plotly_chart(yield_curve(display), width="stretch")
            dp = market_data().treasury_curve()
            st.markdown(quality_badge(dp.quality, dp.note) + f" · arrêtée au {dp.as_of}",
                        unsafe_allow_html=True)

# ------------------------------------------------------------------ #
with tabs[3]:
    engine = scenario_engine(portfolio)
    results = engine.run_all(rates_scenarios()[:6] + credit_scenarios())
    risk_rail([(r.scenario.name, r.total_impact_pct) for r in results])

    st.markdown("#### Détail d'un scénario")
    choice = st.selectbox("Scénario", [r.scenario.name for r in results])
    chosen = next(r for r in results if r.scenario.name == choice)
    c1, c2 = st.columns([1, 1])
    with c1:
        st.plotly_chart(impact_bars(chosen.by_asset_class(), "Par classe d'actif", height=260),
                        width="stretch")
    with c2:
        top = chosen.top_contributors(8)
        st.plotly_chart(
            impact_bars({c.position_name[:28]: c.impact_pct for c in top}, "Principales contributions", height=260),
            width="stretch",
        )
    st.markdown("**Explication ligne à ligne**")
    st.dataframe(
        pd.DataFrame([{"position": c.position_name, "poids": c.weight,
                       "impact": c.impact_pct, "formule": c.formula} for c in chosen.top_contributors(15)]),
        width="stretch", hide_index=True,
    )
    confidence_note(chosen.confidence, chosen.coverage, chosen.assumptions)

# ------------------------------------------------------------------ #
with tabs[4]:
    st.caption("Déformations non parallèles appliquées via les key rate durations.")
    shape_key = st.selectbox("Forme", list(CURVE_SHAPES), format_func=lambda k: CURVE_SHAPE_LABELS[k])
    shape = interpolate_shape(CURVE_SHAPES[shape_key])
    st.dataframe(pd.DataFrame([shape], index=["bp"]), width="stretch")

    engine = scenario_engine(portfolio)
    from models.enums import ShockType
    from models.scenario import Scenario, Shock

    scenario = Scenario(f"curve_{shape_key}", CURVE_SHAPE_LABELS[shape_key],
                        (Shock(ShockType.RATES_CURVE, 0.0, "ALL", curve=CURVE_SHAPES[shape_key]),))
    result = engine.run(scenario)

    kpi_row([{"label": "Impact portefeuille", "value": fmt(result.total_impact_pct),
              "positive": result.total_impact_pct >= 0}])

    segment_impacts: dict[str, float] = {t: 0.0 for t in KEY_RATE_TENORS}
    for contribution in result.contributions:
        krd = contribution.inputs.get("krd") or {}
        for tenor, value in krd.items():
            segment_impacts[tenor] = segment_impacts.get(tenor, 0.0) - (
                contribution.weight * value * shape.get(tenor, 0.0) * 1e-4
            )
    if any(abs(v) > 1e-12 for v in segment_impacts.values()):
        st.plotly_chart(waterfall(list(segment_impacts), list(segment_impacts.values()),
                                  "Décomposition par segment de courbe"), width="stretch")
    confidence_note(result.confidence, result.coverage, result.assumptions)
