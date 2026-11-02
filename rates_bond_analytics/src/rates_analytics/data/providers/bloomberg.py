"""Bloomberg Desktop API provider (reference source).

Requires a running Terminal on the same machine and the ``blpapi`` package
(``pip install --index-url=https://blpapi.bloomberg.com/repository/releases/python/simple blpapi``).
``blpapi`` is imported lazily: the rest of the engine works without it.

The provider talks to Bloomberg through a minimal :class:`BlpClient` protocol
(``reference`` / ``historical``) so that it can be unit-tested with a fake
client; :class:`BlpapiClient` is the real implementation (//blp/refdata).

Mapping spec fields: ``ticker`` (e.g. ``EESWE10 Curncy``), optional ``field``
(default ``PX_LAST``) and ``scale`` (0.01 for rates quoted in %).
Bonds are requested as ``/isin/<ISIN>``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Protocol, Sequence

import pandas as pd

from rates_analytics.data.mapping import MarketDataMap
from rates_analytics.data.provider import (
    BOND_PRICE_COLUMNS,
    BOND_STATIC_COLUMNS,
    QUOTE_COLUMNS,
    Capability,
    DataProvider,
    empty_frame,
    empty_history,
)
from rates_analytics.exceptions import ConfigurationError, DataProviderError

logger = logging.getLogger(__name__)

STATIC_FIELDS = {
    "SECURITY_DES": "name", "CNTRY_OF_RISK": "issuer", "CRNCY": "currency", "CPN": "coupon",
    "CPN_FREQ": "frequency", "DAY_CNT_DES": "day_count", "ISSUE_DT": "issue_date",
    "INT_ACC_DT": "accrual_start", "FIRST_CPN_DT": "first_coupon_date", "MATURITY": "maturity",
}
PRICE_FIELDS = ["PX_BID", "PX_ASK", "PX_MID", "YLD_YTM_MID"]


class BlpClient(Protocol):
    """Minimal Bloomberg request interface."""

    def reference(self, securities: Sequence[str], fields: Sequence[str]) -> dict[str, dict[str, Any]]:
        """ReferenceDataRequest -> {security: {field: value}} (missing fields omitted)."""

    def historical(self, securities: Sequence[str], fields: Sequence[str],
                   start: date, end: date) -> dict[str, pd.DataFrame]:
        """HistoricalDataRequest -> {security: DataFrame(index=date, columns=fields)}."""

    def close(self) -> None: ...


class BlpapiClient:
    """Real ``blpapi`` client on //blp/refdata (Desktop API, localhost:8194)."""

    def __init__(self, host: str = "localhost", port: int = 8194, timeout_ms: int = 30000) -> None:
        try:
            import blpapi  # noqa: PLC0415  (lazy, optional dependency)
        except ImportError as exc:
            raise ConfigurationError("blpapi is not installed; see README (Bloomberg provider)") from exc
        self._blpapi = blpapi
        self.timeout_ms = timeout_ms
        opts = blpapi.SessionOptions()
        opts.setServerHost(host)
        opts.setServerPort(int(port))
        self._session = blpapi.Session(opts)
        if not self._session.start():
            raise DataProviderError(f"Cannot start Bloomberg session on {host}:{port} (is the Terminal running?)")
        if not self._session.openService("//blp/refdata"):
            raise DataProviderError("Cannot open //blp/refdata")
        self._svc = self._session.getService("//blp/refdata")

    def _send(self, request) -> list:
        self._session.sendRequest(request)
        messages = []
        while True:
            ev = self._session.nextEvent(self.timeout_ms)
            et = ev.eventType()
            if et == self._blpapi.Event.TIMEOUT:
                raise DataProviderError("Bloomberg request timed out")
            for msg in ev:
                if msg.hasElement("responseError"):
                    raise DataProviderError(f"Bloomberg error: {msg.getElement('responseError')}")
                messages.append(msg)
            if et == self._blpapi.Event.RESPONSE:
                return messages

    def reference(self, securities, fields):
        req = self._svc.createRequest("ReferenceDataRequest")
        for s in securities:
            req.getElement("securities").appendValue(s)
        for f in fields:
            req.getElement("fields").appendValue(f)
        out: dict[str, dict[str, Any]] = {}
        for msg in self._send(req):
            if not msg.hasElement("securityData"):
                continue
            arr = msg.getElement("securityData")
            for i in range(arr.numValues()):
                sd = arr.getValueAsElement(i)
                sec = sd.getElementAsString("security")
                if sd.hasElement("securityError"):
                    logger.warning("Bloomberg security error on %s", sec)
                    continue
                fd = sd.getElement("fieldData")
                out[sec] = {f: fd.getElement(f).getValue() for f in fields if fd.hasElement(f)}
        return out

    def historical(self, securities, fields, start, end):
        req = self._svc.createRequest("HistoricalDataRequest")
        for s in securities:
            req.getElement("securities").appendValue(s)
        for f in fields:
            req.getElement("fields").appendValue(f)
        req.set("startDate", start.strftime("%Y%m%d"))
        req.set("endDate", end.strftime("%Y%m%d"))
        req.set("periodicitySelection", "DAILY")
        out: dict[str, pd.DataFrame] = {}
        for msg in self._send(req):
            if not msg.hasElement("securityData"):
                continue
            sd = msg.getElement("securityData")
            sec = sd.getElementAsString("security")
            if sd.hasElement("securityError"):
                logger.warning("Bloomberg security error on %s", sec)
                continue
            fd = sd.getElement("fieldData")
            rows = []
            for i in range(fd.numValues()):
                row = fd.getValueAsElement(i)
                rec = {"date": pd.Timestamp(row.getElementAsDatetime("date"))}
                rec.update({f: row.getElementAsFloat(f) for f in fields if row.hasElement(f)})
                rows.append(rec)
            frame = pd.DataFrame(rows)
            out[sec] = frame.set_index("date") if not frame.empty else frame
        return out

    def close(self) -> None:
        self._session.stop()


