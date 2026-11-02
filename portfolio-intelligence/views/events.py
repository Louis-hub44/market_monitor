"""Event Radar : calendrier relié aux positions du portefeuille."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from components.ui import empty_state, kpi_row, page_header, quality_badge, warning_list
from services.events.radar import build_radar, to_rows
from utils.state import market_data, require_portfolio

page_header("Événements", "Event Radar",
            "Publications de résultats, banques centrales et statistiques reliées au portefeuille")

portfolio = require_portfolio()
if portfolio is None:
    st.stop()

horizon = st.slider("Horizon (jours)", 7, 60, 21, step=7)

if st.button("Actualiser le calendrier", type="primary") or st.session_state.get("pi_events_loaded"):
    st.session_state["pi_events_loaded"] = True
    with st.spinner("Collecte des calendriers…"):
        events, warnings = build_radar(portfolio, market_data(), days_ahead=horizon)

    if not events:
        empty_state("Aucun événement relié au portefeuille sur la période.",
                    "Les résultats ne sont rapprochés que pour les tickers présents dans le portefeuille.")
    else:
        kpi_row([
            {"label": "Événements", "value": str(len(events))},
            {"label": "Résultats", "value": str(sum(1 for e in events if e.category == "EARNINGS"))},
            {"label": "Banques centrales", "value": str(sum(1 for e in events if e.category == "CENTRAL_BANK"))},
            {"label": "Score max", "value": str(max(e.impact_score for e in events))},
        ])

        categories = st.multiselect("Catégories", sorted({e.category for e in events}),
                                    default=sorted({e.category for e in events}))
        filtered = [e for e in events if e.category in categories]

        for event in filtered[:40]:
            with st.container(border=True):
                c1, c2 = st.columns([3, 1])
                with c1:
                    st.markdown(
                        f"**{event.title}** · {event.when.isoformat()} (J+{event.days_ahead})  \n"
                        f"<span class='mut'>{event.category}"
                        + (f" · facteur {event.factor}" if event.factor else "")
                        + f" · poids portefeuille {event.portfolio_weight:.2%}</span>",
                        unsafe_allow_html=True,
                    )
                    if event.detail:
                        st.caption(" · ".join(f"{k} : {v}" for k, v in event.detail.items() if v is not None))
                with c2:
                    st.markdown(
                        f"<div class='pi-kpi'><div class='k-label'>Score d'impact</div>"
                        f"<div class='k-value'>{event.impact_score}</div>"
                        f"<div class='k-foot'>{quality_badge(event.quality)}</div></div>",
                        unsafe_allow_html=True,
                    )
                    st.caption(event.score_formula)

        frame = pd.DataFrame(to_rows(filtered))
        st.download_button("Exporter le calendrier", frame.to_csv(index=False).encode("utf-8"),
                           file_name="event_radar.csv", mime="text/csv")

    warning_list(warnings)
else:
    st.caption("Le calendrier interroge les providers : lancez la collecte explicitement.")
