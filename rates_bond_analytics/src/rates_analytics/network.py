"""Network access behind a corporate SSL-inspection proxy (ported from Market Monitor).

The problem
-----------
On a corporate network (Zscaler, Netskope, Fortinet, Palo Alto…) HTTPS traffic is
intercepted and re-signed with an internal root certificate that Python's own
trust store (``certifi``) does not know::

    SSLError: certificate verify failed: self signed certificate in certificate chain

Symptom: **every** online source (ECB, FMP, CNBC, Bundesbank…) fails at once and the
terminal falls back to the sample data.

Four modes, in order of preference
----------------------------------
1. ``system``   – verification **on**, using the operating-system trust store
   (Windows certificate store, macOS keychain) through ``truststore``. IT usually
   deploys the proxy root CA there, so this works without any file. Safest option.
2. ``ca``       – verification **on**, with an explicit corporate CA bundle (.pem/.crt).
3. ``insecure`` – verification **off** (``verify=False``). Troubleshooting on the trusted
   corporate network only, never on public Wi-Fi: traffic could then be read or altered.
4. ``standard`` – default ``requests`` behaviour (certifi).

The policy is applied to one :class:`requests.Session` shared by every REST provider
(the proxy is a workstation problem, not a source problem). Nothing is global: the
policy never leaks into other libraries of the process.

Configuration: ``network:`` block of ``config.yaml`` (``mode``, ``ca_bundle``) or the
environment (``RATES_NETWORK_MODE``, ``RATES_CA_BUNDLE``). The standard
``REQUESTS_CA_BUNDLE`` / ``SSL_CERT_FILE`` and Market Monitor's
``MARKET_MONITOR_INSECURE_SSL`` / ``MARKET_MONITOR_CA_BUNDLE`` are honoured too, so one
``.env`` serves the whole terminal.
"""

from __future__ import annotations

import logging
import os
import ssl
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from rates_analytics.exceptions import ConfigurationError

logger = logging.getLogger(__name__)

MODES = ("standard", "system", "ca", "insecure")
ENV_MODE = "RATES_NETWORK_MODE"
ENV_CA_BUNDLE = "RATES_CA_BUNDLE"
ENV_MM_INSECURE = "MARKET_MONITOR_INSECURE_SSL"
ENV_MM_CA_BUNDLE = "MARKET_MONITOR_CA_BUNDLE"

CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: one probe per source: tells a global failure (proxy, firewall) from one provider being down.
#: A 401 / 400 (no key sent) still proves that the host is reachable.
PROBES: dict[str, str] = {
    "ECB": "https://data-api.ecb.europa.eu/service/dataflow/ECB/EST",
    "FMP": "https://financialmodelingprep.com/stable/treasury-rates",
    "CNBC": "https://ts-api.cnbc.com/harmony/app/bars/DE10Y-DE/1D/"
            "20260901000000/20260930235959/adjusted/EST5EDT.json",
    "BUNDESBANK": "https://api.statistiken.bundesbank.de/rest/data/BBSIS/"
                  "D.I.ZST.ZI.EUR.S1311.B.A604.R10XX.R.A.A._Z._Z.A?lastNObservations=1",
    "STOOQ": "https://stooq.com/q/d/l/?s=10dey.b&i=d",
}

PROXY_BLOCK_MARKERS = ("zscaler", "netskope", "forcepoint", "bluecoat", "fortiguard", "websense",
                       "access denied", "site blocked", "this site is blocked", "url filtering", "web filter")

SSL_ERROR_MARKERS = (
    "self signed certificate", "self-signed certificate", "certificate verify failed",
    "ssl certificate problem", "sslerror", "ssl: certificate", "unable to get local issuer certificate",
    "certificate_verify_failed", "sslcertverification", "tlsv1 alert",
)

#: usual locations of a corporate root certificate (listed on the start screen)
COMMON_CA_PATHS = (
    r"C:\ProgramData\Zscaler\ZscalerRootCertificate.pem",
    r"C:\ProgramData\Netskope\STAgent\data\nscacert.pem",
    r"C:\ProgramData\corporate-ca.pem",
    "/Library/Application Support/Zscaler/ZscalerRootCertificate.pem",
    "/Library/Application Support/Netskope/STAgent/data/nscacert.pem",
    "/usr/local/share/ca-certificates/zscaler.crt",
    "/usr/local/share/ca-certificates/corporate.crt",
)

SSL_HINT = ("Un proxy d'inspection SSL réécrit le trafic : essayez le mode « magasin Windows », "
            "ou le certificat de l'entreprise (fichier .pem demandé à l'informatique), ou, sur le "
            "réseau de confiance de l'entreprise uniquement, le contournement SSL.")

