"""PNG of the daily-macro bands, print-friendly (white background), via matplotlib.

Matplotlib (Agg canvas, no pyplot state) renders offline and deterministically - no
headless browser needed, which matters for an unattended 8 a.m. run.
"""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

import pandas as pd
from matplotlib import font_manager
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from market_monitor.export.builder import DailyMacroData
from market_monitor.export.text import data_sources
from market_monitor.text_format import (
    CHANGE_DECIMALS,
    HORIZON_LABELS,
    NNBSP,
    format_change,
    format_level_with_unit,
    french_date,
)

NAVY, MUTED, RULE, ACCENT = "#1B2835", "#6B7B8C", "#D5DCE3", "#F0A830"
UP, DOWN = "#1E7F6F", "#C4553F"
FONT_CANDIDATES = ("Arial", "Liberation Sans", "DejaVu Sans")
WIDTH_IN = 8.0          # fits an A4 page with margins
TITLE_IN, BAND_TITLE_IN, BAND_IN, FOOTER_IN = 0.42, 0.27, 0.8, 0.25


@lru_cache(maxsize=1)
def _font() -> str:
    """First installed candidate (Arial on Windows) without matplotlib fallback warnings."""
    installed = {f.name for f in font_manager.fontManager.ttflist}
    return next((f for f in FONT_CANDIDATES if f in installed), "DejaVu Sans")


def _t(text: str) -> str:
    """Narrow no-break space is missing from some fonts: use a regular no-break space."""
    return text.replace(NNBSP, "\u00a0")


def _colour(x: float, change_unit: str) -> str:
    """Sign colour of the change *as displayed* (a +0,04 pb move shown "+0,0" stays neutral)."""
    if pd.isna(x) or round(x, CHANGE_DECIMALS[change_unit]) == 0:
        return MUTED
    return UP if x > 0 else DOWN


def render_png(data: DailyMacroData, target: str | Path | BinaryIO, *, dpi: int = 200) -> None:
    """Title, one band of tiles per section, footer with sources and stale marks."""
    height = TITLE_IN + len(data.sections) * (BAND_TITLE_IN + BAND_IN) + FOOTER_IN
    fig = Figure(figsize=(WIDTH_IN, height), dpi=dpi, facecolor="white")
    FigureCanvasAgg(fig)
    fig.text(0.0, 1.0, _t(f"{data.title}, arrêté au {french_date(data.as_of)}"), ha="left",
             va="top", fontsize=12, fontweight="bold", color=NAVY, family=_font(),
             transform=fig.transFigure)
    top = 1.0 - TITLE_IN / height
    stale_seen = False
    for title, table in data.sections:
        stale_seen |= _band(fig, title, table, data.columns, top, height)
        top -= (BAND_TITLE_IN + BAND_IN) / height
    footer = f"Sources : {data_sources(data.universe)}."
    if stale_seen:
        footer += " * Dernière cotation antérieure de plus de deux jours ouvrés."
    fig.text(0.0, 0.0, _t(footer), ha="left", va="bottom", fontsize=6.5, color=MUTED, family=_font())
    fig.savefig(target, format="png", dpi=dpi, facecolor="white", bbox_inches="tight", pad_inches=0.15)


def png_bytes(data: DailyMacroData, *, dpi: int = 200) -> bytes:
    buffer = BytesIO()
    render_png(data, buffer, dpi=dpi)
    return buffer.getvalue()


def _band(fig: Figure, title: str, table: pd.DataFrame, columns: tuple[str, ...],
          top: float, height: float) -> bool:
    """Draw one band; returns True if a stale row was marked."""
    fig.text(0.0, top, _t(title), ha="left", va="top", fontsize=9.5, fontweight="bold",
             color=NAVY, family=_font())
    rule_y = top - 0.21 / height
    fig.add_artist(_hline(fig, rule_y, ACCENT, 1.2))
    n = max(len(table), 1)
    tile_top = rule_y - 0.08 / height
    stale_seen = False
    for k, (_, row) in enumerate(table.iterrows()):
        x = k / n
        name = row["name"] + (" *" if row["stale"] else "")
        stale_seen |= bool(row["stale"])
        lines = [
            (name, 7, "normal", MUTED),
            (format_level_with_unit(row["level"], row["asset_class"], row["quote"]), 10.5, "bold", NAVY),
            (format_change(row[columns[0]], row["change_unit"]), 9, "bold",
             _colour(row[columns[0]], row["change_unit"])),
        ]
        lines += [(f"{HORIZON_LABELS[c.removeprefix('chg_')]} {format_change(row[c], row['change_unit'])}",
                   7, "normal", MUTED) for c in columns[1:]]
        y = tile_top
        for text, size, weight, colour in lines:
            fig.text(x + 0.008, y, _t(text), ha="left", va="top", fontsize=size,
                     fontweight=weight, color=colour, family=_font())
            y -= (size / 72 + 0.06) / height
        if k:
            fig.add_artist(_vline(fig, x, tile_top + 0.02 / height, tile_top - (BAND_IN - 0.12) / height))
    return stale_seen


def _hline(fig: Figure, y: float, colour: str, width: float) -> Line2D:
    return Line2D([0, 1], [y, y], transform=fig.transFigure, color=colour, linewidth=width)


def _vline(fig: Figure, x: float, y0: float, y1: float) -> Line2D:
    return Line2D([x, x], [y0, y1], transform=fig.transFigure, color=RULE, linewidth=0.6)
