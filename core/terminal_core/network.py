"""Session HTTP unique du terminal, derrière un proxy d'inspection SSL d'entreprise.

Tous les accès réseau du terminal (BCE, Bundesbank, Stooq, CNBC, FRED, FMP, Yahoo…) passent
par :func:`get_session`. La politique est fixée une fois, au même endroit :

* **proxy** : configurable (``TERMINAL_HTTPS_PROXY`` / ``TERMINAL_HTTP_PROXY`` /
  ``TERMINAL_NO_PROXY`` ou bloc ``network`` d'une configuration). À défaut, les variables
  standard ``HTTPS_PROXY`` / ``NO_PROXY`` s'appliquent ;
* **confiance TLS**, dans cet ordre (mode ``auto``) :

  1. ``truststore`` : magasin de certificats du système (Windows, macOS, Linux), où
     l'informatique déploie la racine du proxy. Un bundle CA d'entreprise fourni est
     **ajouté** à ce magasin ;
  2. sinon le **bundle CA de l'entreprise** (``TERMINAL_CA_BUNDLE``, ou les variables
     historiques des projets, ou ``REQUESTS_CA_BUNDLE`` / ``SSL_CERT_FILE``), fusionné avec
     ``certifi`` pour rester valable hors du proxy ;
  3. sinon ``certifi``.

* **jamais** ``verify=False`` : :class:`SecureSession` refuse la désactivation de la
  vérification, à la construction comme à chaque requête (:class:`InsecureTLSError`).
  Les anciens réglages de contournement (``PI_INSECURE_SSL``, ``MARKET_MONITOR_INSECURE_SSL``,
  ``RATES_NETWORK_MODE=insecure``) sont ignorés, avec un avertissement.

Le module n'importe rien du terminal : il est partagé par tous les projets.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import ssl
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import certifi
import requests
from requests.adapters import HTTPAdapter
from requests.utils import should_bypass_proxies

logger = logging.getLogger(__name__)

TRUST_MODES = ("auto", "system", "ca", "certifi")

CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

ENV_HTTPS_PROXY = "TERMINAL_HTTPS_PROXY"
ENV_HTTP_PROXY = "TERMINAL_HTTP_PROXY"
ENV_NO_PROXY = "TERMINAL_NO_PROXY"
ENV_CA_BUNDLE = "TERMINAL_CA_BUNDLE"
ENV_TRUST = "TERMINAL_TRUST"
ENV_TIMEOUT = "TERMINAL_TIMEOUT_S"

#: variables CA des projets, par priorité décroissante, puis variables standard
LEGACY_CA_VARS = ("PI_CA_BUNDLE", "RATES_CA_BUNDLE", "MARKET_MONITOR_CA_BUNDLE")
STANDARD_CA_VARS = ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE")
#: anciens contournements, désormais refusés
LEGACY_INSECURE_VARS = ("PI_INSECURE_SSL", "MARKET_MONITOR_INSECURE_SSL")
LEGACY_MODE_VAR = "RATES_NETWORK_MODE"
_LEGACY_MODES = {"system": "system", "ca": "ca", "standard": "certifi"}

_TRUE = frozenset({"1", "true", "yes", "oui", "on"})

#: emplacements usuels d'un certificat racine d'entreprise (proposés par le diagnostic)
COMMON_CA_PATHS = (
    r"C:\ProgramData\Zscaler\ZscalerRootCertificate.pem",
    r"C:\ProgramData\Netskope\STAgent\data\nscacert.pem",
    r"C:\ProgramData\corporate-ca.pem",
    "/Library/Application Support/Zscaler/ZscalerRootCertificate.pem",
    "/Library/Application Support/Netskope/STAgent/data/nscacert.pem",
    "/usr/local/share/ca-certificates/zscaler.crt",
    "/usr/local/share/ca-certificates/corporate.crt",
)


class NetworkConfigurationError(ValueError):
    """Configuration réseau invalide (mode inconnu, certificat introuvable, contournement demandé)."""


class InsecureTLSError(NetworkConfigurationError):
    """Tentative de désactiver la vérification des certificats (``verify=False``)."""


# ===================================================================== réglages
@dataclass(frozen=True)
class NetworkSettings:
    """Politique réseau : proxy, confiance TLS, délai.

    Attributes:
        https_proxy, http_proxy: URL du proxy (``http://hôte:port``), ``None`` = variables
            standard de l'environnement.
        no_proxy: hôtes contournant le proxy, séparés par des virgules.
        ca_bundle: fichier PEM de l'autorité de l'entreprise.
        trust: ``auto`` | ``system`` | ``ca`` | ``certifi``.
        timeout_s: délai par défaut des requêtes.
        notes: avertissements de configuration (réglages ignorés…), affichés par le diagnostic.
    """

    https_proxy: str | None = None
    http_proxy: str | None = None
    no_proxy: str | None = None
    ca_bundle: Path | None = None
    trust: str = "auto"
    timeout_s: float = 20.0
    notes: tuple[str, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        if self.trust not in TRUST_MODES:
            if str(self.trust).lower() in ("insecure", "none", "off", "false", "0"):
                raise InsecureTLSError(
                    "La désactivation de la vérification SSL n'est pas supportée. "
                    "Utilisez trust='system' (magasin de certificats du poste) ou 'ca' (certificat "
                    "de l'entreprise).")
            raise NetworkConfigurationError(f"trust doit valoir l'un de {TRUST_MODES}, reçu {self.trust!r}")
        if self.timeout_s <= 0:
            raise NetworkConfigurationError("timeout_s doit être strictement positif")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None,
                 config: Mapping[str, Any] | None = None) -> NetworkSettings:
        """Réglages d'un bloc de configuration, surchargés par l'environnement.

        Clés du bloc : ``https_proxy`` (ou ``proxy``), ``http_proxy``, ``no_proxy``,
        ``ca_bundle``, ``trust``, ``timeout_s``.
        """
        env = os.environ if env is None else env
        cfg = dict(config or {})
        notes: list[str] = []

        def pick(var: str, key: str, *aliases: str) -> str | None:
            value = (env.get(var) or "").strip()
            if value:
                return value
            for k in (key, *aliases):
                raw = cfg.get(k)
                if raw not in (None, ""):
                    return str(raw).strip()
            return None

        https_proxy = pick(ENV_HTTPS_PROXY, "https_proxy", "proxy")
        http_proxy = pick(ENV_HTTP_PROXY, "http_proxy") or https_proxy
        no_proxy = pick(ENV_NO_PROXY, "no_proxy")

        trust = (pick(ENV_TRUST, "trust") or "").lower()
        legacy_mode = (env.get(LEGACY_MODE_VAR) or "").strip().lower()
        if not trust and legacy_mode:
            if legacy_mode in _LEGACY_MODES:
                trust = _LEGACY_MODES[legacy_mode]
            else:
                notes.append(f"{LEGACY_MODE_VAR}={legacy_mode} ignoré : la vérification SSL reste active.")
        for var in LEGACY_INSECURE_VARS:
            if (env.get(var) or "").strip().lower() in _TRUE:
                notes.append(f"{var} ignoré : la désactivation de la vérification SSL n'est plus supportée.")

        bundle_raw = pick(ENV_CA_BUNDLE, "ca_bundle")
        source = ENV_CA_BUNDLE if (env.get(ENV_CA_BUNDLE) or "").strip() else "configuration"
        if not bundle_raw:
            for var in (*LEGACY_CA_VARS, *STANDARD_CA_VARS):
                if (env.get(var) or "").strip():
                    bundle_raw, source = env[var].strip(), var
                    break
        bundle: Path | None = Path(bundle_raw).expanduser() if bundle_raw else None
        if bundle is not None and not bundle.is_file():
            if trust == "ca":
                raise NetworkConfigurationError(f"Certificat CA introuvable ({source}) : {bundle}")
            notes.append(f"Certificat CA introuvable ({source}), ignoré : {bundle}")
            bundle = None

        timeout = pick(ENV_TIMEOUT, "timeout_s")
        try:
            timeout_s = float(timeout) if timeout else 20.0
        except ValueError as exc:
            raise NetworkConfigurationError(f"timeout_s invalide : {timeout!r}") from exc

        for note in notes:
            logger.warning(note)
        return cls(https_proxy=https_proxy, http_proxy=http_proxy, no_proxy=no_proxy, ca_bundle=bundle,
                   trust=trust or "auto", timeout_s=timeout_s, notes=tuple(notes))


# ===================================================================== confiance TLS
def truststore_available() -> bool:
    try:
        import truststore  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass(frozen=True)
class TrustPlan:
    """Confiance TLS effective : ``kind`` in ``system`` | ``ca`` | ``certifi``.

    ``verify`` est la valeur transmise à ``requests`` (``True`` ou un chemin de bundle, jamais
    ``False``) ; ``ssl_context`` est le contexte truststore en mode ``system``.
    """

    kind: str
    verify: bool | str
    ssl_context: ssl.SSLContext | None = None
    description: str = ""


def _merged_bundle(ca_bundle: Path) -> str:
    """Fichier PEM = certifi + autorité de l'entreprise (valable avec et sans le proxy)."""
    corporate = ca_bundle.read_bytes()
    public = Path(certifi.where()).read_bytes()
    digest = hashlib.sha256(public + b"\0" + corporate).hexdigest()[:16]
    target = Path(tempfile.gettempdir()) / f"terminal_ca_{digest}.pem"
    if not target.is_file():
        tmp = target.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_bytes(public.rstrip() + b"\n" + corporate.rstrip() + b"\n")
        os.replace(tmp, target)
    return str(target)


def _system_context(extra: Path | None) -> ssl.SSLContext:
    import truststore

    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if extra is not None:
        ctx.load_verify_locations(cafile=str(extra))
    return ctx


def resolve_trust(settings: NetworkSettings) -> TrustPlan:
    """Traduit le mode de confiance demandé en réglage ``requests`` effectif."""
    mode = settings.trust
    bundle = settings.ca_bundle
    if mode == "auto":
        mode = "system" if truststore_available() else ("ca" if bundle else "certifi")
    if mode == "system":
        if not truststore_available():
            raise NetworkConfigurationError("trust='system' exige le paquet truststore (pip install truststore)")
        extra = f" + {bundle.name}" if bundle else ""
        return TrustPlan("system", True, _system_context(bundle), f"magasin de certificats du système{extra}")
    if mode == "ca":
        if bundle is None or not bundle.is_file():
            raise NetworkConfigurationError(f"trust='ca' exige un certificat CA existant (reçu {bundle})")
        return TrustPlan("ca", _merged_bundle(bundle), None, f"certificat d'entreprise {bundle.name} + certifi")
    return TrustPlan("certifi", certifi.where(), None, "certificats publics (certifi)")


# ===================================================================== session
class _ContextAdapter(HTTPAdapter):
    """Adaptateur HTTPS dont le contexte SSL est celui de truststore (proxy compris)."""

    def __init__(self, ctx: ssl.SSLContext, **kwargs: Any) -> None:
        self._ctx = ctx
        super().__init__(**kwargs)

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        kwargs["ssl_context"] = self._ctx
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args: Any, **kwargs: Any):
        kwargs["ssl_context"] = self._ctx
        return super().proxy_manager_for(*args, **kwargs)