_TRUE = frozenset({"1", "true", "yes", "oui", "on"})


# ===================================================================== settings
@dataclass(frozen=True)
class NetworkSettings:
    """SSL policy of the data sessions."""

    mode: str = "standard"
    ca_bundle: Path | None = None
    timeout_s: float = 20.0

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ConfigurationError(f"network.mode must be one of {MODES}, got {self.mode!r}")
        if self.mode == "ca" and self.ca_bundle is None:
            raise ConfigurationError("network.mode 'ca' needs network.ca_bundle (path to a .pem / .crt)")

    @property
    def verifies(self) -> bool:
        return self.mode != "insecure"

    @property
    def label(self) -> str:
        if self.mode == "ca" and self.ca_bundle is not None:
            return f"Certificat · {self.ca_bundle.name}"
        return {"standard": "Connexion standard", "system": "Magasin de certificats système",
                "insecure": "SSL contourné"}.get(self.mode, self.mode)

    @classmethod
    def from_config(cls, raw: Mapping[str, Any] | None, env: Mapping[str, str] | None = None) -> NetworkSettings:
        """Config block (``mode``, ``ca_bundle``, ``timeout_s``) overridden by the environment."""
        env = os.environ if env is None else env
        raw = dict(raw or {})
        mode = str(raw.get("mode", "standard")).strip().lower()
        bundle = str(raw.get("ca_bundle") or "").strip()
        env_bundle = (env.get(ENV_CA_BUNDLE) or env.get(ENV_MM_CA_BUNDLE) or "").strip()
        if env_bundle:
            bundle, mode = env_bundle, "ca"
        elif not bundle and mode == "ca":
            bundle = (env.get("REQUESTS_CA_BUNDLE") or env.get("SSL_CERT_FILE") or "").strip()
        if env.get(ENV_MM_INSECURE, "").strip().lower() in _TRUE and not env_bundle:
            mode = "insecure"
        if env.get(ENV_MODE, "").strip():
            mode = env[ENV_MODE].strip().lower()
        return cls(mode=mode, ca_bundle=Path(bundle) if bundle and mode == "ca" else None,
                   timeout_s=float(raw.get("timeout_s", 20.0)))


# ===================================================================== sessions
class _SystemTrustAdapter(HTTPAdapter):
    """HTTPS adapter whose SSL context trusts the OS certificate store (``truststore``)."""

    def __init__(self, ctx: ssl.SSLContext, **kwargs: Any) -> None:
        self._ctx = ctx
        super().__init__(**kwargs)

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        kwargs["ssl_context"] = self._ctx
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args: Any, **kwargs: Any):
        kwargs["ssl_context"] = self._ctx
        return super().proxy_manager_for(*args, **kwargs)


def system_ssl_context() -> ssl.SSLContext:
    """SSL context backed by the OS trust store (Windows / macOS / Linux)."""
    try:
        import truststore
    except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
        raise ConfigurationError("Mode 'system' needs the 'truststore' package: pip install truststore") from exc
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def configure_session(session: requests.Session, network: NetworkSettings) -> requests.Session:
    """Apply the SSL policy to ``session`` (modified in place and returned)."""
    if str(session.headers.get("User-Agent", "")).startswith("python-requests"):
        session.headers["User-Agent"] = CHROME_USER_AGENT   # some sources reject the default agent
    if network.mode == "ca":
        assert network.ca_bundle is not None
        if not network.ca_bundle.is_file():
            raise ConfigurationError(f"CA bundle not found: {network.ca_bundle}")
        session.verify = str(network.ca_bundle)
        logger.info("data session trusts the corporate CA %s", network.ca_bundle)
    elif network.mode == "insecure":
        session.verify = False
        silence_insecure_warnings()
        logger.warning("SSL verification disabled for data calls (trusted corporate network only)")
    elif network.mode == "system":
        session.verify = True
        session.mount("https://", _SystemTrustAdapter(system_ssl_context()))
        logger.info("data session uses the operating-system trust store")
    else:
        session.verify = True
    return session


def build_session(network: NetworkSettings | None = None) -> requests.Session:
    """New session configured with ``network`` (standard policy by default)."""
    return configure_session(requests.Session(), network or NetworkSettings())


def silence_insecure_warnings() -> None:
    """Stop urllib3's flood of warnings once verification is disabled."""
    try:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:  # pragma: no cover
        pass


