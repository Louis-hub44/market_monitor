"""Dashboard global — « qu'est-ce qui peut impacter mon portefeuille aujourd'hui ? »

Organisation de la page, du plus général au plus fin :

1. **bande d'identité** — quel portefeuille, quelle NAV, quel arrêté ;
2. **performance et risque** — deux lignes d'indicateurs, chiffres alignés ;
3. **trajectoire** — NAV et drawdown sur le même axe temporel ;
4. **allocation** — une barre empilée, plus lisible qu'un camembert ;
5. **risques dominants** — classement de scénarios, puis décomposition du
   scénario sélectionné ;
6. **sensibilité taux et événements** — les deux déclencheurs les plus courants
   d'un mouvement de NAV à court terme.

Les blocs coûteux en appels réseau (historiques, calendrier) sont derrière un
bouton : la page doit s'ouvrir instantanément.
"""

from __future__ import annotations

import streamlit as st

from components.charts import (
    allocation_bar,
    donut,
    impact_bars,
    nav_area,
    ranked_impacts,
    waterfall,
)
from components.ui import (
    allocation_legend,
    block_header,
    confidence_note,
    empty_state,
    fmt,
    identity_band,
    kpi_row,
    page_header,
    warning_list,
)
from services.events.radar import build_radar
from services.portfolio.analytics import build_snapshot
from services.scenarios.library import stress_battery
from utils.state import fixed_income_analytics, market_data, require_portfolio, scenario_engine

page_header("Vue d'ensemble", "Dashboard",
            "Performance, expositions, risques dominants et événements à venir")

portfolio = require_portfolio()
if portfolio is None:
    st.stop()

md = market_data()
load_history = st.session_state.get("pi_dash_history", False)

with st.spinner("Valorisation…"):
    snapshot = build_snapshot(portfolio, md, with_history=load_history)

perf = snapshot.performance

# ------------------------------------------------------------------ #
# 1. Identité du portefeuille
# ------------------------------------------------------------------ #
identity_band(
    name=portfolio.name,
    meta=(f"{portfolio.portfolio_type.value.replace('_', ' ')} · "
          f"{len(portfolio.positions)} lignes · "
          f"arrêté au {portfolio.as_of.isoformat() if portfolio.as_of else '—'}"),
    cells=[
        ("NAV notionnelle" if portfolio.is_notional else "NAV",
         fmt(snapshot.nav, "money") + f" {portfolio.base_currency}"),
        ("Trésorerie", fmt(portfolio.cash / snapshot.nav if snapshot.nav else 0, "pct0", 1)),
        ("Top 10", fmt(snapshot.concentration["top10_weight"], "pct0", 1)),
        ("N effectif", f"{snapshot.concentration['effective_n']:.1f}"),
    ],
)

if portfolio.is_notional:
    st.caption(
        "Le fichier ne fournissait que des poids : la NAV est une base notionnelle. "
        "Les ratios et les impacts en pourcentage sont exacts ; les montants absolus "
        "(valorisations, DV01) sont conventionnels."
    )

# ------------------------------------------------------------------ #
# 2. Performance et risque
# ------------------------------------------------------------------ #
block_header(
    "Performance",
    f"couverture des historiques {snapshot.performance_coverage:.0%}" if perf
    else "historiques non chargés",
)

kpi_row([
    {"label": "YTD", "value": fmt(perf.ytd if perf else None),
     "positive": (perf.ytd or 0) >= 0 if perf and perf.ytd is not None else None},
    {"label": "1 mois", "value": fmt(perf.m1 if perf else None),
     "positive": (perf.m1 or 0) >= 0 if perf and perf.m1 is not None else None},
    {"label": "3 mois", "value": fmt(perf.m3 if perf else None),
     "positive": (perf.m3 or 0) >= 0 if perf and perf.m3 is not None else None},
    {"label": "1 an", "value": fmt(perf.y1 if perf else None),
     "positive": (perf.y1 or 0) >= 0 if perf and perf.y1 is not None else None},
    {"label": "Volatilité 1 an", "value": fmt(perf.volatility if perf else None, "pct0", 1)},
])

kpi_row([
    {"label": "Max drawdown", "value": fmt(perf.max_drawdown if perf else None),
     "positive": False if perf and perf.max_drawdown is not None else None},
    {"label": "VaR 95 % · 1 j", "value": fmt(perf.var_95_1d if perf else None)},
    {"label": "Sharpe", "value": fmt(perf.sharpe, "num") if perf and perf.sharpe else "—",
     "foot": "taux sans risque non fourni" if perf and perf.sharpe is None else ""},
    {"label": "Poids max", "value": fmt(snapshot.concentration["max_weight"], "pct0", 1),
     "foot": "concentration élevée" if snapshot.concentration["max_weight"] > 0.10 else ""},
    {"label": "Prix rafraîchis", "value": fmt(snapshot.refreshed_share, "pct0", 0),
     "foot": "part des lignes cotées"},
])

# ------------------------------------------------------------------ #
# 3. Trajectoire
# ------------------------------------------------------------------ #
if not load_history:
    if st.button("Charger les historiques de prix"):
        st.session_state["pi_dash_history"] = True
        st.rerun()
    st.caption("Performance, volatilité et drawdown nécessitent un historique par ligne.")
