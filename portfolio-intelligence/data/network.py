# -*- coding: utf-8 -*-
"""Accès réseau derrière un proxy d'inspection SSL d'entreprise.

Le problème
-----------
Sur un réseau d'entreprise équipé d'un proxy d'inspection SSL (Zscaler,
Netskope, Fortinet, Palo Alto…), le proxy intercepte le trafic HTTPS et le
re-signe avec un certificat interne absent du magasin de confiance de Python :

    SSLError: certificate verify failed: self signed certificate in
    certificate chain

Symptôme dans cette plateforme : **tous** les providers tombent en même temps
et pour le même motif. Sans correctif, l'application bascule intégralement sur
le cache — elle reste utilisable, mais plus rien n'est frais. Le
`DataPoint` le dira honnêtement (qualité CACHED), ce qui est le comportement
correct, mais ce n'est pas une situation de travail acceptable.

``yfinance`` mérite un traitement à part : il s'appuie en interne sur
``curl_cffi`` (impersonation de navigateur) pour contourner la détection
anti-bot de Yahoo. Il accepte cependant n'importe quelle session explicitement
transmise — ``curl_cffi.requests.Session`` **ou** ``requests.Session``. On lui
impose donc une session ``requests`` standard, dont le comportement
``verify=False`` / ``verify=<chemin CA>`` est stable et documenté, sans dépendre
des particularités internes de ``curl_cffi``. La perte de l'empreinte TLS
« navigateur » est compensée par un ``User-Agent`` réaliste.

Ordre de préférence
-------------------
1. **Certificat CA de l'entreprise** (``ca_bundle``) — la vérification reste
   active, le proxy est simplement reconnu. C'est la seule option réellement
   sûre, et celle à demander à l'équipe informatique.
2. **Vérification désactivée** (``insecure_ssl``) — dépannage sur un réseau
   d'entreprise de confiance uniquement. Jamais sur un réseau public, jamais
   sur un poste qui manipule des ordres.
3. **Aucune personnalisation** — comportement natif.

La même configuration s'applique à tous les providers REST (FMP) et à Yahoo :
le proxy est un problème de poste, pas un problème de source.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

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

#: Emplacements usuels d'un magasin de certificats d'entreprise, proposés dans
#: l'interface pour éviter à l'utilisateur de chercher le chemin.
COMMON_CA_PATHS = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/usr/local/share/ca-certificates/corporate.crt",
    "/usr/local/share/ca-certificates/zscaler.crt",
    "/Library/Application Support/Netskope/STAgent/data/nscacert.pem",
    "/Library/Application Support/Zscaler/ZscalerRootCertificate.pem",
    r"C:\ProgramData\corporate-ca.pem",
    r"C:\ProgramData\Zscaler\ZscalerRootCertificate.pem",
    r"C:\ProgramData\Netskope\STAgent\data\nscacert.pem",
)

_TRUE_VALUES = {"1", "true", "yes", "oui", "on"}


# --------------------------------------------------------------------------- #
# Environnement
# --------------------------------------------------------------------------- #
def env_defaults() -> tuple[bool, str]:
    """Configuration réseau lue depuis l'environnement.

    Permet de fixer le réglage une fois pour toutes au niveau du poste ou du
    conteneur, plutôt que de le ressaisir dans l'interface ::

        PI_INSECURE_SSL=1
        PI_CA_BUNDLE=/chemin/vers/entreprise.pem

    ``REQUESTS_CA_BUNDLE`` et ``SSL_CERT_FILE`` — variables standard souvent
    déjà positionnées par les équipes informatiques — sont également reconnues.

    Returns:
        Le couple ``(insecure_ssl, ca_bundle)``.
    """
    insecure = any(
        os.environ.get(name, "").strip().lower() in _TRUE_VALUES
        for name in ("PI_INSECURE_SSL", "INSECURE_SSL")
    )
    ca_bundle = (
        os.environ.get("PI_CA_BUNDLE")
        or os.environ.get("CA_BUNDLE")
        or os.environ.get("REQUESTS_CA_BUNDLE")
        or os.environ.get("SSL_CERT_FILE")
        or ""
    ).strip()
    return insecure, ca_bundle


def discover_ca_bundles() -> list[str]:
    """Magasins de certificats effectivement présents sur la machine."""
    found = [path for path in COMMON_CA_PATHS if os.path.isfile(path)]
    env_bundle = env_defaults()[1]
    if env_bundle and os.path.isfile(env_bundle) and env_bundle not in found:
        found.insert(0, env_bundle)
    return found


def proxy_env() -> dict[str, str]:
    """Variables de proxy actives, pour affichage dans le diagnostic."""
    names = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
             "NO_PROXY", "no_proxy")
    return {name: os.environ[name] for name in names if os.environ.get(name)}


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def configure_session(session: requests.Session, insecure_ssl: bool = False,
                      ca_bundle: str | None = None) -> requests.Session:
    """Applique la politique SSL de l'entreprise à une session existante.

    Args:
        session: session à configurer, modifiée sur place.
        insecure_ssl: désactive la vérification des certificats. À réserver à un
            réseau d'entreprise de confiance — jamais sur un réseau public.
        ca_bundle: chemin d'un fichier PEM. S'il est fourni et valide, il
            **l'emporte sur** ``insecure_ssl`` : la vérification reste active et
            reconnaît en plus cette autorité.

    Returns:
        La session, configurée.
    """
    ca_bundle = (ca_bundle or "").strip() or None
    if ca_bundle and not os.path.isfile(ca_bundle):
        logger.warning("Certificat CA introuvable, réglage ignoré : %s", ca_bundle)
        ca_bundle = None

    # User-Agent réaliste : compense la perte de l'empreinte TLS « navigateur »
    # quand on remplace curl_cffi par requests. Les providers REST peuvent le
    # surcharger ensuite par leur propre identifiant.
    session.headers.update({"User-Agent": CHROME_USER_AGENT})

    if ca_bundle:
        session.verify = ca_bundle
        logger.info("Session réseau avec certificat CA d'entreprise : %s", ca_bundle)
    elif insecure_ssl:
        session.verify = False
        silence_insecure_warnings()
        logger.warning(
            "Vérification SSL désactivée pour les appels de données "
            "(réseau de confiance uniquement)."
        )
    else:
        # Réinitialisation explicite : la fonction peut être rappelée à chaud
        # pour *réactiver* la vérification. Sans cette branche, le bascule
        # serait à sens unique — une session dégradée le resterait.
        session.verify = True
    return session


def build_session(insecure_ssl: bool = False, ca_bundle: str | None = None):
    """Session dédiée à Yahoo/yfinance.

    Returns:
        Une ``requests.Session`` configurée, ou ``None`` si aucune
        personnalisation n'est demandée — auquel cas yfinance conserve son
        comportement natif (``curl_cffi`` avec impersonation de navigateur),
        qui passe mieux les protections anti-bot de Yahoo.
    """
    ca_bundle = (ca_bundle or "").strip() or None
    if ca_bundle and not os.path.isfile(ca_bundle):
        ca_bundle = None
    if not (insecure_ssl or ca_bundle):
        return None
    return configure_session(requests.Session(), insecure_ssl, ca_bundle)


def silence_insecure_warnings() -> None:
    """Coupe le flot d'avertissements urllib3 quand la vérification est levée."""
    try:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:  # pragma: no cover - urllib3 absent ou API modifiée
        pass