def discover_ca_bundles(env: Mapping[str, str] | None = None) -> list[str]:
    """Corporate certificate files actually present on this machine."""
    env = os.environ if env is None else env
    found = [p for p in COMMON_CA_PATHS if os.path.isfile(p)]
    for var in (ENV_CA_BUNDLE, ENV_MM_CA_BUNDLE, "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        p = (env.get(var) or "").strip()
        if p and os.path.isfile(p) and p not in found:
            found.insert(0, p)
    return found


def proxy_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Active proxy variables (shown on the start screen)."""
    env = os.environ if env is None else env
    names = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")
    return {n: env[n] for n in names if env.get(n)}


# =================================================================== diagnostic
def looks_like_ssl_error(message: Any) -> bool:
    text = str(message or "").lower()
    return bool(text) and any(m in text for m in SSL_ERROR_MARKERS)


def looks_like_proxy_block(body: Any) -> bool:
    text = str(body or "")[:5000].lower()
    return "<html" in text and any(m in text for m in PROXY_BLOCK_MARKERS)


def count_ssl_failures(errors: Mapping[str, str]) -> int:
    return sum(1 for reason in errors.values() if looks_like_ssl_error(reason))


def check_endpoint(url: str, session: requests.Session, timeout: float = 10) -> dict[str, Any]:
    """Probe one endpoint and qualify the failure.

    Returns ``{ok, status, latency_ms, kind, message, hint}``, ``kind`` in
    ``ok | ssl | proxy | blocked | timeout | dns | error``.
    """
    started = time.perf_counter()
    try:
        resp = session.get(url, timeout=timeout)
    except Exception as exc:
        latency = round((time.perf_counter() - started) * 1000)
        text = f"{type(exc).__name__}: {exc}"
        low = text.lower()
        if looks_like_ssl_error(text):
            kind, hint = "ssl", SSL_HINT
        elif "proxy" in low:
            kind, hint = "proxy", "Proxy requis : renseignez HTTPS_PROXY dans .env avant de lancer l'application."
        elif "timeout" in low or "timed out" in low:
            kind, hint = "timeout", "Délai dépassé : le pare-feu bloque probablement la sortie vers ce site."
        elif any(m in low for m in ("name or service not known", "nodename nor servname", "getaddrinfo")):
            kind, hint = "dns", "Résolution DNS impossible : vérifiez le réseau ou le proxy."
        else:
            kind, hint = "error", "Vérifiez l'accès sortant du poste."
        return {"ok": False, "status": None, "latency_ms": latency, "kind": kind, "message": text[:300], "hint": hint}
    latency = round((time.perf_counter() - started) * 1000)
    status = resp.status_code
    if looks_like_proxy_block(getattr(resp, "text", "")):
        return {"ok": False, "status": status, "latency_ms": latency, "kind": "proxy",
                "message": f"page de blocage du proxy d'entreprise (HTTP {status})",
                "hint": "Site filtré par le proxy : demandez son ouverture à l'informatique."}
    if status in (403, 429):
        return {"ok": False, "status": status, "latency_ms": latency, "kind": "blocked",
                "message": f"HTTP {status}", "hint": "Le réseau atteint la source mais l'accès est refusé."}
    return {"ok": True, "status": status, "latency_ms": latency, "kind": "ok",
            "message": f"joignable en {latency} ms (HTTP {status})", "hint": ""}


def check_connectivity(network: NetworkSettings | None = None, probes: Mapping[str, str] | None = None,
                       timeout: float = 8, session: requests.Session | None = None) -> dict[str, Any]:
    """Probe every source in parallel and summarise: ``{results, kind, message, hint}``.

    ``kind`` is the **dominant** cause: when every source fails on a certificate, the
    diagnostic points at the proxy, not at the providers.
    """
    probes = PROBES if probes is None else probes
    own = session is None
    sess = session or build_session(network)
    try:
        with ThreadPoolExecutor(max_workers=max(1, len(probes))) as pool:
            futures = {n: pool.submit(check_endpoint, u, sess, timeout) for n, u in probes.items()}
            results = {n: f.result() for n, f in futures.items()}
    finally:
        if own:
            sess.close()
    kinds = [r["kind"] for r in results.values()]
    if kinds and all(k == "ok" for k in kinds):
        kind, message, hint = "ok", "toutes les sources sont joignables", ""
    elif kinds and all(k == "ssl" for k in kinds):
        kind, message, hint = ("ssl", "toutes les sources échouent sur le certificat : un proxy d'inspection "
                               "SSL intercepte le trafic", SSL_HINT)
    elif any(k == "ok" for k in kinds):
        failed = [n for n, r in results.items() if not r["ok"]]
        kind, message = "partial", f"réseau opérationnel, mais {', '.join(failed)} ne répond pas"
        hint = next(r["hint"] for r in results.values() if not r["ok"])
    elif results:
        first = next(iter(results.values()))
        kind, message, hint = first["kind"], first["message"], first["hint"]
    else:
        kind, message, hint = "ok", "aucune source à tester", ""
    return {"results": results, "kind": kind, "message": message, "hint": hint}
