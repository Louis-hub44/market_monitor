"""Phase 7 (v1.6): top movers, contributions in points, breadth, sectors, safeguards."""

import math
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from market_monitor.config import EquitySettings
from market_monitor.data.service import MarketDataService
from market_monitor.equities.constituents import Composition, Compositions, Member, save_composition
from market_monitor.equities.instruments import is_stock_id, stock_id, stock_instrument, stock_ticker
from market_monitor.equities.movers import (
    STATUS_CHECK,
    STATUS_CLOSED,
    STATUS_NOT_QUOTED,
    STATUS_OK,
    compute_movers,
    index_session,
    verify_members,
)
from market_monitor.equities.panel import EquityPanel
from market_monitor.equities.universe import FX_TO_EUR, EquityUniverse
from market_monitor.monitor import MarketMonitor
from market_monitor.referential import referential_from_dicts
from tests.test_bars_history import BarsFake

SESSION = date(2026, 10, 26)        # Monday
PREVIOUS = date(2026, 10, 23)       # Friday
DAYS = pd.bdate_range("2025-06-02", SESSION, name="date")


def _frame(close: np.ndarray, volume: float | np.ndarray = 1000.0, index=DAYS) -> pd.DataFrame:
    close = np.asarray(close, dtype="float64")
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "last": close,
                         "volume": volume}, index=index)


def _path(flat: float, last: float | None, n: int = len(DAYS)) -> np.ndarray:
    values = np.full(n, flat)
    if last is not None:
        values[-1] = last
    return values


def _panel(tmp_path, bars: dict[str, pd.DataFrame], splits=None) -> EquityPanel:
    panel = EquityPanel(tmp_path / "panel")
    windows = {t: (b.index[0].date(), b.index[-1].date()) for t, b in bars.items()}
    panel.update(bars, windows, splits=splits or {}, now=datetime(2026, 10, 26, 22, 30, tzinfo=UTC))
    return panel


MEMBERS = (
    Member("A.PA", "Alpha", "Industrie", 50.0, mic="XPAR", currency="EUR"),
    Member("B.PA", "Bravo", "Finance", 30.0, mic="XPAR", currency="EUR"),
    Member("C.PA", "Charlie", "Finance", 20.0, mic="XPAR", currency="EUR"),
    Member("D.PA", "Delta", "Énergie", None, mic="XPAR", currency="EUR"),
    Member("E.PA", "Echo", "Énergie", None, mic="XPAR", currency="EUR"),
)
COMP = Composition("CAC", "CAC test", MEMBERS, as_of=PREVIOUS, weights="holdings")


def _market(tmp_path) -> EquityPanel:
    volume_a = np.full(len(DAYS), 1000.0)
    volume_a[-1] = 3000.0
    bars = {
        "A.PA": _frame(_path(100.0, 102.0), volume_a),     # +2 %, volume x3, new high
        "B.PA": _frame(_path(50.0, 49.0)),                 # -2 %, new low
        "C.PA": _frame(_path(10.0, 10.1)),                 # +1 %
        "D.PA": _frame(_path(20.0, None)).iloc[:-1],       # no print on the session
        "E.PA": _frame(_path(40.0, 20.0)),                 # -50 %: split-like
    }
    return _panel(tmp_path, bars)


def _levels() -> pd.Series:
    return pd.Series([1000.0, 1006.0], index=pd.DatetimeIndex([PREVIOUS, SESSION]))


