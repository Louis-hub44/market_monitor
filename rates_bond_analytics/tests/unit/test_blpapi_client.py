"""``BlpapiClient`` against a fake ``blpapi`` module (no Terminal needed).

The fake reproduces the small part of the blpapi object model the client uses
(Session / Service / Request / Event / Message / Element), so that the parsing
of ReferenceData and HistoricalData responses is tested end to end.
"""

from __future__ import annotations

import sys
import types
from datetime import date, datetime

import pandas as pd
import pytest

from rates_analytics.data.providers.bloomberg import BloombergProvider, BlpapiClient
from rates_analytics.exceptions import DataProviderError


class El:
    """Element: wraps a dict (sequence of named sub-elements), a list (array) or a scalar."""

    def __init__(self, value):
        self.v = value

    def hasElement(self, name):
        return isinstance(self.v, dict) and name in self.v

    def getElement(self, name):
        return El(self.v[name])

    def getElementAsString(self, name):
        return str(self.v[name])

    def getElementAsFloat(self, name):
        return float(self.v[name])

    def getElementAsDatetime(self, name):
        return self.v[name]

    def getValue(self):
        return self.v

    def numValues(self):
        return len(self.v)

    def getValueAsElement(self, i):
        return El(self.v[i])

    def __str__(self):
        return str(self.v)


class Request:
    def __init__(self, kind):
        self.kind, self.lists, self.params = kind, {"securities": [], "fields": []}, {}

    def getElement(self, name):
        req = self

        class _Arr:
            def appendValue(self, x):
                req.lists[name].append(x)

        return _Arr()

    def set(self, k, v):
        self.params[k] = v


class Event(list):
    TIMEOUT, PARTIAL_RESPONSE, RESPONSE = 10, 6, 5

    def __init__(self, kind, msgs=()):
        super().__init__(msgs)
        self.kind = kind

    def eventType(self):
        return self.kind


def make_blpapi(responder, start_ok=True, service_ok=True):
    """Fake blpapi module; ``responder(request) -> list[Event]``."""
    mod = types.ModuleType("blpapi")
    sent = []

    class SessionOptions:
        def setServerHost(self, h):
            self.host = h

        def setServerPort(self, p):
            self.port = p

    class Service:
        def createRequest(self, kind):
            return Request(kind)

    class Session:
        def __init__(self, opts):
            self.queue, self.stopped = [], False

        def start(self):
            return start_ok

        def openService(self, name):
            return service_ok

        def getService(self, name):
            return Service()

        def sendRequest(self, req):
            sent.append(req)
            self.queue = list(responder(req))

        def nextEvent(self, timeout):
            return self.queue.pop(0)

        def stop(self):
            self.stopped = True

    mod.SessionOptions, mod.Session, mod.Event = SessionOptions, Session, Event
    mod.sent = sent
    return mod


def ref_responder(req):
    data = []
    for s in req.lists["securities"]:
        if s == "BAD Curncy":
            data.append({"security": s, "securityError": {"message": "Unknown security"}})
        else:
            data.append({"security": s, "fieldData": {f: 2.5 for f in req.lists["fields"] if f != "PX_BID"}})
    half = len(data) // 2   # split across a partial and a final response
    return [Event(Event.PARTIAL_RESPONSE, [El({"securityData": data[:half]})]),
            Event(Event.RESPONSE, [El({"securityData": data[half:]}), El({"other": 1})])]


def hist_responder(req):
    out = []
    for s in req.lists["securities"]:
        rows = [{"date": datetime(2026, 10, d), "PX_LAST": 2.0 + d / 100} for d in (5, 6)]
        out.append(Event(Event.PARTIAL_RESPONSE, [El({"securityData": {"security": s, "fieldData": rows}})]))
    out.append(Event(Event.RESPONSE, [El({"securityData": {"security": "BAD", "securityError": {}}})]))
    return out


@pytest.fixture
def fake(monkeypatch):
    def install(responder, **kw):
        mod = make_blpapi(responder, **kw)
        monkeypatch.setitem(sys.modules, "blpapi", mod)
        return mod
    return install


def test_reference_request_parsing(fake):
    mod = fake(ref_responder)
    client = BlpapiClient("localhost", 8194)
    out = client.reference(["EESWE10 Curncy", "BAD Curncy", "ESTRON Index"], ["PX_LAST", "PX_BID"])
    assert out == {"EESWE10 Curncy": {"PX_LAST": 2.5}, "ESTRON Index": {"PX_LAST": 2.5}}   # error skipped, field missing
    assert mod.sent[0].kind == "ReferenceDataRequest" and mod.sent[0].lists["fields"] == ["PX_LAST", "PX_BID"]
    client.close()


def test_historical_request_parsing(fake):
    mod = fake(hist_responder)
    out = BlpapiClient().historical(["EESWE10 Curncy"], ["PX_LAST"], date(2026, 10, 1), date(2026, 10, 6))
    frame = out["EESWE10 Curncy"]
    assert list(frame["PX_LAST"]) == [2.05, 2.06] and frame.index[-1] == pd.Timestamp("2026-10-06")
    assert mod.sent[0].params == {"startDate": "20261001", "endDate": "20261006", "periodicitySelection": "DAILY"}


def test_errors_timeout_and_session(fake):
    fake(lambda req: [Event(Event.TIMEOUT)])
    with pytest.raises(DataProviderError, match="timed out"):
        BlpapiClient().reference(["X"], ["PX_LAST"])
    fake(lambda req: [Event(Event.RESPONSE, [El({"responseError": {"message": "bad request"}})])])
    with pytest.raises(DataProviderError, match="Bloomberg error"):
        BlpapiClient().reference(["X"], ["PX_LAST"])
    fake(ref_responder, start_ok=False)
    with pytest.raises(DataProviderError, match="Terminal running"):
        BlpapiClient()
    fake(ref_responder, service_ok=False)
    with pytest.raises(DataProviderError, match="refdata"):
        BlpapiClient()


def test_provider_end_to_end_through_fake_blpapi(fake):
    """Provider -> real BlpapiClient -> fake blpapi: mapping, scaling and lazy connection."""
    from rates_analytics.data.mapping import MarketDataMap

    fake(ref_responder)
    mapping = MarketDataMap({"EUR.ESTR.OIS.10Y": {"bloomberg": {"ticker": "EESWE10 Curncy", "scale": 0.01}}})
    prov = BloombergProvider(mapping, today=lambda: date(2026, 10, 6))
    q = prov.get_quotes(["EUR.ESTR.OIS.10Y"], date(2026, 10, 6))
    assert q.loc["EUR.ESTR.OIS.10Y", "value"] == pytest.approx(0.025)
    prov.close()
