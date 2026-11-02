"""Financial Modeling Prep provider (UST constant-maturity yields, ...).

Endpoint used for treasuries: ``GET {base_url}/treasury-rates?from=&to=&apikey=``
returning ``[{"date": "2026-10-06", "month1": 4.1, ..., "year30": 4.6}, ...]``
(the API caps each call to ~90 days, so the window is chunked).

Spec fields: ``endpoint``, ``field``, ``scale``. The API key comes from the
``FMP_API_KEY`` environment variable (``.env``) and is masked in every error.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Sequence

import pandas as pd

from rates_analytics.data.mapping import MarketDataMap
from rates_analytics.data.provider import Capability, DataProvider, empty_history
from rates_analytics.data.providers.http import HttpClient
from rates_analytics.exceptions import ConfigurationError, DataProviderError

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://financialmodelingprep.com/stable"


class FmpProvider(DataProvider):
    """Financial Modeling Prep REST API."""

    name = "fmp"
    capabilities = frozenset({Capability.HISTORY, Capability.QUOTES})

    def __init__(self, mapping: MarketDataMap | None = None, api_key: str | None = None,
                 base_url: str = DEFAULT_BASE_URL, timeout_s: float = 20.0, retries: int = 2,
                 max_days_per_call: int = 90, session: Any | None = None,
                 quote_lookback_days: int = 10) -> None:
        super().__init__(mapping, quote_lookback_days)
        if not api_key:
            raise ConfigurationError("FMP provider needs FMP_API_KEY (set it in .env)")
        if max_days_per_call < 1:
            raise ConfigurationError("max_days_per_call must be >= 1")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_days = max_days_per_call
        self.http = HttpClient(timeout_s, retries, session=session, secrets=(api_key,))

    def _windows(self, start: date, end: date):
        cur = start
        while cur <= end:
            stop = min(end, cur + timedelta(days=self.max_days - 1))
            yield cur, stop
            cur = stop + timedelta(days=1)

    def _fetch_endpoint(self, endpoint: str, start: date, end: date) -> pd.DataFrame:
        records: list[dict] = []
        for a, b in self._windows(start, end):
            resp = self.http.get(f"{self.base_url}/{endpoint}",
                                 params={"from": a.isoformat(), "to": b.isoformat(), "apikey": self._api_key})
            payload = resp.json()
            if isinstance(payload, dict):  # FMP reports errors as {"Error Message": "..."}
                msg = payload.get("Error Message") or payload.get("message") or str(payload)[:200]
                raise DataProviderError(f"FMP {endpoint}: {msg}".replace(self._api_key, "***"))
            records.extend(payload)
        if not records:
            return pd.DataFrame()
        df = pd.DataFrame.from_records(records)
        if "date" not in df:
            raise DataProviderError(f"FMP {endpoint}: no 'date' field in payload")
        df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("date")), name="date")
        return df[~df.index.duplicated(keep="last")].sort_index()

    def get_history(self, ids: Sequence[str], start: date, end: date) -> pd.DataFrame:
        by_endpoint: dict[str, list[tuple[str, Any]]] = defaultdict(list)
        for mid in ids:
            spec = self.mapping.spec(mid, self.name)
            if spec is not None:
                by_endpoint[str(spec["endpoint"])].append((mid, spec))
        cols: dict[str, pd.Series] = {}
        for endpoint, items in by_endpoint.items():
            raw = self._fetch_endpoint(endpoint, start, end)
            for mid, spec in items:
                field = str(spec["field"])
                if field in raw:
                    cols[mid] = pd.to_numeric(raw[field], errors="coerce") * float(spec.get("scale", 1.0))
                else:
                    logger.warning("FMP %s: field %r absent", endpoint, field)
        if not cols:
            return empty_history()
        out = pd.concat(cols, axis=1)
        out.index.name = "date"
        return out
