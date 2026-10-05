"""Shared fakes and fixtures (no network, no Bloomberg needed)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from market_monitor.data.base import DataProvider
from market_monitor.data.models import HistoryRequest, HistoryResult
from market_monitor.data.quality import assemble_frame


def bday_series(start: str, end: str, base: float = 100.0) -> pd.Series:
    index = pd.bdate_range(start, end, name="date")
    return pd.Series(base + np.arange(len(index), dtype="float64"), index=index)


class FakeProvider(DataProvider):
    """In-memory provider recording every request."""

    def __init__(self, name: str = "fake", data: dict[str, pd.Series] | None = None) -> None:
        self.name = name
        self.data = data or {}
        self.calls: list[HistoryRequest] = []
        self.fail: Exception | None = None
        self.available = True

    def is_available(self) -> bool:
        return self.available

    def _fetch_history(self, request: HistoryRequest) -> HistoryResult:
        self.calls.append(request)
        if self.fail is not None:
            raise self.fail
        series = {
            t: s.loc[pd.Timestamp(request.start) : pd.Timestamp(request.end)]
            for t, s in self.data.items()
            if t in request.tickers
        }
        return HistoryResult(data=assemble_frame(series, request.tickers))


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> None:
        self.now += timedelta(**kwargs)


class FakeResponse:
    def __init__(self, status: int = 200, payload: Any = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """requests.Session stand-in returning queued responses (or raising)."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: Any = None, timeout: Any = None, headers: Any = None) -> Any:
        self.calls.append({"url": url, "params": dict(params or {})})
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def clock() -> MutableClock:
    # 08:00 UTC = 10:00 Paris, Monday 5 October 2026
    return MutableClock(datetime(2026, 10, 5, 8, 0, tzinfo=UTC))
