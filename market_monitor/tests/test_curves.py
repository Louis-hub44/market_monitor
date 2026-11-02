"""Phase 5 (v1.6): sovereign yield curves - J-1 / S-1 / M-1, slopes, spreads to the Bund."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from market_monitor.analytics.curves import CurveSet, build_curves, curve_ids
from market_monitor.analytics.performance import PerformanceEngine
from market_monitor.data.service import MarketDataService
from market_monitor.export.curves_chart import curves_png_bytes
from market_monitor.monitor import MarketMonitor
from market_monitor.referential import load_referential, referential_from_dicts
from market_monitor.ui.charts import curve_changes_figure, curves_today_figure, curves_vs_figure
from market_monitor.ui.curves_view import levels_table, slopes_table, spreads_table
from tests.conftest import FakeProvider

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 10, 5)


def _point(country: str, tenor: int) -> dict:
    return {"name": f"{country} {tenor}", "class": "rates", "curve": {"country": country, "tenor": tenor},
            "tickers": {"free": f"{country}{tenor}"}}


REF = referential_from_dicts(
    {"defaults": {"rates": {"quote": "yield", "unit": "%"}},
     "instruments": {f"{c}_{t}Y": _point(c, t) for c in ("DE", "FR", "US") for t in (2, 10)}},
    {"watchlists": {"all": {"instruments": ["*"]}}},
)
# closes: 05/09 (M-1), 25/09 (S-1 target 25/09), 01/10 (J-1), 02/10 (today = last close)
DATES = pd.to_datetime(["2026-09-02", "2026-09-25", "2026-10-01", "2026-10-02"])
LEVELS = pd.DataFrame({
    "DE_2Y": [2.00, 2.05, 2.10, 2.12], "DE_10Y": [2.60, 2.62, 2.70, 2.69],
    "FR_2Y": [2.20, 2.24, 2.30, 2.33], "FR_10Y": [3.30, 3.35, 3.40, 3.41],
    "US_2Y": [3.50, 3.52, 3.55, 3.51], "US_10Y": [4.10, 4.12, 4.15, 4.10],
}, index=DATES)


def _curves(levels: pd.DataFrame = LEVELS) -> CurveSet:
    insts = [REF.get(i) for i in levels.columns]
    table = PerformanceEngine().compute(levels, insts, AS_OF)
    table["origin"] = "cnbc:X"
    return build_curves(table, REF, AS_OF)


def test_points_slopes_and_spreads_hand_computed():
    curves = _curves()
    de2 = curves.points.loc["DE_2Y"]
    assert de2["level"] == 2.12 and de2["ref_J-1"] == 2.10
    assert de2["chg_J-1"] == pytest.approx(2.0) and de2["chg_S-1"] == pytest.approx(7.0)
    assert de2["chg_M-1"] == pytest.approx(12.0)
    slope = curves.slopes.set_index(["country", "slope"]).loc[("DE", "2s10s")]
    assert slope["level"] == pytest.approx(57.0)                 # 100 x (2,69 - 2,12)
    assert slope["chg_J-1"] == pytest.approx(57.0 - 60.0)         # 2s10s was 60 bp on J-1
    spread = curves.spreads.set_index(["country", "tenor"]).loc[("FR", 10.0)]
    assert spread["level"] == pytest.approx(72.0) and spread["chg_J-1"] == pytest.approx(2.0)
    assert ("DE", 2.0) not in curves.spreads.set_index(["country", "tenor"]).index
    assert curves.curve("FR").to_dict() == {2.0: 2.33, 10.0: 3.41}
    assert curves.curve("US", "S-1").to_dict() == {2.0: 3.52, 10.0: 4.12}
    assert curves.countries == ["DE", "FR", "US"] and not curves.notes


def test_notes_flag_mixed_dates_and_sources():
    levels = LEVELS.copy()
    levels.loc[pd.Timestamp("2026-10-02"), "FR_10Y"] = float("nan")   # FR 10 ans stuck on 01/10
    insts = [REF.get(i) for i in levels.columns]
    table = PerformanceEngine().compute(levels, insts, AS_OF)
    table["origin"] = ["cnbc:A", "bbk:B", "cnbc:C", "cnbc:D", "cnbc:E", "cnbc:F"]
    assert build_curves(table, REF, AS_OF).notes == [
        "Bund : sources mêlées (Bundesbank, CNBC), pentes à lire avec prudence",
        "OAT : points de dates différentes (10 ans au 01/10, autres au 02/10)",
    ]


def test_curve_ids_order_and_repository_points():
    assert curve_ids(REF, ["US", "DE"]) == ["US_2Y", "US_10Y", "DE_2Y", "DE_10Y"]
    repo = load_referential(ROOT / "config" / "instruments.yaml", ROOT / "config" / "watchlists.yaml")
    ids = curve_ids(repo)
    assert ids[:4] == ["BUND_2Y", "BUND_5Y", "BUND_10Y", "BUND_30Y"] and len(ids) == 22


def test_monitor_curves_end_to_end():
    data = {f"{c}{t}": LEVELS[f"{c}_{t}Y"] for c in ("DE", "FR", "US") for t in (2, 10)}
    monitor = MarketMonitor(MarketDataService([FakeProvider("free", data)]), REF)
    curves = monitor.curves(AS_OF, ["DE", "FR"])
    assert list(curves.points.index) == ["DE_2Y", "DE_10Y", "FR_2Y", "FR_10Y"]
    assert curves.slopes["country"].tolist() == ["DE", "FR"]
    empty = MarketMonitor(MarketDataService([FakeProvider("free", {})]), REF).curves(AS_OF, ["GB"])
    assert empty.points.empty and empty.slopes.empty and empty.countries == []


def test_figures_png_and_tables():
    curves = _curves()
    countries = ["DE", "FR", "US"]
    assert len(curves_today_figure(curves, countries).data) == 3
    vs = curves_vs_figure(curves, countries, ["J-1", "M-1"])
    assert len(vs.data) == 9
    assert len({round(a.x, 3) for a in vs.layout.annotations}) == 3     # one title per panel
    bars = curve_changes_figure(curves, countries, "S-1")
    assert [t.type for t in bars.data] == ["bar"] * 3 and bars.data[0].text[0] == "+7,0"
    assert curves_png_bytes(curves, countries, dpi=60)[:4] == b"\x89PNG"
    slopes = slopes_table(curves, countries)
    assert slopes.iloc[0].tolist() == ["Bund", "2s10s", "+57,0", "−3,0", "+0,0", "−3,0"]
    spreads = spreads_table(curves, countries, "J-1")
    assert spreads.set_index("Pays").at["OAT", "10 ans"] == "72,0 (+2,0)"
    levels = levels_table(curves, ["DE"], "J-1")
    assert levels.iloc[0]["2 ans"] == "2,120 % (+2,0)"


def test_streamlit_curves_view(tmp_path, monkeypatch):
    monkeypatch.setenv("MM_TEST_CACHE", str(tmp_path / "cache"))
    at = AppTest.from_file(str(Path(__file__).with_name("ui_instrument_app.py")), default_timeout=90)
    at.run()
    at.segmented_control(key="mm-view").set_value("curves").run()
    assert not at.exception, at.exception
    assert len(at.get("plotly_chart")) == 3 and len(at.get("download_button")) == 1
    text = " ".join(m.value for m in at.markdown)
    assert "Pentes (pb)" in text and "Spreads contre le Bund" in text
