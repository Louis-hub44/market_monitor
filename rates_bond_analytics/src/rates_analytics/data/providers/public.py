"""Keyless public sources: sovereign yields, credit spreads, indices (ported from Market Monitor).

Spec: ``public: {ticker: "cnbc:DE10Y-DE|bbk:BBSIS/…|stooq:10dey.b", scale: 0.01}``. The
``|``-separated alternatives are tried **in order** and the first non-empty series wins,
so one source being down (Stooq's anti-bot page, a host filtered by the proxy) does not
lose the id. Prefixes:

* ``cnbc:<symbol>``  – CNBC daily bars (unofficial endpoint), yields in %: ``DE10Y-DE``,
  ``FR2Y-FR``, ``IT30Y-IT``, ``ES5Y-ES``. Same source for every country, so spreads are
  consistent;
* ``bbk:<flow>/<key>`` – Deutsche Bundesbank SDMX web service, e.g. the official Svensson
  zero-coupon curve of Federal securities ``BBSIS/D.I.ZST.ZI.EUR.S1311.B.A604.R10XX.R.A.A._Z._Z.A``;
* ``stooq:<symbol>`` – Stooq daily CSV (``10dey.b`` = Bund 10Y), often behind an anti-bot page;
* ``fred:<SERIES>`` – FRED graph CSV export, no key (``BAA10Y``, ``VIXCLS``, ``BAMLHE00EHYIOAS``…).
  ICE BofA series are limited by FRED to the last 3 years;
* ``fredapi:<SERIES>`` – official FRED web API (other host, free key in ``FRED_API_KEY``), useful
  when the proxy filters ``fred.stlouisfed.org`` (chain ``fred:X|fredapi:X``);
* ``yahoo:<symbol>`` – Yahoo Finance chart API, adjusted close (``^STOXX50E``, ``IEAC.L``,
  ``EURUSD=X``…), through the same session as everything else (no yfinance needed).

A source that is unreachable as a whole (network, anti-bot page) is skipped for the rest
of the request (circuit breaker) instead of timing out once per id.
"""

from __future__ import annotations

import io
import logging
import os
from collections.abc import Sequence
from datetime import date
from typing import Any
from urllib.parse import quote

import numpy as np
import pandas as pd

from rates_analytics.data.mapping import MarketDataMap
from rates_analytics.data.provider import Capability, DataProvider, empty_history
from rates_analytics.data.providers.http import HttpClient
from rates_analytics.exceptions import DataProviderError

logger = logging.getLogger(__name__)

CNBC_URL = "https://ts-api.cnbc.com/harmony/app/bars"
BUNDESBANK_URL = "https://api.statistiken.bundesbank.de/rest/data"
STOOQ_URL = "https://stooq.com/q/d/l/"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
ENV_FRED_API_KEY = "FRED_API_KEY"
SDMX_CSV = "application/vnd.sdmx.data+csv;version=1.0.0"


class SourceUnavailable(DataProviderError):
    """The whole source is unusable (anti-bot page, network): skip its other tickers."""


def _clean(values: Any, index: Any) -> pd.Series:
    s = pd.Series(pd.to_numeric(pd.Series(values, dtype="object"), errors="coerce").to_numpy(dtype=float),
                  index=pd.DatetimeIndex(pd.to_datetime(index, errors="coerce")).normalize())
    s = s[s.index.notna()].replace([np.inf, -np.inf], np.nan).dropna().sort_index()
    s = s[~s.index.duplicated(keep="last")]
    s.index.name = "date"
    return s


# --------------------------------------------------------------------- parsers
def parse_cnbc_bars(payload: Any, symbol: str = "") -> pd.Series:
    """``barData.priceBars[]`` -> close by date (``tradeTime`` ``YYYYMMDDhhmmss`` or epoch ms)."""
    bar_data = payload.get("barData") if isinstance(payload, dict) else None
    if not isinstance(bar_data, dict):
        raise DataProviderError(f"unexpected CNBC payload for {symbol}")
    dates: list[Any] = []
    closes: list[Any] = []
    for bar in bar_data.get("priceBars") or []:
        if not isinstance(bar, dict):
            continue
        stamp = str(bar.get("tradeTime") or "")
        if len(stamp) >= 8 and stamp[:8].isdigit():
            dates.append(f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}")
        else:
            try:
                dates.append(pd.Timestamp(int(float(bar.get("tradeTimeinMills") or "")), unit="ms"))
            except (TypeError, ValueError):
                dates.append(None)
        closes.append(bar.get("close"))
    return _clean(closes, dates)