class SecureSession(requests.Session):
    """``requests.Session`` qui ne peut pas désactiver la vérification des certificats.

    * ``session.verify = False`` lève :class:`InsecureTLSError` ;
    * ``session.get(url, verify=False)`` aussi, avant tout envoi ;
    * ``REQUESTS_CA_BUNDLE`` ne peut pas remplacer en douce la confiance choisie ;
    * le délai par défaut s'applique quand l'appelant n'en donne pas.
    """

    def __init__(self, settings: NetworkSettings | None = None) -> None:
        self._verify: bool | str = True
        super().__init__()
        self.settings = settings or NetworkSettings()
        self.trust = resolve_trust(self.settings)
        self.verify = self.trust.verify
        if self.trust.ssl_context is not None:
            self.mount("https://", _ContextAdapter(self.trust.ssl_context))
        self.headers["User-Agent"] = CHROME_USER_AGENT
        self._proxies = {k: v for k, v in (("https", self.settings.https_proxy),
                                           ("http", self.settings.http_proxy)) if v}

    # -- verify verrouillé -------------------------------------------------
    @property  # type: ignore[override]
    def verify(self) -> bool | str:
        return self._verify

    @verify.setter
    def verify(self, value: bool | str | None) -> None:
        if value is False or value is None or value == "":
            raise InsecureTLSError("verify=False est interdit : la vérification des certificats reste active.")
        self._verify = value

    def merge_environment_settings(self, url, proxies, stream, verify, cert):  # type: ignore[no-untyped-def]
        if verify is False:
            raise InsecureTLSError(f"verify=False refusé pour {url}")
        explicit_proxies = bool(proxies)  # requests complète `proxies` sur place
        settings = super().merge_environment_settings(url, proxies, stream, verify, cert)
        # La confiance de la session l'emporte sur REQUESTS_CA_BUNDLE ; un chemin explicite
        # passé par l'appelant reste accepté (il ne désactive rien).
        settings["verify"] = verify if isinstance(verify, str) and verify else self._verify
        if self._proxies and not explicit_proxies:
            merged = dict(settings.get("proxies") or {})
            if self.settings.no_proxy and should_bypass_proxies(url, no_proxy=self.settings.no_proxy):
                for scheme in ("http", "https", "all"):
                    merged.pop(scheme, None)
            else:
                merged.update(self._proxies)
            settings["proxies"] = merged
        return settings

    def request(self, method, url, *args, **kwargs):  # type: ignore[no-untyped-def,override]
        if kwargs.get("verify") is False:
            raise InsecureTLSError(f"verify=False refusé pour {url}")
        kwargs.setdefault("timeout", self.settings.timeout_s)
        return super().request(method, url, *args, **kwargs)


