# -*- coding: utf-8 -*-
"""Accès réseau derrière un proxy d'inspection SSL d'entreprise.

Toutes les requêtes de la plateforme passent par **la session HTTP unique du terminal**
(:mod:`terminal_core.network`, partagée avec Rates & Bond Analytics et le moteur de stress).
Ce module n'en est qu'une couche de compatibilité :

* **proxy** : ``TERMINAL_HTTPS_PROXY`` / ``TERMINAL_NO_PROXY``, sinon ``HTTPS_PROXY`` / ``NO_PROXY`` ;
* **confiance TLS** : magasin de certificats du poste (``truststore``), complété du certificat
  de l'entreprise s'il est fourni (``PI_CA_BUNDLE`` ou ``TERMINAL_CA_BUNDLE``) ; sinon ce
  certificat fusionné avec certifi ; sinon certifi ;
* **jamais** ``verify=False`` : l'ancien contournement ``PI_INSECURE_SSL`` est ignoré (avec un
  avertissement), et la session refuse toute désactivation de la vérification.

yfinance reçoit la même session (singleton ``YfData``) : aucune requête ne lui échappe.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import requests
from terminal_core import network as core
from terminal_core.check_network import Probe, probe
from terminal_core.network import apply_to_yfinance

logger = logging.getLogger(__name__)

CHROME_USER_AGENT = core.CHROME_USER_AGENT

#: Points de sondage : un par source, pour distinguer une panne globale
#: (proxy, pare-feu) d'une indisponibilité d'un seul fournisseur.
PROBES: dict[str, str] = {
    "FMP": "https://financialmodelingprep.com/stable/profile?symbol=AAPL",
    "YAHOO": "https://query2.finance.yahoo.com/v1/test/getcrumb",
}

#: Fragments qui identifient sans ambiguïté un échec de validation de certificat.
SSL_ERROR_MARKERS = (
    "self signed certificate",
    "self-signed certificate",
    "certificate verify failed",
    "ssl certificate problem",
    "sslerror",
    "ssl: certificate",
    "unable to get local issuer certificate",
    "curl: (60)",
    "openssl verify result",
    "certificate_verify_failed",
    "sslcertverification",
    "tlsv1 alert",
)

COMMON_CA_PATHS = core.COMMON_CA_PATHS

SSL_HINT = ("Un proxy d'inspection SSL réécrit le trafic. Installez le paquet truststore (magasin "
            "de certificats du poste) ou renseignez le certificat racine de votre entreprise "
            "(PI_CA_BUNDLE ou TERMINAL_CA_BUNDLE).")


# --------------------------------------------------------------------------- #
# Réglages et session
# --------------------------------------------------------------------------- #
def env_ca_bundle() -> str:
    """Certificat d'entreprise déclaré dans l'environnement (chemin existant, sinon "")."""
    settings = core.NetworkSettings.from_env()
    return str(settings.ca_bundle) if settings.ca_bundle else ""


def network_settings(ca_bundle: str | None = None) -> core.NetworkSettings:
    """Réglages du terminal, avec éventuellement un certificat choisi dans l'interface."""
    base = core.NetworkSettings.from_env()
    path = (ca_bundle or "").strip()
    if not path:
        return base
    if not os.path.isfile(path):
        logger.warning("Certificat CA introuvable, réglage ignoré : %s", path)
        return base
    return core.with_overrides(base, ca_bundle=Path(path))


def session_for(ca_bundle: str | None = None) -> requests.Session:
    """Session partagée du terminal pour ce certificat (``None`` = réglage de l'environnement)."""
    return core.get_session(network_settings(ca_bundle))


def describe(ca_bundle: str | None = None) -> str:
    """Résumé lisible de la configuration active, affiché dans l'interface."""
    path = (ca_bundle or "").strip()
    if path and not os.path.isfile(path):
        return f"Certificat introuvable ({path}) — vérification SSL avec le magasin du poste"
    return "Vérification SSL active — " + core.describe(network_settings(ca_bundle))["trust"]