def apply_to_yfinance(session) -> bool:
    """Impose la session au singleton HTTP de yfinance.

    Passer ``session=`` à chaque ``yf.Ticker`` ne suffit pas partout : certains
    appels (téléchargements groupés, fondamentaux) passent par le singleton
    ``YfData``. On le configure donc explicitement, en tolérant les évolutions
    d'API d'une version à l'autre.

    Returns:
        ``True`` si la session a bien été prise en compte.
    """
    if session is None:
        return False
    try:
        from yfinance.data import YfData

        data = YfData(session=session)
        if getattr(data, "_session", None) is not session:
            data._set_session(session)        # noqa: SLF001 - pas d'API publique
        return getattr(data, "_session", None) is session
    except Exception as exc:                  # noqa: BLE001
        logger.warning("Session non appliquée à yfinance : %s", exc)
        return False


def describe(insecure_ssl: bool, ca_bundle: str | None) -> str:
    """Résumé lisible de la configuration active, affiché dans l'interface."""
    ca_bundle = (ca_bundle or "").strip()
    if ca_bundle and os.path.isfile(ca_bundle):
        return f"Certificat d'entreprise reconnu ({os.path.basename(ca_bundle)})"
    if insecure_ssl:
        return "Vérification SSL désactivée — réseau de confiance uniquement"
    return "Connexion directe (vérification SSL standard)"