_LOCK = threading.Lock()
_SESSIONS: dict[NetworkSettings, SecureSession] = {}


def build_session(settings: NetworkSettings | None = None) -> SecureSession:
    """Nouvelle session configurée (pour un usage isolé, un test ou un diagnostic)."""
    return SecureSession(settings or NetworkSettings.from_env())


def get_session(settings: NetworkSettings | None = None) -> SecureSession:
    """Session partagée du processus pour ``settings`` (lus dans l'environnement par défaut)."""
    settings = settings or NetworkSettings.from_env()
    with _LOCK:
        session = _SESSIONS.get(settings)
        if session is None:
            session = _SESSIONS[settings] = SecureSession(settings)
            logger.info("session réseau : %s", describe(settings)["trust"])
        return session


def reset_sessions() -> None:
    """Ferme et oublie les sessions partagées (changement de réglage à chaud, tests)."""
    with _LOCK:
        for session in _SESSIONS.values():
            session.close()
        _SESSIONS.clear()


def with_overrides(settings: NetworkSettings, **changes: Any) -> NetworkSettings:
    """Copie modifiée de ``settings`` (validation comprise)."""
    return replace(settings, **changes)


# ===================================================================== yfinance
def apply_to_yfinance(session: requests.Session) -> bool:
    """Impose ``session`` au singleton HTTP de yfinance (``YfData``).

    Passer ``session=`` à chaque appel ne couvre pas tous les chemins de yfinance ; le singleton,
    si. Renvoie ``True`` si la session est effectivement prise en compte.
    """
    try:
        from yfinance.data import YfData
    except Exception:  # yfinance absent ou API modifiée
        return False
    try:
        data = YfData(session=session)
        if getattr(data, "_session", None) is not session:
            data._set_session(session)
        return getattr(data, "_session", None) is session
    except Exception as exc:
        logger.warning("session non appliquée à yfinance : %s", exc)
        return False


