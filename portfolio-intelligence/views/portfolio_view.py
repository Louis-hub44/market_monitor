"""Détail du portefeuille : lignes, expositions, concentration, rapport d'import."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from components.charts import bar_breakdown, heatmap
from components.ui import empty_state, fmt, kpi_row, page_header, warning_list
from services.portfolio.analytics import correlations, refresh_prices
from utils.state import import_report, market_data, require_portfolio

page_header("Portefeuille", "Composition et expositions", "Vue commune aux trois modules")

portfolio = require_portfolio()
if portfolio is None:
    st.stop()

concentration = portfolio.concentration()
kpi_row([
    {"label": "NAV", "value": fmt(portfolio.nav, "money") + f" {portfolio.base_currency}"},
    {"label": "Lignes", "value": str(concentration["n_positions"])},
    {"label": "Poids max", "value": fmt(concentration["max_weight"], "pct0")},
    {"label": "Top 10", "value": fmt(concentration["top10_weight"], "pct0")},
    {"label": "HHI", "value": fmt(concentration["hhi"], "num", 3),
     "foot": f"N effectif {concentration['effective_n']:.1f}"},
])

if concentration["max_weight"] > 0.10:
    st.warning(
        f"Concentration significative : la première ligne pèse {concentration['max_weight']:.1%} de la NAV."
    )

tabs = st.tabs(["Positions", "Expositions", "Corrélations", "Rapport d'import"])

with tabs[0]:
    if st.button("Rafraîchir les prix auprès des providers"):
        with st.spinner("Récupération des cours…"):
            quotes = refresh_prices(portfolio, market_data(), cross_check=True)
        ok = sum(1 for q in quotes.values() if q.is_usable)
        st.success(f"{ok}/{len(quotes)} lignes rafraîchies.")

    weights = portfolio.weights()
    frame = pd.DataFrame([
        {
            "identifiant": p.identifier,
            "libellé": p.name,
            "classe": p.asset_class.value,
            "devise": p.currency,
            "prix": p.price,
            "valorisation": round(p.market_value_base, 2),
            "poids": weights.get(p.identifier, 0.0),
            "secteur": p.sector,
            "pays": p.country,
            "coupon": p.coupon,
            "maturité": p.maturity,
            "qualité prix": p.quality.value,
        }
        for p in portfolio.positions
    ])
    st.dataframe(
        frame.sort_values("poids", ascending=False),
        width="stretch", hide_index=True,
        column_config={"poids": st.column_config.NumberColumn(format="%.2f%%")},
    )
    st.download_button("Exporter en CSV", frame.to_csv(index=False).encode("utf-8"),
                       file_name=f"{portfolio.name}_positions.csv", mime="text/csv")

with tabs[1]:
    cols = st.columns(2)
    axes = {
        "classe d'actif": portfolio.exposure_by_asset_class(),
        "secteur": portfolio.exposure_by_sector(),
        "pays": portfolio.exposure_by_country(),
        "devise": portfolio.exposure_by_currency(),
    }
    for i, (label, data) in enumerate(axes.items()):
        with cols[i % 2]:
            st.plotly_chart(bar_breakdown(data, f"Par {label}"), width="stretch")

with tabs[2]:
    if st.button("Calculer la matrice de corrélation (top 20 lignes)"):
        with st.spinner("Récupération des historiques…"):
            corr = correlations(portfolio, market_data())
        if corr.empty:
            empty_state("Historiques insuffisants pour calculer des corrélations.")
        else:
            st.plotly_chart(heatmap(corr, "Corrélations des rendements quotidiens",
                                    percent=False, diverging=True), width="stretch")
    else:
        st.caption("Le calcul nécessite un historique par ligne : lancez-le explicitement.")

with tabs[3]:
    report = import_report()
    if report is None:
        empty_state("Aucun rapport d'import disponible.")
    else:
        st.write(f"**Fichier** : {report.file_name} — {report.rows_kept}/{report.rows_read} lignes retenues")
        st.write(f"**Type détecté** : {report.detected_type.value if report.detected_type else '—'}")
        st.write("**Colonnes reconnues**")
        st.json(report.column_mapping, expanded=False)
        if report.unmapped_columns:
            st.caption("Colonnes ignorées : " + ", ".join(map(str, report.unmapped_columns)))
        warning_list(report.errors + report.inferences)

warning_list(portfolio.warnings)
