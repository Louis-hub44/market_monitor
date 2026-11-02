"""Phase 8 (v1.6): « La séance d'hier », the default view."""

import math
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from market_monitor.analytics.session_review import (
    INDEX_BAND,
    IndexLine,
    StockMove,
    build_review,
    curve_moves,
    index_line,
    movers_sentence,
)
from market_monitor.config import load_settings
from market_monitor.equities.movers import compute_movers
from market_monitor.exceptions import ConfigError
from market_monitor.referential import load_referential
from tests.synthetic import synthetic_monitor
from tests.test_movers import COMP, SESSION, _levels, _market

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 10, 5)


@pytest.fixture(scope="module")
def market():
    ref = load_referential(ROOT / "config" / "instruments.yaml", ROOT / "config" / "watchlists.yaml")
    monitor = synthetic_monitor(ref, end="2026-10-05")
    return monitor, monitor.performance("home", AS_OF), monitor.curves(AS_OF)


def test_review_blocks_from_the_performance_table(market):
    monitor, report, curves = market
    review = build_review(report.table, AS_OF, curves=curves)
    assert review.session == AS_OF
    assert list(review.indices.index) == [i for i in INDEX_BAND if i in report.table.index]
    best, worst = review.sectors_best, review.sectors_worst
    assert len(best) == len(worst) == 3
    sectors = report.table[report.table["group"] == "Secteurs Stoxx 600"]["chg_1d"]
    assert best["change"].iloc[0] == pytest.approx(sectors.max())
    assert worst["change"].iloc[0] == pytest.approx(sectors.min())
    assert set(review.cross_asset) == {"Change", "Matières premières", "Volatilité"}
    assert "EURUSD" in review.cross_asset["Change"].index and "VIX" in review.cross_asset["Volatilité"].index
    z = review.zscores["z"].abs().to_numpy()
    assert len(z) == 6 and all(z[:-1] >= z[1:])
    # no members loaded: the block says so, the rest is there
    assert review.stock_lines == [] and any("equities refresh" in n for n in review.notes)


def test_curve_moves_match_the_curve_set(market):
    _, _, curves = market
    moves = curve_moves(curves).set_index("country")
    assert {"DE", "FR", "IT", "ES", "US"} <= set(moves.index)
    bund = curves.points[(curves.points["country"] == "DE") & (curves.points["tenor"] == 10.0)].iloc[0]
    assert moves.at["DE", "level"] == pytest.approx(bund["level"])
    assert moves.at["DE", "chg"] == pytest.approx(bund["chg_J-1"])
    assert math.isnan(moves.at["DE", "spread"])               # the Bund has no spread to itself
    btp = curves.spreads[(curves.spreads["country"] == "IT") & (curves.spreads["tenor"] == 10.0)].iloc[0]
    assert moves.at["IT", "spread"] == pytest.approx(btp["level"])
    assert curve_moves(None).empty


def test_review_degrades_without_inputs():
    empty = build_review(pd.DataFrame(), AS_OF)
    assert empty.session is None and empty.indices.empty and empty.zscores.empty
    assert any("courbes" in n for n in empty.notes) and any("secteurs" in n for n in empty.notes)


def test_index_lines_and_sentence(tmp_path):
    movers = compute_movers(COMP, _market(tmp_path), SESSION, index_levels=_levels())
    line = index_line(movers, 3)
    assert [m.ticker for m in line.risers] == ["A.PA", "C.PA"]
    assert [m.ticker for m in line.fallers] == ["B.PA"]
    assert line.checks == 1 and line.counted == 3                 # Echo (-50 %) stays out
    review = build_review(pd.DataFrame(), SESSION, movers={"CAC": movers})
    assert [x.index for x in review.stock_lines] == ["CAC"]
    text = movers_sentence(line)
    assert text == ("CAC test (+0,6 %) : en tête Alpha (+2,0 %), Charlie (+1,0 %) ; "
                    "en queue Bravo (−2,0 %).").replace(" %", " %")


def test_sentence_without_moves():
    line = IndexLine("X", "Indice X", math.nan, None, (), ())
    assert movers_sentence(line) == "Indice X : aucun titre comptable."
    line = IndexLine("X", "Indice X", 1.0, None, (StockMove("A", "Alpha", 4.0),), ())
    assert movers_sentence(line).startswith("Indice X (+1,0")


def test_default_view_setting(tmp_path):
    assert load_settings(ROOT / "config" / "config.yaml").ui.default_view == "seance"
    bad = tmp_path / "config.yaml"
    bad.write_text("ui:\n  default_view: accueil\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="default_view"):
        load_settings(bad)


# ------------------------------------------------------------------------ UI
def test_streamlit_session_view_with_members(tmp_path, monkeypatch):
    monkeypatch.setenv("MM_TEST_DIR", str(tmp_path))
    monkeypatch.setenv("MM_TEST_VIEW", "seance")
    at = AppTest.from_file(str(Path(__file__).with_name("ui_movers_app.py")), default_timeout=90)
    at.session_state["mm-asof"] = date(2026, 10, 26)   # the demo's last session (not today)
    at.run()
    assert not at.exception, at.exception
    assert at.segmented_control(key="mm-view").value == "seance"
    text = " ".join(m.value for m in at.markdown)
    assert "La séance du lundi 26 octobre 2026" in text
    assert "Valeurs marquantes par indice" in text and "CAC 40" in text and "▲" in text and "▼" in text
    assert "1 titre(s) à vérifier" in text                          # Worldline, out of the ranking
    assert any("Secteurs absents" in c.value for c in at.caption)  # degrades block by block
    # a z-score row opens the instrument page
    assert at.dataframe
    at.session_state["mm-view"] = "overview"
    at.run()
    assert not at.exception, at.exception


def test_main_app_opens_on_the_session(tmp_path, monkeypatch):
    at = AppTest.from_file(str(Path(__file__).with_name("ui_main_app.py")), default_timeout=90)
    at.run()
    at.segmented_control(key="mm-net-mode").set_value("insecure").run()
    at.button(key="mm-net-start").click().run()
    assert not at.exception, at.exception
    assert at.segmented_control(key="mm-view").value == "seance"
    text = " ".join(m.value for m in at.markdown)
    assert "La séance du" in text and "Taux souverains (10 ans)" in text and "Secteurs Stoxx 600" in text
    assert "Plus forts z-scores" in text
