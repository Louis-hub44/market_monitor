"""Start screen: choose the data source and the SSL policy **before** any data request.

Behind a corporate SSL-inspection proxy every online source fails at once; this gate lets
the user pick the trust source (automatic, Windows store or corporate certificate) first,
test the sources, then launch the loading. Certificate verification is never disabled. The choice lives in ``st.session_state`` and can be
changed from the sidebar or from the certificate-error banner.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

import streamlit as st

from app import theme
from rates_analytics.config import Settings
from rates_analytics.exceptions import ConfigurationError
from rates_analytics.network import (
    NetworkSettings,
    check_connectivity,
    count_ssl_failures,
    discover_ca_bundles,
    proxy_env,
)

STATE_KEY = "rba-connection"

PROVIDERS = {
    "free": "Sources gratuites",
    "fmp": "FMP + gratuites",
    "bloomberg": "Bloomberg",
    "static": "Données d'exemple",
}
PROVIDER_HELP = {
    "free": "ECB Data Portal (€STR, courbe AAA) + CNBC / Bundesbank / Stooq (taux souverains génériques). "
            "Sans cotations de swap publiques, les courbes €STR / EURIBOR sont des proxys signalés comme tels.",
    "fmp": "Financial Modeling Prep (clé FMP_API_KEY dans .env) puis les sources gratuites.",
    "bloomberg": "Desktop API (terminal ouvert sur ce poste) : source de référence, cotations de swap réelles.",
    "static": "Instantané illustratif hors ligne (data/sample). Aucun appel réseau.",
}
MODES = {
    "auto": "Automatique",
    "system": "Magasin Windows",
    "ca": "Certificat d'entreprise",
    "standard": "Standard",
}
MODE_HELP = {
    "auto": "Magasin de certificats du poste (truststore), complété du certificat d'entreprise s'il est "
            "configuré ; sinon certificats publics. Recommandé, au bureau comme à la maison.",
    "system": "Vérification SSL active avec le magasin de certificats de Windows, où l'informatique installe "
              "le certificat du proxy. À essayer en premier au bureau : aucune configuration.",
    "ca": "Vérification SSL active avec le certificat racine du proxy (fichier .pem / .crt fourni par "
          "l'informatique).",
    "standard": "Vérification SSL normale (certifi). À la maison ou hors proxy d'entreprise.",
}
KIND_LABELS = {"ok": "OK", "ssl": "Certificat", "proxy": "Proxy", "proxy_auth": "Proxy (407)",
               "timeout": "Délai", "blocked": "Filtré", "dns": "DNS", "http": "Source en erreur", "error": "Erreur"}


@dataclass(frozen=True)
class Connection:
    """What the user chose on the start screen."""

    provider: str
    network: NetworkSettings

    @property
    def offline(self) -> bool:
        return self.provider == "static"


# ====================================================================== state
def active_connection() -> Connection | None:
    value = st.session_state.get(STATE_KEY)
    return value if isinstance(value, Connection) else None


def launch(connection: Connection) -> None:
    st.session_state[STATE_KEY] = connection


def back_to_gate() -> None:
    st.session_state.pop(STATE_KEY, None)


def network_from(mode: str, ca_path: str) -> NetworkSettings:
    if mode == "ca":
        path = ca_path.strip().strip('"')
        return NetworkSettings(mode="ca", ca_bundle=Path(path)) if path else NetworkSettings()
    return NetworkSettings(mode=mode if mode in MODES else "auto")


# ======================================================================= gate
def render_gate(settings: Settings) -> Connection | None:
    """Draw the start screen; return the connection once "Lancer" has been clicked."""
    chosen = active_connection()
    if chosen is not None:
        return chosen
    st.markdown(
        '<div class="rba-hero"><div class="rba-brand-sub">Mini terminal · module taux</div>'
        '<div class="rba-brand">Rates &amp; Bond Analytics</div>'
        '<div class="rba-hero-txt">Choisissez la source et la connexion avant de charger les marchés. '
        "Aucune donnée n'est demandée tant que vous n'avez pas cliqué sur <b>Lancer le chargement</b>.</div></div>",
        unsafe_allow_html=True)
    _, centre, _ = st.columns([1, 2.6, 1])
    with centre, st.container(border=True):
        st.markdown('<div class="rba-card-title">1 · Source de données</div>', unsafe_allow_html=True)
        default_provider = settings.data.provider if settings.data.provider in PROVIDERS else "free"
        provider = st.segmented_control("Source", list(PROVIDERS), default=default_provider, required=True,
                                        format_func=lambda p: PROVIDERS[p], label_visibility="collapsed",
                                        key="rba-provider") or default_provider
        st.caption(PROVIDER_HELP[provider])

        network = NetworkSettings()
        if provider != "static":
            st.markdown('<div class="rba-card-title">2 · Connexion réseau</div>', unsafe_allow_html=True)
            default_mode = settings.network.mode if settings.network.mode in MODES else "auto"
            mode = st.segmented_control("Mode", list(MODES), default=default_mode, required=True,
                                        format_func=lambda m: MODES[m], label_visibility="collapsed",
                                        key="rba-net-mode") or default_mode
            st.caption(MODE_HELP[mode])
            ca_path = _ca_input(settings) if mode == "ca" else ""
            try:
                network = network_from(mode, ca_path)
            except ConfigurationError as exc:
                st.error(str(exc))
            proxies = proxy_env()
            if proxies:
                st.caption("Proxy détecté dans l'environnement : " + ", ".join(sorted(proxies)) + ".")

        test_col, start_col = st.columns(2)
        test = test_col.button("Tester les sources", icon=":material/network_check:", width="stretch",
                               key="rba-test", disabled=provider == "static")
        bad_ca = network.mode == "ca" and not (network.ca_bundle and network.ca_bundle.is_file())
        start = start_col.button("Lancer le chargement", type="primary", icon=":material/play_arrow:",
                                 width="stretch", key="rba-start", disabled=bad_ca)
        if test:
            _render_diagnostic(network)
    if start:
        launch(Connection(provider, network))
        st.rerun()
    return None


def _ca_input(settings: Settings) -> str:
    found = discover_ca_bundles()
    default = str(settings.network.ca_bundle or (found[0] if found else ""))
    path = st.text_input("Chemin du certificat (.pem / .crt)", value=default, key="rba-ca",
                         placeholder=r"C:\ProgramData\Zscaler\ZscalerRootCertificate.pem")
    if found:
        st.caption("Certificats trouvés sur ce poste : " + " · ".join(found))
    clean = path.strip().strip('"')
    if clean and not os.path.isfile(clean):
        st.error("Fichier introuvable.")
    return path


def _render_diagnostic(network: NetworkSettings) -> None:
    with st.spinner("Test des sources de données…"):
        try:
            diag = check_connectivity(network, timeout=8)
        except ConfigurationError as exc:
            st.error(str(exc))
            return
    st.markdown(diagnostic_html(diag["results"]), unsafe_allow_html=True)
    message = diag["message"][:1].upper() + diag["message"][1:] + "."
    if diag["kind"] == "ok":
        st.success(message)
    else:
        st.error(message)
        if diag["hint"]:
            st.caption(diag["hint"])


def diagnostic_html(results: Mapping[str, Mapping[str, Any]]) -> str:
    """One chip per probed source (pure, unit-tested)."""
    chips = []
    for name, res in results.items():
        cls = "ok" if res["ok"] else "ko"
        label = KIND_LABELS.get(str(res["kind"]), str(res["kind"]))
        if res.get("status") and res["kind"] != "ok":
            label += f' ({res["status"]})'
        latency = f' · {res["latency_ms"]} ms' if res["ok"] else ""
        chips.append(f'<span class="rba-probe {cls}" title="{escape(str(res["message"]))}">'
                     f"<b>{escape(name)}</b> {escape(label)}{latency}</span>")
    return f'<div class="rba-probes">{"".join(chips)}</div>'


# ==================================================================== sidebar
def render_sidebar_status(connection: Connection) -> None:
    st.markdown('<div class="rba-side-title">Connexion</div>', unsafe_allow_html=True)
    kind = "ok"
    label = "Hors ligne" if connection.offline else connection.network.label
    st.markdown(theme.chip(PROVIDERS.get(connection.provider, connection.provider), "") + " "
                + theme.chip(label, "" if connection.offline else kind), unsafe_allow_html=True)
    if st.button("Changer la source / connexion", icon=":material/tune:", width="stretch", key="rba-change"):
        back_to_gate()
        st.rerun()


def render_ssl_rescue(errors: Mapping[str, str], connection: Connection) -> None:
    """Banner offering another SSL mode when most data failures are certificate errors."""
    n_ssl = count_ssl_failures(errors)
    if not n_ssl or n_ssl * 2 < len(errors):
        return
    with st.container(border=True):
        text, b1, b2 = st.columns([3, 1, 1], vertical_alignment="center")
        text.markdown(f"**Proxy d'inspection SSL détecté** — {n_ssl} série(s) refusée(s) sur le certificat.")
        if connection.network.mode != "system" and b1.button("Magasin Windows", icon=":material/verified_user:",
                                                             width="stretch", key="rba-rescue-system"):
            launch(Connection(connection.provider, NetworkSettings(mode="system")))
            st.rerun()
        if b2.button("Certificat d'entreprise", type="primary", icon=":material/badge:", width="stretch",
                     key="rba-rescue-ca"):
            back_to_gate()
            st.rerun()
