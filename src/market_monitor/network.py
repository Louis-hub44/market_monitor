"""Network access behind a corporate SSL-inspection proxy.

The problem
-----------
On a corporate network with an SSL-inspection proxy (Zscaler, Netskope,
Fortinet, Palo Alto...), HTTPS traffic is intercepted and re-signed with an
internal certificate that Python's trust store does not know::

    SSLError: certificate verify failed: self signed certificate in certificate chain

Symptom in this application: **every** online source (FMP, ECB, Yahoo) fails at
the same time for the same reason, and the dashboard only serves the cache.

``yfinance`` needs special care: it uses ``curl_cffi`` (browser impersonation)
internally, which ignores ``REQUESTS_CA_BUNDLE``. It does however accept any
explicitly supplied session - ``curl_cffi.requests.Session`` **or**
``requests.Session``. We therefore hand it a standard ``requests`` session whose
``verify=False`` / ``verify=<CA path>`` behaviour is stable and documented. The
lost browser TLS fingerprint is compensated by a realistic ``User-Agent``.

Order of preference
-------------------
1. **Corporate CA certificate** (``ca_bundle``) - verification stays on, the
   proxy is simply trusted. The only really safe option: ask IT for the file.
2. **Verification disabled** (``insecure_ssl``) - troubleshooting on a trusted
   corporate network only. Never on a public network.
3. **No customisation** - native behaviour (yfinance keeps ``curl_cffi``).

The same policy applies to every REST source (FMP, ECB) and to Yahoo: the proxy
is a workstation problem, not a source problem.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

logger = logging.getLogger(__name__)

ENV_INSECURE_SSL = "MARKET_MONITOR_INSECURE_SSL"
ENV_CA_BUNDLE = "MARKET_MONITOR_CA_BUNDLE"

CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: One probe per source, to tell a global failure (proxy, firewall) from a
#: single provider being down.
#: A 401 (no API key / cookie sent by the probe) still proves the host is reachable.
PROBES: dict[str, str] = {
    "FMP": "https://financialmodelingprep.com/stable/profile?symbol=AAPL",
    "ECB": "https://data-api.ecb.europa.eu/service/dataflow/ECB/EST",
    "YAHOO": "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=5d&interval=1d",
    "STOOQ": "https://stooq.com/q/d/l/?s=10dey.b&i=d",
    "BUNDESBANK": "https://api.statistiken.bundesbank.de/rest/data/BBSIS/"
                  "D.I.ZST.ZI.EUR.S1311.B.A604.R10XX.R.A.A._Z._Z.A?lastNObservations=1",
    "FRED": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS2",
    "STOXX": "https://www.stoxx.com/document/Indices/Current/HistoricalData/h_v2tx.txt",
}

#: Fragments of the block pages served by corporate web proxies.
PROXY_BLOCK_MARKERS = ("zscaler", "netskope", "forcepoint", "bluecoat", "fortiguard",
                       "websense", "access denied", "site blocked", "this site is blocked",
                       "url filtering", "web filter")

#: Fragments that unambiguously identify a certificate-validation failure.
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

#: Usual locations of a corporate certificate store, listed by ``doctor``.
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

_TRUE_VALUES = frozenset({"1", "true", "yes", "oui", "on"})


# ------------------------------------------------------------------ environment
def env_insecure(env: Mapping[str, str]) -> bool | None:
    """``MARKET_MONITOR_INSECURE_SSL`` as a boolean, ``None`` when unset."""
    value = env.get(ENV_INSECURE_SSL, "").strip().lower()
    return (value in _TRUE_VALUES) if value else None


def env_ca_bundle(env: Mapping[str, str]) -> str:
    """CA bundle from the environment.

    ``REQUESTS_CA_BUNDLE`` and ``SSL_CERT_FILE`` - standard variables often already
    set by IT teams - are honoured too, so that yfinance benefits from them.
    """
    return (
        env.get(ENV_CA_BUNDLE)
        or env.get("REQUESTS_CA_BUNDLE")
        or env.get("SSL_CERT_FILE")
        or ""
    ).strip()


def discover_ca_bundles(env: Mapping[str, str] | None = None) -> list[str]:
    """Certificate stores actually present on this machine."""
    found = [path for path in COMMON_CA_PATHS if os.path.isfile(path)]
    bundle = env_ca_bundle(os.environ if env is None else env)
    if bundle and os.path.isfile(bundle) and bundle not in found:
        found.insert(0, bundle)
    return found


def proxy_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Active proxy variables, shown by the diagnostic."""
    env = os.environ if env is None else env
    names = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")
    return {name: env[name] for name in names if env.get(name)}


