"""Sovereign-rate sources tested from the user's workstation (``market-monitor diagnostic-taux``).

The rates come from unofficial or picky servers (CNBC, Bundesbank) that answer differently
on a corporate network. This report shows, for each source, the raw HTTP answer (status
and first characters) and what the client reads from it - enough to fix a source remotely.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from functools import partial

import pandas as pd
import requests

from market_monitor.data.providers.free import DEFAULT_ECB_URL, ECBClient
from market_monitor.data.providers.public import (
    BUNDESBANK_URL,
    CNBC_URL,
    SDMX_CSV,
    BundesbankClient,
    CnbcClient,
)
from market_monitor.exceptions import MarketMonitorError

BUND_10Y_KEY = "D.I.ZST.ZI.EUR.S1311.B.A604.R10XX.R.A.A._Z._Z.A"
ECB_PATTERN = "D.DE+FR+IT+ES....YLD"


def rate_sources_report(session: requests.Session | None, today: date,
                        ecb_base_url: str | None = None) -> list[str]:
    """Lines of the report (raw answer + parsed values per source)."""
    session = session or requests.Session()
    start, end = today - timedelta(days=10), today
    lines: list[str] = []

    bbk_url = f"{BUNDESBANK_URL}/BBSIS/{BUND_10Y_KEY}"
    bbk_params = {"startPeriod": start.isoformat(), "endPeriod": end.isoformat()}
    lines += _raw("Bundesbank, Bund 10 ans", session, bbk_url, bbk_params, {"Accept": SDMX_CSV})
    lines += _parsed(lambda: BundesbankClient(session=session, max_retries=0)
                     .fetch(f"BBSIS/{BUND_10Y_KEY}", start, end))

    for symbol in ("DE10Y-DE", "FR10Y-FR"):
        url = (f"{CNBC_URL}/{symbol}/1D/{start:%Y%m%d}000000/{end:%Y%m%d}235959"
               "/adjusted/EST5EDT.json")
        lines += _raw(f"CNBC, {symbol}", session, url, None, None)
        cnbc = CnbcClient(session=session, max_retries=0)
        lines += _parsed(partial(cnbc.fetch, symbol, start, end))

    lines.append(f"== BCE, recherche FM/{ECB_PATTERN} (rendements quotidiens par pays)")
    try:
        found = ECBClient(ecb_base_url or DEFAULT_ECB_URL, session=session).search("FM", ECB_PATTERN)
        lines.append(f"   {len(found)} série(s)")
        lines += [f"   {row['KEY']}  {row.get('TIME_PERIOD', '')} = {row.get('OBS_VALUE', '')}"
                  for _, row in found.head(20).iterrows()]
    except MarketMonitorError as exc:
        lines.append(f"   erreur : {exc}")
    return lines


def _raw(label: str, session: requests.Session, url: str, params: dict[str, str] | None,
         headers: dict[str, str] | None) -> list[str]:
    lines = [f"== {label}"]
    try:
        response = session.get(url, params=params, headers=headers, timeout=20)
        body = " ".join(str(response.text).split())[:300]
        kind = response.headers.get("Content-Type", "?") if hasattr(response, "headers") else "?"
        lines.append(f"   HTTP {response.status_code} ({kind}) : {body}")
    except requests.RequestException as exc:
        lines.append(f"   échec réseau : {type(exc).__name__}: {str(exc)[:200]}")
    return lines


def _parsed(fetch: Callable[[], pd.Series]) -> list[str]:
    try:
        series = fetch()
    except MarketMonitorError as exc:
        return [f"   lu : erreur - {exc}"]
    if series.empty:
        return ["   lu : aucune donnée"]
    return [f"   lu : {d:%d/%m} = {v}" for d, v in series.tail(3).items()]
