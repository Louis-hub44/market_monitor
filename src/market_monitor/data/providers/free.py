"""Free fallback provider: ECB Data Portal, keyless public sources + Yahoo Finance.

Native tickers (prefix routes to the source, no prefix = Yahoo):
    * ``ecb:<flow>/<key>``  e.g. ``ecb:EST/B.EU000A2X2A25.WT`` (€STR),
      ``ecb:YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y`` (AAA euro curve, 10Y spot);
    * ``stooq:``, ``bbk:``, ``fred:``, ``stoxx:`` - see :mod:`.public` (sovereign yields,
      Bundesbank curve, FRED, VSTOXX);
    * ``yf:<symbol>`` or ``<symbol>``  e.g. ``^STOXX50E``, ``EURUSD=X``, ``BZ=F``, ``^VIX``.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from functools import partial
from typing import Any, Protocol

import pandas as pd
import requests

from market_monitor.data.base import DataProvider
from market_monitor.data.models import Field, HistoryRequest, HistoryResult
from market_monitor.data.providers._http import get_with_retry
from market_monitor.data.providers.public import BundesbankClient, FredClient, StooqClient, StoxxClient
from market_monitor.data.quality import assemble_frame, clean_series
from market_monitor.exceptions import DataProviderError, ProviderUnavailableError
from market_monitor.network import apply_to_yfinance

logger = logging.getLogger(__name__)

ECB_PREFIX = "ecb:"
YAHOO_PREFIX = "yf:"
DEFAULT_ECB_URL = "https://data-api.ecb.europa.eu/service/data"
_YF_FIELDS = {
    Field.LAST: "Close", Field.OPEN: "Open", Field.HIGH: "High",
    Field.LOW: "Low", Field.VOLUME: "Volume",
}

Downloader = Callable[[Sequence[str], date, date], pd.DataFrame]


class SeriesClient(Protocol):
    """One series per call (ECB, Stooq, Bundesbank, FRED, STOXX): LAST field only."""

    def fetch(self, key: str, start: date, end: date) -> pd.Series: ...


class ECBClient:
    """ECB Data Portal SDMX REST client (CSV format)."""

    def __init__(
        self,
        base_url: str = DEFAULT_ECB_URL,
        *,
        timeout_s: float = 20.0,
        max_retries: int = 2,
        session: requests.Session | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._session = session or requests.Session()

    def fetch(self, series_key: str, start: date, end: date) -> pd.Series:
        """Observations of ``<flow>/<key>`` between ``start`` and ``end``."""
        flow, sep, key = series_key.partition("/")
        if not sep or not flow or not key:
            raise DataProviderError(f"invalid ECB key {series_key!r} (expected FLOW/KEY)")
        response = get_with_retry(
            self._session,
            f"{self._base_url}/{flow}/{key}",
            params={
                "startPeriod": start.isoformat(),
                "endPeriod": end.isoformat(),
                "format": "csvdata",
                "detail": "dataonly",
            },
            timeout_s=self._timeout_s,
            max_retries=self._max_retries,
            backoff_s=1.0,
            source="ECB",
        )
        if response.status_code == 404:  # SDMX "No results found" for the window
            return clean_series(None)
        if response.status_code >= 400:
            raise DataProviderError(f"ECB HTTP {response.status_code} for {series_key}")
        return parse_ecb_csv(response.text)


def parse_ecb_csv(text: str) -> pd.Series:
    """Parse an SDMX-CSV payload into a cleaned series (``TIME_PERIOD`` -> ``OBS_VALUE``)."""
    if not text.strip():
        return clean_series(None)
    try:
        frame = pd.read_csv(io.StringIO(text))
    except (pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        raise DataProviderError(f"unparseable ECB CSV: {exc}") from exc
    if not {"TIME_PERIOD", "OBS_VALUE"} <= set(frame.columns):
        raise DataProviderError("ECB CSV without TIME_PERIOD / OBS_VALUE columns")
    return clean_series(pd.Series(frame["OBS_VALUE"].to_numpy(), index=frame["TIME_PERIOD"]))


class YahooClient:
    """Thin wrapper around ``yfinance.download`` (unofficial API - fallback only).

    ``session`` (see :func:`market_monitor.network.build_session`) replaces yfinance's
    native ``curl_cffi`` session - needed behind a corporate SSL-inspection proxy.
    """

    def __init__(self, downloader: Downloader | None = None, *, session: Any = None) -> None:
        self._download = downloader or partial(_yfinance_download, session=session)

    def fetch(
        self, tickers: Sequence[str], start: date, end: date, field: Field
    ) -> dict[str, pd.Series]:
        try:
            raw = self._download(list(tickers), start, end)
        except ProviderUnavailableError:
            raise
        # yfinance raises heterogeneous exception types: normalise at the boundary.
        except Exception as exc:  # noqa: BLE001
            raise DataProviderError(f"yfinance download failed: {exc}") from exc
        return extract_yahoo_field(raw, tickers, _YF_FIELDS[field])


def _yfinance_download(
    tickers: Sequence[str], start: date, end: date, *, session: Any = None
) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise ProviderUnavailableError("yfinance is not installed") from exc
    if session is not None:
        # yfinance shares one singleton session: (re)apply ours before each call.
        apply_to_yfinance(session)
    return yf.download(
        tickers=list(tickers),
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),  # yfinance end is exclusive
        interval="1d",
        auto_adjust=False,
        actions=False,
        progress=False,
        threads=True,
        group_by="column",
        session=session,
    )


def extract_yahoo_field(
    raw: pd.DataFrame | None, tickers: Sequence[str], column: str
) -> dict[str, pd.Series]:
    """Extract one OHLCV column from any yfinance output layout."""
    if raw is None or raw.empty:
        return {}
    if isinstance(raw.columns, pd.MultiIndex):
        block: pd.DataFrame | pd.Series
        if column in raw.columns.get_level_values(0):
            block = raw[column]
        elif column in raw.columns.get_level_values(1):
            block = raw.xs(column, axis=1, level=1)
        else:
            return {}
        frame = block.to_frame(tickers[0]) if isinstance(block, pd.Series) else block
        return {t: frame[t] for t in tickers if t in frame.columns}
    if column in raw.columns and len(tickers) == 1:
        return {tickers[0]: raw[column]}
    return {}


class FreeProvider(DataProvider):
    """Routes each ticker to a series source or to Yahoo according to its prefix."""

    name = "free"

    def __init__(
        self,
        ecb: ECBClient | None = None,
        yahoo: YahooClient | None = None,
        *,
        sources: Mapping[str, SeriesClient] | None = None,
    ) -> None:
        """``sources`` maps a prefix (``"stooq:"``...) to its client; defaults to the public ones."""
        self._yahoo = yahoo or YahooClient()
        self._sources: dict[str, SeriesClient] = dict(
            sources if sources is not None else default_sources())
        self._sources[ECB_PREFIX] = ecb or ECBClient()

    def _fetch_history(self, request: HistoryRequest) -> HistoryResult:
        series: dict[str, pd.Series] = {}
        errors: dict[str, str] = {}
        yahoo: dict[str, str] = {}
        for ticker in request.tickers:
            prefix = next((p for p in self._sources if ticker.startswith(p)), None)
            if prefix is None:
                yahoo[ticker] = ticker.removeprefix(YAHOO_PREFIX)
            elif request.field is not Field.LAST:
                errors[ticker] = f"{prefix.rstrip(':').upper()} series only support the LAST field"
            else:
                try:
                    series[ticker] = self._sources[prefix].fetch(ticker[len(prefix):],
                                                                 request.start, request.end)
                except DataProviderError as exc:  # one source down: the others still served
                    errors[ticker] = str(exc)
        if yahoo:
            self._fetch_yahoo(yahoo, request, series, errors)
        return HistoryResult(data=assemble_frame(series, request.tickers), errors=errors)

    def _fetch_yahoo(
        self,
        mapping: dict[str, str],
        request: HistoryRequest,
        series: dict[str, pd.Series],
        errors: dict[str, str],
    ) -> None:
        symbols = list(dict.fromkeys(mapping.values()))
        try:
            fetched = self._yahoo.fetch(symbols, request.start, request.end, request.field)
        except DataProviderError as exc:
            errors.update({t: str(exc) for t in mapping})
            return
        for ticker, symbol in mapping.items():
            if symbol in fetched:
                series[ticker] = fetched[symbol]


def default_sources(
    *, timeout_s: float = 20.0, session: requests.Session | None = None
) -> dict[str, SeriesClient]:
    """Keyless public sources, sharing the corporate SSL policy through ``session``."""
    kwargs: dict[str, Any] = {"timeout_s": timeout_s, "session": session}
    return {
        "stooq:": StooqClient(**kwargs),
        "bbk:": BundesbankClient(**kwargs),
        "fred:": FredClient(**kwargs),
        "stoxx:": StoxxClient(**kwargs),
    }
