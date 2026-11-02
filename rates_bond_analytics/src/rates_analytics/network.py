"""Network access behind a corporate SSL-inspection proxy — thin layer over ``terminal_core``.

Every HTTP call of the terminal goes through the single session of
:mod:`terminal_core.network` (shared with Portfolio Intelligence and the stress engine):

* proxy: ``TERMINAL_HTTPS_PROXY`` / ``TERMINAL_NO_PROXY`` or the ``network.proxy`` key of
  ``config.yaml``, else the standard ``HTTPS_PROXY`` / ``NO_PROXY``;
* trust, mode ``auto`` (default): OS trust store through ``truststore`` (the corporate CA, if
  given, is added to it), else the corporate CA bundle, else ``certifi``. Forced modes:
  ``system``, ``ca``, ``standard`` (certifi);
* **verification is never disabled**: the former ``insecure`` mode is refused in code and
  ignored (with a warning) in ``config.yaml`` and in the legacy environment variables.

This module keeps the public API the dashboard and the data layer were written against
(:class:`NetworkSettings`, :func:`build_session`, the diagnostic helpers).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from terminal_core import network as core
from terminal_core.check_network import Probe, looks_like_proxy_block, probe

from rates_analytics.exceptions import ConfigurationError

logger = logging.getLogger(__name__)

MODES = ("auto", "system", "ca", "standard")
_TO_TRUST = {"auto": "auto", "system": "system", "ca": "ca", "standard": "certifi"}
_FROM_TRUST = {v: k for k, v in _TO_TRUST.items()}
ENV_MODE = "RATES_NETWORK_MODE"
ENV_CA_BUNDLE = "RATES_CA_BUNDLE"

CHROME_USER_AGENT = core.CHROME_USER_AGENT
COMMON_CA_PATHS = core.COMMON_CA_PATHS

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

SSL_ERROR_MARKERS = (
    "self signed certificate", "self-signed certificate", "certificate verify failed",
    "ssl certificate problem", "sslerror", "ssl: certificate", "unable to get local issuer certificate",
    "certificate_verify_failed", "sslcertverification", "tlsv1 alert",
)

SSL_HINT = ("Un proxy d'inspection SSL réécrit le trafic : essayez le mode « magasin Windows » "
            "(truststore), ou le certificat racine de l'entreprise (fichier .pem demandé à l'informatique).")


# ===================================================================== settings
@dataclass(frozen=True)
class NetworkSettings:
    """SSL / proxy policy of the data sessions (dashboard start screen, ``config.yaml``)."""

    mode: str = "auto"
    ca_bundle: Path | None = None
    timeout_s: float = 20.0
    proxy: str | None = None
    no_proxy: str | None = None

    def __post_init__(self) -> None:
        if self.mode == "insecure":
            raise ConfigurationError("Le mode 'insecure' (verify=False) n'est plus supporté : "
                                     "utilisez 'system' (magasin de certificats) ou 'ca' (certificat d'entreprise).")
        if self.mode not in MODES:
            raise ConfigurationError(f"network.mode must be one of {MODES}, got {self.mode!r}")
        if self.mode == "ca" and self.ca_bundle is None:
            raise ConfigurationError("network.mode 'ca' needs network.ca_bundle (path to a .pem / .crt)")

    @property
    def verifies(self) -> bool:
        """Always ``True``: certificate verification cannot be turned off."""
        return True

    @property
    def label(self) -> str:
        if self.mode == "ca" and self.ca_bundle is not None:
            return f"Certificat · {self.ca_bundle.name}"
        return {"auto": "Automatique (magasin système)", "standard": "Connexion standard",
                "system": "Magasin de certificats système"}.get(self.mode, self.mode)

    @classmethod
    def from_config(cls, raw: Mapping[str, Any] | None, env: Mapping[str, str] | None = None) -> NetworkSettings:
        """Config block (``mode``, ``ca_bundle``, ``timeout_s``, ``proxy``, ``no_proxy``) overridden by
        the environment (``TERMINAL_*`` first, then ``RATES_NETWORK_MODE`` / ``RATES_CA_BUNDLE`` and the
        other legacy variables, see :mod:`terminal_core.network`)."""
        env = os.environ if env is None else env
        raw = dict(raw or {})
        mode = str(raw.get("mode") or "auto").strip().lower()
        legacy = (env.get(ENV_MODE) or "").strip().lower()
        if legacy in _TO_TRUST:
            mode = legacy
        notes: list[str] = []
        if mode not in _TO_TRUST:
            notes.append(f"network.mode={mode} ignoré : la vérification SSL reste active.")
            mode = "auto"
        cfg = {"trust": _TO_TRUST[mode], "ca_bundle": raw.get("ca_bundle") or None,
               "timeout_s": raw.get("timeout_s"), "https_proxy": raw.get("proxy"), "no_proxy": raw.get("no_proxy")}
        try:
            c = core.NetworkSettings.from_env(env=env, config=cfg)
        except core.NetworkConfigurationError as exc:
            raise ConfigurationError(str(exc)) from exc
        for note in notes:
            logger.warning(note)
        return cls(mode=_FROM_TRUST[c.trust], ca_bundle=c.ca_bundle, timeout_s=c.timeout_s,
                   proxy=c.https_proxy, no_proxy=c.no_proxy)

    def to_core(self) -> core.NetworkSettings:
        if self.ca_bundle is not None and not self.ca_bundle.is_file():
            if self.mode == "ca":
                raise ConfigurationError(f"CA bundle not found: {self.ca_bundle}")
            logger.warning("CA bundle not found, ignored: %s", self.ca_bundle)
            bundle = None
        else:
            bundle = self.ca_bundle
        try:
            return core.NetworkSettings(https_proxy=self.proxy, http_proxy=self.proxy, no_proxy=self.no_proxy,
                                        ca_bundle=bundle, trust=_TO_TRUST[self.mode], timeout_s=self.timeout_s)
        except core.NetworkConfigurationError as exc:
            raise ConfigurationError(str(exc)) from exc


# ===================================================================== sessions
def build_session(network: NetworkSettings | None = None) -> requests.Session:
    """The terminal's shared session for ``network`` (``auto`` policy by default)."""
    try:
        return core.get_session((network or NetworkSettings()).to_core())
    except core.NetworkConfigurationError as exc:
        raise ConfigurationError(str(exc)) from exc


