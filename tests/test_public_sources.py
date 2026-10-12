from datetime import date

import pandas as pd
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
    assert set(default_sources()) == {"stooq:", "bbk:", "fred:", "fredapi:", "stoxx:", "cnbc:", "msci:"}


def test_repository_maps_former_bloomberg_only_rates_to_free_sources():
    ref = load_referential(REPO_CONFIG.parent / "instruments.yaml", REPO_CONFIG.parent / "watchlists.yaml")
    for country in ("BUND", "OAT", "BTP", "BONOS"):
        for tenor in ("2Y", "5Y", "10Y", "30Y"):
            chain = ref.get(f"{country}_{tenor}").tickers["free"].ticker.split("|")
            assert chain[0].startswith("cnbc:")  # CNBC matches the Investing closes
            if country == "BUND":  # then the official Bundesbank curve
                assert chain[1].startswith("bbk:BBSIS/") and chain[2].startswith("stooq:")
            else:
                assert chain[1].startswith("stooq:")
    # the whole US curve comes from CNBC first (one source, one date), then the former sources
    assert ref.get("UST_2Y").tickers["free"].ticker == "cnbc:US2Y|fred:DGS2|2YY=F"
    for tenor, fallback in (("5Y", "^FVX"), ("10Y", "^TNX"), ("30Y", "^TYX")):
        assert ref.get(f"UST_{tenor}").tickers["free"].ticker == f"cnbc:US{tenor}|{fallback}"
    assert "|bbk:BBSIS/D.I.ZST.ZI.EUR.S1311.B.A604.R10XX" in ref.get("BUND_10Y").tickers["free"].ticker
    assert ref.get("V2X").tickers["free"].ticker == "stoxx:v2tx"
    assert ref.get("SX86P").tickers["free"].proxy


# ------------------------------------------------------------ chains, breaker
class CountingClient:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def fetch(self, key, start, end):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _series(*values):
    import pandas as pd
    return pd.Series(values, index=pd.to_datetime(["2026-10-01", "2026-10-02"][:len(values)]))


def test_chain_falls_back_and_warns():
    from market_monitor.exceptions import ProviderUnavailableError

    stooq = CountingClient(ProviderUnavailableError("Stooq unreachable (ReadTimeout)"))
    bbk = CountingClient(_series(2.61, 2.63))
    provider = FreeProvider(ECBClient(session=FakeSession([])), YahooClient(lambda *a: None),
                            sources={"stooq:": stooq, "bbk:": bbk})
    ticker = "stooq:10dey.b|bbk:BBSIS/X"
    result = provider.get_history(ticker, START, END)
    assert result.data[ticker].tolist() == [2.61, 2.63]
    assert "bbk:BBSIS/X" in result.warnings[ticker] and "ReadTimeout" in result.warnings[ticker]


def test_chain_reports_every_failure_and_yahoo_alternative():
    stooq = CountingClient(_series())
    provider = FreeProvider(ECBClient(session=FakeSession([])), YahooClient(lambda *a: None),
                            sources={"stooq:": stooq})
    result = provider.get_history("stooq:x|2YY=F", START, END)
    assert "stooq:x: no data returned" in result.errors["stooq:x|2YY=F"]
    assert "2YY=F" in result.errors["stooq:x|2YY=F"]


def test_unreachable_source_is_skipped_until_the_ttl_expires():
    from market_monitor.exceptions import ProviderUnavailableError

    now = [0.0]
    stooq = CountingClient(ProviderUnavailableError("Stooq unreachable (ConnectTimeout)"))
    provider = FreeProvider(ECBClient(session=FakeSession([])), YahooClient(lambda *a: None),
                            sources={"stooq:": stooq}, down_ttl_s=60, clock=lambda: now[0])
    result = provider.get_history(["stooq:a", "stooq:b", "stooq:c"], START, END)
    assert stooq.calls == 1  # one timeout, not three
    assert "skipped" in result.errors["stooq:b"]
    now[0] = 61
    provider.get_history("stooq:a", START, END)
    assert stooq.calls == 2


def test_ecb_search_lists_series():
    text = ("KEY,FREQ,TITLE,TIME_PERIOD,OBS_VALUE\n"
            "FM.D.DE.EUR.4F.BB.DE10YT_RR.YLD,D,Germany 10Y,2026-10-02,2.70\n")
    found = ECBClient(session=FakeSession([FakeResponse(200, text=text)])).search("FM", "D.DE....YLD")
    assert found.loc[0, "KEY"] == "FM.D.DE.EUR.4F.BB.DE10YT_RR.YLD" and found.loc[0, "OBS_VALUE"] == 2.70
    assert ECBClient(session=FakeSession([FakeResponse(404)])).search("FM", "x").empty


