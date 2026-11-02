"""Bloomberg provider tested offline with a fake ``blpapi`` module."""

import sys
import types
from datetime import date

import pytest

from market_monitor.data.providers.bloomberg import (
    BloombergProvider,
    element_to_py,
    parse_historical_payload,
    parse_reference_payload,
)
from market_monitor.exceptions import DataProviderError, ProviderUnavailableError

HIST = {
    "securityData": {
        "security": "GDBR10 Index",
        "fieldData": [{"date": date(2026, 10, 1), "PX_LAST": 2.712},
                      {"date": date(2026, 10, 2), "PX_LAST": 2.698}],
    }
}


# ---------------------------------------------------------------- pure parsing
def test_parse_history_with_wrapper():
    [(ticker, series, error)] = parse_historical_payload({"HistoricalDataResponse": HIST}, "PX_LAST")
    assert ticker == "GDBR10 Index" and error is None
    assert series.tolist() == [2.712, 2.698]


def test_parse_history_errors():
    bad_sec = {"securityData": {"security": "XX Index", "securityError": {
        "category": "BAD_SEC", "message": "Unknown/Invalid security"}}}
    assert parse_historical_payload(bad_sec, "PX_LAST")[0][2] == "BAD_SEC / Unknown/Invalid security"
    bad_fld = {"securityData": {"security": "A", "fieldData": [], "fieldExceptions": [
        {"fieldId": "PX_LAST", "errorInfo": {"message": "Field not applicable"}}]}}
    assert "Field not applicable" in parse_historical_payload(bad_fld, "PX_LAST")[0][2]
    with pytest.raises(DataProviderError):
        parse_historical_payload({"responseError": {"message": "boom"}}, "PX_LAST")


def test_parse_reference():
    payload = {"securityData": [
        {"security": "EURUSD Curncy", "fieldData": {"PX_LAST": 1.1712, "LAST_UPDATE_DT": date(2026, 10, 5)}},
        {"security": "XX", "securityError": {"message": "Unknown"}},
    ]}
    rows = parse_reference_payload(payload)
    assert rows[0][1] == 1.1712 and rows[0][3] is None
    assert rows[1][3] == "Unknown"


class _El:
    """Minimal blpapi.Element stand-in."""

    def __init__(self, name, value=None, children=None, array=None):
        self._n, self._v, self._c, self._a = name, value, children, array

    def name(self): return self._n
    def isArray(self): return self._a is not None
    def isComplexType(self): return self._c is not None
    def values(self): return self._a
    def elements(self): return self._c
    def getValue(self): return self._v


def test_element_walker():
    row = _El("row", children=[_El("date", date(2026, 10, 2)), _El("PX_LAST", 1.0)])
    root = _El("root", children=[_El("security", "A"), _El("fieldData", array=[row])])
    expected = {"security": "A", "fieldData": [{"date": date(2026, 10, 2), "PX_LAST": 1.0}]}
    assert element_to_py(root) == expected


# ------------------------------------------------------------- session flow
class _Event:
    TIMEOUT, PARTIAL_RESPONSE, RESPONSE, SESSION_STATUS = 10, 6, 5, 2

    def __init__(self, kind, messages):
        self.kind, self.messages = kind, messages

    def eventType(self): return self.kind
    def __iter__(self): return iter(self.messages)


class _Msg:
    def __init__(self, payload, cid="cid-1", mtype="HistoricalDataResponse"):
        self.payload, self.cid, self.mtype = payload, cid, mtype

    def toPy(self): return self.payload
    def correlationIds(self): return [self.cid]
    def messageType(self): return self.mtype


class _List(list):
    def appendValue(self, v): self.append(v)


class _Request:
    def __init__(self, kind):
        self.kind, self.elements, self.params = kind, {"securities": _List(), "fields": _List()}, {}

    def getElement(self, name): return self.elements[name]
    def set(self, k, v): self.params[k] = v


class _Session:
    def __init__(self, events, start_ok=True):
        self.events, self.start_ok, self.sent, self.stopped = list(events), start_ok, [], False

    def start(self): return self.start_ok
    def openService(self, _): return True
    def stop(self): self.stopped = True
    def getService(self, _): return types.SimpleNamespace(createRequest=_Request)
    def sendRequest(self, request): self.sent.append(request); return "cid-1"
    def nextEvent(self, _timeout): return self.events.pop(0)


@pytest.fixture
def fake_blpapi(monkeypatch):
    module = types.ModuleType("blpapi")
    module.Event = _Event
    monkeypatch.setitem(sys.modules, "blpapi", module)
    return module


def test_history_end_to_end(fake_blpapi):
    other = {"securityData": {"security": "SX5E Index",
                              "fieldData": [{"date": date(2026, 10, 2), "PX_LAST": 5600.0}]}}
    session = _Session([
        _Event(_Event.PARTIAL_RESPONSE, [_Msg(HIST), _Msg(HIST, cid="stale")]),
        _Event(_Event.RESPONSE, [_Msg(other)]),
    ])
    with BloombergProvider(session_factory=lambda: session) as provider:
        result = provider.get_history(["GDBR10 Index", "SX5E Index"], "2026-10-01", "2026-10-02")
    request = session.sent[0]
    assert request.kind == "HistoricalDataRequest"
    assert request.params["startDate"] == "20261001" and request.elements["fields"] == ["PX_LAST"]
    assert result.data.loc["2026-10-02", "SX5E Index"] == 5600.0
    assert result.data["GDBR10 Index"].count() == 2
    assert session.stopped


def test_timeout_and_session_failures(fake_blpapi):
    provider = BloombergProvider(session_factory=lambda: _Session([_Event(_Event.TIMEOUT, [])]))
    with pytest.raises(ProviderUnavailableError, match="timeout"):
        provider.get_history("A", "2026-10-01", "2026-10-02")
    provider = BloombergProvider(session_factory=lambda: _Session([], start_ok=False))
    with pytest.raises(ProviderUnavailableError, match="Terminal"):
        provider.get_history("A", "2026-10-01", "2026-10-02")


def test_without_blpapi(monkeypatch):
    monkeypatch.setitem(sys.modules, "blpapi", None)  # makes the import fail
    provider = BloombergProvider()
    with pytest.raises(ProviderUnavailableError, match="not installed"):
        provider.get_history("A", "2026-10-01", "2026-10-02")


def test_snapshot_end_to_end(fake_blpapi):
    payload = {"securityData": [
        {"security": "EURUSD Curncy", "fieldData": {"PX_LAST": 1.17, "LAST_UPDATE_DT": date(2026, 10, 5)}},
        {"security": "BAD Index", "securityError": {"message": "Unknown"}},
    ]}
    session = _Session([_Event(_Event.RESPONSE, [_Msg(payload, mtype="ReferenceDataResponse")])])
    snap = BloombergProvider(session_factory=lambda: session).get_snapshot(["EURUSD Curncy", "BAD Index"])
    assert session.sent[0].kind == "ReferenceDataRequest"
    assert snap.data.loc["EURUSD Curncy", "value"] == 1.17
    assert snap.errors == {"BAD Index": "Unknown"}


def test_session_lost(fake_blpapi):
    session = _Session([_Event(_Event.SESSION_STATUS, [_Msg({}, mtype="SessionTerminated")])])
    provider = BloombergProvider(session_factory=lambda: session)
    with pytest.raises(ProviderUnavailableError, match="session lost"):
        provider.get_history("A", "2026-10-01", "2026-10-02")
