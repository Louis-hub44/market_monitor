"""Financial Modeling Prep provider (``stable`` REST API).

Native tickers:
    * FMP symbols: ``^GSPC``, ``^STOXX50E``, ``EURUSD``, ``BZUSD``, ``GCUSD``, ``BTCUSD``...
    * US Treasury par yields (in %): ``treasury:year2``, ``treasury:year10``...
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import pandas as pd
import requests

from market_monitor.data.base import DataProvider
from market_monitor.data.models import Field, HistoryRequest, HistoryResult, SnapshotResult
from market_monitor.data.providers._http import get_with_retry, redact
from market_monitor.data.quality import assemble_frame, clean_series, to_float
from market_monitor.exceptions import DataProviderError, ProviderUnavailableError

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://financialmodelingprep.com/stable"
TREASURY_PREFIX = "treasury:"
TREASURY_TENORS = frozenset(
    {"month1", "month2", "month3", "month6", "year1", "year2", "year3",
     "year5", "year7", "year10", "year20", "year30"}
)
TREASURY_CHUNK_DAYS = 90  # FMP caps the treasury endpoint at ~3 months per call
_FIELD_MAP = {
    Field.LAST: "close", Field.OPEN: "open", Field.HIGH: "high",
    Field.LOW: "low", Field.VOLUME: "volume",
}


class FMPProvider(DataProvider):
    """Daily EOD history and real-time quotes from FMP."""

    name = "fmp"

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 15.0,
        max_retries: int = 3,
        backoff_s: float = 1.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_s <= 0 or max_retries < 0 or backoff_s < 0:
            raise ValueError("timeout_s must be > 0, max_retries and backoff_s >= 0")
        self._api_key = (api_key or "").strip()
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._session = session or requests.Session()
        self._sleep = sleep

    def is_available(self) -> bool:
        return bool(self._api_key)

    # ------------------------------------------------------------------ history
    def _fetch_history(self, request: HistoryRequest) -> HistoryResult:
        self._require_key()
        series: dict[str, pd.Series] = {}
        errors: dict[str, str] = {}
        treasury = [t for t in request.tickers if t.startswith(TREASURY_PREFIX)]
        if treasury:
            self._fetch_treasury(treasury, request, series, errors)
        for ticker in request.tickers:
            if ticker in treasury:
                continue
            try:
                series[ticker] = self._fetch_eod(ticker, request)
            except ProviderUnavailableError:
                raise
            except DataProviderError as exc:
                errors[ticker] = str(exc)
        return HistoryResult(data=assemble_frame(series, request.tickers), errors=errors)

    def _fetch_eod(self, ticker: str, request: HistoryRequest) -> pd.Series:
        payload = self._get(
            "historical-price-eod/full",
            {"symbol": ticker, "from": request.start.isoformat(), "to": request.end.isoformat()},
        )
        # stable API -> list of bars ; legacy v3 -> {"symbol":..., "historical": [...]}
        rows = payload.get("historical", []) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise DataProviderError(f"unexpected FMP payload for {ticker}")
        return _rows_to_series(rows, _FIELD_MAP[request.field])

    def _fetch_treasury(
        self,
        tickers: list[str],
        request: HistoryRequest,
        series: dict[str, pd.Series],
        errors: dict[str, str],
    ) -> None:
        tenors = {t: t[len(TREASURY_PREFIX):] for t in tickers}
        valid = {t: k for t, k in tenors.items() if k in TREASURY_TENORS}
        for ticker in set(tenors) - set(valid):
            errors[ticker] = f"unknown treasury tenor (allowed: {sorted(TREASURY_TENORS)})"
        if request.field is not Field.LAST:
            for ticker in valid:
                errors[ticker] = "treasury rates only support the LAST field"
            return
        if not valid:
            return
        rows: list[dict[str, Any]] = []
        for chunk_start, chunk_end in _chunks(request.start, request.end, TREASURY_CHUNK_DAYS):
            payload = self._get(
                "treasury-rates", {"from": chunk_start.isoformat(), "to": chunk_end.isoformat()}
            )
            if isinstance(payload, list):
                rows.extend(payload)
        for ticker, tenor in valid.items():
            series[ticker] = _rows_to_series(rows, tenor)

    # ----------------------------------------------------------------- snapshot
    def _fetch_snapshot(self, tickers: tuple[str, ...]) -> SnapshotResult:
        self._require_key()
        quotes = [t for t in tickers if not t.startswith(TREASURY_PREFIX)]
        eod = [t for t in tickers if t.startswith(TREASURY_PREFIX)]
        rows: dict[str, tuple[float, Any]] = {}
        errors: dict[str, str] = {}
        for ticker in quotes:
            try:
                rows[ticker] = self._fetch_quote(ticker)
            except ProviderUnavailableError:
                raise
            except DataProviderError as exc:
                errors[ticker] = str(exc)
        frame = pd.DataFrame.from_dict(rows, orient="index", columns=["value", "as_of"])
        result = SnapshotResult(data=frame, errors=errors)
        if eod:  # no real-time quote for treasury rates: last EOD value
            fallback = super()._fetch_snapshot(tuple(eod))
            result.data = (
                fallback.data if result.data.empty else pd.concat([result.data, fallback.data])
            )
            result.errors.update(fallback.errors)
        return result

    def _fetch_quote(self, ticker: str) -> tuple[float, Any]:
        payload = self._get("quote", {"symbol": ticker})
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            raise DataProviderError(f"no quote returned for {ticker}")
        quote = payload[0]
        price = to_float(quote.get("price"))
        stamp = quote.get("timestamp")
        as_of = pd.Timestamp(stamp, unit="s") if stamp else pd.NaT  # naive UTC
        return price, as_of

    # --------------------------------------------------------------------- http
    def _require_key(self) -> None:
        if not self._api_key:
            raise ProviderUnavailableError("FMP_API_KEY is not set (.env)")

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        response = get_with_retry(
            self._session,
            f"{self._base_url}/{path}",
            params={**params, "apikey": self._api_key},
            timeout_s=self._timeout_s,
            max_retries=self._max_retries,
            backoff_s=self._backoff_s,
            source="FMP",
            secrets=(self._api_key,),
            sleep=self._sleep,
        )
        return self._decode(response, path)

    def _decode(self, response: requests.Response, path: str) -> Any:
        status = response.status_code
        if status == 401:
            raise ProviderUnavailableError("FMP rejected the API key (401)")
        if status == 429:
            raise ProviderUnavailableError("FMP rate limit / daily quota reached (429)")
        if status in (402, 403):
            raise DataProviderError(f"FMP: '{path}' not covered by the subscription ({status})")
        if status >= 400:
            raise DataProviderError(f"FMP HTTP {status} on '{path}'")
        try:
            payload = response.json()
        except ValueError as exc:
            raise DataProviderError(f"FMP returned non-JSON content on '{path}'") from exc
        if isinstance(payload, dict) and "Error Message" in payload:
            message = redact(str(payload["Error Message"]), (self._api_key,))
            if "api key" in message.lower():
                raise ProviderUnavailableError(f"FMP: {message}")
            raise DataProviderError(f"FMP: {message}")
        return payload


def _rows_to_series(rows: list[Any], key: str) -> pd.Series:
    """List of ``{"date": ..., key: ...}`` dicts -> cleaned float series."""
    values = {r["date"]: r.get(key) for r in rows if isinstance(r, dict) and "date" in r}
    if not values:
        return clean_series(None)
    return clean_series(pd.Series(values, dtype="object"))


def _chunks(start: date, end: date, days: int) -> list[tuple[date, date]]:
    """Split ``[start, end]`` into consecutive windows of at most ``days`` days."""
    out: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=days - 1))
        out.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return out
