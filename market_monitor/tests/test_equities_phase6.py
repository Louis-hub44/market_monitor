"""Phase 6 (v1.6): index compositions, members' prices loaded by batch, session calendars."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from market_monitor.cli import main
from market_monitor.config import EquitySettings
from market_monitor.equities import calendars
from market_monitor.equities import cli as equity_cli
from market_monitor.equities.constituents import (
    Composition,
    Compositions,
    Member,
    composition_from_dict,
    load_composition,
    save_composition,
)
from market_monitor.equities.loader import EquityLoader, yahoo_equity_fetcher
from market_monitor.equities.panel import EquityPanel
from market_monitor.equities.sources import (
    exchange_codes,
    from_holdings,
    marketcap_weights,
    parse_ishares_csv,
    refresh_composition,
    sector_fr,
    yahoo_symbol,
)
from market_monitor.equities.universe import EquityUniverse
from market_monitor.exceptions import DataProviderError, ProviderUnavailableError
from market_monitor.referential import ReferentialError

ROOT = Path(__file__).resolve().parents[1]
END = date(2026, 10, 26)

FIXTURES = Path(__file__).with_name("fixtures")
ISHARES_UK = (FIXTURES / "ishares_holdings_en.csv").read_text(encoding="utf-8")
ISHARES_DE = (FIXTURES / "ishares_holdings_de.csv").read_text(encoding="utf-8")


# ------------------------------------------------------------- compositions
def test_repository_compositions():
    comps = Compositions(ROOT / "config" / "constituents")
    assert sorted(c.index for c in comps) == ["CAC", "DAX", "FTSEMIB", "IBEX", "NDX", "SPX", "SX5E", "SXXP"]
    assert len(comps.get("CAC").members) == 40 and len(comps.get("IBEX").members) == 35
    assert comps.get("SXXP").group == "extended" and "SXXP" not in comps.indices(("core",))
    assert all(m.mic and m.currency == "EUR" for m in comps.get("SX5E").members)
    core = comps.tickers()
    assert len(core) == len(set(core)) and "MC.PA" in core
    assert comps.member_info()["SAP.DE"].sector == "Technologie"


def test_composition_roundtrip_and_validation(tmp_path):
    comp = Composition("CAC", "CAC 40", (Member("MC.PA", "LVMH", "Luxe", 11.2, mic="XPAR"),),
                       as_of=date(2026, 10, 20), weights="holdings", refresh={"kind": "manual"})
    path = save_composition(comp, tmp_path / "CAC.yaml")
    back = load_composition(path)
    assert back.members == comp.members and back.as_of == date(2026, 10, 20) and back.weighted
    assert back.member("MC.PA") is not None and back.member("X") is None
    bad = [{"name": "x"}, {"index": "X", "group": "other"}, {"index": "X", "weights": "free"},
           {"index": "X", "refresh": {"kind": "ftp"}}, {"index": "X", "members": [{"name": "no ticker"}]},
           {"index": "X", "members": [{"ticker": "A", "weight": -1}]},
           {"index": "X", "members": [{"ticker": "A"}, {"ticker": "A"}]}]
    for raw in bad:
        with pytest.raises(ReferentialError):
            composition_from_dict(raw)
    (tmp_path / "other.yaml").write_text("index: CAC\nname: dup\n", encoding="utf-8")
    with pytest.raises(ReferentialError, match="two composition files"):
        Compositions(tmp_path)


# ------------------------------------------------------------------ sources
def test_ishares_english_file():
    holdings = parse_ishares_csv(ISHARES_UK)
    assert holdings.as_of == date(2026, 10, 23)
    by_ticker = {m.ticker: m for m in holdings.members}
    assert set(by_ticker) == {"ASML.AS", "NOVO-B.CO", "RR.L", "MC.PA"}       # cash and unknown out
    assert by_ticker["ASML.AS"].weight == 2.85 and by_ticker["ASML.AS"].sector == "Technologie"
    assert by_ticker["NOVO-B.CO"].mic == "XCSE" and by_ticker["NOVO-B.CO"].currency == "DKK"
    assert by_ticker["MC.PA"].mic == "XPAR" and by_ticker["ASML.AS"].name == "Asml Holding NV"
    assert holdings.skipped == ("XYZ (Narnia Exchange)",)
    fixed = parse_ishares_csv(ISHARES_UK, {"XYZ": "XYZ.PA"})
    assert "XYZ.PA" in {m.ticker for m in fixed.members}


def test_ishares_german_file_and_errors():
    holdings = parse_ishares_csv(ISHARES_DE)
    assert holdings.as_of == date(2026, 10, 23)
    assert [(m.ticker, m.weight) for m in holdings.members] == [("SAP.DE", 12.34), ("SIE.DE", 9.10)]
    with pytest.raises(DataProviderError, match="not an iShares"):
        parse_ishares_csv("a,b\n1,2\n")


def test_symbols_exchanges_and_sectors():
    assert exchange_codes("Nyse Euronext - Euronext Paris") == (".PA", "XPAR")
    assert exchange_codes("New York Stock Exchange Inc.") == ("", "XNYS")
    assert exchange_codes("NASDAQ") == ("", "XNAS") and exchange_codes("Moon") is None
    assert yahoo_symbol("BRKB", "") == "BRK-B" and yahoo_symbol("BF.B", "") == "BF-B"
    assert yahoo_symbol("VOLV B", ".ST") == "VOLV-B.ST" and yahoo_symbol("BT.A", ".L") == "BT-A.L"
    assert sector_fr("Utilities") == "Services aux collectivités" and sector_fr("Luxe") == "Luxe"


def test_weights_from_market_caps():
    members = (Member("A", "A"), Member("B", "B"), Member("C", "C"))
    weighted = marketcap_weights(members, {"A": 300.0, "B": 100.0})
    assert [m.weight for m in weighted] == [75.0, 25.0, None]
    assert marketcap_weights(members, {}) == members


def test_refresh_kinds():
    base = Composition("SXXP", "Stoxx 600", (Member("ASML.AS", "ASML"), Member("OLD.PA", "Old")),
                       refresh={"kind": "ishares", "url": "https://x"}, weights="holdings")
    result = refresh_composition(base, fetch_text=lambda url: ISHARES_UK)
    assert result.added == ("MC.PA", "NOVO-B.CO", "RR.L") and result.removed == ("OLD.PA",)
    assert result.composition.as_of == date(2026, 10, 23) and "entrées" in result.summary()
    with pytest.raises(DataProviderError, match="no iShares URL"):
        refresh_composition(Composition("X", "X", (), refresh={"kind": "ishares"}))
    fmp = Composition("NDX", "Nasdaq 100", (), refresh={"kind": "fmp", "list": "nasdaq-constituent"})
    rows = [{"symbol": "AAPL", "name": "Apple", "sector": "Technology"}, {"symbol": "BRK.B", "name": "B"}]
    caps = {"AAPL": 3.0, "BRK-B": 1.0}
    out = refresh_composition(fmp, fmp_list=lambda path: rows, market_caps=lambda t: caps,
                              today=END).composition
    assert [(m.ticker, m.weight, m.sector) for m in out.members] == [("AAPL", 75.0, "Technologie"),
                                                                    ("BRK-B", 25.0, "")]
    with pytest.raises(DataProviderError, match="FMP unavailable"):
        refresh_composition(fmp)
    manual = Composition("CAC", "CAC", (Member("MC.PA", "LVMH"),), weights="marketcap")
    estimated = refresh_composition(manual, market_caps=lambda t: {"MC.PA": 1.0}, today=END)
    assert estimated.composition.as_of == END
    assert refresh_composition(manual).composition is manual
    hand = Composition("DAX", "DAX", (), refresh={"kind": "manual", "ishares_url": "https://y"})
    assert len(refresh_composition(hand, fetch_text=lambda url: ISHARES_DE).composition.members) == 2
    with pytest.raises(DataProviderError, match="no equity line"):
        from_holdings(base, parse_ishares_csv(ISHARES_DE.split("\"SAP")[0]), "x")


# -------------------------------------------------------------------- panel
def _bars(start: str, end: str, base: float = 100.0) -> pd.DataFrame:
    index = pd.bdate_range(start, end)
    close = base + np.arange(len(index), dtype="float64")
    return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "last": close,
                         "volume": 1000.0}, index=index)


def test_panel_roundtrip_and_replace(tmp_path):
    panel = EquityPanel(tmp_path)
    now = datetime(2026, 10, 27, 7, tzinfo=UTC)
    panel.update({"A": _bars("2026-10-01", "2026-10-09")}, {"A": (date(2026, 10, 1), date(2026, 10, 9))},
                 now=now)
    panel.update({"A": _bars("2026-10-08", "2026-10-16", 200.0)},
                 {"A": (date(2026, 10, 8), date(2026, 10, 16))}, splits={"A": {"2026-10-12": 2.0}}, now=now)
    fresh = EquityPanel(tmp_path)
    assert fresh.bars("A").loc["2026-10-08", "last"] == 200.0          # new download wins on overlap
    assert fresh.bars("A").index[0] == pd.Timestamp("2026-10-01")
    cov = fresh.coverage("A")
    assert (cov.start, cov.end, cov.splits) == (date(2026, 10, 1), date(2026, 10, 16), {"2026-10-12": 2.0})
    fresh.update({"A": _bars("2026-10-13", "2026-10-16", 5.0)},
                 {"A": (date(2026, 10, 13), date(2026, 10, 16))}, replace=["A"], now=now)
    assert EquityPanel(tmp_path).bars("A").index[0] == pd.Timestamp("2026-10-13")
    assert EquityPanel(tmp_path).bars("NONE").empty and fresh.tickers == ["A"]
    (tmp_path / "last.parquet").write_text("broken")
    assert EquityPanel(tmp_path).field("last").empty


# ------------------------------------------------------------------- loader
class FakeYahoo:
    def __init__(self, data: dict[str, pd.DataFrame], fail_at: int | None = None, mute: bool = False):
        self.data, self.calls, self.fail_at, self.mute = data, [], fail_at, mute

    def __call__(self, tickers, start, end):
        self.calls.append((tuple(tickers), start, end))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise ProviderUnavailableError("Yahoo refuse (limite de requêtes)")
        if self.mute:
            return {}
        return {t: self.data[t].loc[pd.Timestamp(start):pd.Timestamp(end)] for t in tickers if t in self.data}


def _loader(tmp_path, fake, **kwargs):
    sleeps = []
    clock = kwargs.pop("clock", lambda: datetime(2026, 10, 27, 7, tzinfo=UTC))
    return EquityLoader(EquityPanel(tmp_path), fake, chunk_size=2, pause_s=1.5, sleep=sleeps.append,
                        clock=clock, **kwargs), sleeps


def test_loader_first_load_then_incremental(tmp_path):
    data = {t: _bars("2025-06-02", "2026-10-26", 50.0 + k) for k, t in enumerate("ABCDE")}
    fake = FakeYahoo(data)
    loader, sleeps = _loader(tmp_path, fake)
    report = loader.refresh(list("ABCDE") + ["ZZZ"], END)
    assert report.calls == 3 and sleeps == [1.5, 1.5]                  # 6 symbols by 2
    assert sorted(report.updated) == list("ABCDE") and list(report.failed) == ["ZZZ"]
    assert fake.calls[0][1] == END - timedelta(days=400)
    again = _loader(tmp_path, fake, clock=lambda: datetime(2026, 10, 28, 7, tzinfo=UTC))[0]
    assert again.refresh(list("AB"), END).unchanged == ["A", "B"]      # downloaded after the close
    later = _loader(tmp_path, fake, clock=lambda: datetime(2026, 10, 29, 7, tzinfo=UTC))[0]
    later.refresh(["A"], date(2026, 10, 28))
    assert fake.calls[-1] == (("A",), date(2026, 10, 19), date(2026, 10, 28))   # last week only


def test_loader_reloads_a_restated_history_and_records_splits(tmp_path):
    data = {"S": _bars("2025-06-02", "2026-10-16", 100.0)}
    fake = FakeYahoo(data)
    loader, _ = _loader(tmp_path, fake)
    loader.refresh(["S"], date(2026, 10, 16))
    restated = data["S"].copy()
    restated[["open", "high", "low", "last"]] /= 2                       # 2-for-1 split on 19/10
    restated = pd.concat([restated, _bars("2026-10-19", "2026-10-26", restated["last"].iloc[-1])])
    restated["split"] = 0.0
    restated.loc["2026-10-19", "split"] = 2.0
    fake.data["S"] = restated
    loader = _loader(tmp_path, fake, clock=lambda: datetime(2026, 10, 27, 7, tzinfo=UTC))[0]
    report = loader.refresh(["S"], END)
    assert report.restated == ["S"] and report.splits == {"S": {"2026-10-19": 2.0}}
    bars = EquityPanel(tmp_path).bars("S")
    assert bars["last"].pct_change().abs().max() < 0.05                 # no fake -50 % move
    assert EquityPanel(tmp_path).coverage("S").splits == {"2026-10-19": 2.0}


def test_loader_stops_and_resumes(tmp_path):
    data = {t: _bars("2025-06-02", "2026-10-26") for t in "ABCDEF"}
    loader, _ = _loader(tmp_path, FakeYahoo(data, fail_at=2))
    report = loader.refresh(list("ABCDEF"), END)
    assert report.stopped and report.remaining == list("CDEF") and sorted(report.updated) == ["A", "B"]
    assert "relancer plus tard" in report.summary()
    resumed = FakeYahoo(data)
    _loader(tmp_path, resumed)[0].refresh(list("ABCDEF"), END)
    assert [c[0] for c in resumed.calls] == [("C", "D"), ("E", "F")]   # A, B already up to date
    mute, _ = _loader(tmp_path / "m", FakeYahoo(data, mute=True))
    report = mute.refresh(list("ABCDEF"), END)
    assert report.calls == 2 and report.remaining == ["E", "F"] and "source muette" in report.stopped


def test_yahoo_fetcher_reads_bars_and_splits():
    def download(tickers, start, end, session):
        idx = pd.to_datetime(["2026-10-23", "2026-10-26"])
        fields = ["Open", "High", "Low", "Close", "Volume", "Stock Splits"]
        cols = pd.MultiIndex.from_product([fields, tickers])
        rows = [[1, 2, 0.5, 1.5, 10, 0], [1, 2, 0.5, 1.6, 11, 2]]
        return pd.DataFrame([[v for v in row for _ in tickers] for row in rows], index=idx, columns=cols)
    out = yahoo_equity_fetcher(download=download)(["A", "B"], date(2026, 10, 23), END)
    assert set(out) == {"A", "B"} and out["A"]["split"].tolist() == [0.0, 2.0]

    def limited(*args):
        raise RuntimeError("Too Many Requests. Rate limited. Try after a while.")
    with pytest.raises(ProviderUnavailableError):
        yahoo_equity_fetcher(download=limited)(["A"], END, END)

    def broken(*args):
        raise RuntimeError("boom")
    with pytest.raises(DataProviderError):
        yahoo_equity_fetcher(download=broken)(["A"], END, END)


# ---------------------------------------------------------------- calendars
def test_session_calendars():
    paris = __import__("zoneinfo").ZoneInfo("Europe/Paris")
    monday_morning = datetime(2026, 10, 26, 8, 0, tzinfo=paris)
    assert calendars.last_closed_session("XPAR", monday_morning) == date(2026, 10, 23)
    assert calendars.last_closed_session("XPAR", datetime(2026, 10, 26, 18, 30, tzinfo=paris)) == END
    new_york_open = datetime(2026, 10, 26, 20, 0, tzinfo=paris)
    assert calendars.last_closed_session("XNYS", new_york_open) == date(2026, 10, 23)
    assert not calendars.is_session("XPAR", date(2026, 12, 25)) and calendars.is_session("XLON", END)
    assert calendars.previous_session("XETR", date(2026, 12, 28)) == date(2026, 12, 23)
    assert calendars.calendar("NOPE") is None
    sunday = datetime(2026, 10, 25, 9, tzinfo=paris)
    assert calendars.last_closed_session("NOPE", sunday) == date(2026, 10, 23)
    assert calendars.market_of("", "SEK") == "XSTO" and calendars.market_of("XMIL") == "XMIL"


# ------------------------------------------------------------- universe/CLI
def _universe(tmp_path, data=None) -> EquityUniverse:
    cons = tmp_path / "constituents"
    save_composition(Composition("SXXP", "Stoxx 600", (Member("ASML.AS", "ASML", currency="EUR"),
                                                       Member("RR.L", "Rolls", currency="GBP")),
                                 group="extended"), cons / "SXXP.yaml")
    save_composition(Composition("CAC", "CAC 40", (Member("MC.PA", "LVMH", currency="EUR"),),
                                 weights="marketcap", refresh={"kind": "manual"}), cons / "CAC.yaml")
    settings = EquitySettings(constituents_dir=cons, directory=tmp_path / "panel", chunk_size=10, pause_s=0)
    fake = FakeYahoo(data or {t: _bars("2025-06-02", "2026-10-26") for t in ("ASML.AS", "RR.L", "MC.PA",
                                                                               "GBPEUR=X")})
    return EquityUniverse.from_settings(settings, fetcher=fake)


def test_universe_symbols_fx_and_status(tmp_path):
    uni = _universe(tmp_path)
    assert uni.indices() == ["CAC"] and uni.indices(with_extended=True) == ["CAC", "SXXP"]
    assert uni.symbols(["SXXP"]) == ["ASML.AS", "RR.L", "GBPEUR=X"]
    report = uni.refresh_prices(with_extended=True, end=END)
    assert sorted(report.updated) == ["ASML.AS", "GBPEUR=X", "MC.PA", "RR.L"]
    status = uni.status().set_index("index")
    assert status.loc["SXXP", "loaded"] == 2 and status.loc["CAC", "last_close"] == END


def test_cli_constituents_and_equities(tmp_path, capsys, monkeypatch):
    uni = _universe(tmp_path)
    monkeypatch.setattr(equity_cli, "universe_factory", lambda settings: uni)
    monkeypatch.setattr(equity_cli, "yahoo_market_caps", lambda session: (lambda t: {"MC.PA": 5.0}))
    config = tmp_path / "config.yaml"
    config.write_text("providers:\n  priority: [free]\ncache:\n  directory: c\n", encoding="utf-8")
    base = ["--config", str(config)]
    assert main([*base, "constituents", "list"]) == 0
    assert "Stoxx 600" in capsys.readouterr().out
    assert main([*base, "constituents", "refresh", "--index", "CAC"]) == 0
    assert load_composition(tmp_path / "constituents" / "CAC.yaml").members[0].weight == 100.0
    holdings = tmp_path / "h.csv"
    holdings.write_text(ISHARES_UK, encoding="utf-8")
    assert main([*base, "constituents", "import", str(holdings), "--index", "SXXP"]) == 0
    assert "4 titres au 23/10/2026" in capsys.readouterr().out
    assert main([*base, "equities", "refresh", "--avec-stoxx600"]) == 0
    assert "mis à jour" in capsys.readouterr().out
    assert main([*base, "equities", "status"]) == 0
    assert "Avec cours" in capsys.readouterr().out
    assert main([*base, "constituents", "check", "--index", "SXXP"]) == 1   # NOVO-B.CO unknown to the fake
    assert "sans réponse : NOVO-B.CO" in capsys.readouterr().out
    assert main([*base, "constituents", "refresh", "--index", "NOPE"]) == 2
