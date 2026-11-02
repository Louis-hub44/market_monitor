"""Free fallback provider: ECB Data Portal, keyless public sources + Yahoo Finance.

Native tickers (prefix routes to the source, no prefix = Yahoo):
    * ``ecb:<flow>/<key>``  e.g. ``ecb:EST/B.EU000A2X2A25.WT`` (€STR),
      ``ecb:YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y`` (AAA euro curve, 10Y spot);
    * ``stooq:``, ``bbk:``, ``fred:``, ``stoxx:``, ``cnbc:`` - see :mod:`.public` (sovereign
      yields, Bundesbank curve, FRED, VSTOXX);
    * ``yf:<symbol>`` or ``<symbol>``  e.g. ``^STOXX50E``, ``EURUSD=X``, ``BZ=F``, ``^VIX``;
    * ``a|b|c`` - fallback chain: the first alternative returning data wins (e.g. Stooq,
      then the Bundesbank when a corporate firewall blocks Stooq).

A source that is unreachable (network error after retries) is skipped for
``down_ttl_s`` seconds, so a firewalled host costs one timeout, not one per ticker.
"""

from __future__ import annotations

import io
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from functools import partial
from typing import Any, Protocol

import pandas as pd
import requests

from market_monitor.data.base import DataProvider
from market_monitor.data.models import (
    CHAIN_SEP,
    NO_DATA,
    BarsResult,
    Field,
    HistoryRequest,
    HistoryResult,
    chain_alternatives,
)
from market_monitor.data.providers._http import get_with_retry
from market_monitor.data.providers.public import (
    BundesbankClient,
    CnbcClient,
    FredApiClient,
    FredClient,
    MsciClient,
    StooqClient,
    StoxxClient,
)
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


    def search(self, flow: str, pattern: str) -> pd.DataFrame:
        """Series of ``flow`` matching ``pattern`` (empty dimension = wildcard, ``+`` = OR).

        One row per series: ``KEY``, ``TITLE`` (when published), last ``TIME_PERIOD`` and
        ``OBS_VALUE`` - to find a key, then use it as ``ecb:<flow>/<key>``.
        """
        response = get_with_retry(
            self._session, f"{self._base_url}/{flow}/{pattern}",
            params={"lastNObservations": 1, "format": "csvdata"},
            timeout_s=self._timeout_s, max_retries=self._max_retries, backoff_s=1.0, source="ECB",
        )
        if response.status_code == 404:
            return pd.DataFrame(columns=["KEY", "TITLE", "TIME_PERIOD", "OBS_VALUE"])
        if response.status_code >= 400:
            raise DataProviderError(f"ECB HTTP {response.status_code} for {flow}/{pattern}")
        frame = pd.read_csv(io.StringIO(response.text))
        if "KEY" not in frame.columns:
            raise DataProviderError("ECB CSV without KEY column")
        columns = [c for c in ("KEY", "TITLE", "TIME_PERIOD", "OBS_VALUE") if c in frame.columns]
        return frame[columns].reset_index(drop=True)


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

    def fetch_bars(self, tickers: Sequence[str], start: date, end: date) -> dict[str, pd.DataFrame]:
        """Every OHLCV column from a single download."""
        try:
            raw = self._download(list(tickers), start, end)
        except ProviderUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DataProviderError(f"yfinance download failed: {exc}") from exc
        columns = {f.value: extract_yahoo_field(raw, tickers, col) for f, col in _YF_FIELDS.items()}
        return {t: pd.DataFrame({c: found[t] for c, found in columns.items() if t in found})
                for t in tickers if t in columns["last"]}


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


FUTURES_PREFIX = "fut:"