def test_cli_ecb_series(monkeypatch, capsys):
    import pandas as pd

    from market_monitor.cli import main

    frame = pd.DataFrame({"KEY": ["FM.D.FR.EUR.4F.BB.FR10YT_RR.YLD"], "TITLE": ["France 10Y"],
                          "TIME_PERIOD": ["2026-10-02"], "OBS_VALUE": [3.1]})
    monkeypatch.setattr(ECBClient, "search", lambda self, flow, pattern: frame)
    assert main(["--config", str(REPO_CONFIG), "ecb-series", "FM", "D.FR....YLD"]) == 0
    assert "ecb:FM/D.FR.EUR.4F.BB.FR10YT_RR.YLD  2026-10-02 = 3.1  France 10Y" in capsys.readouterr().out


def test_stooq_anti_bot_page_trips_the_breaker():
    from market_monitor.exceptions import ProviderUnavailableError

    page = '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body><noscript>T'
    with pytest.raises(ProviderUnavailableError, match="anti-bot"):
        parse_stooq_csv(page, "10fry.b")
    sources = {"stooq:": StooqClient(session=FakeSession([FakeResponse(200, text=page)]))}
    provider = FreeProvider(ECBClient(session=FakeSession([])), YahooClient(lambda *a: None),
                            sources=sources)
    result = provider.get_history(["stooq:2fry.b", "stooq:10fry.b"], START, END)
    assert len(sources["stooq:"]._session.calls) == 1  # second ticker skipped
    assert "skipped" in result.errors["stooq:10fry.b"]


CNBC = {"barData": {"priceBars": [
    {"tradeTime": "20260930000000", "tradeTimeinMills": "1790726400000", "close": "3.05"},
    {"tradeTime": "20261001000000", "close": "3.10"},
    {"tradeTimeinMills": "1791158400000", "close": "3.12"},   # 2026-10-05, outside the window
    {"tradeTime": "20261002000000", "close": "3.08"},
]}}


def test_cnbc_bars():
    from market_monitor.data.providers.public import CnbcClient, parse_cnbc_bars

    assert parse_cnbc_bars(CNBC).tolist() == [3.05, 3.10, 3.08, 3.12]
    session = FakeSession([FakeResponse(200, payload=CNBC)])
    assert CnbcClient(session=session).fetch("FR10Y-FR", START, END).tolist() == [3.10, 3.08]
    assert session.calls[0]["url"].endswith(
        "/FR10Y-FR/1D/20261001000000/20261002235959/adjusted/EST5EDT.json")
    with pytest.raises(DataProviderError, match="unexpected CNBC payload"):
        parse_cnbc_bars({"error": "x"})
    with pytest.raises(DataProviderError, match="no JSON"):
        CnbcClient(session=FakeSession([FakeResponse(200)])).fetch("X", START, END)


# ------------------------------------------------------------------- MSCI
MSCI_PAYLOAD = {"msci_index_code": "891800", "indexes": {"INDEX_LEVELS": [
    {"level_eod": 1735.1, "calc_date": 20260930},
    {"level_eod": 1739.4, "calc_date": 20261001},
    {"level_eod": 1742.92, "calc_date": 20261002},
]}}


def test_msci_client_parses_levels_and_params():
    from market_monitor.data.providers.public import MsciClient

    session = FakeSession([FakeResponse(200, payload=MSCI_PAYLOAD)])
    series = MsciClient(session=session).fetch("891800", START, END)
    assert series.tolist() == [1739.4, 1742.92]
    params = session.calls[0]["params"]
    assert params["index_codes"] == "891800" and params["index_variant"] == "STRD"
    assert params["currency_symbol"] == "USD" and params["start_date"] == "20261001"


def test_msci_key_variants_and_errors():
    from market_monitor.data.providers.public import parse_msci_key, parse_msci_levels

    assert parse_msci_key("891800") == ("891800", "STRD", "USD")
    assert parse_msci_key("990100/netr/eur") == ("990100", "NETR", "EUR")
    with pytest.raises(DataProviderError, match="invalid MSCI index code"):
        parse_msci_key("EM")
    with pytest.raises(DataProviderError, match="Index not found"):
        parse_msci_levels({"error_message": "Index not found"}, "1")
    with pytest.raises(DataProviderError, match="unexpected MSCI payload"):
        parse_msci_levels({"foo": 1}, "1")


