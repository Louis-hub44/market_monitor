"""Keyless public sources used by the free provider for what Yahoo / the ECB do not cover.

Native tickers (the prefix routes to the source, see :class:`FreeProvider`):
    * ``stooq:<symbol>`` - Stooq daily CSV; sovereign yields in % such as ``stooq:10dey.b``
      (Bund 10Y), ``stooq:2fry.b`` (OAT 2Y), ``stooq:10ity.b`` (BTP), ``stooq:30esy.b`` (Bonos);
    * ``bbk:<flow>/<key>`` - Deutsche Bundesbank SDMX web service, e.g. the Svensson
      zero-coupon curve of listed Federal securities
      ``bbk:BBSIS/D.I.ZST.ZI.EUR.S1311.B.A604.R10XX.R.A.A._Z._Z.A`` (10Y);
    * ``fred:<series>`` - St. Louis Fed FRED, e.g. ``fred:DGS2`` (UST 2Y constant maturity);
    * ``stoxx:<symbol>`` - STOXX historical index file, e.g. ``stoxx:v2tx`` (VSTOXX).

Each client returns one cleaned :class:`pandas.Series` (date -> value) for ``[start, end]``.
"""

from __future__ import annotations

import io
from datetime import date

import pandas as pd
import requests

from market_monitor.data.providers._http import get_with_retry
from market_monitor.data.quality import clean_series
from market_monitor.exceptions import DataProviderError

STOOQ_URL = "https://stooq.com/q/d/l/"
BUNDESBANK_URL = "https://api.statistiken.bundesbank.de/rest/data"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
STOXX_URL = "https://www.stoxx.com/document/Indices/Current/HistoricalData"
SDMX_CSV = "application/vnd.sdmx.data+csv;version=1.0.0"