elif not snapshot.nav_series.empty:
    block_header("Trajectoire",
                 f"{perf.observations} observations · NAV reconstituée à poids constants")
    st.plotly_chart(nav_area(snapshot.nav_series), width="stretch",
                    config={"displayModeBar": False})
else:
    empty_state("Aucun historique n'a pu être récupéré pour les lignes de ce portefeuille.")

# ------------------------------------------------------------------ #
# 4. Allocation
# ------------------------------------------------------------------ #
block_header("Allocation")
asset_mix = {k: v for k, v in snapshot.exposures["classe d'actif"].items() if v > 0}
st.plotly_chart(allocation_bar(asset_mix), width="stretch",
                config={"displayModeBar": False})
allocation_legend(asset_mix)

with st.expander("Détail des expositions"):
    axes = [axis for axis in snapshot.exposures if axis != "classe d'actif"]
    tabs = st.tabs([axis.capitalize() for axis in axes])
    for tab, axis in zip(tabs, axes):
        with tab:
            data = {k: v for k, v in snapshot.exposures[axis].items() if v > 0}
            if not data:
                st.caption("Axe non renseigné dans le fichier portefeuille.")
                continue
            left, right = st.columns([1, 1])
            with left:
                st.plotly_chart(donut(dict(list(data.items())[:10]), height=280),
                                width="stretch", config={"displayModeBar": False})
            with right:
                allocation_legend(dict(list(data.items())[:12]))

st.divider()

# ------------------------------------------------------------------ #
# 5. Risques dominants
# ------------------------------------------------------------------ #
block_header("Principaux risques",
             "impacts estimés à partir des expositions réelles — pas des prévisions")

engine = scenario_engine(portfolio)
results = engine.worst_scenarios(stress_battery(portfolio.base_currency), n=8)
chiffrable = [r for r in results if abs(r.total_impact_pct) > 1e-9]

if not chiffrable:
    empty_state(
        "Aucun scénario n'a pu être chiffré.",
        "Les obligations nécessitent coupon et maturité ; les actions, un secteur ou un bêta.",
    )
else:
    left, right = st.columns([1.15, 1])
    with left:
        st.plotly_chart(
            ranked_impacts([(r.scenario.name, r.total_impact_pct) for r in chiffrable],
                           height=320),
            width="stretch", config={"displayModeBar": False},
        )
    with right:
        selected = st.selectbox("Décomposer un scénario", [r.scenario.name for r in chiffrable])
        chosen = next(r for r in chiffrable if r.scenario.name == selected)
        by_shock = chosen.by_shock()
        if len(by_shock) > 1:
            st.plotly_chart(waterfall(list(by_shock), list(by_shock.values()), height=250),
                            width="stretch", config={"displayModeBar": False})
        else:
            st.plotly_chart(impact_bars(chosen.by_asset_class(), height=250),
                            width="stretch", config={"displayModeBar": False})
        confidence_note(chosen.confidence, chosen.coverage, chosen.assumptions)

    with st.expander(f"Contributions ligne à ligne — {chosen.scenario.name}"):
        st.dataframe(
            [{"position": c.position_name, "classe": c.asset_class.value,
              "choc": c.shock_label, "poids": c.weight, "impact": c.impact_pct,
              "formule": c.formula} for c in chosen.top_contributors(12)],
            width="stretch", hide_index=True,
        )

st.divider()

# ------------------------------------------------------------------ #
# 6. Sensibilité taux et événements
# ------------------------------------------------------------------ #
col_fi, col_events = st.columns([1, 1])

with col_fi:
    block_header("Sensibilité taux")
    fi = fixed_income_analytics(portfolio)
    if fi.sleeve_value:
        kpi_row([
            {"label": "Duration modifiée", "value": fmt(fi.modified_duration, "num"),
             "foot": f"poche {fi.sleeve_value / snapshot.nav:.0%} · couverture {fi.coverage:.0%}"},
            {"label": "Convexité", "value": fmt(fi.convexity, "num", 1)},
            {"label": "DV01", "value": fmt(fi.dv01, "money")},
        ])
        krd = {k: v for k, v in fi.key_rate_durations.items() if abs(v) > 1e-9}
        if krd:
            st.plotly_chart(impact_bars(krd, height=210), width="stretch",
                            config={"displayModeBar": False})
            st.caption("Key rate durations, en années — détail dans Fixed Income.")
    else:
        st.caption("Pas de poche obligataire identifiée dans ce portefeuille.")

with col_events:
    block_header("Événements à venir", "14 jours")
    if st.session_state.get("pi_dash_events"):
        events, radar_warnings = build_radar(portfolio, md, days_ahead=14)
        if events:
            for event in events[:6]:
                st.markdown(
                    f"<div class='pi-rail'><div class='r-name'>"
                    f"<b>{event.when.strftime('%d/%m')}</b> · {event.title[:42]}</div>"
                    f"<div class='r-bar'><div class='r-fill' "
                    f"style='width:{event.impact_score}%'></div></div>"
                    f"<div class='r-val'>{event.impact_score}</div></div>",
                    unsafe_allow_html=True,
                )
            st.caption("Score = importance de l'événement × exposition du portefeuille.")
        else:
            empty_state("Aucun événement relié au portefeuille sur la période.")
        warning_list(radar_warnings)
    elif st.button("Charger le calendrier"):
        st.session_state["pi_dash_events"] = True
        st.rerun()

warning_list(snapshot.warnings)
