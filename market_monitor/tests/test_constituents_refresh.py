"""v1.6.1: compositions behind a corporate proxy - Wikipedia lists, market-cap coverage,
quiet yfinance, SSL diagnosis."""

import logging
from datetime import date

import pandas as pd
import pytest

from market_monitor.cli import main
from market_monitor.config import load_settings
from market_monitor.equities import cli as equity_cli
from market_monitor.equities import sources
from market_monitor.equities.constituents import Composition, Member, load_composition, save_composition
from market_monitor.equities.loader import yahoo_equity_fetcher
from market_monitor.equities.sources import (
    CAP_PROBE,
    parse_wikipedia_members,
    refresh_composition,
    weigh_by_market_cap,
)
from market_monitor.exceptions import DataProviderError, ProviderUnavailableError
from market_monitor.network import blocked_reason, captured_logs

TODAY = date(2026, 10, 27)
SSL_TEXT = ("Failed to perform, curl: (60) SSL certificate OpenSSL verify result: self signed "
            "certificate in certificate chain (19)")


def _wiki(header: list[str], rows: list[list[str]]) -> str:
    """A page shaped like Wikipedia's: a navigation table, then the sortable constituents table."""
    head = "".join(f"<th>{h}<sup class='reference'><a href='#n'>[1]</a></sup></th>" for h in header)
    body = "".join("<tr>" + "".join(f"<td><a href='/wiki/x'>{c}</a>\n</td>" for c in r) + "</tr>"
                   for r in rows)
    return ("<html><head><title>Nasdaq-100 - Wikipedia</title></head><body>"
            "<table class='infobox'><tr><th>Symbol</th><th>Name</th></tr>"
            "<tr><td>X</td><td>Y</td></tr></table>"
            f"<table class='wikitable sortable' id='constituents'><tbody><tr>{head}</tr>{body}"
            "</tbody></table></body></html>")


SPX_ROWS = [["BRK.B", "Berkshire Hathaway", "Financials", "Multi-Sector Holdings"],
            ["MMM", "3M", "Industrials", "Conglomerates"]] + [
    [f"T{k}", f"Company {k}", "Information Technology", "Software"] for k in range(60)]
NDX_ROWS = [["Apple Inc.", "AAPL", "Technology", "Hardware"]] + [
    [f"Company {k}", f"N{k}", "Telecommunications", "Telecom"] for k in range(60)]


def test_wikipedia_tables_both_layouts():
    spx = parse_wikipedia_members(_wiki(["Symbol", "Security", "GICS Sector", "GICS Sub-Industry"], SPX_ROWS))
    assert len(spx) == 62 and spx[0].ticker == "BRK-B" and spx[0].fmp == "BRK-B"
    assert spx[0].name == "Berkshire Hathaway" and spx[0].sector == "Finance"
    assert spx[2].sector == "Technologie" and spx[0].mic == "XNYS" and spx[0].currency == "USD"
    ndx = parse_wikipedia_members(_wiki(["Company", "Ticker", "ICB Industry", "ICB Subsector"], NDX_ROWS),
                                  "XNAS")
    assert ndx[0].ticker == "AAPL" and ndx[0].name == "Apple Inc." and ndx[0].sector == "Technologie"
    assert ndx[1].sector == "Communication" and ndx[0].mic == "XNAS"
    with pytest.raises(DataProviderError, match="constituents table"):
        parse_wikipedia_members("<html><table class='wikitable'><tr><th>A</th></tr></table></html>")


def _members(n: int) -> tuple[Member, ...]:
    return tuple(Member(f"T{k}.PA", f"T{k}", mic="XPAR", currency="EUR") for k in range(n))


def test_market_cap_coverage_gate():
    members = _members(10)
    caps = {f"T{k}.PA": 10.0 + k for k in range(7)}          # 7 / 10 < 80 %
    weighted, method, note = weigh_by_market_cap(members, lambda t: caps, "CAC 40")
    assert method == "none" and all(m.weight is None for m in weighted) and "7/10" in note
    caps.update({"T7.PA": 1.0, "T8.PA": 1.0})                # 9 / 10
    weighted, method, note = weigh_by_market_cap(members, lambda t: caps, "CAC 40")
    assert method == "marketcap" and note is None and weighted[9].weight is None
    assert sum(m.weight for m in weighted if m.weight) == pytest.approx(100.0)


def test_manual_index_is_not_rewritten_without_market_caps():
    comp = Composition("CAC", "CAC 40", _members(10), weights="marketcap", refresh={"kind": "manual"})
    with pytest.raises(DataProviderError, match="0/10"):
        refresh_composition(comp, market_caps=lambda t: {}, today=TODAY)


