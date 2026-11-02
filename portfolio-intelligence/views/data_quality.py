"""Page Qualité des données : provenance, fraîcheur, disponibilité des endpoints."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from components.ui import empty_state, kpi_row, page_header, quality_badge
from config.constants import load_model_assumptions
from config.settings import get_settings
from data.network import discover_ca_bundles, proxy_env
from models.enums import DataQuality
from utils.state import current_portfolio, market_data

page_header("Qualité des données", "Provenance et fiabilité",
            "Aucune donnée synthétique n'est présentée comme réelle")

settings = get_settings()
md = market_data()

st.markdown("#### Niveaux de qualité")
levels = {
    DataQuality.VERIFIED: "Source officielle, ou deux sources indépendantes concordantes.",
    DataQuality.API_CONFIRMED: "Source financière fiable, source unique.",
    DataQuality.CACHED: "Dernière donnée réelle connue ; le provider était indisponible.",
    DataQuality.ESTIMATED: "Valeur déduite d'un modèle ou d'une autre donnée (ex. YTM déduit du prix).",
    DataQuality.UNVERIFIED: "Fiabilité insuffisante — la donnée n'est pas utilisée pour décider.",
}
for level, description in levels.items():
    st.markdown(f"{quality_badge(level)} {description}", unsafe_allow_html=True)

st.divider()
st.markdown("#### Réseau")
st.caption(
    "Derrière un proxy d'inspection SSL d'entreprise (Zscaler, Netskope, Fortinet…), "
    "toutes les sources échouent simultanément sur la validation du certificat. "
    "Le diagnostic ci-dessous distingue ce cas d'une panne de fournisseur."
)
st.markdown(f"Configuration active : **{md.network_summary()}**")

proxies = proxy_env()
if proxies:
    st.caption("Proxy détecté : " + " · ".join(f"{k}={v}" for k, v in proxies.items()))

if st.button("Diagnostiquer la connexion"):
    with st.spinner("Sondage des fournisseurs…"):
        diagnostic = md.network_diagnostic()
    if diagnostic["kind"] == "ok":
        st.success(diagnostic["message"])
    elif diagnostic["kind"] == "ssl":
        st.error(diagnostic["message"])
    else:
        st.warning(diagnostic["message"])
    if diagnostic["hint"]:
        st.info(diagnostic["hint"])
    st.dataframe(
        pd.DataFrame([
            {"source": name, "joignable": r["ok"], "code": r["status"],
             "latence_ms": r["latency_ms"], "cause": r["kind"], "détail": r["message"][:120]}
            for name, r in diagnostic["results"].items()
        ]),
        width="stretch", hide_index=True,
    )
    st.session_state["pi_ssl_blocked"] = diagnostic["ssl_blocked"]

if st.session_state.get("pi_ssl_blocked") or any(h.get("ssl_blocked") for h in md.health()):
    with st.expander("Corriger l'accès derrière un proxy d'entreprise", expanded=True):
        st.markdown(
            "**Correction immédiate** — panneau latéral → **Connexion** → "
            "*Vérification SSL*. Le réglage s'applique à chaud, sans redémarrage."
        )
        st.markdown(
            "**1. Certificat de l'entreprise — solution recommandée.** La vérification "
            "SSL reste active, le proxy est simplement reconnu. Demandez le certificat "
            "racine à votre équipe informatique."
        )
        bundles = discover_ca_bundles()
        if bundles:
            st.caption("Magasins détectés sur ce poste :")
            for path in bundles:
                st.code(path, language="text")
        else:
            st.caption("Aucun magasin de certificats reconnu automatiquement sur ce poste.")
        st.markdown(
            "**2. Contournement — dépannage uniquement.** Désactive la vérification des "
            "certificats. À n'utiliser que sur un réseau d'entreprise de confiance, "
            "jamais sur un réseau public ni sur un poste qui passe des ordres."
        )
        st.markdown("**Pour figer le réglage sans repasser par l'interface**, dans `.env` :")
        st.code("PI_CA_BUNDLE=/chemin/vers/entreprise.pem\n# ou, en dépannage :\nPI_INSECURE_SSL=1",
                language="bash")

st.divider()
st.markdown("#### Bloomberg (Desktop API)")
bbg = md.bloomberg
c1, c2 = st.columns([2, 1])
with c1:
    if not bbg.library_available:
        st.warning(
            "Paquet `blpapi` absent. Installation depuis l'index Bloomberg :\n\n"
            "`pip install --index-url=https://blpapi.bloomberg.com/repository/"
            "releases/python/simple blpapi`"
        )
    elif not bbg.is_configured:
        st.info("Bloomberg désactivé (ENABLE_BLOOMBERG=false ou mode hors ligne).")
    else:
        st.success(f"Bibliothèque présente — cible {bbg.host}:{bbg.port}")
    st.caption(
        "La Desktop API exige un Terminal lancé et une session ouverte sur ce poste. "
        "Il n'y a ni clé ni jeton : les droits sont ceux de l'utilisateur connecté, "
        "et un champ hors abonnement revient vide plutôt qu'en erreur."
    )
with c2:
    if st.button("Tester Bloomberg", width="stretch", disabled=not bbg.library_available):
        with st.spinner("Interrogation du Terminal…"):
            results = bbg.self_test()
        st.dataframe(pd.DataFrame([{"capacité": k, "statut": v} for k, v in results.items()]),
                     width="stretch", hide_index=True)

st.divider()
st.markdown("#### Clé API")
c1, c2 = st.columns([2, 1])
c1.markdown(f"Clé FMP : **{md.api_key_display()}** — source : {md.api_key_source()}")
if c2.button("Vérifier la clé", width="stretch"):
    with st.spinner("Appel de test…"):
        valid, message = md.fmp.verify_key()
    (st.success if valid else st.error)(message)
st.caption("La clé se saisit dans le panneau latéral → Connexion. Elle n'est jamais "
           "journalisée ni affichée en entier.")

st.divider()
st.markdown("#### Providers")
cols = st.columns(len(md.health()))
for col, health in zip(cols, md.health()):
    with col:
        st.markdown(f"**{health['provider']}** — "
                    + ("configuré" if health["configured"] else "non configuré"))
        st.caption(f"cache : {health['cache']['entries']} entrées · {health['cache']['size_kb']} Ko · "
                   f"plus ancienne {health['cache']['oldest_age_h']} h")
        if health.get("session"):
            st.caption(f"session : {health['session']}")
        if health["last_error"]:
            st.error(health["last_error"])
            if health.get("ssl_blocked"):
                st.caption("Cause probable : proxy d'inspection SSL — voir la section Réseau.")

c1, c2 = st.columns(2)
if c1.button("Tester les endpoints FMP"):
    with st.spinner("Test en cours…"):
        results = md.fmp.self_test()
    st.dataframe(pd.DataFrame([{"endpoint": k, "statut": v} for k, v in results.items()]),
                 width="stretch", hide_index=True)
if c2.button("Vider le cache"):
    removed = md.fmp.cache.clear() + md.yahoo.cache.clear()
    st.success(f"{removed} entrées supprimées.")

st.divider()
st.markdown("#### Journal de collecte")
rows = md.journal_rows()
if rows:
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
else:
    st.caption("Aucun appel enregistré durant cette session.")

st.divider()
st.markdown("#### Qualité du portefeuille chargé")
portfolio = current_portfolio()
if portfolio is None:
    empty_state("Aucun portefeuille chargé.")
else:
    kpi_row([
        {"label": "Lignes", "value": str(len(portfolio.positions))},
        {"label": "Secteur renseigné", "value": f"{portfolio.coverage('sector'):.0%}"},
        {"label": "Pays renseigné", "value": f"{portfolio.coverage('country'):.0%}"},
        {"label": "Maturité renseignée", "value": f"{portfolio.coverage('maturity'):.0%}"},
        {"label": "Prix renseigné", "value": f"{portfolio.coverage('price'):.0%}"},
    ])
    counts = pd.Series([p.quality.value for p in portfolio.positions]).value_counts()
    st.bar_chart(counts)

st.divider()
st.markdown("#### Configuration active")
st.json(settings.masked(), expanded=False)

st.markdown("#### Hypothèses de modèle en vigueur")
st.caption("Utilisées uniquement en dernier recours ; tout résultat qui s'en sert est marqué "
           "« confiance faible ». Surchargeables via MODEL_ASSUMPTIONS_FILE.")
st.json(load_model_assumptions(), expanded=False)
