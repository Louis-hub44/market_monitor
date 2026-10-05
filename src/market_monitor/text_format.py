"""French number and date formatting shared by the UI and the exports (pure)."""

from __future__ import annotations

import math
from datetime import date
from typing import Any

import pandas as pd

MISSING = "–"
NNBSP = "\u202f"  # narrow no-break space: French thousands separator
DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
          "septembre", "octobre", "novembre", "décembre"]
ASSET_CLASS_LABELS = {
    "equity": "Actions", "rates": "Taux", "credit": "Crédit", "fx": "Change",
    "commodities": "Matières premières", "volatility": "Volatilité", "crypto": "Crypto",
}
SOURCE_LABELS = {"bloomberg": "BBG", "fmp": "FMP", "free": "Libre", "derived": "Calcul"}
CHANGE_UNIT_LABELS = {"pct": "%", "bp": "pb", "abs": "pts"}
CHANGE_DECIMALS = {"pct": 2, "bp": 1, "abs": 2}
HORIZON_LABELS = {"1d": "1J", "1w": "1S", "mtd": "MTD", "ytd": "YTD"}
CHANGE_COLUMNS = ["chg_1d", "chg_1w", "chg_mtd", "chg_ytd"]
Z_FOR_CHANGE = {"chg_1d": "z_1d", "chg_1w": "z_1w"}


def _is_missing(x: Any) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x)) or x is pd.NaT


def fr_number(x: float | None, decimals: int, *, sign: bool = False) -> str:
    """``1234.5`` -> ``1 234,50`` (narrow no-break space, decimal comma)."""
    if x is None or _is_missing(x):
        return MISSING
    value = round(float(x), decimals) or 0.0  # no "−0,0"
    text = f"{value:{'+' if sign else ''},.{decimals}f}"
    text = text.replace(",", NNBSP).replace(".", ",")
    return text.replace("-", "−")  # typographic minus


def french_date(d: date, *, weekday: bool = True) -> str:
    """``date(2026, 10, 5)`` -> ``lundi 5 octobre 2026``."""
    core = f"{d.day} {MONTHS[d.month - 1]} {d.year}"
    return f"{DAYS[d.weekday()]} {core}" if weekday else core


def short_date(value: Any) -> str:
    """Timestamp -> ``02/10`` (``MISSING`` when absent)."""
    return MISSING if _is_missing(value) else pd.Timestamp(value).strftime("%d/%m")


def level_decimals(asset_class: str, quote: str, level: float) -> int:
    """Market-conventional precision for a level."""
    if quote == "yield":
        return 3
    if quote == "spread":
        return 1
    if asset_class == "fx":
        return 4 if abs(level) < 20 else 2
    if asset_class == "crypto":
        return 0
    return 2


def format_level(row: pd.Series) -> str:
    level = row["level"]
    if _is_missing(level):
        return MISSING
    return fr_number(level, level_decimals(row["asset_class"], row["quote"], level))


def format_change(x: float | None, change_unit: str, *, with_unit: bool = True) -> str:
    """``(5.0, "bp")`` -> ``+5,0 pb``."""
    if _is_missing(x):
        return MISSING
    text = fr_number(x, CHANGE_DECIMALS[change_unit], sign=True)
    return f"{text}{NNBSP}{CHANGE_UNIT_LABELS[change_unit]}" if with_unit else text


def format_source(source: Any, proxy: Any) -> str:
    if _is_missing(source):
        return MISSING
    label = SOURCE_LABELS.get(str(source), str(source))
    return f"{label} (proxy)" if not _is_missing(proxy) else label


def format_level_with_unit(level: float | None, asset_class: str, quote: str) -> str:
    """Level with its unit when it carries meaning: ``2,700 %``, ``72,1 pb``."""
    if level is None or _is_missing(level):
        return MISSING
    text = fr_number(level, level_decimals(asset_class, quote, level))
    suffix = {"yield": "%", "spread": "pb"}.get(quote)
    return f"{text}{NNBSP}{suffix}" if suffix else text