def parse_sdmx_csv(text: str, source: str = "SDMX") -> pd.Series:
    """``TIME_PERIOD`` -> ``OBS_VALUE`` of an SDMX-CSV payload (comma or semicolon)."""
    if not text.strip():
        return _clean([], [])
    try:
        frame = pd.read_csv(io.StringIO(text), sep=None, engine="python")
    except (pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        raise DataProviderError(f"unparseable {source} CSV: {exc}") from exc
    if not {"TIME_PERIOD", "OBS_VALUE"} <= set(frame.columns):
        raise DataProviderError(f"{source} CSV without TIME_PERIOD / OBS_VALUE columns")
    return _clean(frame["OBS_VALUE"].to_numpy(), frame["TIME_PERIOD"])


def parse_stooq_csv(text: str, symbol: str = "") -> pd.Series:
    """Close column of a Stooq CSV; Stooq answers ``No data`` or an anti-bot HTML page."""
    body = text.strip()
    if not body or body.lower().startswith("no data"):
        return _clean([], [])
    if body.startswith("<"):
        raise SourceUnavailable("Stooq unreachable (anti-bot page requiring JavaScript)")
    if not body.lower().startswith("date"):
        raise DataProviderError(f"Stooq refused {symbol}: {body.splitlines()[0][:120]}")
    frame = pd.read_csv(io.StringIO(body))
    if "Close" not in frame.columns:
        raise DataProviderError(f"Stooq CSV without Close column for {symbol}")
    return _clean(frame["Close"].to_numpy(), frame["Date"])


def parse_fred_csv(text: str, series_id: str = "") -> pd.Series:
    """FRED graph CSV (``observation_date,<ID>`` or ``DATE,<ID>``; missing values are ``.``)."""
    body = text.strip()
    if not body:
        return _clean([], [])
    if body.startswith("<"):
        raise SourceUnavailable("FRED answered an HTML page (filtered by the proxy?)")
    frame = pd.read_csv(io.StringIO(body), na_values=["."])
    if frame.shape[1] < 2:
        raise DataProviderError(f"unexpected FRED CSV for {series_id}: {body.splitlines()[0][:120]}")
    return _clean(frame.iloc[:, 1].to_numpy(), frame.iloc[:, 0])


def parse_fred_observations(payload: Any, series_id: str = "") -> pd.Series:
    """FRED API JSON ``observations[] -> {date, value}`` (missing values are ``.``)."""
    observations = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(observations, list):
        raise DataProviderError(f"unexpected FRED API payload for {series_id}")
    rows = [o for o in observations if isinstance(o, dict)]
    return _clean([o.get("value") for o in rows], [o.get("date") for o in rows])


def parse_yahoo_chart(payload: Any, symbol: str = "") -> pd.Series:
    """Yahoo chart JSON -> adjusted close (close if absent) by exchange-local date."""
    chart = payload.get("chart") if isinstance(payload, dict) else None
    if not isinstance(chart, dict):
        raise DataProviderError(f"unexpected Yahoo payload for {symbol}")
    if chart.get("error"):
        err = chart["error"]
        raise DataProviderError(f"Yahoo refused {symbol}: {err.get('description', err) if isinstance(err, dict) else err}")
    results = chart.get("result") or []
    if not results:
        return _clean([], [])
    res = results[0]
    stamps = res.get("timestamp") or []
    offset = int((res.get("meta") or {}).get("gmtoffset") or 0)
    indicators = res.get("indicators") or {}
    adj = (indicators.get("adjclose") or [{}])[0].get("adjclose")
    closes = adj if adj else (indicators.get("quote") or [{}])[0].get("close") or []
    dates = pd.to_datetime([int(t) + offset for t in stamps], unit="s")
    return _clean(list(closes)[: len(dates)], dates[: len(closes)])


# -------------------------------------------------------------------- provider
class PublicProvider(DataProvider):
    """CNBC / Bundesbank / Stooq / FRED / Yahoo chains (no key, except the optional FRED API key)."""

    name = "public"
    capabilities = frozenset({Capability.HISTORY, Capability.QUOTES})

    def __init__(self, mapping: MarketDataMap | None = None, timeout_s: float = 20.0, retries: int = 1,
                 session: Any | None = None, quote_lookback_days: int = 10,
                 cnbc_url: str = CNBC_URL, bundesbank_url: str = BUNDESBANK_URL, stooq_url: str = STOOQ_URL,
                 fred_url: str = FRED_URL, fred_api_url: str = FRED_API_URL, yahoo_url: str = YAHOO_URL,
                 fred_api_key: str | None = None) -> None:
        super().__init__(mapping, quote_lookback_days)
        self.fred_api_key = (fred_api_key if fred_api_key is not None else os.environ.get(ENV_FRED_API_KEY, "")).strip()
        self.http = HttpClient(timeout_s, retries, session=session, secrets=(self.fred_api_key,))
        self.urls = {"cnbc": cnbc_url.rstrip("/"), "bbk": bundesbank_url.rstrip("/"), "stooq": stooq_url,
                     "fred": fred_url, "fredapi": fred_api_url, "yahoo": yahoo_url.rstrip("/")}

    # ---------------------------------------------------------------- sources
    def _cnbc(self, symbol: str, start: date, end: date) -> pd.Series:
        url = f"{self.urls['cnbc']}/{symbol}/1D/{start:%Y%m%d}000000/{end:%Y%m%d}235959/adjusted/EST5EDT.json"
        resp = self.http.get(url, allow_404=True)
        if resp is None:
            return _clean([], [])
        try:
            payload = resp.json()
        except ValueError as exc:
            raise DataProviderError(f"CNBC returned no JSON for {symbol}") from exc
        return parse_cnbc_bars(payload, symbol)

    def _bbk(self, key: str, start: date, end: date) -> pd.Series:
        flow, sep, series = key.partition("/")
        if not sep:
            raise DataProviderError(f"invalid Bundesbank key {key!r} (expected FLOW/KEY)")
        resp = self.http.get(f"{self.urls['bbk']}/{flow}/{series}", allow_404=True,
                             params={"startPeriod": start.isoformat(), "endPeriod": end.isoformat()},
                             headers={"Accept": SDMX_CSV})
        return parse_sdmx_csv(resp.text if resp is not None else "", "Bundesbank")

    def _stooq(self, symbol: str, start: date, end: date) -> pd.Series:
        resp = self.http.get(self.urls["stooq"], allow_404=True,
                             params={"s": symbol.lower(), "i": "d", "d1": f"{start:%Y%m%d}", "d2": f"{end:%Y%m%d}"})
        return parse_stooq_csv(resp.text if resp is not None else "", symbol)

    def _fred(self, series_id: str, start: date, end: date) -> pd.Series:
        resp = self.http.get(self.urls["fred"], allow_404=True,
                             params={"id": series_id, "cosd": start.isoformat(), "coed": end.isoformat()})
        return parse_fred_csv(resp.text if resp is not None else "", series_id)

    def _fredapi(self, series_id: str, start: date, end: date) -> pd.Series:
        if not self.fred_api_key:
            raise SourceUnavailable(f"FRED API: no key ({ENV_FRED_API_KEY} missing from .env)")
        resp = self.http.get(self.urls["fredapi"], allow_404=True,
                             params={"series_id": series_id, "api_key": self.fred_api_key, "file_type": "json",
                                     "observation_start": start.isoformat(), "observation_end": end.isoformat()})
        if resp is None:
            return _clean([], [])
        try:
            payload = resp.json()
        except ValueError as exc:
            raise DataProviderError(f"FRED API returned no JSON for {series_id}") from exc
        return parse_fred_observations(payload, series_id)

    def _yahoo(self, symbol: str, start: date, end: date) -> pd.Series:
        p1 = int(pd.Timestamp(start).timestamp())
        p2 = int((pd.Timestamp(end) + pd.Timedelta(days=1)).timestamp())
        resp = self.http.get(f"{self.urls['yahoo']}/{quote(symbol, safe='=.-')}", allow_404=True,
                             params={"period1": p1, "period2": p2, "interval": "1d", "events": "div,splits"})
        if resp is None:
            return _clean([], [])
        try:
            payload = resp.json()
        except ValueError as exc:
            raise SourceUnavailable(f"Yahoo returned no JSON for {symbol} (consent / anti-bot page?)") from exc
        return parse_yahoo_chart(payload, symbol)

    def fetch_ticker(self, ticker: str, start: date, end: date, broken: set[str] | None = None) -> tuple[pd.Series, str]:
        """First non-empty series of a ``a|b|c`` chain -> (series, source used)."""
        broken = set() if broken is None else broken
        errors = []
        for alt in (t.strip() for t in ticker.split("|") if t.strip()):
            prefix, _, sym = alt.partition(":")
            fetch = {"cnbc": self._cnbc, "bbk": self._bbk, "stooq": self._stooq, "fred": self._fred,
                     "fredapi": self._fredapi, "yahoo": self._yahoo}.get(prefix)
            if fetch is None:
                raise DataProviderError(f"unknown public source prefix in {alt!r} "
                                        "(cnbc / bbk / stooq / fred / fredapi / yahoo)")
            if prefix in broken:
                continue
            try:
                s = fetch(sym, start, end)
            except SourceUnavailable as exc:
                broken.add(prefix)
                errors.append(str(exc))
                continue
            except DataProviderError as exc:
                if "failed after" in str(exc):   # network-level failure: the whole host is down
                    broken.add(prefix)
                errors.append(str(exc))
                continue
            s = s.loc[pd.Timestamp(start):pd.Timestamp(end)]
            if not s.empty:
                return s, prefix
        if errors and len(errors) == len([t for t in ticker.split("|") if t.strip()]):
            raise DataProviderError("; ".join(errors))
        return _clean([], []), ""

    def get_history(self, ids: Sequence[str], start: date, end: date) -> pd.DataFrame:
        cols: dict[str, pd.Series] = {}
        broken: set[str] = set()
        failures: list[str] = []
        for mid in ids:
            spec = self.mapping.spec(mid, self.name)
            if spec is None:
                continue
            try:
                s, _ = self.fetch_ticker(str(spec["ticker"]), start, end, broken)
            except DataProviderError as exc:
                failures.append(f"{mid}: {exc}")
                continue
            if not s.empty:
                cols[mid] = s * float(spec.get("scale", 1.0))
        if not cols:
            if failures:
                raise DataProviderError("public sources failed – " + " | ".join(failures[:3]))
            return empty_history()
        if failures:
            logger.warning("public provider: %d id(s) failed, e.g. %s", len(failures), failures[0])
        out = pd.concat(cols, axis=1)
        out.index.name = "date"
        return out