# --------------------------------------------------------------------------- #
# Diagnostic
# --------------------------------------------------------------------------- #
def looks_like_ssl_error(message: Any) -> bool:
    """Détecte un échec de validation de certificat dans un message d'erreur.

    Sert à transformer une erreur technique en action : quand tous les
    fournisseurs échouent pour ce motif, l'interface propose le correctif au
    lieu d'afficher une trace.
    """
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
        ``{ok, status, latency_ms, kind, message, hint}`` où ``kind`` vaut
        ``ok``, ``ssl``, ``proxy``, ``timeout``, ``blocked``, ``dns`` ou
        ``error``, et ``hint`` donne l'action à mener.
    """
    started = time.perf_counter()
    close_after = session is None
    if session is None:
        session = configure_session(requests.Session(), *env_defaults())

    try:
        response = session.get(url, timeout=timeout)
        latency = (time.perf_counter() - started) * 1000
        if response.status_code in (401, 403):
            return {
                "ok": False, "status": response.status_code,
                "latency_ms": round(latency), "kind": "blocked",
                "message": f"Le serveur répond {response.status_code} "
                           f"(authentification refusée ou filtrage).",
                "hint": "Le réseau atteint bien le fournisseur : le problème "
                        "vient de la clé API ou d'un filtrage applicatif, pas "
                        "du proxy.",
            }
        if response.status_code == 429:
            return {
                "ok": False, "status": 429, "latency_ms": round(latency),
                "kind": "blocked",
                "message": "Quota atteint (HTTP 429).",
                "hint": "Réduisez MAX_WORKERS ou attendez la fenêtre suivante.",
            }
        return {
            "ok": True, "status": response.status_code,
            "latency_ms": round(latency), "kind": "ok",
            "message": f"Connexion établie en {latency:.0f} ms "
                       f"(HTTP {response.status_code}).",
            "hint": "",
        }
    except Exception as exc:  # noqa: BLE001 - on qualifie tous les cas
        latency = (time.perf_counter() - started) * 1000
        text = f"{type(exc).__name__}: {exc}"
        low = text.lower()
        if looks_like_ssl_error(text):
            kind, hint = "ssl", (
                "Un proxy d'inspection SSL réécrit le trafic. Renseignez le "
                "certificat de votre entreprise (PI_CA_BUNDLE), ou activez le "
                "contournement si le réseau est de confiance."
            )
        elif "proxy" in low:
            kind, hint = "proxy", (
                "Un proxy est requis. Renseignez HTTPS_PROXY dans "
                "l'environnement avant de lancer l'application."
            )
        elif "timeout" in low or "timed out" in low:
            kind, hint = "timeout", (
                "Délai dépassé : le pare-feu bloque probablement la sortie."
            )
        elif "name or service not known" in low or "nodename nor servname" in low \
                or "getaddrinfo" in low:
            kind, hint = "dns", (
                "Résolution DNS impossible. Vérifiez la connexion réseau ou la "
                "configuration du proxy."
            )
        else:
            kind, hint = "error", "Vérifiez l'accès sortant du poste."
        return {"ok": False, "status": None, "latency_ms": round(latency),
                "kind": kind, "message": text[:300], "hint": hint}
    finally:
        if close_after:
            try:
                session.close()
            except Exception:  # pragma: no cover
                pass


def check_connectivity(session=None, timeout: int = 12) -> dict[str, Any]:
    """Sonde tous les fournisseurs et synthétise le diagnostic.

    Returns:
        ``{results: {source: diagnostic}, kind, message, hint, ssl_blocked}``.
        ``kind`` reflète la cause **dominante** : si les deux sources échouent
        sur un certificat, le diagnostic pointe le proxy plutôt que les
        fournisseurs.
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
        hint = ("Renseignez le certificat racine de votre entreprise ci-dessous. "
                "Le contournement n'est à utiliser qu'en dépannage.")
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
