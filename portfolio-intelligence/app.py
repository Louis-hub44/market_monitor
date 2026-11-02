"""Portfolio Intelligence & Risk Platform — point d'entrée Streamlit.

Lancement :  streamlit run app.py

Le fichier ne contient que du câblage : navigation, panneau latéral et
chargement du portefeuille. Toute la logique métier est dans `services/`.
"""

from __future__ import annotations

import logging
from pathlib import Path

import streamlit as st

from components.ui import inject_css, quality_badge, theme_toggle
from data.network import discover_ca_bundles
from config.settings import get_settings
from models.enums import PortfolioType
from services.portfolio.loader import load_portfolio
from utils.state import clear_derived, current_portfolio, market_data, set_portfolio

st.set_page_config(
    page_title="Portfolio Intelligence",
    page_icon=":material/monitoring:",
    layout="wide",
    initial_sidebar_state="expanded",
)

logging.basicConfig(level=get_settings().log_level)
inject_css()

SAMPLES = {
    "Actions — Europe/US": "sample_equity_portfolio.csv",
    "Obligataire — souverain & crédit": "sample_fixed_income_portfolio.csv",
    "Diversifié — multi-actifs": "sample_multi_asset_portfolio.csv",
}


def sidebar() -> None:
    settings = get_settings()
    with st.sidebar:
        st.markdown('<div class="pi-eyebrow">Portfolio Intelligence</div>', unsafe_allow_html=True)
        st.caption("Plateforme interne d'analyse et de risque")
        theme_toggle()

        st.divider()
        st.markdown("**Portefeuille**")

        uploaded = st.file_uploader("Importer un fichier", type=["csv", "xlsx", "xls"],
                                    label_visibility="collapsed")
        forced = st.selectbox(
            "Type de portefeuille",
            ["Détection automatique"] + [t.value for t in PortfolioType],
            help="La détection s'appuie sur la part obligataire et actions du fichier.",
        )
        forced_type = None if forced == "Détection automatique" else PortfolioType(forced)

        quantity_modes = {
            "Détection automatique": None,
            "Nominal / nb de titres (× prix)": False,
            "Valorisation déjà calculée": True,
        }
        quantity_choice = st.selectbox(
            "Colonne de quantité", list(quantity_modes),
            help="Beaucoup d'exports nomment « nominal » une colonne qui contient déjà "
                 "la valeur de marché. La détection s'appuie sur la « rondeur » des "
                 "valeurs : un nominal est rond, une valorisation traîne des centimes.",
        )
        quantity_means_value = quantity_modes[quantity_choice]

        if uploaded is not None and st.button("Charger le fichier", type="primary", width="stretch"):
            portfolio, report = load_portfolio(
                uploaded.getvalue(), file_name=uploaded.name,
                base_currency=settings.base_currency, portfolio_type=forced_type,
                quantity_means_value=quantity_means_value,
            )
            if report.ok:
                set_portfolio(portfolio, report)
                st.success(f"{report.rows_kept} lignes chargées ({portfolio.portfolio_type.value})")
            else:
                st.error("Import impossible : " + " ; ".join(report.errors[:3]))

        sample = st.selectbox("Modèle d'exemple", ["—"] + list(SAMPLES))
        if sample != "—" and st.button("Charger le modèle", width="stretch"):
            path = Path(__file__).parent / "samples" / SAMPLES[sample]
            portfolio, report = load_portfolio(path, file_name=path.name,
                                               base_currency=settings.base_currency,
                                               portfolio_type=forced_type,
                                               quantity_means_value=quantity_means_value)
            set_portfolio(portfolio, report)
            st.info("Modèle de démonstration : positions fictives, données de marché réelles.")

        portfolio = current_portfolio()
        if portfolio is not None:
            st.divider()
            st.markdown(f"**{portfolio.name}**")
            st.caption(
                f"{portfolio.portfolio_type.value} · {len(portfolio.positions)} lignes · "
                f"NAV {portfolio.nav:,.0f} {portfolio.base_currency}".replace(",", " ")
            )
            if st.button("Recalculer", width="stretch"):
                clear_derived()
                st.rerun()

        st.divider()
        st.markdown("**Sources de données**")
        md = market_data()
        health = md.health()
        for provider in health:
            state = "connecté" if provider["configured"] else "non configuré"
            if provider["provider"] == "BLOOMBERG" and not provider["configured"]:
                state = "Terminal absent"
            st.markdown(
                f"{provider['provider']} · {state} "
                + (quality_badge("UNVERIFIED", provider["last_error"] or "")
                   if provider["last_error"] else ""),
                unsafe_allow_html=True,
            )
        if any(p.get("ssl_blocked") for p in health):
            st.error(
                "Certificat SSL non validé sur toutes les sources : un proxy "
                "d'entreprise intercepte le trafic. Renseignez le certificat de "
                "l'entreprise ci-dessous (ou installez le paquet truststore)."
            )

        connection_panel(md)


