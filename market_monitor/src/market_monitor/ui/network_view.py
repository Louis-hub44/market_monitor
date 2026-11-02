"""Network start screen: choose the SSL policy *before* any data request is sent.

Behind a corporate SSL-inspection proxy the first data load fails on every source;
the gate lets the user pick "corporate certificate" or "bypass SSL" first, test the
connection, then start loading. The choice lives in ``st.session_state`` and can be
changed at any time from the sidebar or from the certificate-error banner.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from html import escape
from pathlib import Path
from typing import Any

import requests
import streamlit as st

from market_monitor.config import NetworkSettings, Settings
from market_monitor.network import (
    check_connectivity,
    configure_session,
    count_ssl_failures,
    discover_ca_bundles,
    proxy_env,
)

MODES = {
    "standard": "Connexion standard",
    "ca": "Certificat d'entreprise",
    "insecure": "Contourner le SSL",
}
MODE_HELP = {
    "standard": "Vérification SSL normale. À utiliser hors du réseau de l'entreprise.",
    "ca": "Vérification SSL active, en reconnaissant le certificat racine du proxy (fichier .pem "
          "ou .crt fourni par l'informatique). Solution la plus sûre.",
    "insecure": "Vérification SSL désactivée pour toutes les sources de données. Uniquement sur le réseau "
                "de confiance de l'entreprise, jamais sur un Wi-Fi public.",
}
KIND_LABELS = {"ok": "OK", "ssl": "Certificat", "proxy": "Bloqué par le proxy", "timeout": "Délai",
               "blocked": "Filtré", "dns": "DNS", "error": "Erreur"}


# ====================================================================== state
def _state_key(key: str) -> str:
    return f"{key}-network"


def active_network(key: str = "mm") -> NetworkSettings | None:
    """Network policy chosen on the start screen, ``None`` until the user starts loading."""
    value = st.session_state.get(_state_key(key))
    return value if isinstance(value, NetworkSettings) else None


def start_loading(network: NetworkSettings, key: str = "mm") -> None:
    st.session_state[_state_key(key)] = network


def back_to_gate(key: str = "mm") -> None:
    st.session_state.pop(_state_key(key), None)


def mode_of(network: NetworkSettings) -> str:
    if network.ca_bundle is not None:
        return "ca"
    return "insecure" if network.insecure_ssl else "standard"


def network_from(mode: str, ca_path: str) -> NetworkSettings:
    if mode == "ca":
        path = ca_path.strip().strip('"')
        return NetworkSettings(ca_bundle=Path(path) if path else None)
    return NetworkSettings(insecure_ssl=mode == "insecure")


def status_label(network: NetworkSettings) -> str:
    """Short label for the top bar / sidebar chip."""
    if network.ca_bundle is not None:
        return f"Certificat · {network.ca_bundle.name}"
    return "SSL contourné" if network.insecure_ssl else "Connexion standard"


# ======================================================================= gate
def render_network_gate(settings: Settings, key: str = "mm") -> NetworkSettings | None:
    """Draw the start screen; return the chosen policy once "Lancer" has been clicked.

    No data request is sent before that click: the caller stops the script on ``None``.
    """
    chosen = active_network(key)
    if chosen is not None:
        return chosen

    st.markdown(
        '<div class="mm-hero"><div class="mm-brand">Market Monitor</div>'
        '<div class="mm-hero-sub">Choisissez la connexion avant de charger les marchés. '
        "Aucune donnée n'est demandée tant que vous n'avez pas cliqué sur "
        "<b>Lancer le chargement</b>.</div></div>",
        unsafe_allow_html=True,
    )
    _, centre, _ = st.columns([1, 2.4, 1])
    with centre, st.container(border=True):
        st.markdown('<div class="mm-card-title">Connexion réseau</div>', unsafe_allow_html=True)
        mode = st.segmented_control(
            "Mode", list(MODES), default=mode_of(settings.network), required=True,
            format_func=lambda m: MODES[m], label_visibility="collapsed", key=f"{key}-net-mode",
        ) or "standard"
        st.caption(MODE_HELP[mode])
        ca_path = ""
        if mode == "ca":
            ca_path = _ca_input(settings, key)
        elif mode == "insecure":
            st.warning("La vérification des certificats sera désactivée pour cette session.")
        network = network_from(mode, ca_path)

        proxies = proxy_env()
        if proxies:
            st.caption("Proxy détecté dans l'environnement : " + ", ".join(sorted(proxies)) + ".")

        test_col, start_col = st.columns(2)
        test = test_col.button("Tester la connexion", icon=":material/network_check:",
                               width="stretch", key=f"{key}-net-test")
        start = start_col.button("Lancer le chargement", type="primary", icon=":material/play_arrow:",
                                 width="stretch", key=f"{key}-net-start",
                                 disabled=mode == "ca" and not _bundle_ok(network))
        if test:
            _render_diagnostic(network)
    if start:
        start_loading(network, key)
        st.rerun()
    return None


def _ca_input(settings: Settings, key: str) -> str:
    found = discover_ca_bundles()
    default = str(settings.network.ca_bundle or (found[0] if found else ""))
    path = st.text_input("Chemin du certificat (.pem / .crt)", value=default, key=f"{key}-net-ca",
                         placeholder=r"C:\ProgramData\Zscaler\ZscalerRootCertificate.pem")
    if found:
        st.caption("Certificats trouvés sur ce poste : " + " · ".join(found))
    clean = path.strip().strip('"')
    if clean and not os.path.isfile(clean):
        st.error("Fichier introuvable.")
    return path


def _bundle_ok(network: NetworkSettings) -> bool:
    return network.ca_bundle is not None and network.ca_bundle.is_file()


def _render_diagnostic(network: NetworkSettings) -> None:
    session = configure_session(requests.Session(), network.insecure_ssl, network.ca_bundle)
    with st.spinner("Test des sources de données…"):
        try:
            diag = check_connectivity(session, timeout=8, parallel=True)
        finally:
            session.close()
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
        chips.append(f'<span class="mm-probe {cls}" title="{escape(str(res["message"]))}">'
                     f"<b>{escape(name)}</b> {escape(label)}{latency}</span>")
    return f'<div class="mm-probes">{"".join(chips)}</div>'


# ==================================================================== sidebar
def render_network_sidebar(network: NetworkSettings, key: str = "mm") -> None:
    """Current policy + "change" button, at the bottom of the sidebar."""
    with st.sidebar:
        st.markdown('<div class="mm-side-title">Connexion</div>', unsafe_allow_html=True)
        cls = "warn" if network.insecure_ssl and network.ca_bundle is None else "ok"
        st.markdown(f'<span class="mm-chip {cls}">{escape(status_label(network))}</span>',
                    unsafe_allow_html=True)
        if st.button("Changer la connexion", icon=":material/tune:", width="stretch",
                     key=f"{key}-net-change"):
            back_to_gate(key)
            st.rerun()


def render_ssl_rescue(errors: Mapping[str, str], key: str = "mm") -> None:
    """Banner offering the bypass when most failures are certificate errors."""
    n_ssl = count_ssl_failures(errors)
    if not n_ssl or n_ssl * 2 < len(errors):
        return
    network = active_network(key)
    if network is None:  # page embedded without the start screen: nothing to switch
        return
    if network.insecure_ssl and network.ca_bundle is None:
        st.error(f"{n_ssl} série(s) en échec de certificat malgré le contournement SSL : "
                 "vérifiez HTTPS_PROXY ou le pare-feu.")
        return
    with st.container(border=True):
        text, button = st.columns([3, 1], vertical_alignment="center")
        text.markdown(f"**Proxy d'inspection SSL détecté** — {n_ssl} série(s) refusée(s) sur le "
                      "certificat. Les données affichées viennent du cache.")
        if button.button("Contourner le SSL", type="primary", icon=":material/lock_open:",
                         width="stretch", key=f"{key}-net-rescue"):
            start_loading(NetworkSettings(insecure_ssl=True), key)
            st.rerun()
