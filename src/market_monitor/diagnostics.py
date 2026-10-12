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

from market_monitor.analytics.performance import required_start
from market_monitor.data.providers.free import DEFAULT_ECB_URL, ECBClient
from market_monitor.data.providers.public import (
    BUNDESBANK_URL,
    CNBC_URL,
    SDMX_CSV,
    BundesbankClient,
    CnbcClient,
)
from market_monitor.exceptions import MarketMonitorError
from market_monitor.monitor import MarketMonitor

BUND_10Y_KEY = "D.I.ZST.ZI.EUR.S1311.B.A604.R10XX.R.A.A._Z._Z.A"
#: ECB "FM" keys have 7 dimensions: FREQ.REF_AREA.CURRENCY.PROVIDER.INSTRUMENT.PROVIDER_ID.DATA_TYPE
ECB_PATTERNS = ("D.DE+FR+IT+ES.....YLD", "B.DE+FR+IT+ES.....YLD")


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

    for pattern in ECB_PATTERNS:
        lines.append(f"== BCE, recherche FM/{pattern} (rendements quotidiens par pays)")
        try:
            found = ECBClient(ecb_base_url or DEFAULT_ECB_URL, session=session).search("FM", pattern)
            lines.append(f"   {len(found)} série(s)")
            lines += [f"   {row['KEY']}  {row.get('TIME_PERIOD', '')} = {row.get('OBS_VALUE', '')}"
                      for _, row in found.head(30).iterrows()]
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


REVIEW_IDS = ("BUND_10Y", "OAT_10Y")
LONG_WINDOW_DAYS = 460  # the review loads ~15 months (YTD reference + z-score window)


def review_report(monitor: MarketMonitor, today: date,
                  session: requests.Session | None = None) -> list[str]:
    """What the morning review really loads for the Bund and the OAT.

    Configured chain, then the same history call as the review (cache included, ~15 months):
    serving source, fallback warnings, errors and last closes - plus a long raw CNBC request,
    to tell a refused long window from a transient refusal.
    """
    ref = monitor.referential
    lines = ["== Revue : chaînes configurées"]
    for instrument_id in REVIEW_IDS:
        spec = ref.get(instrument_id).tickers.get("free")
        lines.append(f"   {instrument_id} : {spec.ticker if spec else '(pas de ticker gratuit)'}")
    start = required_start(today, monitor.analytics)
    lines.append(f"== Revue : chargement du {start:%d/%m/%Y} au {today:%d/%m/%Y} (cache compris)")
    try:
        hist = monitor.history(list(REVIEW_IDS), start, today)
    except MarketMonitorError as exc:
        return [*lines, f"   erreur : {exc}"]
    for instrument_id in REVIEW_IDS:
        column = hist.levels[instrument_id].dropna() if instrument_id in hist.levels else pd.Series()
        last = ", ".join(f"{d:%d/%m} = {v:.4f}" for d, v in column.tail(3).items()) or "aucune"
        lines.append(f"   {instrument_id} : servi par {hist.origins.get(instrument_id, '?')} "
                     f"({hist.sources.get(instrument_id, '?')}) ; dernières valeurs : {last}")
        for label, messages in (("avertissement", hist.warnings), ("erreur", hist.errors)):
            if instrument_id in messages:
                lines.append(f"      {label} : {messages[instrument_id]}")
        for note in hist.notes.get(instrument_id, []):
            lines.append(f"      à vérifier : {note}")
    long_start = today - timedelta(days=LONG_WINDOW_DAYS)
    url = (f"{CNBC_URL}/DE10Y-DE/1D/{long_start:%Y%m%d}000000/{today:%Y%m%d}235959"
           "/adjusted/EST5EDT.json")
    lines += _raw(f"CNBC, DE10Y-DE sur {LONG_WINDOW_DAYS} jours (fenêtre de la revue)",
                  session or requests.Session(), url, None, None)
    return lines