def _to_date(v: Any) -> date | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return pd.Timestamp(v).date()


_FREQ = {1: 1, 2: 2, 4: 4, 12: 12, 0: 0}


class BloombergProvider(DataProvider):
    """Bloomberg Desktop API provider."""

    name = "bloomberg"
    capabilities = frozenset(Capability)

    def __init__(self, mapping: MarketDataMap | None = None, host: str = "localhost", port: int = 8194,
                 timeout_ms: int = 30000, default_field: str = "PX_LAST",
                 client: BlpClient | None = None, quote_lookback_days: int = 10,
                 today: Any = date.today) -> None:
        super().__init__(mapping, quote_lookback_days)
        self._client = client
        self._conn = (host, port, timeout_ms)
        self.default_field = default_field
        self._today = today

    @property
    def client(self) -> BlpClient:
        if self._client is None:  # lazy connection: only when data is first requested
            self._client = BlpapiClient(*self._conn)
        return self._client

    def _specs(self, ids: Sequence[str]) -> dict[str, Mapping[str, Any]]:
        return {mid: s for mid in ids if (s := self.mapping.spec(mid, self.name)) is not None}

    def get_history(self, ids: Sequence[str], start: date, end: date) -> pd.DataFrame:
        specs = self._specs(ids)
        if not specs:
            return empty_history()
        by_field: dict[str, list[str]] = {}
        for mid, s in specs.items():
            by_field.setdefault(str(s.get("field", self.default_field)), []).append(mid)
        cols: dict[str, pd.Series] = {}
        for fld, mids in by_field.items():
            tickers = {str(specs[m]["ticker"]): m for m in mids}
            raw = self.client.historical(list(tickers), [fld], start, end)
            for tkr, frame in raw.items():
                if tkr in tickers and not frame.empty and fld in frame:
                    mid = tickers[tkr]
                    cols[mid] = frame[fld].astype(float) * float(specs[mid].get("scale", 1.0))
        if not cols:
            return empty_history()
        out = pd.concat(cols, axis=1)
        out.index = pd.DatetimeIndex(out.index, name="date")
        return out

    def get_quotes(self, ids: Sequence[str], as_of: date) -> pd.DataFrame:
        if as_of < self._today():
            return super().get_quotes(ids, as_of)
        specs = self._specs(ids)
        rows: dict[str, dict] = {}
        by_field: dict[str, list[str]] = {}
        for mid, s in specs.items():
            by_field.setdefault(str(s.get("field", self.default_field)), []).append(mid)
        for fld, mids in by_field.items():
            tickers = {str(specs[m]["ticker"]): m for m in mids}
            raw = self.client.reference(list(tickers), [fld])
            for tkr, vals in raw.items():
                if tkr in tickers and vals.get(fld) is not None:
                    mid = tickers[tkr]
                    rows[mid] = {"value": float(vals[fld]) * float(specs[mid].get("scale", 1.0)),
                                 "obs_date": as_of, "source": self.name}
        if not rows:
            return empty_frame(QUOTE_COLUMNS)
        df = pd.DataFrame.from_dict(rows, orient="index")[QUOTE_COLUMNS]
        df.index.name = "id"
        return df

    def get_bond_static(self, isins: Sequence[str]) -> pd.DataFrame:
        secs = {f"/isin/{i}": i for i in isins}
        raw = self.client.reference(list(secs), list(STATIC_FIELDS))
        rows = {}
        for sec, vals in raw.items():
            if sec not in secs:
                continue
            r = {STATIC_FIELDS[k]: v for k, v in vals.items() if k in STATIC_FIELDS}
            r["coupon"] = float(r["coupon"]) / 100.0 if r.get("coupon") is not None else None
            r["frequency"] = _FREQ.get(int(r["frequency"])) if r.get("frequency") is not None else None
            for c in ("issue_date", "accrual_start", "first_coupon_date", "maturity"):
                r[c] = _to_date(r.get(c))
            r["source"] = self.name
            rows[secs[sec]] = r
        if not rows:
            return empty_frame(BOND_STATIC_COLUMNS)
        df = pd.DataFrame.from_dict(rows, orient="index").reindex(columns=BOND_STATIC_COLUMNS)
        df.index.name = "id"
        return df

    def get_bond_prices(self, isins: Sequence[str], as_of: date) -> pd.DataFrame:
        secs = {f"/isin/{i}": i for i in isins}
        rows = {}
        if as_of >= self._today():
            raw = self.client.reference(list(secs), PRICE_FIELDS)
            snap = {sec: (vals, as_of) for sec, vals in raw.items()}
        else:
            hist = self.client.historical(list(secs), PRICE_FIELDS, 
                                            as_of - timedelta(days=self.quote_lookback_days), as_of)
            snap = {}
            for sec, frame in hist.items():
                if not frame.empty:
                    last = frame.sort_index().iloc[-1]
                    snap[sec] = (last.to_dict(), frame.sort_index().index[-1].date())
        for sec, (vals, obs) in snap.items():
            if sec not in secs:
                continue
            bid, ask = vals.get("PX_BID"), vals.get("PX_ASK")
            mid = vals.get("PX_MID")
            if mid is None and bid is not None and ask is not None:
                mid = 0.5 * (float(bid) + float(ask))
            y = vals.get("YLD_YTM_MID")
            rows[secs[sec]] = {"bid": bid, "ask": ask, "mid": mid,
                               "yield_mid": float(y) / 100.0 if y is not None else None,
                               "obs_date": obs, "source": self.name}
        if not rows:
            return empty_frame(BOND_PRICE_COLUMNS)
        df = pd.DataFrame.from_dict(rows, orient="index").reindex(columns=BOND_PRICE_COLUMNS)
        df.index.name = "id"
        return df

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
