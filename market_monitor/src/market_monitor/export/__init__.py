"""Daily-macro export: Excel (formulas, ready to paste), PNG bands and plain text."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from market_monitor.alerts import AlertRule
from market_monitor.analytics.curves import curve_ids
from market_monitor.data.models import DateLike
from market_monitor.equities.universe import EquityUniverse
from market_monitor.export.builder import DailyMacroData, build_daily_macro, select_movers
from market_monitor.export.charts import build_charts, render_charts_png
from market_monitor.export.curves_chart import curves_png_bytes
from market_monitor.export.excel import excel_bytes, write_excel
from market_monitor.export.image import png_bytes, render_png
from market_monitor.export.layout import DailyMacroLayout, load_layout
from market_monitor.export.text import render_text
from market_monitor.monitor import MarketMonitor


@dataclass
class ExportResult:
    excel: Path
    png: Path
    text: Path
    data: DailyMacroData
    charts: Path | None = None  # ``<prefix>_<date>_graphiques.png`` when the layout has charts
    curves: Path | None = None  # ``<prefix>_<date>_courbes.png`` when ``curves_chart`` is enabled


def export_daily_macro(monitor: MarketMonitor, layout: DailyMacroLayout, out_dir: str | Path,
                       as_of: DateLike | None = None, rules: Sequence[AlertRule] = (), *,
                       equities: EquityUniverse | None = None) -> ExportResult:
    """Build once, write ``<prefix>_<date>.xlsx / .png / .txt`` (+ ``_graphiques.png``,
    ``_courbes.png``) into ``out_dir``.

    Alerts (if ``rules``) go to the text and the Excel only - never to the public PNG.
    ``equities`` (index members) feeds « Valeurs marquantes » when the layout enables it.
    """
    data = build_daily_macro(monitor, layout, as_of, rules, equities=equities)
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / f"{layout.file_prefix}_{data.as_of.isoformat()}"
    paths = {ext: stem.with_suffix(f".{ext}") for ext in ("xlsx", "png", "txt")}
    write_excel(data, paths["xlsx"])
    render_png(data, paths["png"])
    paths["txt"].write_text(render_text(data), encoding="utf-8")
    charts = None
    if layout.charts:
        charts = folder / f"{stem.name}_graphiques.png"
        render_charts_png(build_charts(monitor, layout.charts, data.as_of, base=data.levels,
                                       provisional=data.provisional), charts)
    curves = None
    if layout.curves_chart.enabled:
        png = curves_chart_png(monitor, layout, data)
        if png is not None:
            curves = folder / f"{stem.name}_courbes.png"
            curves.write_bytes(png)
    return ExportResult(paths["xlsx"], paths["png"], paths["txt"], data, charts, curves)


def curves_chart_png(monitor: MarketMonitor, layout: DailyMacroLayout, data: DailyMacroData) -> bytes | None:
    """Print PNG of the curves configured in ``curves_chart`` (same closes rule as the bands).

    ``None`` when none of the countries has a curve point (the export goes on without it).
    """
    rule = layout.curves_chart
    now = monitor.data_time()
    ids = curve_ids(monitor.referential, rule.countries)
    provisional = (layout.closing.open_sessions(monitor.referential, ids, now) if data.as_of >= now.date()
                   else set())
    curves = monitor.curves(data.as_of, rule.countries, provisional=provisional)
    shown = [c for c in rule.countries if not curves.curve(c).dropna().empty]
    if not shown:
        return None
    return curves_png_bytes(curves, shown, comparisons=rule.comparisons, changes_since=rule.changes_since)


__all__ = [
    "DailyMacroData", "DailyMacroLayout", "ExportResult", "build_daily_macro", "curves_chart_png",
    "excel_bytes",
    "export_daily_macro", "load_layout", "png_bytes", "render_png", "render_text",
    "select_movers", "write_excel",
]