# -------------------------------------------------------------------- engine
def test_movers_returns_contributions_and_residual(tmp_path):
    movers = compute_movers(COMP, _market(tmp_path), SESSION, index_levels=_levels())
    assert (movers.session, movers.previous) == (SESSION, PREVIOUS)
    m = movers.members
    assert m.at["A.PA", "return_pct"] == pytest.approx(2.0)
    assert m.at["B.PA", "return_pct"] == pytest.approx(-2.0)
    # drifted weights (flat prices since the composition date: unchanged), points = w r I_prev
    assert m.at["A.PA", "contribution"] == pytest.approx(0.5 * 0.02 * 1000)
    assert m.at["B.PA", "contribution"] == pytest.approx(0.3 * -0.02 * 1000)
    assert m.at["C.PA", "contribution"] == pytest.approx(0.2 * 0.01 * 1000)
    assert m.at["A.PA", "contribution_pct"] == pytest.approx(1.0)
    assert movers.contributions_total == pytest.approx(6.0)
    assert movers.index_change_pts == pytest.approx(6.0) and movers.residual == pytest.approx(0.0)
    assert movers.index_change_pct == pytest.approx(0.6)
    assert not any("poids à rafraîchir" in n for n in movers.notes)
    assert list(movers.top(2)["ticker"]) == ["A.PA", "C.PA"]
    assert list(movers.flop(1)["ticker"]) == ["B.PA"]
    assert list(movers.contributors(5)["ticker"]) == ["B.PA", "C.PA", "A.PA"]


def test_movers_flags_not_quoted_and_suspect_moves(tmp_path):
    movers = compute_movers(COMP, _market(tmp_path), SESSION, index_levels=_levels())
    m = movers.members
    assert m.at["D.PA", "status"] == STATUS_NOT_QUOTED and "23/10" in m.at["D.PA", "reason"]
    assert m.at["E.PA", "status"] == STATUS_CHECK
    assert "seuil" in m.at["E.PA", "reason"] and "split" in m.at["E.PA", "reason"]
    assert "E.PA" not in set(movers.flop(10)["ticker"]) and "E.PA" not in set(movers.top(10)["ticker"])
    assert any("Echo" in c for c in movers.checks)
    assert any("non coté" in n and "Delta" in n for n in movers.notes)
    b = movers.breadth()
    assert (b["counted"], b["members"], b["advancers"], b["decliners"]) == (3, 5, 2, 1)


def test_breadth_volume_and_sectors(tmp_path):
    movers = compute_movers(COMP, _market(tmp_path), SESSION, index_levels=_levels())
    m = movers.members
    assert m.at["A.PA", "volume_ratio"] == pytest.approx(3.0)
    assert m.at["A.PA", "new_high"] is True and m.at["B.PA", "new_low"] is True
    assert m.at["A.PA", "above_ma50"] is True and m.at["B.PA", "above_ma200"] is False
    b = movers.breadth()
    assert b["new_highs"] == 2 and b["new_lows"] == 1          # A and C up from a flat year
    assert b["above_ma50"] == pytest.approx(200 / 3)
    sectors = movers.sectors().set_index("sector")
    assert sectors.at["Finance", "members"] == 2
    assert sectors.at["Finance", "average_pct"] == pytest.approx(-0.5)
    assert sectors.at["Finance", "weighted_pct"] == pytest.approx((30 * -2 + 20 * 1) / 50)
    assert sectors.at["Finance", "contribution"] == pytest.approx(-4.0)
    assert list(movers.sectors()["sector"]) == ["Industrie", "Finance"]


def test_threshold_is_configurable_and_weights_drift(tmp_path):
    bars = {"A.PA": _frame(np.r_[np.full(len(DAYS) - 1, 100.0), 112.0]),
            "B.PA": _frame(np.linspace(50.0, 100.0, len(DAYS)))}     # doubled since the composition
    comp = Composition("CAC", "x", MEMBERS[:2], as_of=date(2025, 6, 2), weights="holdings")
    panel = _panel(tmp_path, bars)
    loose = compute_movers(comp, panel, SESSION, max_abs_return_pct=25.0)
    strict = compute_movers(comp, panel, SESSION, max_abs_return_pct=10.0)
    assert loose.members.at["A.PA", "status"] == STATUS_OK
    assert strict.members.at["A.PA", "status"] == STATUS_CHECK
    prev_b = float(bars["B.PA"]["last"].iloc[-2])
    drift_b = 30.0 * prev_b / 50.0
    assert loose.members.at["B.PA", "weight"] == pytest.approx(100 * drift_b / (drift_b + 50.0))
    assert math.isnan(loose.members.at["A.PA", "contribution"])      # no index level: no points
    assert loose.members.at["A.PA", "contribution_pct"] == pytest.approx(12.0 * 50 / (50 + drift_b))