class _CsvClient:
    """Shared plumbing: session, retries, HTTP status -> :class:`DataProviderError`."""

    source = "?"
    default_url = ""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout_s: float = 20.0,
        max_retries: int = 2,
        session: requests.Session | None = None,
    ) -> None:
        self._base_url = (base_url or self.default_url).rstrip("/")
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._session = session or requests.Session()

    def _get(self, url: str, params: dict[str, str], key: str,
             headers: dict[str, str] | None = None) -> str | None:
        """Body of a successful response, ``None`` on 404 (no data for the window)."""
        response = get_with_retry(
            self._session, url, params=params, timeout_s=self._timeout_s,
            max_retries=self._max_retries, backoff_s=1.0, source=self.source, headers=headers,
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise DataProviderError(f"{self.source} HTTP {response.status_code} for {key}")
        return str(response.text)


def _window(series: pd.Series, start: date, end: date) -> pd.Series:
    cleaned = clean_series(series)
    return cleaned.loc[pd.Timestamp(start):pd.Timestamp(end)]


# ---------------------------------------------------------------------- Stooq
class StooqClient(_CsvClient):
    """Stooq daily history (``Date,Open,High,Low,Close[,Volume]``), close column."""

    source = "Stooq"
    default_url = STOOQ_URL

    def fetch(self, symbol: str, start: date, end: date) -> pd.Series:
        text = self._get(self._base_url + "/", {
            "s": symbol.lower(), "i": "d",
            "d1": start.strftime("%Y%m%d"), "d2": end.strftime("%Y%m%d"),
        }, symbol)
        return _window(parse_stooq_csv(text or "", symbol), start, end)


def parse_stooq_csv(text: str, symbol: str = "") -> pd.Series:
    """Close prices of a Stooq CSV; Stooq answers ``No data`` (or a quota message) in plain text."""
    body = text.strip()
    if not body or body.lower().startswith("no data"):
        return clean_series(None)
    if not body.lower().startswith("date"):
        raise DataProviderError(f"Stooq refused {symbol}: {body.splitlines()[0][:120]}")
    frame = pd.read_csv(io.StringIO(body))
    if "Close" not in frame.columns:
        raise DataProviderError(f"Stooq CSV without Close column for {symbol}")
    return clean_series(pd.Series(frame["Close"].to_numpy(), index=frame["Date"]))


# ----------------------------------------------------------------- Bundesbank
class BundesbankClient(_CsvClient):
    """Deutsche Bundesbank SDMX REST web service (SDMX-CSV)."""

    source = "Bundesbank"
    default_url = BUNDESBANK_URL

    def fetch(self, series_key: str, start: date, end: date) -> pd.Series:
        flow, sep, key = series_key.partition("/")
        if not sep or not flow or not key:
            raise DataProviderError(f"invalid Bundesbank key {series_key!r} (expected FLOW/KEY)")
        text = self._get(f"{self._base_url}/{flow}/{key}",
                         {"startPeriod": start.isoformat(), "endPeriod": end.isoformat()},
                         series_key, headers={"Accept": SDMX_CSV})
        return _window(parse_sdmx_csv(text or "", self.source), start, end)


def parse_sdmx_csv(text: str, source: str = "SDMX") -> pd.Series:
    """``TIME_PERIOD`` -> ``OBS_VALUE`` of an SDMX-CSV payload (comma or semicolon)."""
    if not text.strip():
        return clean_series(None)
    try:
        frame = pd.read_csv(io.StringIO(text), sep=None, engine="python")
    except (pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        raise DataProviderError(f"unparseable {source} CSV: {exc}") from exc
    if not {"TIME_PERIOD", "OBS_VALUE"} <= set(frame.columns):
        raise DataProviderError(f"{source} CSV without TIME_PERIOD / OBS_VALUE columns")
    return clean_series(pd.Series(frame["OBS_VALUE"].to_numpy(), index=frame["TIME_PERIOD"]))


# ----------------------------------------------------------------------- FRED
class FredClient(_CsvClient):
    """FRED graph CSV export (no API key): first column date, second column value."""

    source = "FRED"
    default_url = FRED_URL

    def fetch(self, series_id: str, start: date, end: date) -> pd.Series:
        text = self._get(self._base_url, {"id": series_id, "cosd": start.isoformat(),
                                          "coed": end.isoformat()}, series_id)
        return _window(parse_fred_csv(text or "", series_id), start, end)


def parse_fred_csv(text: str, series_id: str = "") -> pd.Series:
    """FRED CSV (``observation_date,<ID>``; missing values are ``.``)."""
    body = text.strip()
    if not body:
        return clean_series(None)
    frame = pd.read_csv(io.StringIO(body), na_values=["."])
    if frame.shape[1] < 2:
        raise DataProviderError(f"unexpected FRED CSV for {series_id}: {body.splitlines()[0][:120]}")
    values = pd.to_numeric(frame.iloc[:, 1], errors="coerce")
    return clean_series(pd.Series(values.to_numpy(), index=frame.iloc[:, 0]))


# ---------------------------------------------------------------------- STOXX
class StoxxClient(_CsvClient):
    """STOXX historical index file ``h_<symbol>.txt`` (``Date;Symbol;Indexvalue``, dd.mm.yyyy)."""

    source = "STOXX"
    default_url = STOXX_URL

    def fetch(self, symbol: str, start: date, end: date) -> pd.Series:
        text = self._get(f"{self._base_url}/h_{symbol.lower()}.txt", {}, symbol)
        return _window(parse_stoxx_txt(text or "", symbol), start, end)


def parse_stoxx_txt(text: str, symbol: str = "") -> pd.Series:
    body = text.strip()
    if not body:
        return clean_series(None)
    if body.lstrip().startswith("<"):
        raise DataProviderError(f"STOXX returned a web page instead of data for {symbol}")
    frame = pd.read_csv(io.StringIO(body), sep=";", skipinitialspace=True)
    frame.columns = [str(c).strip() for c in frame.columns]
    if frame.shape[1] < 2 or "Date" not in frame.columns:
        raise DataProviderError(f"unexpected STOXX file for {symbol}")
    dates = pd.to_datetime(frame["Date"].astype(str).str.strip(), format="%d.%m.%Y", errors="coerce")
    values = pd.to_numeric(frame.iloc[:, -1], errors="coerce")
    return clean_series(pd.Series(values.to_numpy(), index=dates))