class FuturesClient:
    """Front-month future rolled *before* expiry (``fut:<rule>/<generic>/<contract>/<days>``).

    Yahoo's generic (``BZ=F``) keeps the expiring contract until its last day, when it trades
    away from the market's reference (Brent, 29/10/2026: December 115,32 $ the day before
    expiry, January 111,64 $ on Investing). In the ``days`` business days before the front
    contract's last trading day, the next contract (``BZ{code}{yy}.NYM`` -> ``BZF27.NYM``) is
    quoted instead - the "most active" convention of Investing and the press. A date the
    next contract has no print for keeps the generic's (older windows: expired contracts).
    """

    def __init__(self, yahoo: YahooClient) -> None:
        self._yahoo = yahoo

    @staticmethod
    def parse(key: str) -> tuple[str, str, str, int]:
        parts = key.split("/")
        if len(parts) != 4 or not parts[3].isdigit():
            raise DataProviderError(f"futures key {key!r}: expected <rule>/<generic>/<contract>/<days>")
        return parts[0], parts[1], parts[2], int(parts[3])

    def _plan(self, rule: str, pattern: str, ahead: int, start: date, end: date) -> dict[pd.Timestamp, str]:
        """Business days of the early-roll windows -> symbol of the contract quoted then."""
        from market_monitor.rolls import contract_symbol, front_contract

        out = {}
        for day in pd.bdate_range(start, end):
            early, usual = front_contract(rule, day, ahead), front_contract(rule, day, 0)
            if early != usual:
                out[day] = contract_symbol(pattern, *early)
        return out

    def fetch(self, key: str, start: date, end: date) -> pd.Series:
        rule, generic, pattern, ahead = self.parse(key)
        base = self._yahoo.fetch([generic], start, end, Field.LAST).get(generic, pd.Series(dtype="float64"))
        plan = self._plan(rule, pattern, ahead, start, end)
        if not plan:
            return base
        first = min(plan)
        symbols = sorted(set(plan.values()))
        try:
            found = self._yahoo.fetch(symbols, first.date(), end, Field.LAST)
        except DataProviderError:
            return base  # next contract unreachable: the generic, roll flagged at expiry
        patch = {day: float(found[symbol].loc[day]) for day, symbol in plan.items()
                 if symbol in found and day in found[symbol].index and pd.notna(found[symbol].loc[day])}
        if not patch:
            return base
        return pd.Series(patch, dtype="float64").combine_first(base).sort_index()

    def fetch_bars(self, key: str, start: date, end: date) -> pd.DataFrame:
        rule, generic, pattern, ahead = self.parse(key)
        base = self._yahoo.fetch_bars([generic], start, end).get(generic, pd.DataFrame())
        plan = self._plan(rule, pattern, ahead, start, end)
        if not plan or base.empty:
            return base
        try:
            found = self._yahoo.fetch_bars(sorted(set(plan.values())), min(plan).date(), end)
        except DataProviderError:
            return base
        out = base.copy()
        for day, symbol in plan.items():
            bars = found.get(symbol)
            if bars is not None and day in bars.index and pd.notna(bars.loc[day].get("last")):
                out.loc[day, bars.columns] = bars.loc[day].to_numpy()
        return out.sort_index()