def test_split_reported_by_yahoo_goes_to_check(tmp_path):
    bars = {"A.PA": _frame(_path(100.0, 104.0)), "B.PA": _frame(_path(50.0, 51.0))}
    panel = _panel(tmp_path, bars, splits={"A.PA": {"2026-10-26": 2.0}})
    movers = compute_movers(Composition("CAC", "x", MEMBERS[:2]), panel, SESSION)
    assert movers.members.at["A.PA", "status"] == STATUS_CHECK
    assert "split signalé" in movers.members.at["A.PA", "reason"]
    assert "pas de poids : contributions non calculées" in movers.notes


def test_local_holiday_and_fx_conversion(tmp_path):
    holiday = date(2026, 8, 31)        # UK summer bank holiday, Paris open
    days = pd.bdate_range("2025-06-02", holiday, name="date")
    london = _frame(np.full(len(days) - 1, 500.0), index=days[:-1])
    rate = np.full(len(days), 1.15)
    rate[-1] = 1.15 * 1.01
    bars = {"A.PA": _frame(_path(100.0, 101.0, len(days)), index=days),
            "B.PA": _frame(_path(100.0, 99.0, len(days)), index=days),
            "V.L": london,
            "S.ST": _frame(_path(100.0, 101.0, len(days)), index=days),
            "SEKEUR=X": _frame(rate, 0.0, index=days)}
    members = (MEMBERS[0], MEMBERS[1], Member("V.L", "Victor", "Santé", 5.0, mic="XLON", currency="GBp"),
               Member("S.ST", "Sierra", "Industrie", 5.0, mic="XSTO", currency="SEK"))
    comp = Composition("SXXP", "Stoxx test", members, group="extended", weights="holdings")
    movers = compute_movers(comp, _panel(tmp_path, bars), holiday, fx_to_eur=FX_TO_EUR)
    assert movers.session == holiday
    assert movers.members.at["V.L", "status"] == STATUS_CLOSED
    assert movers.members.at["S.ST", "return_pct"] == pytest.approx(100 * (1.01 * 1.01 - 1))
    assert any("place fermée" in n for n in movers.notes)


def test_session_follows_the_index_market():
    paris = ZoneInfo("Europe/Paris")
    cac = Composition("CAC", "CAC 40", MEMBERS)
    spx = Composition("SPX", "S&P 500", (Member("AAPL", "Apple", mic="XNAS", currency="USD"),),
                      currency="USD")
    assert index_session(cac, date(2026, 10, 25)) == PREVIOUS                      # Sunday
    morning = datetime(2026, 10, 26, 10, 0, tzinfo=paris)
    assert index_session(cac, SESSION, morning) == PREVIOUS
    assert index_session(spx, SESSION, datetime(2026, 10, 26, 18, 0, tzinfo=paris)) == PREVIOUS
    assert index_session(spx, SESSION, datetime(2026, 10, 26, 23, 0, tzinfo=paris)) == SESSION
    assert index_session(cac, date(2026, 12, 25)) == date(2026, 12, 24)            # Christmas


def test_stale_panel_and_empty_compositions(tmp_path):
    panel = _market(tmp_path)
    later = compute_movers(COMP, panel, date(2026, 10, 28))
    assert later.session == SESSION and any("actualiser" in n for n in later.notes)
    empty = compute_movers(Composition("NDX", "Nasdaq 100", ()), panel, SESSION)
    assert empty.session is None and "constituents refresh" in empty.notes[0]
    unknown = compute_movers(Composition("X", "x", (Member("Z.PA", "Zulu"),)), panel, SESSION)
    assert unknown.members.empty and "actualiser" in unknown.notes[0]


def test_bad_weights_are_reported(tmp_path):
    levels = pd.Series([1000.0, 1030.0], index=pd.DatetimeIndex([PREVIOUS, SESSION]))
    movers = compute_movers(COMP, _market(tmp_path), SESSION, index_levels=levels)
    assert movers.residual == pytest.approx(24.0)
    assert any("poids à rafraîchir" in n for n in movers.notes)


