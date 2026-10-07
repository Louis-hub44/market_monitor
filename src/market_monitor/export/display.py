"""Display conventions of the published bands (labels, decimals, units).

The defaults reproduce the house style (``1 234,56``, ``2,700 %``, ``+5,0 pb``); the
``format:`` section of ``daily_macro.yaml`` and per-line options adapt it to the review
format, e.g. ``6272,95`` / ``5,27%`` / ``−5bps`` / ``100,58 $``:

    format: {thousands_separator: false, compact_units: true, bp_unit: bps}
    sections:
      - title: Taux 10 ans
        decimals: 2           # niveaux
        change_decimals: 0    # variations
        instruments:
          - {id: UST_10Y, label: États-Unis}
          - {id: VIX, change: pct}   # VIX en % plutôt qu'en points
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pandas as pd

from market_monitor.text_format import (
    CHANGE_DECIMALS,
    CHANGE_UNIT_LABELS,
    MISSING,
    NNBSP,
    fr_number,
    level_decimals,
)

CHANGE_UNITS = ("pct", "bp", "abs")


@dataclass(frozen=True)
class NumberStyle:
    """How numbers and units are written."""

    thousands_separator: bool = True
    compact_units: bool = False  # "5,27%" / "−5bps" instead of "5,27 %" / "−5 pb"
    bp_unit: str = "pb"          # "bps" -> "bp" when |change| <= 1

    def number(self, x: float, decimals: int, *, sign: bool = False) -> str:
        text = fr_number(x, decimals, sign=sign)
        return text if self.thousands_separator else text.replace(NNBSP, "")

    def join(self, number: str, unit: str) -> str:
        if not unit:
            return number
        return f"{number}{unit}" if self.compact_units else f"{number}{NNBSP}{unit}"

    def unit_label(self, change_unit: str, value: float | None = None) -> str:
        if change_unit != "bp":
            return CHANGE_UNIT_LABELS[change_unit]
        plural = self.bp_unit
        if value is not None and plural.endswith("s") and abs(value) <= 1:
            return plural[:-1]
        return plural


DEFAULT_STYLE = NumberStyle()


@dataclass(frozen=True)
class LineFormat:
    """Per-line overrides; ``None`` keeps the instrument convention."""

    label: str | None = None
    decimals: int | None = None          # level decimals
    suffix: str | None = None            # replaces the level unit, e.g. " $"
    change_unit: str | None = None       # "pct" to show a vol index move in %
    change_decimals: int | None = None


DEFAULT_LINE = LineFormat()


def _missing(x: Any) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x)) or x is pd.NaT


def level_unit(quote: str, style: NumberStyle) -> str:
    return {"yield": "%", "spread": style.bp_unit}.get(quote, "")


def format_level(row: pd.Series, line: LineFormat = DEFAULT_LINE,
                 style: NumberStyle = DEFAULT_STYLE) -> str:
    level = row["level"]
    if _missing(level):
        return MISSING
    decimals = level_decimals(row["asset_class"], row["quote"], level) if line.decimals is None \
        else line.decimals
    text = style.number(level, decimals)
    if line.suffix is not None:
        return f"{text}{line.suffix}"
    return style.join(text, level_unit(row["quote"], style))


def change_decimals(change_unit: str, line: LineFormat = DEFAULT_LINE) -> int:
    return CHANGE_DECIMALS[change_unit] if line.change_decimals is None else line.change_decimals


def display_change(row: pd.Series, column: str, line: LineFormat = DEFAULT_LINE) -> float:
    """Change of ``column`` in the displayed unit (recomputed in % when overridden)."""
    if line.change_unit is None or line.change_unit == row["change_unit"]:
        return float(row[column]) if not _missing(row[column]) else math.nan
    raw_base: Any = row.get(f"ref_{column.removeprefix('chg_')}")
    raw_level: Any = row["level"]
    if _missing(raw_base) or _missing(raw_level):
        return math.nan
    base, level = float(raw_base), float(raw_level)
    if line.change_unit == "pct":
        return 100.0 * (level / base - 1.0) if base > 0 else math.nan
    if line.change_unit == "bp":
        return (level - base) * float(row.get("bp_factor", 100.0))
    return level - base


def format_change(x: float | None, change_unit: str, decimals: int | None = None,
                  style: NumberStyle = DEFAULT_STYLE) -> str:
    if _missing(x):
        return MISSING
    assert x is not None
    shown = CHANGE_DECIMALS[change_unit] if decimals is None else decimals
    text = style.number(x, shown, sign=True)
    return style.join(text, style.unit_label(change_unit, round(x, shown)))


def _spaced(unit: str, style: NumberStyle) -> str:
    """Literal suffix (Excel number formats): ``" %"`` or ``"%"`` in compact style."""
    return "" if not unit else (unit if style.compact_units else f" {unit}")


def decorate(table: pd.DataFrame, lines: tuple[LineFormat, ...], columns: tuple[str, ...],
             style: NumberStyle) -> pd.DataFrame:
    """Add the display columns used by every renderer to a band.

    ``label``, ``level_text``, ``display_unit``, ``level_decimals``, ``level_suffix``,
    ``change_decimals`` and, per column ``c``: ``disp_c`` (value) and ``text_c``.
    """
    out = table.copy()
    formats = [lines[k] if k < len(lines) else DEFAULT_LINE for k in range(len(out))]
    out["label"] = [f.label or name for f, name in zip(formats, out["name"], strict=True)]
    out["level_text"] = [format_level(r, f, style) for f, (_, r) in zip(formats, out.iterrows(), strict=True)]
    out["display_unit"] = [f.change_unit or u for f, u in zip(formats, out["change_unit"], strict=True)]
    out["level_decimals"] = [
        f.decimals if f.decimals is not None
        else (level_decimals(r["asset_class"], r["quote"], r["level"]) if not _missing(r["level"]) else 2)
        for f, (_, r) in zip(formats, out.iterrows(), strict=True)]
    out["level_suffix"] = [f.suffix if f.suffix is not None else _spaced(level_unit(q, style), style)
                           for f, q in zip(formats, out["quote"], strict=True)]
    out["change_decimals"] = [change_decimals(u, f) for f, u
                              in zip(formats, out["display_unit"], strict=True)]
    for column in columns:
        values = [display_change(r, column, f) for f, (_, r) in zip(formats, out.iterrows(), strict=True)]
        out[f"disp_{column}"] = values
        out[f"text_{column}"] = [format_change(v, u, d, style) for v, u, d in
                                 zip(values, out["display_unit"], out["change_decimals"], strict=True)]
    return out