class FreeProvider(DataProvider):
    """Routes each ticker to a series source or to Yahoo according to its prefix."""

    name = "free"

    def __init__(
        self,
        ecb: ECBClient | None = None,
        yahoo: YahooClient | None = None,
        *,
        sources: Mapping[str, SeriesClient] | None = None,
        down_ttl_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """``sources`` maps a prefix (``"stooq:"``...) to its client; defaults to the public ones."""
        self._yahoo = yahoo or YahooClient()
        self._sources: dict[str, SeriesClient] = dict(
            sources if sources is not None else default_sources())
        self._sources[ECB_PREFIX] = ecb or ECBClient()
        self._sources.setdefault(FUTURES_PREFIX, FuturesClient(self._yahoo))
        self._down_ttl_s = down_ttl_s
        self._clock = clock
        self._down_until: dict[str, float] = {}
        self._down_reason: dict[str, str] = {}

    def _fetch_history(self, request: HistoryRequest) -> HistoryResult:
        series: dict[str, pd.Series] = {}
        errors: dict[str, str] = {}
        warnings: dict[str, str] = {}
        origins: dict[str, str] = {}
        yahoo: dict[str, str] = {}
        for ticker in request.tickers:
            if CHAIN_SEP in ticker:
                self._fetch_chain(ticker, request, series, errors, warnings, origins)
            elif self._prefix(ticker) is None:
                yahoo[ticker] = ticker.removeprefix(YAHOO_PREFIX)
            else:
                try:
                    series[ticker] = self._fetch_series(ticker, request)
                except DataProviderError as exc:  # one source down: the others still served
                    errors[ticker] = str(exc)
        if yahoo:
            self._fetch_yahoo(yahoo, request, series, errors)
        return HistoryResult(data=assemble_frame(series, request.tickers), errors=errors,
                             warnings=warnings, origins=origins)

    def _prefix(self, ticker: str) -> str | None:
        return next((p for p in self._sources if ticker.startswith(p)), None)

    def _fetch_series(self, ticker: str, request: HistoryRequest) -> pd.Series:
        """One prefixed ticker, honouring the circuit breaker of its source."""
        prefix = self._prefix(ticker)
        assert prefix is not None
        if request.field is not Field.LAST:
            raise DataProviderError(f"{prefix.rstrip(':').upper()} series only support the LAST field")
        client = self._sources[prefix]
        return self._guarded(prefix, lambda: client.fetch(ticker[len(prefix):], request.start, request.end))

    def _guarded(self, prefix: str, call: Callable[[], Any]) -> Any:
        """Run a call to a prefixed source behind its circuit breaker."""
        if self._clock() < self._down_until.get(prefix, 0.0):
            raise ProviderUnavailableError(
                f"{prefix.rstrip(':').upper()} skipped (unreachable a moment ago: "
                f"{self._down_reason[prefix]})")
        try:
            return call()
        except ProviderUnavailableError as exc:
            self._down_until[prefix] = self._clock() + self._down_ttl_s
            self._down_reason[prefix] = str(exc)[:160]
            raise

    # --------------------------------------------------------------------- bars
    def _fetch_bars(self, request: HistoryRequest) -> BarsResult:
        """Whole bars in one call per series: Yahoo, Stooq and CNBC publish open / high /
        low; the other sources (ECB, Bundesbank, FRED, STOXX, MSCI) a close only."""
        bars: dict[str, pd.DataFrame] = {}
        errors: dict[str, str] = {}
        warnings: dict[str, str] = {}
        origins: dict[str, str] = {}
        yahoo: dict[str, str] = {}
        for ticker in request.tickers:
            if CHAIN_SEP in ticker:
                _first_alternative(ticker, lambda alt: self._bars_of(alt, request), _no_close,
                                   bars, errors, warnings, origins)
            elif self._prefix(ticker) is None:
                yahoo[ticker] = ticker.removeprefix(YAHOO_PREFIX)
            else:
                try:
                    bars[ticker] = self._source_bars(ticker, request)
                except DataProviderError as exc:
                    errors[ticker] = str(exc)
        if yahoo:
            symbols = list(dict.fromkeys(yahoo.values()))
            try:
                fetched = self._yahoo.fetch_bars(symbols, request.start, request.end)
            except DataProviderError as exc:
                errors.update({t: str(exc) for t in yahoo})
            else:
                bars.update({t: fetched[s] for t, s in yahoo.items() if s in fetched})
        return BarsResult(bars=bars, errors=errors, warnings=warnings, origins=origins)

    def _bars_of(self, alt: str, request: HistoryRequest) -> pd.DataFrame:
        if self._prefix(alt) is None:
            symbol = alt.removeprefix(YAHOO_PREFIX)
            found = self._yahoo.fetch_bars([symbol], request.start, request.end)
            return found.get(symbol, pd.DataFrame())
        return self._source_bars(alt, request)

    def _source_bars(self, ticker: str, request: HistoryRequest) -> pd.DataFrame:
        prefix = self._prefix(ticker)
        assert prefix is not None
        client = self._sources[prefix]
        key = ticker[len(prefix):]
        fetch_bars = getattr(client, "fetch_bars", None)
        if callable(fetch_bars):
            return self._guarded(prefix, lambda: fetch_bars(key, request.start, request.end))
        close = self._guarded(prefix, lambda: client.fetch(key, request.start, request.end))
        return pd.DataFrame({"last": close})

    def _fetch_chain(
        self,
        ticker: str,
        request: HistoryRequest,
        series: dict[str, pd.Series],
        errors: dict[str, str],
        warnings: dict[str, str],
        origins: dict[str, str],
    ) -> None:
        """``a|b|c`` history: see :func:`_first_alternative`."""
        def get(alt: str) -> pd.Series:
            if self._prefix(alt) is None:
                symbol = alt.removeprefix(YAHOO_PREFIX)
                found = self._yahoo.fetch([symbol], request.start, request.end, request.field)
                return found.get(symbol, pd.Series(dtype="float64"))
            return self._fetch_series(alt, request)

        _first_alternative(ticker, get, lambda s: s.dropna().empty, series, errors, warnings, origins)

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

def _no_close(frame: pd.DataFrame) -> bool:
    return "last" not in frame.columns or frame["last"].dropna().empty


def _first_alternative(
    ticker: str,
    get: Callable[[str], Any],
    is_empty: Callable[[Any], bool],
    out: dict[str, Any],
    errors: dict[str, str],
    warnings: dict[str, str],
    origins: dict[str, str],
) -> None:
    """``a|b|c``: first alternative with data wins; a fallback is reported as a warning.

    The winning alternative is returned in ``origins`` so the cache never splices two
    alternatives into one series.
    """
    trail: list[str] = []
    for rank, alt in enumerate(chain_alternatives(ticker)):
        try:
            data = get(alt)
        except DataProviderError as exc:
            trail.append(f"{alt}: {exc}")
            continue
        if is_empty(data):
            trail.append(f"{alt}: {NO_DATA}")
            continue
        out[ticker] = data
        origins[ticker] = alt
        if rank:
            warnings[ticker] = f"served by fallback {alt} ({'; '.join(trail)})"
        return
    errors[ticker] = " / ".join(trail)


def default_sources(
    *, timeout_s: float = 20.0, session: requests.Session | None = None
) -> dict[str, SeriesClient]:
    """Keyless public sources, sharing the corporate SSL policy through ``session``."""
    # one retry only: on a firewalled network each retry is one more full timeout
    kwargs: dict[str, Any] = {"timeout_s": timeout_s, "max_retries": 1, "session": session}
    return {
        "stooq:": StooqClient(**kwargs),
        "bbk:": BundesbankClient(**kwargs),
        "fred:": FredClient(**kwargs),
        "fredapi:": FredApiClient(**kwargs),
        "stoxx:": StoxxClient(**kwargs),
        "cnbc:": CnbcClient(**kwargs),
        "msci:": MsciClient(**kwargs),
    }