# ------------------------------------------------------------------ sessions
def _valid_bundle(ca_bundle: str | os.PathLike[str] | None) -> str | None:
    path = str(ca_bundle or "").strip() or None
    if path and not os.path.isfile(path):
        logger.warning("CA bundle not found, setting ignored: %s", path)
        return None
    return path


def configure_session(
    session: requests.Session,
    insecure_ssl: bool = False,
    ca_bundle: str | os.PathLike[str] | None = None,
) -> requests.Session:
    """Apply the corporate SSL policy to ``session`` (modified in place).

    A valid ``ca_bundle`` **wins over** ``insecure_ssl``: verification stays on and
    additionally trusts that authority.
    """
    bundle = _valid_bundle(ca_bundle)
    # Realistic User-Agent: compensates the lost browser TLS fingerprint when
    # curl_cffi is replaced by requests. REST providers may override it.
    session.headers.update({"User-Agent": CHROME_USER_AGENT})
    if bundle:
        session.verify = bundle
        logger.info("network session with corporate CA bundle: %s", bundle)
    elif insecure_ssl:
        session.verify = False
        silence_insecure_warnings()
        logger.warning("SSL verification disabled for data calls (trusted network only)")
    else:
        # Explicit reset: the function may be called again to *re-enable* checks.
        session.verify = True
    return session


def build_session(
    insecure_ssl: bool = False, ca_bundle: str | os.PathLike[str] | None = None
) -> requests.Session | None:
    """Session for Yahoo/yfinance, or ``None`` when no customisation is requested.

    ``None`` lets yfinance keep its native behaviour (``curl_cffi`` with browser
    impersonation), which gets through Yahoo's anti-bot protections better.
    """
    if not (insecure_ssl or _valid_bundle(ca_bundle)):
        return None
    return configure_session(requests.Session(), insecure_ssl, ca_bundle)


def silence_insecure_warnings() -> None:
    """Stop urllib3's flood of warnings once verification is disabled."""
    try:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:  # pragma: no cover - urllib3 missing or API changed  # noqa: BLE001
        pass


def apply_to_yfinance(session: Any) -> bool:
    """Force ``session`` onto yfinance's HTTP singleton.

    Passing ``session=`` to each call is not enough everywhere: some calls go
    through the ``YfData`` singleton. API drift between versions is tolerated.

    Returns:
        ``True`` if the session was taken into account.
    """
    if session is None:
        return False
    try:
        from yfinance.data import YfData

        data = YfData(session=session)
        if getattr(data, "_session", None) is not session:
            data._set_session(session)
        return getattr(data, "_session", None) is session
    except Exception as exc:  # noqa: BLE001
        logger.warning("session not applied to yfinance: %s", exc)
        return False


def describe(insecure_ssl: bool, ca_bundle: str | os.PathLike[str] | None) -> str:
    """Readable summary of the active configuration (shown by ``doctor``)."""
    bundle = str(ca_bundle or "").strip()
    if bundle and os.path.isfile(bundle):
        return f"certificat d'entreprise reconnu ({os.path.basename(bundle)})"
    if bundle:
        return f"certificat introuvable ({bundle}) - vérification SSL standard"
    if insecure_ssl:
        return "vérification SSL désactivée - réseau de confiance uniquement"
    return "connexion directe (vérification SSL standard)"


# ------------------------------------------------------------------ diagnostic
def looks_like_ssl_error(message: Any) -> bool:
    """True when an error message is a certificate-validation failure."""
    if not message:
        return False
    text = str(message).lower()
    return any(marker in text for marker in SSL_ERROR_MARKERS)


def looks_like_proxy_block(body: Any) -> bool:
    """True when a response body is a corporate proxy's block page."""
    text = str(body or "")[:5000].lower()
    return "<html" in text and any(marker in text for marker in PROXY_BLOCK_MARKERS)


def count_ssl_failures(failures: Mapping[str, str]) -> int:
    """Number of certificate failures among ``{ticker: reason}`` errors."""
    return sum(1 for reason in failures.values() if looks_like_ssl_error(reason))


SSL_HINT = (
    "Un proxy d'inspection SSL réécrit le trafic : renseignez le certificat de "
    f"l'entreprise ({ENV_CA_BUNDLE} ou network.ca_bundle), ou, sur un réseau de "
    f"confiance uniquement, {ENV_INSECURE_SSL}=1."
)