def discover_ca_bundles(env: Mapping[str, str] | None = None) -> list[str]:
    """Corporate certificate files actually present on this machine."""
    return core.discover_ca_bundles(env)


def proxy_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Active proxy variables (shown on the start screen, passwords masked)."""
    return core.proxy_env(env)


# =================================================================== diagnostic
def looks_like_ssl_error(message: Any) -> bool:
    text = str(message or "").lower()
    return bool(text) and any(m in text for m in SSL_ERROR_MARKERS)


def count_ssl_failures(errors: Mapping[str, str]) -> int:
    return sum(1 for reason in errors.values() if looks_like_ssl_error(reason))


def check_endpoint(url: str, session: requests.Session, timeout: float = 10) -> dict[str, Any]:
    """Probe one endpoint and qualify the failure (classification of :mod:`terminal_core.check_network`).

    Returns ``{ok, status, latency_ms, kind, message, hint}``, ``kind`` in
    ``ok | ssl | proxy | proxy_auth | blocked | timeout | dns | http | error``.
    """
    r = probe(Probe(url, url, True, ""), session, timeout)
    hint = SSL_HINT if r.kind == "ssl" else r.hint
    message = f"joignable en {r.latency_ms} ms (HTTP {r.status})" if r.ok else r.message
    return {"ok": r.ok, "status": r.status, "latency_ms": r.latency_ms, "kind": r.kind,
            "message": message, "hint": hint}


def check_connectivity(network: NetworkSettings | None = None, probes: Mapping[str, str] | None = None,
                       timeout: float = 8, session: requests.Session | None = None) -> dict[str, Any]:
    """Probe every source in parallel and summarise: ``{results, kind, message, hint}``.

    ``kind`` is the **dominant** cause: when every source fails on a certificate, the
    diagnostic points at the proxy, not at the providers.
    """
    probes = PROBES if probes is None else probes
    sess = session or build_session(network)
    with ThreadPoolExecutor(max_workers=max(1, len(probes))) as pool:
        futures = {n: pool.submit(check_endpoint, u, sess, timeout) for n, u in probes.items()}
        results = {n: f.result() for n, f in futures.items()}
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


__all__ = [
    "COMMON_CA_PATHS", "MODES", "PROBES", "SSL_HINT", "NetworkSettings", "build_session", "check_connectivity",
    "check_endpoint", "count_ssl_failures", "discover_ca_bundles", "looks_like_proxy_block", "looks_like_ssl_error",
    "proxy_env",
]
