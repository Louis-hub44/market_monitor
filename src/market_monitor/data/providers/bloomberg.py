"""Bloomberg Desktop API provider (``blpapi``, ``//blp/refdata``).

Reference source. Native tickers are standard Bloomberg securities:
``SX5E Index``, ``GDBR10 Index``, ``EURUSD Curncy``, ``CO1 Comdty``, ``ITRXEXE CBIN Curncy``...

``blpapi`` is imported lazily so that the package works on machines without it.
Messages are converted to plain Python structures (``toPy`` when available, a
recursive element walker otherwise), which keeps the parsing logic testable offline.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from collections.abc import Callable
from typing import Any

import pandas as pd

from market_monitor.data.base import DataProvider
from market_monitor.data.models import Field, HistoryRequest, HistoryResult, SnapshotResult
from market_monitor.data.quality import assemble_frame, clean_series, to_float
from market_monitor.exceptions import DataProviderError, ProviderUnavailableError

logger = logging.getLogger(__name__)

REFDATA = "//blp/refdata"
_FIELD_MAP = {
    Field.LAST: "PX_LAST", Field.OPEN: "PX_OPEN", Field.HIGH: "PX_HIGH",
    Field.LOW: "PX_LOW", Field.VOLUME: "PX_VOLUME",
}
_SNAPSHOT_FIELDS = ("PX_LAST", "LAST_UPDATE_DT")
_SESSION_DOWN = frozenset({"SessionTerminated", "SessionConnectionDown", "SessionStartupFailure"})

ParsedSecurity = tuple[str, "pd.Series | None", "str | None"]


class BloombergProvider(DataProvider):
    """Historical (``HistoricalDataRequest``) and snapshot (``ReferenceDataRequest``) data."""

    name = "bloomberg"

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8194,
        timeout_ms: int = 30_000,
        *,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not 0 < port < 65536 or timeout_ms <= 0:
            raise ValueError("invalid Bloomberg port or timeout")
        self._host = host
        self._port = port
        self._timeout_ms = timeout_ms
        self._session_factory = session_factory
        self._session: Any | None = None

    def is_available(self) -> bool:
        return importlib.util.find_spec("blpapi") is not None

    def close(self) -> None:
        """Stop the underlying session (safe to call twice)."""
        if self._session is not None:
            try:
                self._session.stop()
            finally:
                self._session = None

    def __enter__(self) -> BloombergProvider:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ history
    def _fetch_history(self, request: HistoryRequest) -> HistoryResult:
        bbg_field = _FIELD_MAP[request.field]
        bbg_request = self._new_request("HistoricalDataRequest", request.tickers, (bbg_field,))
        bbg_request.set("startDate", request.start.strftime("%Y%m%d"))
        bbg_request.set("endDate", request.end.strftime("%Y%m%d"))
        bbg_request.set("periodicitySelection", "DAILY")
        bbg_request.set("nonTradingDayFillOption", "ACTIVE_DAYS_ONLY")
        series: dict[str, pd.Series] = {}
        errors: dict[str, str] = {}
        for payload in self._send(bbg_request):
            for ticker, values, error in parse_historical_payload(payload, bbg_field):
                if values is not None:
                    series[ticker] = values
                if error:
                    errors[ticker] = error
        return HistoryResult(data=assemble_frame(series, request.tickers), errors=errors)

    # ----------------------------------------------------------------- snapshot
    def _fetch_snapshot(self, tickers: tuple[str, ...]) -> SnapshotResult:
        bbg_request = self._new_request("ReferenceDataRequest", tickers, _SNAPSHOT_FIELDS)
        rows: dict[str, tuple[float, Any]] = {}
        errors: dict[str, str] = {}
        for payload in self._send(bbg_request):
            for ticker, value, as_of, error in parse_reference_payload(payload):
                if error:
                    errors[ticker] = error
                else:
                    rows[ticker] = (value, as_of)
        frame = pd.DataFrame.from_dict(rows, orient="index", columns=["value", "as_of"])
        return SnapshotResult(data=frame, errors=errors)

    # ------------------------------------------------------------------ session
    def _blpapi(self) -> Any:
        try:
            return importlib.import_module("blpapi")
        except ImportError as exc:
            raise ProviderUnavailableError("blpapi is not installed (see README)") from exc

    def _get_session(self) -> Any:
        if self._session is not None:
            return self._session
        blpapi = self._blpapi()
        if self._session_factory is not None:
            session = self._session_factory()
        else:
            options = blpapi.SessionOptions()
            options.setServerHost(self._host)
            options.setServerPort(self._port)
            session = blpapi.Session(options)
        if not session.start():
            raise ProviderUnavailableError(
                f"cannot start a Bloomberg session on {self._host}:{self._port} "
                "(Terminal open and logged in?)"
            )
        if not session.openService(REFDATA):
            session.stop()
            raise ProviderUnavailableError(f"cannot open {REFDATA}")
        self._session = session
        return session

    def _new_request(self, kind: str, tickers: tuple[str, ...], fields: tuple[str, ...]) -> Any:
        service = self._get_session().getService(REFDATA)
        request = service.createRequest(kind)
        securities = request.getElement("securities")
        for ticker in tickers:
            securities.appendValue(ticker)
        field_list = request.getElement("fields")
        for name in fields:
            field_list.appendValue(name)
        return request

    def _send(self, request: Any) -> list[dict[str, Any]]:
        """Send a request and collect its (partial + final) responses as dicts."""
        blpapi = self._blpapi()
        session = self._get_session()
        cid = session.sendRequest(request)
        payloads: list[dict[str, Any]] = []
        while True:
            event = session.nextEvent(self._timeout_ms)
            kind = event.eventType()
            if kind == blpapi.Event.TIMEOUT:
                raise ProviderUnavailableError(f"Bloomberg timeout after {self._timeout_ms} ms")
            done = False
            for message in event:
                if kind in (blpapi.Event.PARTIAL_RESPONSE, blpapi.Event.RESPONSE):
                    if _matches(message, cid):
                        payloads.append(message_to_py(message))
                        done = done or kind == blpapi.Event.RESPONSE
                elif kind == blpapi.Event.SESSION_STATUS and str(message.messageType()) in _SESSION_DOWN:
                    self._session = None
                    raise ProviderUnavailableError(f"Bloomberg session lost ({message.messageType()})")
            if done:
                return payloads


# --------------------------------------------------------------------------- parsing
def _matches(message: Any, cid: Any) -> bool:
    try:
        return cid in list(message.correlationIds())
    except AttributeError:  # very old blpapi
        return message.correlationId() == cid


def message_to_py(message: Any) -> dict[str, Any]:
    """Convert a blpapi message to nested dict/list/scalars."""
    if hasattr(message, "toPy"):
        return message.toPy()
    return element_to_py(message.asElement())


def element_to_py(element: Any) -> Any:
    """Recursive conversion of a blpapi ``Element`` (fallback for blpapi < 3.23)."""
    if element.isArray():
        return [element_to_py(v) if hasattr(v, "isArray") else v for v in element.values()]
    if element.isComplexType():
        return {str(sub.name()): element_to_py(sub) for sub in element.elements()}
    return element.getValue()


def _unwrap(payload: Any, key: str) -> Any:
    """Strip single-key wrappers (e.g. ``{"HistoricalDataResponse": {...}}``)."""
    while isinstance(payload, dict) and key not in payload and len(payload) == 1:
        payload = next(iter(payload.values()))
    return payload


def _error_text(error: Any) -> str:
    if isinstance(error, dict):
        parts = [str(error[k]) for k in ("category", "subcategory", "message") if error.get(k)]
        return " / ".join(parts) or str(error)
    return str(error)


def _field_exceptions_text(exceptions: Any) -> str | None:
    if not exceptions:
        return None
    items = exceptions if isinstance(exceptions, list) else [exceptions]
    return "; ".join(
        f"{e.get('fieldId', '?')}: {_error_text(e.get('errorInfo', e))}" if isinstance(e, dict) else str(e)
        for e in items
    )


def _security_blocks(payload: Any) -> list[dict[str, Any]]:
    body = _unwrap(payload, "securityData")
    if isinstance(body, dict) and "responseError" in body:
        raise DataProviderError(f"Bloomberg request error: {_error_text(body['responseError'])}")
    if not isinstance(body, dict) or "securityData" not in body:
        raise DataProviderError("unexpected Bloomberg response layout")
    blocks = body["securityData"]
    return [b for b in (blocks if isinstance(blocks, list) else [blocks]) if isinstance(b, dict)]


def parse_historical_payload(payload: Any, bbg_field: str) -> list[ParsedSecurity]:
    """``HistoricalDataResponse`` -> ``[(ticker, series | None, error | None)]``."""
    results: list[ParsedSecurity] = []
    for block in _security_blocks(payload):
        ticker = str(block.get("security", ""))
        if block.get("securityError"):
            results.append((ticker, None, _error_text(block["securityError"])))
            continue
        rows = block.get("fieldData") or []
        values = {
            pd.Timestamp(r["date"]): r[bbg_field]
            for r in rows
            if isinstance(r, dict) and "date" in r and bbg_field in r
        }
        series = clean_series(pd.Series(values, dtype="object")) if values else None
        error = None if series is not None and not series.empty else _field_exceptions_text(
            block.get("fieldExceptions")
        )
        results.append((ticker, series, error))
    return results


def parse_reference_payload(payload: Any) -> list[tuple[str, float, Any, str | None]]:
    """``ReferenceDataResponse`` -> ``[(ticker, PX_LAST, LAST_UPDATE_DT, error | None)]``."""
    results: list[tuple[str, float, Any, str | None]] = []
    for block in _security_blocks(payload):
        ticker = str(block.get("security", ""))
        if block.get("securityError"):
            results.append((ticker, float("nan"), pd.NaT, _error_text(block["securityError"])))
            continue
        data = block.get("fieldData") or {}
        value = to_float(data.get("PX_LAST"))
        as_of = pd.Timestamp(data["LAST_UPDATE_DT"]) if data.get("LAST_UPDATE_DT") else pd.NaT
        error = None if pd.notna(value) else (
            _field_exceptions_text(block.get("fieldExceptions")) or "PX_LAST not available"
        )
        results.append((ticker, value, as_of, error))
    return results
