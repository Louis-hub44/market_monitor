"""``market-monitor constituents …`` and ``market-monitor equities …`` commands."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from market_monitor.config import NetworkSettings, Settings
from market_monitor.data.factory import build_provider, rest_session
from market_monitor.data.providers.fmp import FMPProvider
from market_monitor.equities.constituents import save_composition
from market_monitor.equities.sources import from_holdings, parse_ishares_csv, yahoo_market_caps
from market_monitor.equities.universe import EquityUniverse
from market_monitor.exceptions import DataProviderError, MarketMonitorError, ProviderUnavailableError
from market_monitor.network import blocked_reason, build_session

EXIT_OK, EXIT_MISSING, EXIT_ERROR = 0, 1, 2
SSL_HELP = "disable SSL verification (corporate proxy, trusted network only)"
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "text/csv,*/*"}
#: hook for tests: builds the universe from the settings
universe_factory: Callable[[Settings], EquityUniverse] | None = None


def add_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    cons = sub.add_parser("constituents", help="index compositions (config/constituents)")
    actions = cons.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="indices, members, composition date and weights")
    refresh = actions.add_parser("refresh", help="update compositions from their source")
    refresh.add_argument("--index", nargs="*", default=None)
    imp = actions.add_parser("import", help="read an iShares holdings CSV downloaded by hand")
    imp.add_argument("file")
    imp.add_argument("--index", required=True)
    check = actions.add_parser("check", help="check that every member's Yahoo symbol answers")
    check.add_argument("--index", nargs="*", default=None)
    for command in (refresh, check):
        command.add_argument("--contourner-ssl", action="store_true", help=SSL_HELP)

    eq = sub.add_parser("equities", help="members' prices (top movers, breadth)")
    eq_actions = eq.add_subparsers(dest="action", required=True)
    load = eq_actions.add_parser("refresh", help="download the missing closes (resumable)")
    load.add_argument("--index", nargs="*", default=None)
    load.add_argument("--avec-stoxx600", dest="extended", action="store_true",
                      help="also the extended indices (Stoxx 600: ~600 more symbols)")
    load.add_argument("--contourner-ssl", action="store_true", help=SSL_HELP)
    eq_actions.add_parser("status", help="prices held per index")


def universe(settings: Settings) -> EquityUniverse:
    if universe_factory is not None:
        return universe_factory(settings)
    net = settings.network
    return EquityUniverse.from_settings(settings.equities, session=build_session(net.insecure_ssl,
                                                                                  net.ca_bundle))


def run(settings: Settings, args: argparse.Namespace) -> int:
    if getattr(args, "contourner_ssl", False):
        settings = replace(settings, network=NetworkSettings(insecure_ssl=True))
    try:
        uni = universe(settings)
        if args.command == "constituents":
            return {"list": _list, "refresh": _refresh, "import": _import, "check": _check}[args.action](
                settings, uni, args)
        return {"refresh": _load, "status": _status}[args.action](settings, uni, args)
    except MarketMonitorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _list(settings: Settings, uni: EquityUniverse, args: argparse.Namespace) -> int:
    rows = [{"Indice": c.index, "Nom": c.name, "Groupe": c.group, "Titres": len(c.members),
             "Composition du": c.as_of or "–", "Poids": c.weights, "Source": c.source}
            for c in uni.compositions]
    print(pd.DataFrame(rows).to_string(index=False) if rows else "aucune composition")
    return EXIT_OK


class _Fetcher:
    """GET a page or a file; remembers the last answer so a wrong one can be shown."""

    def __init__(self, settings: Settings) -> None:
        self._session = rest_session(settings) or requests.Session()
        self._timeout = settings.free.timeout_s
        self.last: requests.Response | None = None

    def __call__(self, url: str) -> str:
        if "ishares.com" in url and "siteEntryPassthrough" not in url:
            url += ("&" if "?" in url else "?") + "siteEntryPassthrough=true"  # skips the entry page
        try:
            response = self._session.get(url, headers=BROWSER_HEADERS, timeout=self._timeout)
        except requests.exceptions.SSLError as exc:
            raise DataProviderError(blocked_reason([str(exc)])) from exc
        except requests.RequestException as exc:
            raise DataProviderError(f"{type(exc).__name__} sur {url[:80]}") from exc
        self.last = response
        if response.status_code >= 400:
            raise DataProviderError(f"HTTP {response.status_code} on {url[:80]}")
        response.encoding = response.encoding or "utf-8"
        return str(response.text)

    def describe_last(self, folder: Path, index: str) -> str:
        """Save the last answer (a proxy page, a disclaimer...) and say what it was."""
        response = self.last
        if response is None:
            return ""
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{index}_reponse.html"
        path.write_bytes(response.content)
        kind = response.headers.get("Content-Type", "?").split(";")[0]
        match = re.search(r"<title[^>]*>(.*?)</title>", response.text[:20_000], re.I | re.S)
        title = f", page « {' '.join(match.group(1).split())[:80]} »" if match else ""
        return f"réponse reçue : {kind}{title}, enregistrée dans {path}"


def _fetch_text(settings: Settings) -> _Fetcher:
    return _Fetcher(settings)


def _fmp_list(settings: Settings) -> Callable[[str], list[dict[str, Any]]] | None:
    provider = build_provider("fmp", settings)
    if not isinstance(provider, FMPProvider) or not provider.is_available():
        return None

    def fetch(path: str) -> list[dict[str, Any]]:
        payload = provider.get_json(path)
        return [r for r in payload if isinstance(r, dict)] if isinstance(payload, list) else []
    return fetch


def _refresh(settings: Settings, uni: EquityUniverse, args: argparse.Namespace) -> int:
    indices = args.index or [c.index for c in uni.compositions]
    net = settings.network
    yahoo = yahoo_market_caps(build_session(net.insecure_ssl, net.ca_bundle))
    down: list[str] = []  # Yahoo found blocked once: not asked again for the other indices

    def caps(tickers: list[str]) -> dict[str, float]:
        if down:
            raise ProviderUnavailableError(down[0])
        try:
            return yahoo(tickers)
        except ProviderUnavailableError as exc:
            down.append(str(exc))
            raise
    code = EXIT_OK
    debug = settings.equities.directory / "debug"
    for index in indices:
        fetch = _fetch_text(settings)
        try:
            result = uni.refresh_composition(index, fetch_text=fetch, fmp_list=_fmp_list(settings),
                                             market_caps=caps)
        except DataProviderError as exc:
            seen = fetch.describe_last(debug, index) if "holdings file" in str(exc) else ""
            print(f"{index} : échec ({exc}){' - ' + seen if seen else ''}. Composition inchangée.",
                  file=sys.stderr)
            if uni.compositions.get(index).refresh.get("kind") == "ishares" or "holdings" in str(exc):
                print(f"   -> télécharger le fichier des positions dans le navigateur puis "
                      f"`market-monitor constituents import FICHIER --index {index}`", file=sys.stderr)
            code = EXIT_MISSING
            continue
        print(result.summary())
    return code


def _import(settings: Settings, uni: EquityUniverse, args: argparse.Namespace) -> int:
    comp = uni.compositions.get(args.index)
    text = Path(args.file).read_bytes().decode("utf-8-sig", errors="replace")
    result = from_holdings(comp, parse_ishares_csv(text, comp.refresh.get("overrides")),
                           f"iShares (fichier {Path(args.file).name})")
    save_composition(result.composition)
    uni.compositions.replace(result.composition)
    print(result.summary())
    return EXIT_OK


def _check(settings: Settings, uni: EquityUniverse, args: argparse.Namespace) -> int:
    indices = args.index or uni.indices()
    tickers = uni.compositions.tickers(indices)
    end = date.today()
    missing: list[str] = []
    size = settings.equities.chunk_size
    for k in range(0, len(tickers), size):
        chunk = tickers[k:k + size]
        found = uni.fetcher(chunk, end - timedelta(days=10), end)
        missing += [t for t in chunk if t not in found or found[t].empty]
    print(f"{len(tickers) - len(missing)} / {len(tickers)} symboles répondent sur Yahoo.")
    for ticker in missing:
        member = uni.compositions.member_info().get(ticker)
        print(f"  sans réponse : {ticker} ({member.name if member else '?'})")
    return EXIT_OK if not missing else EXIT_MISSING


def _load(settings: Settings, uni: EquityUniverse, args: argparse.Namespace) -> int:
    indices = args.index or uni.indices(args.extended)
    symbols = uni.symbols(indices)
    print(f"{len(symbols)} symboles ({', '.join(indices)}), par paquets de {settings.equities.chunk_size}…")
    report = uni.refresh_prices(indices, progress=lambda done, total: print(
        f"  {done}/{total}", end="\r", flush=True))
    print()
    print(report.summary())
    return EXIT_OK if not report.stopped else EXIT_MISSING


def _status(settings: Settings, uni: EquityUniverse, args: argparse.Namespace) -> int:
    table = uni.status()
    if table.empty:
        print("aucune composition")
        return EXIT_MISSING
    labels = {"index": "Indice", "name": "Nom", "group": "Groupe", "members": "Titres",
              "loaded": "Avec cours", "last_close": "Dernière clôture", "as_of": "Composition du",
              "weights": "Poids"}
    print(table[list(labels)].rename(columns=labels).fillna("–").to_string(index=False))
    return EXIT_OK