def test_second_source_moves_a_gap_to_check(tmp_path):
    movers = compute_movers(COMP, _market(tmp_path), SESSION, index_levels=_levels())
    index = pd.DatetimeIndex([PREVIOUS, SESSION])
    second = {"A.PA": pd.DataFrame({"last": [100.0, 102.05]}, index=index),
              "B.PA": pd.DataFrame({"last": [50.0, 52.0]}, index=index)}
    result = verify_members(movers, ["A.PA", "B.PA", "C.PA"], lambda t, s, e: second)
    assert result["A.PA"] == "ok" and result["C.PA"] == "indisponible"
    assert result["B.PA"].startswith("écart")
    assert movers.members.at["B.PA", "status"] == STATUS_CHECK
    assert "B.PA" not in set(movers.flop(10)["ticker"])

    def down(*_):
        raise RuntimeError("FMP down")
    assert set(verify_members(movers, ["A.PA"], down).values()) == {"indisponible"}


# ------------------------------------------------------- stocks as instruments
def test_stock_instruments_extend_a_monitor():
    member = Member("MC.PA", "LVMH", "Luxe", mic="XPAR", currency="EUR", fmp="MC.PA")
    inst = stock_instrument(member)
    assert inst.id == stock_id("MC.PA") == "EQ:MC.PA" and is_stock_id(inst.id)
    assert stock_ticker(inst.id) == "MC.PA" and set(inst.tickers) == {"free", "fmp"}
    ref = referential_from_dicts({"instruments": {"SPX": {"name": "S&P", "class": "equity",
                                                          "quote": "price", "tickers": {"free": "SPX"}}}},
                                 {"watchlists": {"all": {"instruments": ["*"]}}})
    monitor = MarketMonitor(MarketDataService([BarsFake({}, name="free")]), ref)
    twin = monitor.with_instruments([inst])
    assert "EQ:MC.PA" in twin.referential and "EQ:MC.PA" not in monitor.referential
    assert twin is monitor.with_instruments([inst]) and monitor.with_instruments([]) is monitor
    assert monitor.select("all") == ("SPX",)                  # the overview keeps its lines
    assert twin.is_available("EQ:MC.PA")


def test_universe_movers_uses_the_index_level(tmp_path):
    comp_dir = tmp_path / "constituents"
    comp_dir.mkdir()
    save_composition(COMP, comp_dir / "CAC.yaml")
    settings = EquitySettings(constituents_dir=comp_dir, directory=tmp_path / "panel")
    _market(tmp_path)
    levels = pd.DataFrame({"last": [1000.0, 1006.0]}, index=pd.DatetimeIndex([PREVIOUS, SESSION]))
    ref = referential_from_dicts({"instruments": {"CAC": {"name": "CAC 40", "class": "equity",
                                                          "quote": "price", "tickers": {"free": "^FCHI"}}}},
                                 {})
    monitor = MarketMonitor(MarketDataService([BarsFake({"^FCHI": levels}, name="free")]), ref)
    uni = EquityUniverse(settings, Compositions(comp_dir), EquityPanel(settings.directory),
                         lambda *_: {})
    movers = uni.movers("CAC", monitor, date(2026, 10, 25))
    assert movers.session == PREVIOUS                                       # Sunday -> Friday
    movers = uni.movers("CAC", monitor, SESSION)
    assert movers.session == SESSION and movers.index_change_pts == pytest.approx(6.0)
    assert movers.residual == pytest.approx(0.0)
    movers = uni.movers("CAC", None, SESSION)
    assert math.isnan(movers.index_change_pts)
    assert {i.id for i in uni.instruments(["CAC"])} == {f"EQ:{m.ticker}" for m in MEMBERS}