def test_msci_http_errors():
    from market_monitor.data.providers.public import MsciClient

    assert MsciClient(session=FakeSession([FakeResponse(404)])).fetch("891800", START, END).empty
    with pytest.raises(DataProviderError, match="no JSON"):
        MsciClient(session=FakeSession([FakeResponse(200, text="<html>")])).fetch("891800", START, END)


def test_repository_msci_em_is_official_then_future_proxy():
    ref = load_referential(REPO_CONFIG.parent / "instruments.yaml", REPO_CONFIG.parent / "watchlists.yaml")
    spec = ref.get("MXEF").tickers["free"]
    assert spec.ticker.startswith("msci:891800|")
    assert spec.proxy_for("msci:891800") is None
    assert "MSCI EM" in (spec.proxy_for("MME=F") or "")


# ------------------------------------------------------------- CNBC refusals
def test_cnbc_retries_a_transient_refusal():
    from market_monitor.data.providers.public import CnbcClient

    session = FakeSession([FakeResponse(200, payload={"error": "busy"}), FakeResponse(200, payload=CNBC)])
    series = CnbcClient(session=session).fetch("FR10Y-FR", START, END)
    assert series.tolist() == [3.10, 3.08] and len(session.calls) == 2


def test_cnbc_reports_what_it_received_then_gives_up():
    from market_monitor.data.providers.public import CnbcClient
    from market_monitor.exceptions import ProviderUnavailableError

    client = CnbcClient(session=FakeSession([FakeResponse(200, payload={"error": "busy"})]))
    for symbol in ("FR5Y-FR", "IT2Y-IT"):
        with pytest.raises(DataProviderError, match=r"received: \['error'\]"):
            client.fetch(symbol, START, END)
    with pytest.raises(ProviderUnavailableError):  # third symbol in a row: CNBC set aside
        client.fetch("ES2Y-ES", START, END)
    assert len(client._session.calls) == 9  # 3 symbols x (1 call + 2 retries)


def test_cnbc_long_window_refused_is_fetched_in_pieces():
    from market_monitor.data.providers.public import CnbcClient

    refusal = {"status": "ERROR", "statusMessage": "503 Backend fetch failed"}
    bars = [{"close": "3.4857", "tradeTime": "20261009000000"}]
    session = FakeSession([FakeResponse(200, payload=refusal)] * 3
                          + [FakeResponse(200, payload={"barData": {"priceBars": []}})] * 3
                          + [FakeResponse(200, payload={"barData": {"priceBars": bars}})])
    series = CnbcClient(session=session).fetch("DE10Y-DE", date(2025, 9, 15), date(2026, 10, 12))
    assert series.tolist() == [3.4857]
    urls = [c["url"] for c in session.calls]
    assert len(urls) == 7 and "/20250915000000/20261012235959/" in urls[0]   # 3 tries, 4 pieces
    assert "/20250915000000/20260112235959/" in urls[3] and urls[-1].count("20261012235959") == 1


def test_euro_stoxx_50_is_not_served_by_fmp():
    ref = load_referential(REPO_CONFIG.parent / "instruments.yaml", REPO_CONFIG.parent / "watchlists.yaml")
    assert "fmp" not in ref.get("SX5E").tickers and ref.get("SX5E").tickers["free"].ticker == "^STOXX50E"
    assert "fmp" in ref.get("CAC").tickers  # CAC 40: FMP close matches the official one


def test_cnbc_error_shows_the_status_message():
    from market_monitor.data.providers.public import CnbcPayloadError, parse_cnbc_bars

    refusal = {"requestDetails": {}, "status": 400, "statusMessage": "Invalid date range"}
    with pytest.raises(CnbcPayloadError, match="status 400: 'Invalid date range'"):
        parse_cnbc_bars(refusal, "FR10Y-FR")


def test_bundesbank_xml_answer_is_read():
    xml = ('<?xml version="1.0"?><message:StructureSpecificData><DataSet><Series FREQ="D">'
           '<Obs TIME_PERIOD="2026-10-08" OBS_VALUE="3.496"/>'
           '<Obs TIME_PERIOD="2026-10-09" OBS_VALUE="3.486" OBS_STATUS="A"/>'
           '</Series></DataSet></message:StructureSpecificData>')
    assert parse_sdmx_csv(xml, "Bundesbank").tolist() == [3.496, 3.486]
    generic = ('<generic:Obs><generic:ObsDimension value="2026-10-09"/>'
               '<generic:ObsValue value="3.486"/></generic:Obs>')
    assert parse_sdmx_csv(generic).tolist() == [3.486]
    with pytest.raises(DataProviderError, match="without observations"):
        parse_sdmx_csv("<html><body>Error 500</body></html>", "Bundesbank")
    with pytest.raises(DataProviderError, match="received: 'A,B'"):
        parse_sdmx_csv("A,B\n1,2\n")