def connection_panel(md) -> None:
    """Réglages de connexion : clé API et politique SSL, applicables à chaud."""
    with st.expander("Connexion", expanded=not md.fmp.is_configured):

        # ---- clé FMP ------------------------------------------------- #
        st.markdown("**Clé API Financial Modeling Prep**")
        st.caption(f"Actuelle : {md.api_key_display()} — source : {md.api_key_source()}")

        key_input = st.text_input(
            "Clé FMP", type="password", placeholder="coller la clé ici",
            label_visibility="collapsed", key="fmp_key_input",
        )
        persist_key = st.checkbox(
            "Mémoriser sur ce poste", value=False, key="fmp_key_persist",
            help="Écrit la clé en clair dans .local_config.json (ignoré par git). "
                 "À éviter sur un poste partagé — sans cette option, la clé ne "
                 "vit qu'en mémoire, le temps de la session.",
        )
        c1, c2 = st.columns(2)
        if c1.button("Appliquer", width="stretch", key="apply_key"):
            if key_input.strip():
                md.set_api_key(key_input, persist=persist_key)
                st.success("Clé appliquée.")
            else:
                st.warning("Aucune clé saisie.")
        if c2.button("Tester", width="stretch", key="test_key"):
            with st.spinner("Appel de test…"):
                ok, message = md.fmp.verify_key()
            (st.success if ok else st.error)(message)

        if md.fmp.api_key_override and st.button("Retirer la clé saisie",
                                                 width="stretch"):
            md.set_api_key(None)
            md.forget_local_config()
            st.info("Clé retirée. La variable d'environnement reprend la main si elle existe.")

        st.divider()

        # ---- politique SSL ------------------------------------------- #
        st.markdown("**Vérification SSL**")
        st.caption(md.network_summary())

        ca_choices = ["Aucun (magasin de certificats du poste)"] + discover_ca_bundles()
        current_ca = md.ca_bundle or ""
        if current_ca and current_ca not in ca_choices:
            ca_choices.insert(1, current_ca)
        selected_ca = st.selectbox(
            "Certificat d'entreprise", ca_choices,
            index=ca_choices.index(current_ca) if current_ca in ca_choices else 0,
            format_func=lambda p: p if p.startswith("Aucun") else Path(p).name,
            help="Solution recommandée : la vérification reste active et le "
                 "proxy est reconnu.",
        )
        custom_ca = st.text_input("…ou chemin manuel", value="", placeholder="/chemin/entreprise.pem")

        persist_net = st.checkbox("Mémoriser ce réglage réseau", value=False, key="net_persist")

        if st.button("Appliquer les réglages réseau", type="primary", width="stretch"):
            ca = custom_ca.strip() or (selected_ca if not selected_ca.startswith("Aucun") else "")
            md.set_network(ca, persist=persist_net)
            if ca:
                st.success(f"Certificat appliqué : {Path(ca).name}")
            else:
                st.success("Magasin de certificats du poste rétabli.")

        if st.button("Diagnostiquer la connexion", width="stretch"):
            with st.spinner("Sondage des fournisseurs…"):
                diagnostic = md.network_diagnostic()
            (st.success if diagnostic["kind"] == "ok" else st.error)(diagnostic["message"])
            if diagnostic["hint"]:
                st.caption(diagnostic["hint"])


sidebar()

pages = [
    st.Page("views/dashboard.py", title="Dashboard", icon=":material/dashboard:", default=True),
    st.Page("views/portfolio_view.py", title="Portefeuille", icon=":material/table_rows:"),
    st.Page("views/equity.py", title="Equity", icon=":material/trending_up:"),
    st.Page("views/fixed_income.py", title="Fixed Income", icon=":material/show_chart:"),
    st.Page("views/diversified.py", title="Diversifié", icon=":material/donut_large:"),
    st.Page("views/events.py", title="Événements", icon=":material/event:"),
    st.Page("views/data_quality.py", title="Qualité des données", icon=":material/verified:"),
]
st.navigation(pages).run()