# ------------------------------------------------------------------- the view
DEMO_MEMBERS = (
    ("MC.PA", "LVMH", "Consommation discrétionnaire", 12.0), ("TTE.PA", "TotalEnergies", "Énergie", 9.0),
    ("SAN.PA", "Sanofi", "Santé", 8.0), ("AIR.PA", "Airbus", "Industrie", 8.0),
    ("SU.PA", "Schneider Electric", "Industrie", 7.5), ("OR.PA", "L'Oréal", "Consommation de base", 7.0),
    ("BNP.PA", "BNP Paribas", "Finance", 6.0), ("CS.PA", "AXA", "Finance", 5.5),
    ("AI.PA", "Air Liquide", "Matériaux", 5.5), ("SAF.PA", "Safran", "Industrie", 5.0),
    ("DG.PA", "Vinci", "Industrie", 4.5), ("GLE.PA", "Société Générale", "Finance", 3.0),
    ("KER.PA", "Kering", "Consommation discrétionnaire", 2.0), ("STLAP.PA", "Stellantis", "Industrie", 3.5),
    ("CAP.PA", "Capgemini", "Technologie", 2.0), ("ORA.PA", "Orange", "Télécoms", 3.0),
    ("ENGI.PA", "Engie", "Services aux collectivités", 3.0),
    ("RI.PA", "Pernod Ricard", "Consommation de base", 2.0),
    ("VIE.PA", "Veolia", "Services aux collectivités", 2.0), ("ACA.PA", "Crédit Agricole", "Finance", 2.5),
    ("URW.PA", "Unibail", "Immobilier", 1.0), ("WLN.PA", "Worldline", "Technologie", 0.5),
)


def demo_market(directory) -> tuple[MarketMonitor, EquityUniverse]:
    """A CAC-like index of 22 members over 18 months (one bad print, one member not quoted)."""
    from pathlib import Path
    root = Path(directory)
    rng = np.random.default_rng(11)
    members = tuple(Member(t, n, s, w, mic="XPAR", currency="EUR", fmp=t) for t, n, s, w in DEMO_MEMBERS)
    comp_dir = root / "constituents"
    comp_dir.mkdir(parents=True, exist_ok=True)
    save_composition(Composition("CAC", "CAC 40", members, as_of=date(2026, 9, 30), weights="holdings",
                                 source="test"), comp_dir / "CAC.yaml")
    total = sum(w for *_, w in DEMO_MEMBERS)
    bars, index_returns = {}, np.zeros(len(DAYS))
    for k, (ticker, _, _, weight) in enumerate(DEMO_MEMBERS):
        returns = rng.normal(0.0003, 0.015, len(DAYS))
        returns[-1] = rng.normal(0, 0.02)
        close = (40 + 20 * k) * np.exp(np.cumsum(returns))
        volume = rng.uniform(0.8e6, 1.2e6, len(DAYS))
        volume[-1] *= 1 + 3 * abs(returns[-1]) / 0.02
        bars[ticker] = _frame(close, volume)
        index_returns += weight / total * returns
    bad = np.array([0.5, 0.5, 0.5, 0.5, 1.0])
    bars["WLN.PA"].iloc[-1, :] = bars["WLN.PA"].iloc[-2, :] * bad        # bad print
    bars["URW.PA"] = bars["URW.PA"].iloc[:-1]                           # not quoted
    panel_dir = root / "panel"
    _panel_at(panel_dir, bars)
    level = 7500 * np.exp(np.cumsum(index_returns))
    fake = BarsFake({"^FCHI": _frame(level, 0.0), **{t: b for t, b in bars.items()}}, name="free")
    ref = referential_from_dicts(
        {"instruments": {"CAC": {"name": "CAC 40", "class": "equity", "quote": "price",
                                 "tickers": {"free": "^FCHI"}}}},
        {"watchlists": {"all": {"instruments": ["*"]}}})
    settings = EquitySettings(constituents_dir=comp_dir, directory=panel_dir)
    universe = EquityUniverse(settings, Compositions(comp_dir), EquityPanel(panel_dir), lambda *_: {})
    return MarketMonitor(MarketDataService([fake]), ref), universe


def _panel_at(directory, bars) -> None:
    panel = EquityPanel(directory)
    windows = {t: (b.index[0].date(), b.index[-1].date()) for t, b in bars.items()}
    panel.update(bars, windows, now=datetime(2026, 10, 26, 22, 30, tzinfo=UTC))


