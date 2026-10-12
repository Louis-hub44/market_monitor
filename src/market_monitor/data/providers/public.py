"""Keyless public sources used by the free provider for what Yahoo / the ECB do not cover.

Native tickers (the prefix routes to the source, see :class:`FreeProvider`):
    * ``stooq:<symbol>`` - Stooq daily CSV; sovereign yields in % such as ``stooq:10dey.b``
      (Bund 10Y), ``stooq:2fry.b`` (OAT 2Y), ``stooq:10ity.b`` (BTP), ``stooq:30esy.b`` (Bonos);
    * ``bbk:<flow>/<key>`` - Deutsche Bundesbank SDMX web service, e.g. the Svensson
      zero-coupon curve of listed Federal securities
      ``bbk:BBSIS/D.I.ZST.ZI.EUR.S1311.B.A604.R10XX.R.A.A._Z._Z.A`` (10Y);
    * ``fred:<series>`` - St. Louis Fed FRED, e.g. ``fred:DGS2`` (UST 2Y constant maturity);
    * ``stoxx:<symbol>`` - STOXX historical index file, e.g. ``stoxx:v2tx`` (VSTOXX);
    * ``cnbc:<symbol>`` - CNBC daily bars (unofficial endpoint), sovereign yields in % such
      as ``cnbc:DE10Y-DE``, ``cnbc:FR2Y-FR``, ``cnbc:IT30Y-IT``, ``cnbc:ES5Y-ES``;
    * ``msci:<code>[/<variant>/<currency>]`` - MSCI end-of-day index levels (the service
      behind msci.com's index pages), e.g. ``msci:891800`` = MSCI Emerging Markets, price
      index (``STRD``) in USD - the level quoted in the press (about 1 700 in 2026).

Each client returns one cleaned :class:`pandas.Series` (date -> value) for ``[start, end]``.
"""

from __future__ import annotations

import io
import os
import re
import time
from datetime import date
from typing import Any

import pandas as pd
import requests

from market_monitor.data.providers._http import get_with_retry
from market_monitor.data.quality import clean_series
from market_monitor.exceptions import DataProviderError, ProviderUnavailableError

