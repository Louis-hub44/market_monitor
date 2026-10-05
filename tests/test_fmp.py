from datetime import date

import pytest
import requests

from market_monitor.data.providers.fmp import FMPProvider, _chunks
from market_monitor.exceptions import ProviderUnavailableError
from tests.conftest import FakeResponse, FakeSession

KEY = "SECRET123"


def _provider(responses):
    session = FakeSession(responses)
    return FMPProvider(KEY, session=session, backoff_s=0, sleep=lambda _: None), session


def test_stable_list_payload():
    bars = [{"symbol": "^GSPC", "date": "2026-10-02", "close": 6700.5},
            {"symbol": "^GSPC", "date": "2026-10-01", "close": 6650.0}]
    provider, session = _provider([FakeResponse(payload=bars)])
    result = provider.get_history(["^GSPC"], "2026-10-01", "2026-10-02")
    assert result.data["^GSPC"].tolist() == [6650.0, 6700.5]
    assert session.calls[0]["url"].endswith("/historical-price-eod/full")
    assert session.calls[0]["params"]["from"] == "2026-10-01"


def test_legacy_dict_payload():
    payload = {"symbol": "EURUSD", "historical": [{"date": "2026-10-02", "close": 1.17}]}
    provider, _ = _provider([FakeResponse(payload=payload)])
    assert provider.get_history("EURUSD", "2026-10-01", "2026-10-02").data["EURUSD"].iloc[0] == 1.17


def test_treasury_rates_and_bad_tenor():
    rows = [{"date": "2026-10-02", "year2": 3.55, "year10": 4.12}]
    provider, session = _provider([FakeResponse(payload=rows)])
    result = provider.get_history(["treasury:year10", "treasury:year11"], "2026-10-01", "2026-10-02")
    assert result.data["treasury:year10"].iloc[0] == 4.12
    assert "unknown treasury tenor" in result.errors["treasury:year11"]
    assert len(session.calls) == 1


def test_per_symbol_errors_do_not_kill_the_request():
    ok = [{"date": "2026-10-02", "close": 1.0}]
    provider, _ = _provider([FakeResponse(403), FakeResponse(payload=ok)])
    result = provider.get_history(["PREMIUM", "OK"], "2026-10-01", "2026-10-02")
    assert "subscription" in result.errors["PREMIUM"]
    assert result.sources == {"OK": "fmp"}


def test_retry_on_429_then_success():
    sleeps = []
    session = FakeSession([FakeResponse(429), FakeResponse(payload=[{"date": "2026-10-02", "close": 2.0}])])
    provider = FMPProvider(KEY, session=session, backoff_s=0.5, sleep=sleeps.append)
    assert provider.get_history("X", "2026-10-01", "2026-10-02").data["X"].iloc[0] == 2.0
    assert sleeps == [0.5]


@pytest.mark.parametrize("response", [FakeResponse(401), FakeResponse(429),
                                      FakeResponse(payload={"Error Message": "Invalid API KEY."})])
def test_provider_level_failures(response):
    provider, _ = _provider([response])
    with pytest.raises(ProviderUnavailableError):
        provider.get_history("X", "2026-10-01", "2026-10-02")


def test_api_key_never_leaks_in_errors():
    err = requests.ConnectionError(f"https://x/?apikey={KEY}")
    provider, _ = _provider([err])
    with pytest.raises(ProviderUnavailableError) as info:
        provider.get_history("X", "2026-10-01", "2026-10-02")
    assert KEY not in str(info.value)


def test_missing_key_is_unavailable():
    provider = FMPProvider(None)
    assert not provider.is_available()
    with pytest.raises(ProviderUnavailableError):
        provider.get_history("X", "2026-10-01", "2026-10-02")


def test_snapshot_quote():
    provider, _ = _provider([FakeResponse(payload=[{"symbol": "^GSPC", "price": 6701.0,
                                                    "timestamp": 1759435200}])])
    snap = provider.get_snapshot(["^GSPC"])
    assert snap.data.loc["^GSPC", "value"] == 6701.0
    assert snap.data.loc["^GSPC", "as_of"].year == 2025


def test_chunks_cover_window_exactly():
    chunks = _chunks(date(2026, 1, 1), date(2026, 12, 31), 90)
    assert chunks[0][0] == date(2026, 1, 1) and chunks[-1][1] == date(2026, 12, 31)
    assert all(b[0] - a[1] == date(2026, 1, 2) - date(2026, 1, 1) for a, b in zip(chunks, chunks[1:]))


def test_snapshot_treasury_falls_back_to_history():
    from datetime import date, timedelta

    day = (date.today() - timedelta(days=1)).isoformat()
    provider, _ = _provider([FakeResponse(payload=[{"date": day, "year10": 4.1}])])
    snap = provider.get_snapshot(["treasury:year10"])
    assert snap.data.loc["treasury:year10", "value"] == 4.1
