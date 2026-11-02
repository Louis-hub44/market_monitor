"""Module Diversifié : allocation, moteur macro, matrice d'impact, top risques."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from components.charts import bar_breakdown, heatmap, impact_bars, waterfall
from components.ui import confidence_note, empty_state, fmt, kpi_row, page_header, risk_rail
from services.scenarios.library import (
    commodity_scenarios,
    credit_scenarios,
    custom_scenario,
    equity_scenarios,
    fx_scenarios,
    macro_scenarios,
    rates_scenarios,
    stress_battery,
)
from utils.state import require_portfolio, scenario_engine

page_header("Diversifié", "Multi-actifs et scénarios macro",
            "Que se passe-t-il pour le portefeuille si le scénario macro anticipé se réalise ?")

portfolio = require_portfolio()
if portfolio is None:
    st.stop()

engine = scenario_engine(portfolio)
tabs = st.tabs(["Allocation", "Scénarios macro", "Matrice d'impact", "Ce qui peut faire mal"])

# ------------------------------------------------------------------ #
with tabs[0]:
    exposures = portfolio.exposure_by_asset_class()
    kpi_row([
        {"label": label.replace("_", " ").title(), "value": fmt(weight, "pct0")}
        for label, weight in list(exposures.items())[:6]
    ])
    c1, c2 = st.columns(2)
    with c1:
        st.plotly_chart(bar_breakdown(exposures, "Allocation par classe d'actif"), width="stretch")
    with c2:
        st.plotly_chart(bar_breakdown(portfolio.exposure_by_currency(), "Exposition devises"),
                        width="stretch")

# ------------------------------------------------------------------ #
with tabs[1]:
    st.markdown("#### Scénarios prédéfinis")
    results = engine.run_all(macro_scenarios())
    risk_rail([(r.scenario.name, r.total_impact_pct) for r in
               sorted(results, key=lambda r: r.total_impact_pct)])

    choice = st.selectbox("Détail", [r.scenario.name for r in results])
    chosen = next(r for r in results if r.scenario.name == choice)
    st.caption(chosen.scenario.narrative or "")
    st.write("Chocs appliqués : " + " · ".join(chosen.scenario.labels()))

    c1, c2 = st.columns(2)
    with c1:
        st.plotly_chart(impact_bars(chosen.by_asset_class(), "Par classe d'actif", height=280),
                        width="stretch")
    with c2:
        by_shock = chosen.by_shock()
        st.plotly_chart(waterfall(list(by_shock), list(by_shock.values()), "Décomposition par choc"),
                        width="stretch")
    confidence_note(chosen.confidence, chosen.coverage, chosen.assumptions)

    st.divider()
    st.markdown("#### Scénario sur mesure")
    c1, c2, c3 = st.columns(3)
    equity_pct = c1.slider("Actions (%)", -40, 40, -10) / 100
    rates_bp = c2.slider("Taux (bp)", -200, 300, 100)
    credit_bp = c3.slider("Spreads crédit (bp)", -100, 400, 100)
    c4, c5, c6 = st.columns(3)
    oil_pct = c4.slider("Pétrole (%)", -50, 60, 0) / 100
    fx_pct = c5.slider("Devise (%)", -20, 20, 0) / 100
    fx_ccy = c6.text_input("Devise choquée", "USD")

    custom = custom_scenario("Scénario personnalisé", equity_pct=equity_pct, rates_bp=rates_bp,
                             credit_bp=credit_bp, oil_pct=oil_pct, fx_pct=fx_pct, fx_currency=fx_ccy)
    result = engine.run(custom)
    kpi_row([{"label": "Impact estimé", "value": fmt(result.total_impact_pct),
              "positive": result.total_impact_pct >= 0,
              "foot": f"couverture {result.coverage:.0%}"}])
    st.plotly_chart(impact_bars(result.by_asset_class(), "Contribution par classe d'actif", height=260),
                    width="stretch")
    st.code(result.explain(), language="text")

# ------------------------------------------------------------------ #
with tabs[2]:
    st.caption("Chaque cellule est calculée à partir des expositions réelles du portefeuille.")
    scenarios = (equity_scenarios()[:2] + rates_scenarios()[:1] + rates_scenarios()[4:6]
                 + credit_scenarios()[1:3] + fx_scenarios(portfolio.base_currency)[:1]
                 + commodity_scenarios() + macro_scenarios()[:2])
    results = engine.run_all(scenarios)

    asset_classes = sorted({c.asset_class.value for r in results for c in r.contributions})
    if not asset_classes:
        empty_state("Aucun impact chiffrable : sensibilités indisponibles.")
    else:
        matrix = pd.DataFrame(index=asset_classes + ["PORTEFEUILLE"],
                              columns=[r.scenario.name for r in results], dtype=float).fillna(0.0)
        for r in results:
            for ac, value in r.by_asset_class().items():
                matrix.loc[ac, r.scenario.name] = value
            matrix.loc["PORTEFEUILLE", r.scenario.name] = r.total_impact_pct
        st.plotly_chart(heatmap(matrix, "Matrice d'impact macro", height=460), width="stretch")
        st.download_button("Exporter la matrice", matrix.to_csv().encode("utf-8"),
                           file_name="matrice_impact.csv", mime="text/csv")

# ------------------------------------------------------------------ #
with tabs[3]:
    st.caption("Batterie automatique de scénarios, classée par impact décroissant sur la NAV.")
    worst = engine.worst_scenarios(stress_battery(portfolio.base_currency), n=10)
    risk_rail([(r.scenario.name, r.total_impact_pct) for r in worst])

    choice = st.selectbox("Comprendre un scénario", [r.scenario.name for r in worst], key="worst_detail")
    chosen = next(r for r in worst if r.scenario.name == choice)
    st.dataframe(
        pd.DataFrame([
            {"position": c.position_name, "classe": c.asset_class.value, "choc": c.shock_label,
             "poids": c.weight, "impact": c.impact_pct, "formule": c.formula,
             "confiance": c.confidence.value}
            for c in chosen.top_contributors(15)
        ]),
        width="stretch", hide_index=True,
    )
    confidence_note(chosen.confidence, chosen.coverage, chosen.assumptions)
