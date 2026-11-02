"""Keyless public sources for sovereign benchmark yields (ported from Market Monitor).

Spec: ``public: {ticker: "cnbc:DE10Y-DE|bbk:BBSIS/…|stooq:10dey.b", scale: 0.01}``. The
``|``-separated alternatives are tried **in order** and the first non-empty series wins,
so one source being down (Stooq's anti-bot page, a host filtered by the proxy) does not
lose the id. Prefixes:

* ``cnbc:<symbol>``  – CNBC daily bars (unofficial endpoint), yields in %: ``DE10Y-DE``,
  ``FR2Y-FR``, ``IT30Y-IT``, ``ES5Y-ES``. Same source for every country, so spreads are
  consistent;
* ``bbk:<flow>/<key>`` – Deutsche Bundesbank SDMX web service, e.g. the official Svensson
  zero-coupon curve of Federal securities ``BBSIS/D.I.ZST.ZI.EUR.S1311.B.A604.R10XX.R.A.A._Z._Z.A``;
* ``stooq:<symbol>`` – Stooq daily CSV (``10dey.b`` = Bund 10Y), often behind an anti-bot page.

A source that is unreachable as a whole (network, anti-bot page) is skipped for the rest
of the request (circuit breaker) instead of timing out once per id.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Sequence
from datetime import date
from typing import Any

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


# -------------------------------------------------------------------- provider
class PublicProvider(DataProvider):
    """CNBC / Bundesbank / Stooq chain for sovereign benchmark yields (no key)."""

    name = "public"
    capabilities = frozenset({Capability.HISTORY, Capability.QUOTES})

    def __init__(self, mapping: MarketDataMap | None = None, timeout_s: float = 20.0, retries: int = 1,
                 session: Any | None = None, quote_lookback_days: int = 10,
                 cnbc_url: str = CNBC_URL, bundesbank_url: str = BUNDESBANK_URL, stooq_url: str = STOOQ_URL) -> None:
        super().__init__(mapping, quote_lookback_days)
        self.http = HttpClient(timeout_s, retries, session=session)
        self.urls = {"cnbc": cnbc_url.rstrip("/"), "bbk": bundesbank_url.rstrip("/"), "stooq": stooq_url}

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

    def fetch_ticker(self, ticker: str, start: date, end: date, broken: set[str] | None = None) -> tuple[pd.Series, str]:
        """First non-empty series of a ``a|b|c`` chain -> (series, source used)."""
        broken = set() if broken is None else broken
        errors = []
        for alt in (t.strip() for t in ticker.split("|") if t.strip()):
            prefix, _, sym = alt.partition(":")
            fetch = {"cnbc": self._cnbc, "bbk": self._bbk, "stooq": self._stooq}.get(prefix)
            if fetch is None:
                raise DataProviderError(f"unknown public source prefix in {alt!r} (cnbc / bbk / stooq)")
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