def check_endpoint(url: str, session: Any = None, timeout: float = 12) -> dict[str, Any]:
    """Probe one endpoint and **qualify** the failure.

    Returns:
        ``{ok, status, latency_ms, kind, message, hint}`` where ``kind`` is
        ``ok``, ``ssl``, ``proxy``, ``timeout``, ``blocked``, ``dns`` or ``error``.
    """
    started = time.perf_counter()
    close_after = session is None
    if session is None:
        session = configure_session(
            requests.Session(), bool(env_insecure(os.environ)), env_ca_bundle(os.environ)
        )
    try:
        response = session.get(url, timeout=timeout)
        latency = round((time.perf_counter() - started) * 1000)
        status = response.status_code
        if looks_like_proxy_block(getattr(response, "text", "")):
            return {
                "ok": False, "status": status, "latency_ms": latency, "kind": "proxy",
                "message": f"page de blocage du proxy d'entreprise (HTTP {status})",
                "hint": "Le site est filtré par le proxy : demandez son ouverture à l'informatique.",
            }
        if status == 401:
            return {
                "ok": True, "status": 401, "latency_ms": latency, "kind": "ok",
                "message": f"joignable en {latency} ms (HTTP 401 : clé ou cookie requis)",
                "hint": "",
            }
        if status == 403:
            return {
                "ok": False, "status": 403, "latency_ms": latency, "kind": "blocked",
                "message": "le serveur répond 403 (accès refusé)",
                "hint": "Le réseau atteint la source mais l'accès est refusé (filtrage ou clé).",
            }
        if response.status_code == 429:
            return {
                "ok": False, "status": 429, "latency_ms": latency, "kind": "blocked",
                "message": "quota atteint (HTTP 429)",
                "hint": "Attendez la fenêtre suivante.",
            }
        return {
            "ok": True, "status": response.status_code, "latency_ms": latency, "kind": "ok",
            "message": f"connexion établie en {latency} ms (HTTP {response.status_code})",
            "hint": "",
        }
    except Exception as exc:  # noqa: BLE001 - every failure is qualified
        latency = round((time.perf_counter() - started) * 1000)
        text = f"{type(exc).__name__}: {exc}"
        low = text.lower()
        if looks_like_ssl_error(text):
            kind, hint = "ssl", SSL_HINT
        elif "proxy" in low:
            kind, hint = "proxy", "Proxy requis : renseignez HTTPS_PROXY avant de lancer l'application."
        elif "timeout" in low or "timed out" in low:
            kind, hint = "timeout", "Délai dépassé : le pare-feu bloque probablement la sortie."
        elif any(m in low for m in ("name or service not known", "nodename nor servname", "getaddrinfo")):
            kind, hint = "dns", "Résolution DNS impossible : vérifiez le réseau ou la configuration du proxy."
        else:
            kind, hint = "error", "Vérifiez l'accès sortant du poste."
        return {"ok": False, "status": None, "latency_ms": latency,
                "kind": kind, "message": text[:300], "hint": hint}
    finally:
        if close_after:
            try:
                session.close()
            except Exception:  # pragma: no cover  # noqa: BLE001
                pass


def check_connectivity(
    session: Any = None,
    timeout: float = 12,
    probes: Mapping[str, str] | None = None,
    *,
    parallel: bool = False,
) -> dict[str, Any]:
    """Probe every source and summarise the diagnostic.

    Returns:
        ``{results: {source: diagnostic}, kind, message, hint, ssl_blocked}``.
        ``kind`` is the **dominant** cause: when every source fails on a
        certificate, the diagnostic points at the proxy, not at the providers.
    """
    probes = PROBES if probes is None else probes
    if parallel:  # firewalled hosts time out together instead of one after the other
        with ThreadPoolExecutor(max_workers=len(probes) or 1) as pool:
            futures = {name: pool.submit(check_endpoint, url, session, timeout)
                       for name, url in probes.items()}
            results = {name: future.result() for name, future in futures.items()}
    else:
        results = {name: check_endpoint(url, session, timeout) for name, url in probes.items()}
    kinds = [r["kind"] for r in results.values()]
    ssl_blocked = bool(kinds) and all(k == "ssl" for k in kinds)

    if all(k == "ok" for k in kinds):
        kind, message, hint = "ok", "toutes les sources sont joignables", ""
    elif ssl_blocked:
        kind = "ssl"
        message = ("toutes les sources échouent sur la validation du certificat : "
                   "un proxy d'inspection SSL intercepte le trafic")
        hint = SSL_HINT
    elif any(k == "ok" for k in kinds):
        failed = [n for n, r in results.items() if not r["ok"]]
        kind = "partial"
        message = f"accès réseau opérationnel, mais {', '.join(failed)} ne répond pas"
        hint = next(r["hint"] for r in results.values() if not r["ok"])
    else:
        first = next(iter(results.values()))
        kind, message, hint = first["kind"], first["message"], first["hint"]

    return {"results": results, "kind": kind, "message": message,
            "hint": hint, "ssl_blocked": ssl_blocked}