def test_demo_market_movers(tmp_path):
    monitor, universe = demo_market(tmp_path)
    movers = universe.movers("CAC", monitor, SESSION)
    assert movers.session == SESSION and len(movers.counted) == 20
    assert movers.members.at["WLN.PA", "status"] == STATUS_CHECK
    assert movers.members.at["URW.PA", "status"] == STATUS_NOT_QUOTED
    assert abs(movers.residual) < 0.2 * abs(movers.index_change_pts) + 1.0


def _movers_app(tmp_path, monkeypatch):
    from pathlib import Path

    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("MM_TEST_DIR", str(tmp_path))
    return AppTest.from_file(str(Path(__file__).with_name("ui_movers_app.py")), default_timeout=90)


def test_streamlit_movers_view_and_stock_page(tmp_path, monkeypatch):
    at = _movers_app(tmp_path, monkeypatch)
    at.run()
    assert not at.exception, at.exception
    at.segmented_control(key="mm-view").set_value("movers").run()
    assert not at.exception, at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Plus fortes hausses" in text and "Largeur de marché" in text and "Worldline" in text
    assert len(at.dataframe) == 2 and len(at.dataframe[0].value) == 10
    assert len(at.get("plotly_chart")) == 2
    assert any("FMP non actif" in c.value for c in at.caption)          # no FMP: Yahoo only, flagged
    assert any("Worldline" in m.value and "seuil" in m.value for m in at.markdown)
    # a click on the first riser opens the member's page (EQ: instrument, Yahoo bars)
    first = at.dataframe[0].value.iloc[0]["Titre"]
    ticker = next(t for t, n, *_ in DEMO_MEMBERS if n == first)
    at.session_state["mm-view"] = "instrument"
    at.session_state["mm-fiche-id"] = f"EQ:{ticker}"
    at.run()
    assert not at.exception, at.exception
    assert at.selectbox(key="mm-fiche-id").value == f"EQ:{ticker}"
    assert any(first in m.value for m in at.markdown)


def test_streamlit_query_param_opens_a_stock(tmp_path, monkeypatch):
    at = _movers_app(tmp_path, monkeypatch)
    at.query_params["fiche"] = "EQ:MC.PA"
    at.run()
    assert not at.exception, at.exception
    assert at.selectbox(key="mm-fiche-id").value == "EQ:MC.PA"


def test_streamlit_without_equity_layer(tmp_path, monkeypatch):
    from pathlib import Path

    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("MM_TEST_CACHE", str(tmp_path / "cache"))
    at = AppTest.from_file(str(Path(__file__).with_name("ui_instrument_app.py")), default_timeout=90)
    at.run()
    at.segmented_control(key="mm-view").set_value("movers").run()
    assert not at.exception, at.exception
    assert any("Couche actions non configurée" in i.value for i in at.info)


def test_streamlit_second_source_check(tmp_path, monkeypatch):
    monkeypatch.setenv("MM_TEST_FMP", "1")
    at = _movers_app(tmp_path, monkeypatch)
    at.run()
    at.segmented_control(key="mm-view").set_value("movers").run()
    shown = {*at.dataframe[0].value["Titre"], *at.dataframe[1].value["Titre"]}
    button = next(b for b in at.button if b.label.startswith("Contrôler les"))
    button.click().run()
    assert not at.exception, at.exception
    assert any("Contrôle 2e source (FMP)" in c.value for c in at.caption)
    if "LVMH" in shown:  # the 3 % gap moves LVMH out of the movers, into "À vérifier"
        assert any("LVMH" in m.value and "2e source" in m.value for m in at.markdown)
        assert "LVMH" not in {*at.dataframe[0].value["Titre"], *at.dataframe[1].value["Titre"]}


def test_streamlit_refresh_button_reports_the_load(tmp_path, monkeypatch):
    at = _movers_app(tmp_path, monkeypatch)
    at.run()
    at.segmented_control(key="mm-view").set_value("movers").run()
    at.button(key="mm-movers-refresh").click().run()
    assert not at.exception, at.exception
    # the demo fetcher returns nothing: the source is reported silent, the view still renders
    assert any("Cours des titres" in w.value and "arrêt" in w.value for w in at.warning)
    assert len(at.dataframe) == 2
