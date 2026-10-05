from datetime import date

import pytest

from market_monitor.config import settings_from_dict
from market_monitor.data.factory import build_provider
from market_monitor.data.models import Field
from market_monitor.data.providers.free import ECBClient, FreeProvider, YahooClient, default_sources
from market_monitor.data.providers.public import (
    SDMX_CSV,
    BundesbankClient,
    FredClient,
    StooqClient,
    StoxxClient,
    parse_fred_csv,
    parse_sdmx_csv,
    parse_stooq_csv,
    parse_stoxx_txt,
)
from market_monitor.exceptions import DataProviderError
from market_monitor.referential import load_referential
from tests.conftest import FakeResponse, FakeSession
from tests.test_config_factory import REPO_CONFIG

START, END = date(2026, 10, 1), date(2026, 10, 2)
STOOQ = "Date,Open,High,Low,Close\n2026-09-30,2.6,2.7,2.5,2.65\n2026-10-01,2.66,2.7,2.6,2.68\n" \
        "2026-10-02,2.7,2.72,2.65,2.70\n"


class HeaderSession(FakeSession):
    def get(self, url, params=None, timeout=None, headers=None):
        self.headers = headers
        return super().get(url, params, timeout, headers)


def test_stooq_client_and_window():
    session = FakeSession([FakeResponse(200, text=STOOQ)])
    series = StooqClient(session=session).fetch("10DEY.B", START, END)
    assert series.tolist() == [2.68, 2.70]  # 30/09 outside the window
    assert session.calls[0]["params"] == {"s": "10dey.b", "i": "d", "d1": "20261001", "d2": "20261002"}


@pytest.mark.parametrize("text", ["No data", "", "  "])
def test_stooq_no_data(text):
    assert parse_stooq_csv(text).empty


def test_stooq_quota_message_is_an_error():
    with pytest.raises(DataProviderError, match="Exceeded the daily hits limit"):
        parse_stooq_csv("Exceeded the daily hits limit", "10dey.b")


def test_bundesbank_requests_sdmx_csv():
    payload = "KEY;FREQ;TIME_PERIOD;OBS_VALUE\nX;D;2026-10-01;2.61\nX;D;2026-10-02;2.63\n"
    session = HeaderSession([FakeResponse(200, text=payload)])
    key = "BBSIS/D.I.ZST.ZI.EUR.S1311.B.A604.R10XX.R.A.A._Z._Z.A"
    assert BundesbankClient(session=session).fetch(key, START, END).tolist() == [2.61, 2.63]
    assert session.headers == {"Accept": SDMX_CSV}
    assert session.calls[0]["url"].endswith("/BBSIS/D.I.ZST.ZI.EUR.S1311.B.A604.R10XX.R.A.A._Z._Z.A")
    with pytest.raises(DataProviderError):
        BundesbankClient(session=session).fetch("no-slash", START, END)
    with pytest.raises(DataProviderError):
        parse_sdmx_csv("A,B\n1,2\n")


def test_fred_csv_with_missing_values():
    text = "observation_date,DGS2\n2026-09-30,3.55\n2026-10-01,.\n2026-10-02,3.58\n"
    assert parse_fred_csv(text).tolist() == [3.55, 3.58]
    session = FakeSession([FakeResponse(200, text=text)])
    assert FredClient(session=session).fetch("DGS2", START, END).tolist() == [3.58]
    assert session.calls[0]["params"]["id"] == "DGS2"


def test_stoxx_file_and_html_page():
    text = "Date;Symbol;Indexvalue\n30.09.2026;V2TX;18.5\n01.10.2026;V2TX;19.1\n02.10.2026;V2TX;17.9\n"
    assert parse_stoxx_txt(text).tolist() == [18.5, 19.1, 17.9]
    session = FakeSession([FakeResponse(200, text=text)])
    assert StoxxClient(session=session).fetch("V2TX", START, END).tolist() == [19.1, 17.9]
    assert session.calls[0]["url"].endswith("/h_v2tx.txt")
    with pytest.raises(DataProviderError, match="web page"):
        parse_stoxx_txt("<html><body>login</body></html>")


def test_http_errors_are_reported_and_404_is_empty():
    assert StooqClient(session=FakeSession([FakeResponse(404)]), max_retries=0).fetch(
        "x", START, END).empty
    with pytest.raises(DataProviderError, match="FRED HTTP 403"):
        FredClient(session=FakeSession([FakeResponse(403)]), max_retries=0).fetch("DGS2", START, END)


def test_free_provider_routes_new_prefixes():
    sources = {"stooq:": StooqClient(session=FakeSession([FakeResponse(200, text=STOOQ)])),
               "fred:": FredClient(session=FakeSession([FakeResponse(500)]), max_retries=0)}
    seen = {}

    def downloader(tickers, start, end):
        seen["tickers"] = tickers
        return None

    provider = FreeProvider(ECBClient(session=FakeSession([])), YahooClient(downloader), sources=sources)
    result = provider.get_history(["stooq:10dey.b", "fred:DGS2", "^VIX"], START, END)
    assert result.data["stooq:10dey.b"].dropna().tolist() == [2.68, 2.70]
    assert "FRED" in result.errors["fred:DGS2"]
    assert seen["tickers"] == ["^VIX"]  # only un-prefixed tickers go to Yahoo
    high = provider.get_history("stooq:10dey.b", START, END, Field.HIGH)
    assert "LAST" in high.errors["stooq:10dey.b"]


def test_default_sources_share_the_corporate_session(tmp_path):
    settings = settings_from_dict({}, base_dir=tmp_path, env={"MARKET_MONITOR_INSECURE_SSL": "1"})
    provider = build_provider("free", settings)
    sessions = {id(client._session) for client in provider._sources.values()}
    assert len(sessions) == 1
    assert next(iter(provider._sources.values()))._session.verify is False
    assert set(default_sources()) == {"stooq:", "bbk:", "fred:", "stoxx:"}


def test_repository_maps_former_bloomberg_only_rates_to_free_sources():
    ref = load_referential(REPO_CONFIG.parent / "instruments.yaml", REPO_CONFIG.parent / "watchlists.yaml")
    for country in ("BUND", "OAT", "BTP", "BONOS"):
        for tenor in ("2Y", "5Y", "10Y", "30Y"):
            assert ref.get(f"{country}_{tenor}").tickers["free"].ticker.startswith("stooq:")
    assert ref.get("UST_2Y").tickers["free"].ticker == "fred:DGS2"
    assert ref.get("V2X").tickers["free"].ticker == "stoxx:v2tx"
    assert ref.get("SX86P").tickers["free"].proxy