# ===================================================================== affichage
def mask_proxy(url: str | None) -> str | None:
    """``http://user:secret@proxy:8080`` -> ``http://user:***@proxy:8080``."""
    if not url:
        return url
    return re.sub(r"(//[^:/@]+):[^@]*@", r"\1:***@", url)


def proxy_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Variables de proxy standard actives (mot de passe masqué)."""
    env = os.environ if env is None else env
    names = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")
    return {n: mask_proxy(env[n]) or "" for n in names if env.get(n)}


def discover_ca_bundles(env: Mapping[str, str] | None = None) -> list[str]:
    """Fichiers de certificat d'entreprise présents sur le poste."""
    env = os.environ if env is None else env
    found = [p for p in COMMON_CA_PATHS if os.path.isfile(p)]
    for var in (ENV_CA_BUNDLE, *LEGACY_CA_VARS, *STANDARD_CA_VARS):
        p = (env.get(var) or "").strip()
        if p and os.path.isfile(p) and p not in found:
            found.insert(0, p)
    return found


def describe(settings: NetworkSettings | None = None) -> dict[str, Any]:
    """Résumé lisible de la configuration effective (aucun secret)."""
    settings = settings or NetworkSettings.from_env()
    try:
        trust = resolve_trust(settings).description
    except NetworkConfigurationError as exc:
        trust = f"ERREUR : {exc}"
    return {
        "trust": trust,
        "trust_mode": settings.trust,
        "truststore": truststore_available(),
        "ca_bundle": str(settings.ca_bundle) if settings.ca_bundle else None,
        "https_proxy": mask_proxy(settings.https_proxy),
        "http_proxy": mask_proxy(settings.http_proxy),
        "no_proxy": settings.no_proxy,
        "proxy_env": proxy_env(),
        "timeout_s": settings.timeout_s,
        "notes": list(settings.notes),
    }