def test_ishares_failure_falls_back_to_wikipedia():
    comp = Composition("SPX", "S&P 500", (), currency="USD", weights="holdings",
                       refresh={"kind": "ishares", "url": "https://www.ishares.com/x.ajax?fileType=csv",
                                "fallback": "wikipedia", "wikipedia_url": "https://en.wikipedia.org/wiki/S"})
    page = _wiki(["Symbol", "Security", "GICS Sector"], [r[:3] for r in SPX_ROWS])

    def fetch(url: str) -> str:
        return "<html><title>Site entry</title></html>" if "ishares" in url else page
    result = refresh_composition(comp, fetch_text=fetch, market_caps=lambda t: {x: 1.0 for x in t},
                                 today=TODAY)
    assert len(result.composition.members) == 62 and result.composition.weights == "marketcap"
    assert result.composition.as_of == TODAY and "Wikipedia" in result.composition.source
    assert "source principale en échec" in result.summary() and "liste Wikipedia" in result.summary()
    # without market caps the list is kept, unweighted, and the summary says so
    bare = refresh_composition(comp, fetch_text=fetch, market_caps=lambda t: {}, today=TODAY)
    assert bare.composition.weights == "none" and "poids non calculés" in bare.summary()


def test_market_caps_stop_when_yahoo_is_blocked(monkeypatch):
    import yfinance

    calls = []

    class Blocked:
        def __init__(self, ticker, session=None):
            calls.append(ticker)
            logging.getLogger("yfinance").error("%s: Yahoo web request for share count failed", ticker)

        @property
        def fast_info(self):
            raise RuntimeError(SSL_TEXT)
    monkeypatch.setattr(yfinance, "Ticker", Blocked)
    with pytest.raises(ProviderUnavailableError, match="certificat SSL refusé"):
        sources.yahoo_market_caps()([f"T{k}" for k in range(40)])
    assert len(calls) == CAP_PROBE


def test_yfinance_logs_are_captured(caplog):
    with caplog.at_level(logging.WARNING), captured_logs("yfinance") as messages:
        logging.getLogger("yfinance").error("AC.PA: Yahoo web request for share count failed")
    assert messages == ["AC.PA: Yahoo web request for share count failed"]
    assert "share count" not in caplog.text
    logging.getLogger("yfinance").error("after")                    # handler removed afterwards
    assert messages == ["AC.PA: Yahoo web request for share count failed"]
    assert blocked_reason([SSL_TEXT]).startswith("certificat SSL refusé")
    assert blocked_reason(["HTTP 404"]) == "HTTP 404"


def test_price_download_behind_an_ssl_proxy():
    def download(tickers, start, end, session):
        logging.getLogger("yfinance").error("Failed download: %s", SSL_TEXT)
        return pd.DataFrame()
    with pytest.raises(ProviderUnavailableError, match="certificat SSL"):
        yahoo_equity_fetcher(download=download)(["MC.PA"], TODAY, TODAY)

    def raising(*_):
        raise RuntimeError(SSL_TEXT)
    with pytest.raises(ProviderUnavailableError, match="contourner-ssl"):
        yahoo_equity_fetcher(download=raising)(["MC.PA"], TODAY, TODAY)


class _Response:
    def __init__(self, text: str, status: int = 200, kind: str = "text/html; charset=utf-8"):
        self.text, self.status_code, self.encoding = text, status, "utf-8"
        self.content = text.encode()
        self.headers = {"Content-Type": kind}


def test_fetcher_passes_the_ishares_entry_page_and_keeps_a_wrong_answer(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("providers:\n  priority: [free]\n", encoding="utf-8")
    settings = load_settings(config)
    fetcher = equity_cli._Fetcher(settings)
    seen = []

    class Session:
        def get(self, url, headers=None, timeout=None):
            seen.append(url)
            return _Response("<html><head><title> Bienvenue sur iShares </title></head></html>")
    fetcher._session = Session()
    fetcher("https://www.ishares.com/us/products/1.ajax?fileType=csv")
    assert seen[-1].endswith("&siteEntryPassthrough=true")
    fetcher("https://en.wikipedia.org/wiki/Nasdaq-100")
    assert seen[-1] == "https://en.wikipedia.org/wiki/Nasdaq-100"
    text = fetcher.describe_last(tmp_path / "debug", "SPX")
    assert "text/html" in text and "« Bienvenue sur iShares »" in text
    assert (tmp_path / "debug" / "SPX_reponse.html").is_file()


def test_cli_refresh_reports_ssl_once_and_keeps_compositions(tmp_path, capsys, monkeypatch):
    folder = tmp_path / "constituents"
    folder.mkdir()
    for index in ("CAC", "DAX"):
        save_composition(Composition(index, index, _members(5), weights="marketcap",
                                     refresh={"kind": "manual"}), folder / f"{index}.yaml")
    config = tmp_path / "config.yaml"
    config.write_text("providers:\n  priority: [free]\ncache:\n  directory: c\n"
                      f"equities:\n  constituents_dir: {folder.as_posix()}\n"
                      f"  directory: {(tmp_path / 'eq').as_posix()}\n", encoding="utf-8")
    asked = []

    def caps(session):
        def fetch(tickers):
            asked.append(len(tickers))
            raise ProviderUnavailableError("Yahoo ne répond pas (certificat SSL refusé : proxy)")
        return fetch
    monkeypatch.setattr(equity_cli, "yahoo_market_caps", caps)
    assert main(["--config", str(config), "constituents", "refresh", "--contourner-ssl"]) == 1
    err = capsys.readouterr().err
    assert err.count("certificat SSL refusé") == 2 and "Composition inchangée" in err
    assert asked == [5]                                  # Yahoo asked once, not for every index
    assert load_composition(folder / "CAC.yaml").as_of is None   # nothing rewritten