STOOQ_URL = "https://stooq.com/q/d/l/"
BUNDESBANK_URL = "https://api.statistiken.bundesbank.de/rest/data"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"
ENV_FRED_API_KEY = "FRED_API_KEY"
STOXX_URL = "https://www.stoxx.com/document/Indices/Current/HistoricalData"
CNBC_URL = "https://ts-api.cnbc.com/harmony/app/bars"
MSCI_URL = "https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph"
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
    if body.lstrip().startswith("<"):
        # anti-bot / JavaScript challenge page: the whole source is unusable, not this symbol,
        # so the circuit breaker skips the remaining Stooq tickers
        raise ProviderUnavailableError("Stooq unreachable (anti-bot page requiring JavaScript)")
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
    """``TIME_PERIOD`` -> ``OBS_VALUE`` of an SDMX-CSV payload (comma or semicolon).

    A server that ignores the CSV request and answers SDMX-ML (XML) is read too.
    """
    if not text.strip():
        return clean_series(None)
    if text.lstrip().startswith("<"):
        return parse_sdmx_xml(text, source)
    try:
        frame = pd.read_csv(io.StringIO(text), sep=None, engine="python")
    except (pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        raise DataProviderError(f"unparseable {source} CSV: {exc}") from exc
    if not {"TIME_PERIOD", "OBS_VALUE"} <= set(frame.columns):
        first = text.strip().splitlines()[0][:120] if text.strip() else ""
        raise DataProviderError(f"{source} CSV without TIME_PERIOD / OBS_VALUE columns "
                                f"(received: {first!r})")
    return clean_series(pd.Series(frame["OBS_VALUE"].to_numpy(), index=frame["TIME_PERIOD"]))


_OBS_ATTRS = re.compile(r"<(?:\w+:)?Obs\b([^>]*)>", re.S)
_ATTR = re.compile(r'(\w+)="([^"]*)"')
_GENERIC_OBS = re.compile(
    r"<(?:\w+:)?Obs>.*?<(?:\w+:)?ObsDimension[^>]*value=\"([^\"]+)\".*?"
    r"<(?:\w+:)?ObsValue[^>]*value=\"([^\"]+)\".*?</(?:\w+:)?Obs>", re.S)


def parse_sdmx_xml(text: str, source: str = "SDMX") -> pd.Series:
    """Observations of SDMX-ML: structure-specific (``<Obs TIME_PERIOD= OBS_VALUE=>``) or generic."""
    dates, values = [], []
    for match in _OBS_ATTRS.finditer(text):
        attrs = dict(_ATTR.findall(match.group(1)))
        if "TIME_PERIOD" in attrs and "OBS_VALUE" in attrs:
            dates.append(attrs["TIME_PERIOD"])
            values.append(attrs["OBS_VALUE"])
    if not dates:
        for period, value in _GENERIC_OBS.findall(text):
            dates.append(period)
            values.append(value)
    if not dates:
        if "<html" in text[:500].lower() or "error" in text[:2000].lower():
            first = " ".join(text.split())[:160]
            raise DataProviderError(f"{source} answered without observations: {first!r}")
        return clean_series(None)
    return clean_series(pd.Series(values, index=dates, dtype="object"))


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


class FredApiClient(_CsvClient):
    """Official FRED web API (``api.stlouisfed.org``): free key in ``FRED_API_KEY``.

    Another host than the graph export: on a corporate network that blocks
    ``fred.stlouisfed.org`` it may still be reachable (chain ``fred:X|fredapi:X``).
    """

    source = "FRED API"
    default_url = FRED_API_URL

    def __init__(self, base_url: str | None = None, *, api_key: str | None = None,
                 **kwargs: Any) -> None:
        super().__init__(base_url, **kwargs)
        self._api_key = (api_key if api_key is not None else os.environ.get(ENV_FRED_API_KEY, "")).strip()

    def fetch(self, series_id: str, start: date, end: date) -> pd.Series:
        if not self._api_key:
            raise DataProviderError(f"FRED API: no key ({ENV_FRED_API_KEY} missing from .env)")
        response = get_with_retry(
            self._session, self._base_url,
            params={"series_id": series_id, "api_key": self._api_key, "file_type": "json",
                    "observation_start": start.isoformat(), "observation_end": end.isoformat()},
            timeout_s=self._timeout_s, max_retries=self._max_retries, backoff_s=1.0,
            source=self.source, secrets=(self._api_key,),
        )
        if response.status_code >= 400:
            raise DataProviderError(f"FRED API HTTP {response.status_code} for {series_id}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise DataProviderError(f"FRED API returned no JSON for {series_id}") from exc
        return _window(parse_fred_observations(payload, series_id), start, end)


def parse_fred_observations(payload: Any, series_id: str = "") -> pd.Series:
    """``observations[] -> {date, value}`` (missing values are ``.``)."""
    observations = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(observations, list):
        raise DataProviderError(f"unexpected FRED API payload for {series_id}")
    rows = [o for o in observations if isinstance(o, dict)]
    values = pd.to_numeric(pd.Series([o.get("value") for o in rows], dtype="object"), errors="coerce")
    return clean_series(pd.Series(values.to_numpy(), index=[o.get("date") for o in rows]))


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


# ----------------------------------------------------------------------- CNBC
class CnbcClient(_CsvClient):
    """CNBC chart service, daily bars (JSON) - unofficial.

    CNBC answers bursts of requests with an empty or truncated payload for some symbols
    (the same request succeeds a moment later): calls are spaced out and such an answer is
    retried after a pause before the symbol is reported as failed.
    """

    source = "CNBC"
    default_url = CNBC_URL
    min_interval_s = 0.3                 # between two calls
    retry_pauses_s = (1.5, 4.0)          # before each retry of an unusable payload
    _sleep = staticmethod(time.sleep)
    _clock = staticmethod(time.monotonic)
    _last_call = 0.0
    max_consecutive_failures = 3       # then the circuit breaker skips CNBC for a while
    _failures = 0

    def fetch(self, symbol: str, start: date, end: date) -> pd.Series:
        url = (f"{self._base_url}/{symbol}/1D/{start:%Y%m%d}000000/{end:%Y%m%d}235959"
               "/adjusted/EST5EDT.json")
        error = DataProviderError(f"CNBC: no answer for {symbol}")
        for pause in (0.0, *self.retry_pauses_s):
            if pause:
                self._sleep(pause)
            try:
                series = _window(parse_cnbc_bars(self._payload(url, symbol), symbol), start, end)
            except CnbcPayloadError as exc:  # transient refusal: try again
                error = exc
                continue
            self._failures = 0
            return series
        self._failures += 1
        if self._failures >= self.max_consecutive_failures:  # CNBC refuses everything: stop
            self._failures = 0
            raise ProviderUnavailableError(f"CNBC unreachable ({error})")
        raise error

    def _payload(self, url: str, symbol: str) -> Any:
        wait = self.min_interval_s - (self._clock() - CnbcClient._last_call)
        if wait > 0:
            self._sleep(wait)
        CnbcClient._last_call = self._clock()
        response = get_with_retry(
            self._session, url, params=None, timeout_s=self._timeout_s,
            max_retries=self._max_retries, backoff_s=1.0, source=self.source,
        )
        if response.status_code == 404:
            return {"barData": {"priceBars": []}}
        if response.status_code >= 400:
            raise DataProviderError(f"CNBC HTTP {response.status_code} for {symbol}")
        try:
            return response.json()
        except ValueError as exc:
            raise CnbcPayloadError(f"CNBC returned no JSON for {symbol}: "
                                   f"{str(response.text)[:80]!r}") from exc


class CnbcPayloadError(DataProviderError):
    """CNBC answered, but not with bars (usually a transient refusal)."""


def parse_cnbc_bars(payload: Any, symbol: str = "") -> pd.Series:
    """``barData.priceBars[]`` -> close by date (``tradeTime`` ``YYYYMMDDhhmmss`` or epoch ms)."""
    bar_data = payload.get("barData") if isinstance(payload, dict) else None
    if not isinstance(bar_data, dict):
        shown = sorted(payload)[:5] if isinstance(payload, dict) else type(payload).__name__
        status = ""
        if isinstance(payload, dict) and (payload.get("status") or payload.get("statusMessage")):
            status = f", status {payload.get('status')!r}: {str(payload.get('statusMessage'))[:120]!r}"
        raise CnbcPayloadError(f"unexpected CNBC payload for {symbol} (received: {shown}{status})")
    bars = bar_data.get("priceBars") or []
    dates, closes = [], []
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        dates.append(_cnbc_day(bar))
        closes.append(bar.get("close"))
    return clean_series(pd.Series(closes, index=pd.DatetimeIndex(dates).normalize(), dtype="object"))


def _cnbc_day(bar: dict[str, Any]) -> pd.Timestamp | None:
    stamp = str(bar.get("tradeTime") or "")
    if len(stamp) >= 8 and stamp[:8].isdigit():
        return pd.Timestamp(f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}")
    try:
        return pd.Timestamp(int(float(bar.get("tradeTimeinMills") or "")), unit="ms")
    except (TypeError, ValueError):
        return None  # NaT once in the index: dropped by clean_series


# ----------------------------------------------------------------------- MSCI
class MsciClient(_CsvClient):
    """MSCI end-of-day index levels (JSON), keyless.

    Key ``<code>[/<variant>/<currency>]``: ``891800`` (MSCI EM), ``990100`` (MSCI World);
    variant ``STRD`` (price, default), ``NETR`` (net return), ``GRTR`` (gross return);
    currency ``USD`` by default.
    """

    source = "MSCI"
    default_url = MSCI_URL

    def fetch(self, key: str, start: date, end: date) -> pd.Series:
        code, variant, currency = parse_msci_key(key)
        response = get_with_retry(
            self._session, self._base_url, timeout_s=self._timeout_s,
            max_retries=self._max_retries, backoff_s=1.0, source=self.source,
            params={"currency_symbol": currency, "index_variant": variant,
                    "start_date": f"{start:%Y%m%d}", "end_date": f"{end:%Y%m%d}",
                    "data_frequency": "DAILY", "index_codes": code},
        )
        if response.status_code == 404:
            return clean_series(None)
        if response.status_code >= 400:
            raise DataProviderError(f"MSCI HTTP {response.status_code} for {key}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise DataProviderError(f"MSCI returned no JSON for {key}") from exc
        return _window(parse_msci_levels(payload, key), start, end)


def parse_msci_key(key: str) -> tuple[str, str, str]:
    parts = [p.strip().upper() for p in key.split("/")]
    code = parts[0]
    if not code.isdigit():
        raise DataProviderError(f"invalid MSCI index code {key!r} (expected e.g. 891800)")
    variant = parts[1] if len(parts) > 1 and parts[1] else "STRD"
    currency = parts[2] if len(parts) > 2 and parts[2] else "USD"
    return code, variant, currency


def parse_msci_levels(payload: Any, key: str = "") -> pd.Series:
    """``indexes.INDEX_LEVELS[] = {level_eod, calc_date: YYYYMMDD}`` -> level by date."""
    if isinstance(payload, dict) and payload.get("error_message"):
        raise DataProviderError(f"MSCI: {payload['error_message']} ({key})")
    indexes = payload.get("indexes") if isinstance(payload, dict) else None
    rows = indexes.get("INDEX_LEVELS") if isinstance(indexes, dict) else None
    if rows is None:
        raise DataProviderError(f"unexpected MSCI payload for {key}")
    dates, levels = [], []
    for row in rows:
        if not isinstance(row, dict):
            continue
        stamp = str(row.get("calc_date") or "")
        if len(stamp) == 8 and stamp.isdigit():
            dates.append(pd.Timestamp(f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"))
            levels.append(row.get("level_eod"))
    return clean_series(pd.Series(levels, index=pd.DatetimeIndex(dates), dtype="object"))
