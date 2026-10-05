"""Daily-macro export: Excel (formulas, ready to paste), PNG bands and plain text."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from market_monitor.alerts import AlertRule
from market_monitor.data.models import DateLike
from market_monitor.export.builder import DailyMacroData, build_daily_macro, select_movers
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


def export_daily_macro(monitor: MarketMonitor, layout: DailyMacroLayout, out_dir: str | Path,
                       as_of: DateLike | None = None, rules: Sequence[AlertRule] = ()) -> ExportResult:
    """Build once, write ``<prefix>_<date>.xlsx / .png / .txt`` into ``out_dir``.

    Alerts (if ``rules``) go to the text and the Excel only - never to the public PNG.
    """
    data = build_daily_macro(monitor, layout, as_of, rules)
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / f"{layout.file_prefix}_{data.as_of.isoformat()}"
    paths = {ext: stem.with_suffix(f".{ext}") for ext in ("xlsx", "png", "txt")}
    write_excel(data, paths["xlsx"])
    render_png(data, paths["png"])
    paths["txt"].write_text(render_text(data), encoding="utf-8")
    return ExportResult(paths["xlsx"], paths["png"], paths["txt"], data)


__all__ = [
    "DailyMacroData", "DailyMacroLayout", "ExportResult", "build_daily_macro", "excel_bytes",
    "export_daily_macro", "load_layout", "png_bytes", "render_png", "render_text",
    "select_movers", "write_excel",
]