def test_rate_sources_report(monkeypatch):
    from market_monitor.data.providers.free import ECBClient
    from market_monitor.diagnostics import rate_sources_report

    csv = "KEY;TIME_PERIOD;OBS_VALUE\nX;2026-10-09;3.486\n"
    refusal = {"requestDetails": {}, "status": 429, "statusMessage": "Too many requests"}
    session = FakeSession([FakeResponse(200, text=csv), FakeResponse(200, text=csv),
                           FakeResponse(200, payload=refusal, text='{"status": 429}')])
    monkeypatch.setattr(ECBClient, "search", lambda self, flow, pattern: pd.DataFrame(
        {"KEY": ["FM.D.FR.EUR.4F.BB.FR10YT_RR.YLD"], "TIME_PERIOD": ["2026-10-09"], "OBS_VALUE": [4.86]}))
    lines = rate_sources_report(session, date(2026, 10, 12))
    text = "\n".join(lines)
    assert "== Bundesbank, Bund 10 ans" in text and "HTTP 200" in text and "lu : 09/10 = 3.486" in text
    assert "CNBC, FR10Y-FR" in text and "Too many requests" in text
    assert "FM.D.FR.EUR.4F.BB.FR10YT_RR.YLD  2026-10-09 = 4.86" in text
    assert "FM/D.DE+FR+IT+ES.....YLD" in text  # 7 dimensions: the 6-field pattern got HTTP 400


def test_cli_diagnostic_with_ssl_bypass(monkeypatch, capsys):
    from market_monitor import cli

    seen = {}

    def fake_report(session, today, ecb_url):
        seen["verify"] = session.verify
        return ["== ok"]

    monkeypatch.setattr(cli, "rate_sources_report", fake_report)
    assert cli.main(["--config", str(REPO_CONFIG), "diagnostic-taux", "--contourner-ssl"]) == 0
    out = capsys.readouterr().out
    assert seen["verify"] is False and "désactivée" in out and "== ok" in out



def test_cnbc_weekend_stamp_is_mondays_session():
    """Diagnostic of 12/10/2026: bars 20261009 (Fri close) and 20261010 (Mon live, stamped Sat)."""
    from market_monitor.data.providers.public import parse_cnbc_bars

    payload = {"barData": {"priceBars": [
        {"tradeTime": "20261002000000", "close": "3.4549"},
        {"tradeTime": "20261003000000", "close": "3.4553"},   # Monday 5 October
        {"tradeTime": "20261008000000", "close": "3.4939"},
        {"tradeTime": "20261009000000", "close": "3.4857"},   # Friday close = Investing 3,486
        {"tradeTime": "20261010000000", "close": "3.4654"},   # Monday 12 October, live
    ]}}
    series = parse_cnbc_bars(payload, "DE10Y-DE")
    assert series.index.strftime("%a %d/%m").tolist() == [
        "Fri 02/10", "Mon 05/10", "Thu 08/10", "Fri 09/10", "Mon 12/10"]
    assert series[pd.Timestamp("2026-10-09")] == 3.4857
    # a real Monday stamp next to a Saturday one: the real one wins
    both = {"barData": {"priceBars": [{"tradeTime": "20261010000000", "close": "1"},
                                      {"tradeTime": "20261012000000", "close": "2"}]}}
    assert parse_cnbc_bars(both).tolist() == [2.0]


def test_review_report_reproduces_the_review_load():
    from market_monitor.data.service import MarketDataService
    from market_monitor.diagnostics import review_report
    from market_monitor.monitor import MarketMonitor
    from tests.conftest import FakeProvider, bday_series

    ref = load_referential(REPO_CONFIG.parent / "instruments.yaml", REPO_CONFIG.parent / "watchlists.yaml")
    bund = ref.get("BUND_10Y").tickers["free"].ticker
    free = FakeProvider("free", {bund: bday_series("2025-06-01", "2026-10-09", 3.0)})
    monitor = MarketMonitor(MarketDataService([free]), ref)
    session = FakeSession([FakeResponse(200, text='{"status": 400, "statusMessage": "range too long"}')])
    text = "\n".join(review_report(monitor, date(2026, 10, 12), session))
    assert f"BUND_10Y : {bund}" in text and "OAT_10Y : cnbc:FR10Y-FR" in text
    assert "BUND_10Y : servi par" in text and "dernières valeurs : " in text
    assert "OAT_10Y" in text and "erreur" in text                # nothing serves the OAT here
    assert "sur 460 jours" in text and "range too long" in text
