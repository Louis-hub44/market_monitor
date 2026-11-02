"""Phase 9 (v1.6): daily macro « Valeurs marquantes » and print PNG of the curves."""

from pathlib import Path

import pytest
import yaml
from openpyxl import load_workbook

from market_monitor.data.service import MarketDataService
from market_monitor.export import build_daily_macro, export_daily_macro, render_text
from market_monitor.export.layout import CurvesChartRule, StocksRule, layout_from_dict, load_layout
from market_monitor.monitor import MarketMonitor
from market_monitor.referential import ReferentialError, load_referential
from tests.synthetic import synthetic_monitor
from tests.test_bars_history import BarsFake
from tests.test_movers import SESSION, demo_market

ROOT = Path(__file__).resolve().parents[1]
REPO_LAYOUT = yaml.safe_load((ROOT / "config" / "daily_macro.yaml").read_text(encoding="utf-8"))


def _layout(ref, **extra):
    raw = {"sections": [{"title": "Indices", "instruments": ["CAC"]}], "movers": {"universe": ["*"]},
           "format": {"thousands_separator": False, "compact_units": True}, **extra}
    return layout_from_dict(raw, ref)


# --------------------------------------------------------------------- layout
def test_layout_defaults_keep_the_export_unchanged():
    layout = load_layout(ROOT / "config" / "daily_macro.yaml",
                         load_referential(ROOT / "config" / "instruments.yaml",
                                          ROOT / "config" / "watchlists.yaml"))
    assert layout.stocks == StocksRule() and not layout.stocks.enabled
    assert layout.curves_chart == CurvesChartRule() and not layout.curves_chart.enabled
    assert layout.stocks.indices == ("CAC", "SX5E", "SPX") and layout.stocks.count == 3


@pytest.mark.parametrize("extra, message", [
    ({"stocks": {"enabled": True, "count": 0}}, "count"),
    ({"stocks": {"indices": []}}, "indices"),
    ({"stocks": {"colour": "red"}}, "unknown keys"),
    ({"curves_chart": {"comparisons": ["J-2"]}}, "comparison"),
    ({"curves_chart": "yes"}, "mapping"),
])
def test_layout_validation(extra, message, tmp_path):
    monitor, _ = demo_market(tmp_path)
    with pytest.raises(ReferentialError, match=message):
        _layout(monitor.referential, **extra)


# ----------------------------------------------------------- valeurs marquantes
def test_stock_sentences_in_text_and_excel(tmp_path):
    monitor, universe = demo_market(tmp_path)
    layout = _layout(monitor.referential, stocks={"enabled": True, "indices": ["CAC", "SX5E"]})
    data = build_daily_macro(monitor, layout, SESSION, equities=universe)
    assert [line.index for line in data.stock_lines] == ["CAC"]
    line = data.stock_lines[0]
    assert [m.name for m in line.risers] == ["Kering", "TotalEnergies", "BNP Paribas"]
    assert [m.name for m in line.fallers] == ["LVMH", "Société Générale", "Vinci"]
    text = render_text(data)
    assert "Valeurs marquantes\n- CAC 40 (+0,6%) : en tête Kering (+3,6%), TotalEnergies (+3,4%), " \
           "BNP Paribas (+3,0%) ; en queue LVMH (−3,0%), Société Générale (−1,7%), Vinci (−1,2%)." in text
    checks = text.split("À vérifier avant diffusion")[1]
    assert "pas de composition pour SX5E" in checks
    assert "FMP inactif" in checks                          # cited members not cross-checked
    assert "Worldline" in checks and "seuil" in checks       # excluded by the safeguards
    out = tmp_path / "out"
    result = export_daily_macro(monitor, layout, out, SESSION, equities=universe)
    sheet = load_workbook(result.excel)["Valeurs marquantes"]
    rows = list(sheet.iter_rows(min_row=2, max_row=7, values_only=True))
    assert rows[0][:4] == ("CAC 40", pytest.approx(line.change_pct / 100), "Hausse", "Kering")
    assert rows[3][2:4] == ("Baisse", "LVMH")
    assert rows[3][5] == pytest.approx(line.fallers[0].return_pct / 100)


def test_second_source_gap_replaces_the_member(tmp_path):
    monitor, universe = demo_market(tmp_path)
    data = {t: b.copy() for t, b in monitor.service.providers[0].data.items()}
    data["MC.PA"].iloc[-1, data["MC.PA"].columns.get_loc("last")] *= 1.03     # FMP disagrees on LVMH
    checked = MarketMonitor(MarketDataService([monitor.service.providers[0], BarsFake(data, name="fmp")]),
                            monitor.referential)
    layout = _layout(checked.referential, stocks={"enabled": True, "indices": ["CAC"]})
    result = build_daily_macro(checked, layout, SESSION, equities=universe)
    fallers = [m.name for m in result.stock_lines[0].fallers]
    assert "LVMH" not in fallers and fallers[0] == "Société Générale"
    text = render_text(result)
    assert "LVMH (MC.PA) : écart" in text and "FMP inactif" not in text


def test_without_members_the_section_says_so(tmp_path):
    monitor, _ = demo_market(tmp_path)
    layout = _layout(monitor.referential, stocks={"enabled": True})
    text = render_text(build_daily_macro(monitor, layout, SESSION, equities=None))
    assert "Valeurs marquantes\n- Cours des titres indisponibles." in text
    assert "couche actions non configurée" in text


# ------------------------------------------------------------------- curves PNG
@pytest.fixture(scope="module")
def full_monitor():
    ref = load_referential(ROOT / "config" / "instruments.yaml", ROOT / "config" / "watchlists.yaml")
    return synthetic_monitor(ref, end="2026-10-05")


def test_disabled_options_leave_the_export_identical(full_monitor, tmp_path):
    plain = layout_from_dict(REPO_LAYOUT, full_monitor.referential)
    explicit = layout_from_dict({**REPO_LAYOUT, "stocks": {"enabled": False},
                                 "curves_chart": {"enabled": False}}, full_monitor.referential)
    a = export_daily_macro(full_monitor, plain, tmp_path / "a", "2026-10-05")
    b = export_daily_macro(full_monitor, explicit, tmp_path / "b", "2026-10-05")
    assert a.text.read_text(encoding="utf-8") == b.text.read_text(encoding="utf-8")
    assert "Valeurs marquantes" not in a.text.read_text(encoding="utf-8")
    assert a.curves is None and b.curves is None
    assert "Valeurs marquantes" not in load_workbook(a.excel).sheetnames


def test_curves_png_is_written(full_monitor, tmp_path):
    layout = layout_from_dict({**REPO_LAYOUT, "curves_chart": {"enabled": True, "countries": ["DE", "IT"],
                                                               "comparisons": ["J-1", "M-1"]}},
                              full_monitor.referential)
    result = export_daily_macro(full_monitor, layout, tmp_path, "2026-10-05")
    assert result.curves is not None and result.curves.name == "daily_macro_2026-10-05_courbes.png"
    assert result.curves.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and result.curves.stat().st_size > 30_000
    nothing = layout_from_dict({**REPO_LAYOUT, "curves_chart": {"enabled": True, "countries": ["BR"]}},
                               full_monitor.referential)
    assert export_daily_macro(full_monitor, nothing, tmp_path / "x", "2026-10-05").curves is None
