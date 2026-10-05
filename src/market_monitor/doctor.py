"""Installation check: configuration files, cache, providers (optionally online)."""

from __future__ import annotations

import importlib.util
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from market_monitor.alerts import load_rules
from market_monitor.config import Settings
from market_monitor.data.factory import build_provider, rest_session
from market_monitor.exceptions import MarketMonitorError
from market_monitor.export import load_layout
from market_monitor.network import SSL_HINT, check_connectivity, describe, looks_like_ssl_error, proxy_env
from market_monitor.referential import Referential, load_referential

#: one ticker per provider for the online probe
PROBES = {"bloomberg": "SX5E Index", "fmp": "^GSPC", "free": "ecb:EST/B.EU000A2X2A25.WT"}
PROBE_DAYS = 10
UNAVAILABLE_HINTS = {"bloomberg": "blpapi non installé", "fmp": "FMP_API_KEY absente du .env"}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def run_checks(settings: Settings, *, online: bool = False) -> list[Check]:
    """Every check, never raising: a failure is reported, not thrown."""
    checks: list[Check] = []
    referential = _attempt(checks, "Référentiel", lambda: _referential(settings))
    if isinstance(referential, Referential):
        _attempt(checks, "Daily macro", lambda: _layout(settings, referential))
        _attempt(checks, "Alertes", lambda: _rules(settings, referential))
    _attempt(checks, "Cache", lambda: _cache(settings))
    checks.append(_network(settings))
    if online and {"fmp", "free"} & set(settings.priority):
        checks.append(_connectivity(settings))
    for name in settings.priority:
        checks.append(_provider(settings, name, online))
    return checks


def _attempt(checks: list[Check], name: str, fn: Callable[[], tuple[object, str]]) -> object:
    try:
        value, detail = fn()
    except (MarketMonitorError, OSError) as exc:
        checks.append(Check(name, False, str(exc)))
        return None
    checks.append(Check(name, True, detail))
    return value


def _referential(settings: Settings) -> tuple[Referential, str]:
    ref = load_referential(settings.instruments_path, settings.watchlists_path)
    return ref, f"{len(ref)} instruments, {len(ref.watchlist_names)} watchlists"


def _layout(settings: Settings, ref: Referential) -> tuple[object, str]:
    layout = load_layout(settings.daily_macro_path, ref)
    return layout, f"{len(layout.sections)} bandeaux, {len(layout.section_ids)} instruments"


def _rules(settings: Settings, ref: Referential) -> tuple[object, str]:
    rules = load_rules(settings.alerts_path, ref)
    return rules, f"{len(rules)} règles"


def _cache(settings: Settings) -> tuple[object, str]:
    if not settings.cache.enabled:
        return None, "désactivé"
    settings.cache.directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=settings.cache.directory):
        pass
    return None, f"accessible en écriture ({settings.cache.directory})"


def _network(settings: Settings) -> Check:
    net = settings.network
    detail = describe(net.insecure_ssl, net.ca_bundle)
    proxies = proxy_env()
    if proxies:
        detail += f" ; proxy : {', '.join(sorted(proxies))}"
    return Check("Réseau", net.ca_bundle is None or net.ca_bundle.is_file(), detail)


def _connectivity(settings: Settings) -> Check:
    diag = check_connectivity(rest_session(settings))
    detail = diag["message"] + (f" -> {diag['hint']}" if diag["hint"] else "")
    return Check("Connectivité", diag["kind"] == "ok", detail)


def _provider(settings: Settings, name: str, online: bool) -> Check:
    label = f"Provider {name}"
    provider = build_provider(name, settings)
    if not provider.is_available():
        return Check(label, False, UNAVAILABLE_HINTS.get(name, "indisponible"))
    if name == "free" and importlib.util.find_spec("yfinance") is None:
        return Check(label, False, "yfinance non installé (ECB seul disponible)")
    if not online:
        return Check(label, True, "disponible (non testé en ligne)")
    end = date.today()
    try:
        result = provider.get_history(PROBES[name], end - timedelta(days=PROBE_DAYS), end)
    except MarketMonitorError as exc:
        return Check(label, False, _with_ssl_hint(f"échec en ligne : {exc}"))
    if result.errors:
        return Check(label, False, _with_ssl_hint(f"{PROBES[name]} : {next(iter(result.errors.values()))}"))
    last = result.data[PROBES[name]].dropna()
    return Check(label, True, f"{PROBES[name]} = {last.iloc[-1]:g} au {last.index[-1]:%d/%m}")


def _with_ssl_hint(detail: str) -> str:
    return f"{detail} -> {SSL_HINT}" if looks_like_ssl_error(detail) else detail
