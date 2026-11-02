"""Diagnostic réseau du terminal : configuration effective + sondage de chaque source.

Usage::

    check-network                 # ou : python scripts/check_network.py
    check-network --json
    check-network --only ECB STOOQ --timeout 5

Codes retour : 0 toutes les sources critiques répondent ; 1 au moins une source critique
en échec ; 2 configuration réseau invalide.

La qualification de chaque échec (``ssl``, ``proxy``, ``proxy_auth``, ``blocked``, ``dns``,
``timeout``, ``http``, ``error``) dit qui corriger : le poste (certificat, proxy) ou la source.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

import requests

from terminal_core.network import (
    NetworkConfigurationError,
    NetworkSettings,
    build_session,
    describe,
    discover_ca_bundles,
)


@dataclass(frozen=True)
class Probe:
    name: str
    url: str
    critical: bool
    usage: str


#: Un point par source. Un 400/401 (requête sans clé) prouve déjà que l'hôte est joignable.
PROBES: tuple[Probe, ...] = (
    Probe("ECB", "https://data-api.ecb.europa.eu/service/data/EST/B.EU000A2X2A25.WT?lastNObservations=1"
          "&format=csvdata", True, "€STR, courbe AAA, change"),
    Probe("BUNDESBANK", "https://api.statistiken.bundesbank.de/rest/data/BBSIS/"
          "D.I.ZST.ZI.EUR.S1311.B.A604.R10XX.R.A.A._Z._Z.A?lastNObservations=1", True, "courbe zéro Bund"),
    Probe("STOOQ", "https://stooq.com/q/d/l/?s=10dey.b&i=d", True, "taux souverains EUR"),
    Probe("FRED", "https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAA10Y", True, "spreads de crédit, VIX"),
    Probe("CNBC", "https://ts-api.cnbc.com/harmony/app/bars/DE10Y-DE/1D/20260901000000/20260930235959/"
          "adjusted/EST5EDT.json", False, "taux souverains (secours)"),
    Probe("FMP", "https://financialmodelingprep.com/stable/treasury-rates", False, "actions, courbe US"),
    Probe("YAHOO", "https://query1.finance.yahoo.com/v8/finance/chart/%5ESTOXX50E?range=5d&interval=1d",
          False, "actions, ETF, change"),
)

SSL_MARKERS = (
    "self signed certificate", "self-signed certificate", "certificate verify failed",
    "ssl certificate problem", "sslerror", "ssl: certificate", "unable to get local issuer certificate",
    "certificate_verify_failed", "sslcertverification", "tlsv1 alert",
)
PROXY_BLOCK_MARKERS = ("zscaler", "netskope", "forcepoint", "bluecoat", "fortiguard", "websense",
                       "access denied", "site blocked", "this site is blocked", "url filtering", "web filter")
DNS_MARKERS = ("name or service not known", "nodename nor servname", "getaddrinfo", "name resolution",
               "temporary failure in name resolution", "no address associated")

HINTS = {
    "ssl": ("Certificat refusé : un proxy d'inspection SSL réécrit le trafic. Installez le paquet "
            "truststore (magasin de certificats du poste) ou renseignez TERMINAL_CA_BUNDLE avec le "
            "certificat racine fourni par l'informatique."),
    "proxy": "Proxy injoignable ou requis : renseignez TERMINAL_HTTPS_PROXY (ou HTTPS_PROXY).",
    "proxy_auth": "Le proxy exige une authentification (HTTP 407) : http://utilisateur:motdepasse@proxy:port.",
    "blocked": "Le réseau atteint la source mais l'accès est filtré : demandez l'ouverture à l'informatique.",
    "dns": "Résolution DNS impossible : réseau coupé ou proxy obligatoire.",
    "timeout": "Délai dépassé : sortie probablement bloquée par le pare-feu.",
    "http": "La source répond en erreur : panne côté fournisseur, réessayer plus tard.",
    "error": "Vérifiez l'accès sortant du poste.",
}


@dataclass
class ProbeResult:
    name: str
    url: str
    critical: bool
    ok: bool
    kind: str
    status: int | None
    latency_ms: int
    message: str
    hint: str


def classify_exception(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if isinstance(exc, requests.exceptions.SSLError) or any(m in text for m in SSL_MARKERS):
        return "ssl"
    if isinstance(exc, requests.exceptions.ProxyError) or "proxy" in text:
        if "407" in text:
            return "proxy_auth"
        # le proxy répond mais refuse d'ouvrir le tunnel : l'hôte est filtré
        return "blocked" if re.search(r"tunnel connection failed: 40[13]", text) else "proxy"
    if isinstance(exc, requests.exceptions.Timeout) or "timed out" in text or "timeout" in text:
        return "timeout"
    if any(m in text for m in DNS_MARKERS):
        return "dns"
    return "error"


def looks_like_proxy_block(body: str) -> bool:
    text = (body or "")[:5000].lower()
    return "<html" in text and any(m in text for m in PROXY_BLOCK_MARKERS)


def probe(p: Probe, session: requests.Session, timeout: float) -> ProbeResult:
    started = time.perf_counter()

    def done(ok: bool, kind: str, status: int | None, message: str) -> ProbeResult:
        latency = round((time.perf_counter() - started) * 1000)
        return ProbeResult(p.name, p.url, p.critical, ok, kind, status, latency, message[:300],
                           "" if ok else HINTS.get(kind, HINTS["error"]))

    try:
        resp = session.get(p.url, timeout=timeout)
    except Exception as exc:  # toute erreur de transport est qualifiée, jamais propagée
        return done(False, classify_exception(exc), None, f"{type(exc).__name__}: {exc}")
    status = resp.status_code
    if status == 407:
        return done(False, "proxy_auth", status, "HTTP 407 : authentification proxy requise")
    if looks_like_proxy_block(resp.text):
        return done(False, "blocked", status, f"page de blocage du proxy d'entreprise (HTTP {status})")
    if status in (403, 429):
        return done(False, "blocked", status, f"HTTP {status}")
    if status >= 500:
        return done(False, "http", status, f"HTTP {status}")
    return done(True, "ok", status, f"HTTP {status}")


def dominant_cause(results: Sequence[ProbeResult]) -> str:
    """``ok`` | ``partial`` | cause commune à tous les échecs (``ssl`` = problème de poste)."""
    if all(r.ok for r in results):
        return "ok"
    if any(r.ok for r in results):
        return "partial"
    kinds = {r.kind for r in results}
    return kinds.pop() if len(kinds) == 1 else "mixed"


def run_checks(settings: NetworkSettings | None = None, only: Sequence[str] | None = None,
               timeout: float = 8.0, session: requests.Session | None = None,
               probes: Sequence[Probe] = PROBES) -> dict[str, Any]:
    settings = settings or NetworkSettings.from_env()
    wanted = [p for p in probes if not only or p.name in {o.upper() for o in only}]
    own = session is None
    sess = session or build_session(settings)
    try:
        with ThreadPoolExecutor(max_workers=max(1, len(wanted))) as pool:
            results = list(pool.map(lambda p: probe(p, sess, timeout), wanted))
    finally:
        if own:
            sess.close()
    return {"config": describe(settings), "ca_found": discover_ca_bundles(), "results": results,
            "cause": dominant_cause(results) if results else "ok",
            "critical_ok": all(r.ok for r in results if r.critical)}


def render(report: Mapping[str, Any]) -> str:
    cfg = report["config"]
    lines = ["Configuration réseau",
             f"  confiance TLS : {cfg['trust']}  (mode {cfg['trust_mode']}, truststore "
             f"{'installé' if cfg['truststore'] else 'absent'})",
             f"  proxy HTTPS   : {cfg['https_proxy'] or 'aucun réglage (variables standard)'}",
             f"  no_proxy      : {cfg['no_proxy'] or '-'}"]
    if cfg["proxy_env"]:
        lines.append("  environnement : " + ", ".join(f"{k}={v}" for k, v in cfg["proxy_env"].items()))
    if report["ca_found"]:
        lines.append("  certificats d'entreprise trouvés : " + ", ".join(report["ca_found"]))
    lines += [f"  ! {n}" for n in cfg["notes"]]
    lines += ["", "Sources"]
    for r in report["results"]:
        flag = "OK " if r.ok else "KO "
        crit = "*" if r.critical else " "
        lines.append(f"  {flag}{crit}{r.name:<11} {r.kind:<10} {r.latency_ms:>6} ms  {r.message}")
        if not r.ok:
            lines.append(f"       -> {r.hint}")
    cause = report["cause"]
    verdict = {"ok": "toutes les sources répondent",
               "partial": "réseau opérationnel ; les sources en échec sont en cause, pas le poste",
               "ssl": "toutes les sources échouent sur le certificat : proxy d'inspection SSL",
               }.get(cause, f"toutes les sources échouent ({cause}) : problème de poste ou de réseau")
    lines += ["", f"Verdict : {verdict}", "(* = source critique pour le moteur de stress)"]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check-network", description=__doc__.splitlines()[0])
    parser.add_argument("--only", nargs="*", help="sources à tester (ECB, STOOQ…)")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        settings = NetworkSettings.from_env()
        report = run_checks(settings, args.only, args.timeout)
    except NetworkConfigurationError as exc:
        print(f"Configuration réseau invalide : {exc}", file=sys.stderr)
        return 2
    if args.json:
        payload = {**report, "results": [asdict(r) for r in report["results"]]}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0 if report["critical_ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