def discover_ca_bundles() -> list[str]:
    return core.discover_ca_bundles()


def proxy_env() -> dict[str, str]:
    """Variables de proxy actives (mot de passe masqué)."""
    return core.proxy_env()


# --------------------------------------------------------------------------- #
# Diagnostic
# --------------------------------------------------------------------------- #
def looks_like_ssl_error(message: Any) -> bool:
    """Détecte un échec de validation de certificat dans un message d'erreur."""
    if not message:
        return False
    text = str(message).lower()
    return any(marker in text for marker in SSL_ERROR_MARKERS)


def count_ssl_failures(failures: dict[str, str]) -> int:
    """Nombre d'échecs imputables au certificat, parmi un dictionnaire d'erreurs."""
    return sum(1 for reason in failures.values() if looks_like_ssl_error(reason))


def check_endpoint(url: str, session=None, timeout: int = 12) -> dict[str, Any]:
    """Teste un point d'accès et **qualifie** la panne.

    Returns:
        ``{ok, status, latency_ms, kind, message, hint}``, ``kind`` parmi ``ok``, ``ssl``,
        ``proxy``, ``proxy_auth``, ``blocked``, ``timeout``, ``dns``, ``http`` ou ``error``.
    """
    session = session or session_for()
    r = probe(Probe(url, url, True, ""), session, timeout)
    if r.ok and r.status == 401:
        return {"ok": False, "status": 401, "latency_ms": r.latency_ms, "kind": "blocked",
                "message": "Le serveur répond 401 (authentification refusée).",
                "hint": "Le réseau atteint bien le fournisseur : le problème vient de la clé API, pas du proxy."}
    if r.ok:
        return {"ok": True, "status": r.status, "latency_ms": r.latency_ms, "kind": "ok",
                "message": f"Connexion établie en {r.latency_ms} ms (HTTP {r.status}).", "hint": ""}
    hint = SSL_HINT if r.kind == "ssl" else r.hint
    return {"ok": False, "status": r.status, "latency_ms": r.latency_ms, "kind": r.kind,
            "message": r.message, "hint": hint}


def check_connectivity(session=None, timeout: int = 12) -> dict[str, Any]:
    """Sonde tous les fournisseurs et synthétise le diagnostic.

    Returns:
        ``{results: {source: diagnostic}, kind, message, hint, ssl_blocked}``. ``kind`` reflète
        la cause **dominante** : si les sources échouent toutes sur un certificat, le diagnostic
        pointe le proxy plutôt que les fournisseurs.
    """
    results = {name: check_endpoint(url, session, timeout) for name, url in PROBES.items()}
    kinds = [r["kind"] for r in results.values()]
    ssl_blocked = all(k == "ssl" for k in kinds) if kinds else False

    if all(k == "ok" for k in kinds):
        kind, message, hint = "ok", "Toutes les sources sont joignables.", ""
    elif ssl_blocked:
        kind = "ssl"
        message = ("Toutes les sources échouent sur la validation du certificat : "
                   "un proxy d'inspection SSL intercepte le trafic.")
        hint = "Renseignez le certificat racine de votre entreprise ci-dessous."
    elif any(k == "ok" for k in kinds):
        failed = [n for n, r in results.items() if not r["ok"]]
        kind = "partial"
        message = f"Accès réseau opérationnel, mais {', '.join(failed)} ne répond pas."
        hint = next(r["hint"] for r in results.values() if not r["ok"])
    else:
        first = next(iter(results.values()))
        kind, message, hint = first["kind"], first["message"], first["hint"]

    return {"results": results, "kind": kind, "message": message,
            "hint": hint, "ssl_blocked": ssl_blocked}


__all__ = [
    "PROBES", "SSL_HINT", "apply_to_yfinance", "check_connectivity", "check_endpoint", "count_ssl_failures",
    "describe", "discover_ca_bundles", "env_ca_bundle", "looks_like_ssl_error", "network_settings",
    "proxy_env", "session_for",
]
