"""ECB Data Portal provider (free fallback).

Serves daily history of ECB SDMX series, e.g.

* ``EST/B.EU000A2X2A25.WT`` – €STR (volume-weighted trimmed mean, % p.a.);
* ``YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y`` – euro-area AAA govt spot rate 10Y;
* ``YC/B.U2.EUR.4F.G_N_A.SV_C_YM.BETA0`` – the ECB's own NSS parameters.

API: ``GET {base_url}/{flow}/{key}?startPeriod=YYYY-MM-DD&endPeriod=...&format=csvdata``
(no key required). Spec fields: ``key`` (``FLOW/SERIES``) and ``scale``.
"""

from __future__ import annotations

import io
import logging
from datetime import date
from typing import Any, Sequence

import pandas as pd

from rates_analytics.data.mapping import MarketDataMap
from rates_analytics.data.provider import Capability, DataProvider, empty_history
from rates_analytics.data.providers.http import HttpClient
from rates_analytics.exceptions import ConfigurationError, DataProviderError

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://data-api.ecb.europa.eu/service/data"


class EcbProvider(DataProvider):
    """ECB Data Portal (SDMX REST, CSV output)."""

    name = "ecb"
    capabilities = frozenset({Capability.HISTORY, Capability.QUOTES})

    def __init__(self, mapping: MarketDataMap | None = None, base_url: str = DEFAULT_BASE_URL,
                 timeout_s: float = 30.0, retries: int = 2, session: Any | None = None,
                 quote_lookback_days: int = 10) -> None:
        super().__init__(mapping, quote_lookback_days)
        self.base_url = base_url.rstrip("/")
        self.http = HttpClient(timeout_s, retries, session=session)

    def _url(self, key: str) -> str:
        if "/" not in key:
            raise ConfigurationError(f"ECB key must be 'FLOW/SERIES', got {key!r}")
        flow, series = key.split("/", 1)
        return f"{self.base_url}/{flow}/{series}"

    def _fetch_series(self, key: str, start: date, end: date) -> pd.Series:
        resp = self.http.get(self._url(key), params={"startPeriod": start.isoformat(),
                                                     "endPeriod": end.isoformat(),
                                                     "format": "csvdata"}, allow_404=True)
        if resp is None or not resp.text.strip():
            return pd.Series(dtype=float)
        try:
            df = pd.read_csv(io.StringIO(resp.text), usecols=["TIME_PERIOD", "OBS_VALUE"])
        except (ValueError, pd.errors.ParserError) as exc:
            raise DataProviderError(f"Unexpected ECB payload for {key}: {exc}") from exc
        s = pd.Series(pd.to_numeric(df["OBS_VALUE"], errors="coerce").values,
                      index=pd.DatetimeIndex(pd.to_datetime(df["TIME_PERIOD"]), name="date"))
        return s.dropna().sort_index()

    def get_history(self, ids: Sequence[str], start: date, end: date) -> pd.DataFrame:
        cols: dict[str, pd.Series] = {}
        for mid in ids:
            spec = self.mapping.spec(mid, self.name)
            if spec is None:
                continue
            s = self._fetch_series(str(spec["key"]), start, end)
            if not s.empty:
                cols[mid] = s * float(spec.get("scale", 1.0))
        if not cols:
            return empty_history()
        out = pd.concat(cols, axis=1)
        out.index.name = "date"
        return out
