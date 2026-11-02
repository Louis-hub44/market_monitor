"""Module Equity : screener, valorisation, résultats, facteurs, risque."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from components.charts import bar_breakdown, impact_bars, valuation_history_chart
from components.ui import empty_state, fmt, kpi_row, page_header, quality_badge, risk_rail
from models.enums import AssetClass
from services.equity.earnings import consensus_revisions, earnings_history, next_earnings
from services.equity.factors import factor_profile, portfolio_beta
from services.equity.screener import ScreenerFilters, run_screener
from services.equity.valuation import valuation_history
from services.scenarios.library import commodity_scenarios, equity_scenarios, fx_scenarios
from utils.state import betas, market_data, require_portfolio, scenario_engine

page_header("Equity", "Analyse actions",
            "Quelles sociétés sont attractives, et quels sont les risques de la poche actions ?")

portfolio = require_portfolio()
if portfolio is None:
    st.stop()

md = market_data()
equities = portfolio.positions_of(AssetClass.EQUITY)
tabs = st.tabs(["Screener", "Valorisation", "Résultats & consensus", "Facteurs", "Risque & scénarios"])

# ------------------------------------------------------------------ #
with tabs[0]:
    st.caption("Le filtrage est exécuté côté provider ; l'enrichissement en ratios coûte un appel par société.")
    c1, c2, c3, c4 = st.columns(4)
    sector = c1.text_input("Secteur", placeholder="Technology")
    country = c2.text_input("Pays (code ISO)", placeholder="FR")
    exchange = c3.text_input("Place", placeholder="NASDAQ")
    limit = c4.number_input("Nombre de lignes", 10, 500, 100, step=10)
    c5, c6, c7 = st.columns(3)
    cap_min = c5.number_input("Capitalisation min (M)", 0, 5_000_000, 1_000, step=500)
    cap_max = c6.number_input("Capitalisation max (M, 0 = illimité)", 0, 10_000_000, 0, step=500)
    enrich = c7.checkbox("Enrichir avec les ratios (lent)", value=False)

    if st.button("Lancer le screener", type="primary"):
        filters = ScreenerFilters(
            sector=sector or None, country=country or None, exchange=exchange or None,
            market_cap_min=cap_min * 1e6 or None,
            market_cap_max=(cap_max * 1e6) if cap_max else None,
            limit=int(limit),
        )
        with st.spinner("Interrogation du provider…"):
            result = run_screener(filters, md, enrich_ratios=enrich)
        if not result.is_usable:
            empty_state("Aucun résultat.", result.note)
        else:
            st.markdown(quality_badge(result.quality, result.note), unsafe_allow_html=True)
            st.dataframe(result.value, width="stretch", hide_index=True)
            st.download_button("Exporter en CSV", result.value.to_csv(index=False).encode("utf-8"),
                               file_name="screener.csv", mime="text/csv")

# ------------------------------------------------------------------ #
with tabs[1]:
    universe = [p.identifier for p in equities] or []
    symbol = st.selectbox("Société", universe) if universe else st.text_input("Symbole", "AAPL")
    if symbol and st.button("Analyser la valorisation"):
        with st.spinner("Historique des multiples…"):
            dp = valuation_history(symbol, md)
        if not dp.is_usable:
            empty_state("Historique indisponible.", dp.note)
        else:
            for history in dp.value:
                st.markdown(f"**{history.label}** {quality_badge(history.quality)}", unsafe_allow_html=True)
                kpi_row([
                    {"label": "Actuel", "value": fmt(history.current, "num", 1)},
                    {"label": f"Moyenne {history.years} ans", "value": fmt(history.mean, "num", 1)},
                    {"label": "Minimum", "value": fmt(history.minimum, "num", 1)},
                    {"label": "Maximum", "value": fmt(history.maximum, "num", 1)},
                    {"label": "Décote / prime", "value": fmt(history.discount_vs_mean),
                     "positive": (history.discount_vs_mean or 0) < 0},
                ])
                st.plotly_chart(
                    valuation_history_chart(history.series, history.current, history.mean, history.label),
                    width="stretch",
                )

# ------------------------------------------------------------------ #
with tabs[2]:
    universe = [p.identifier for p in equities]
    symbol = st.selectbox("Société ", universe, key="earnings_symbol") if universe else st.text_input("Symbole ", "AAPL")
    if symbol and st.button("Charger les publications"):
        nxt = next_earnings(symbol, md)
        if nxt.is_usable:
            record = nxt.value
            kpi_row([
                {"label": "Prochaine publication", "value": record.date.isoformat() if record.date else "—",
                 "quality": nxt.quality},
                {"label": "EPS consensus", "value": fmt(record.eps_estimated, "num")},
                {"label": "CA consensus", "value": fmt(record.revenue_estimated, "money")},
            ])
        else:
            st.caption(nxt.note)

        hist = earnings_history(symbol, md)
        if hist.is_usable:
            rows = [
                {
                    "date": r.date.isoformat() if r.date else "—",
                    "EPS estimé": r.eps_estimated, "EPS publié": r.eps_actual,
                    "surprise EPS": r.eps_surprise,
                    "CA estimé": r.revenue_estimated, "CA publié": r.revenue_actual,
                    "surprise CA": r.revenue_surprise,
                }
                for r in hist.value if not r.is_future
            ]
            if rows:
                st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

        cons = consensus_revisions(symbol, md)
        if cons.is_usable:
            st.markdown("**Consensus par exercice**")
            st.dataframe(pd.DataFrame(cons.value), width="stretch", hide_index=True)
            st.caption(cons.note)
        else:
            st.caption(cons.note)

# ------------------------------------------------------------------ #
with tabs[3]:
    st.caption("Les bêtas sont estimés par régression sur les historiques réels ; "
               "les lignes sans historique suffisant sont exclues et signalées.")
    if st.button("Estimer les bêtas et le profil factoriel"):
        with st.spinner("Régressions sur historiques…"):
            estimates = betas(portfolio)
        beta, beta_coverage = portfolio_beta(portfolio, estimates)
        kpi_row([
            {"label": "Bêta poche actions", "value": fmt(beta, "num"),
             "foot": f"estimé sur {beta_coverage:.0%} de la poche"},
            {"label": "Lignes estimées", "value": f"{len(estimates)}/{len(equities)}"},
        ])
        if estimates:
            frame = pd.DataFrame([
                {"identifiant": e.identifier, "bêta": round(e.beta, 3),
                 "R²": round(e.r2, 3) if e.r2 else None, "proxy": e.proxy, "qualité": e.quality.value}
                for e in estimates.values()
            ])
            st.dataframe(frame.sort_values("bêta", ascending=False), width="stretch", hide_index=True)

        metrics = pd.DataFrame()  # alimenté par le screener enrichi ou un futur cache de ratios
        profile = factor_profile(metrics, portfolio.weights())
        if profile.scores:
            st.plotly_chart(impact_bars(profile.scores, "Exposition factorielle"), width="stretch")
        else:
            st.caption("Profil factoriel : ratios fondamentaux non chargés pour les lignes du portefeuille. "
                       "Utilisez le screener enrichi pour alimenter les scores.")

# ------------------------------------------------------------------ #
with tabs[4]:
    if not equities:
        empty_state("Aucune ligne actions dans ce portefeuille.")
    else:
        sleeve = sum(p.market_value_base for p in equities)
        kpi_row([
            {"label": "Poche actions", "value": fmt(sleeve / portfolio.nav, "pct0")},
            {"label": "Lignes", "value": str(len(equities))},
            {"label": "Top 10", "value": fmt(
                sum(w for _, w in portfolio.top_holdings(10)), "pct0")},
        ])
        c1, c2 = st.columns(2)
        with c1:
            st.plotly_chart(bar_breakdown(portfolio.exposure_by_sector(), "Exposition sectorielle"),
                            width="stretch")
        with c2:
            st.plotly_chart(bar_breakdown(portfolio.exposure_by_country(), "Exposition pays"),
                            width="stretch")

        st.markdown("#### Scénarios actions")
        engine = scenario_engine(portfolio)
        scenarios = equity_scenarios() + fx_scenarios(portfolio.base_currency) + commodity_scenarios()
        results = engine.run_all(scenarios)
        risk_rail([(r.scenario.name, r.total_impact_pct) for r in
                   sorted(results, key=lambda r: r.total_impact_pct)])
