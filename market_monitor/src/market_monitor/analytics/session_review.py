"""« La séance d'hier » : the previous session in one screen (engine side, no Streamlit).

Assembles what a morning reader wants first, from results computed elsewhere:

* the performance table (closes only, cross-checked movers): index band, Stoxx 600
  sectors, FX / commodities / volatility, largest z-scores;
* the yield curves: 10-year yields, 2s10s slopes and spreads to the Bund, J-1 changes;
* the index movers (members): three risers and three fallers per index.

Every block is optional: a missing input leaves its block empty with a note, so the view
still renders without index members, curves or a full watchlist.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from market_monitor.analytics.curves import SHORT_LABELS, CurveSet
from market_monitor.analytics.performance import Horizon, rank_movers
from market_monitor.equities.movers import IndexMovers

#: index band, display order (ids of the referential)
INDEX_BAND = ("SX5E", "SXXP", "CAC", "DAX", "FTSEMIB", "IBEX", "SPX", "NDX", "NKY")
#: indices whose members are shown (three up / three down), display order
MOVERS_INDICES = ("SX5E", "CAC", "DAX", "FTSEMIB", "IBEX", "SPX", "NDX")
SECTOR_GROUP = "Secteurs Stoxx 600"
CROSS_ASSET_CLASSES = {"fx": "Change", "commodities": "Matières premières", "volatility": "Volatilité"}
CURVE_TENOR = 10.0
ROW_COLUMNS = ["id", "name", "asset_class", "level", "change", "change_unit", "z", "unit", "quote", "stale",
               "check"]


@dataclass(frozen=True)
class StockMove:
    ticker: str
    name: str
    return_pct: float


@dataclass(frozen=True)
class IndexLine:
    """One index with its three risers and three fallers."""

    index: str
    name: str
    change_pct: float
    session: date | None
    risers: tuple[StockMove, ...]
    fallers: tuple[StockMove, ...]
    counted: int = 0
    checks: int = 0


@dataclass
class SessionReview:
    as_of: date
    session: date | None
    indices: pd.DataFrame
    sectors_best: pd.DataFrame
    sectors_worst: pd.DataFrame
    cross_asset: dict[str, pd.DataFrame]
    zscores: pd.DataFrame
    curves: pd.DataFrame
    stock_lines: list[IndexLine] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def build_review(
    table: pd.DataFrame,
    as_of: date,
    *,
    curves: CurveSet | None = None,
    movers: Mapping[str, IndexMovers] | None = None,
    top: int = 3,
    zscores: int = 6,
    band: Sequence[str] = INDEX_BAND,
) -> SessionReview:
    rows = _rows(table)
    indices = rows.loc[[i for i in band if i in rows.index]]
    sectors = rows[table["group"] == SECTOR_GROUP] if "group" in table else rows.iloc[:0]
    sectors = sectors[~sectors["stale"].astype(bool)].dropna(subset=["change"])
    sectors = sectors.sort_values("change", ascending=False)
    cross = {label: rows[rows["asset_class"] == cls] for cls, label in CROSS_ASSET_CLASSES.items()}
    ranked = rank_movers(table, Horizon.D1, zscores) if not table.empty else rows
    review = SessionReview(
        as_of=as_of, session=_session(table, band), indices=indices,
        sectors_best=sectors.head(top), sectors_worst=sectors.tail(top).iloc[::-1],
        cross_asset={k: v for k, v in cross.items() if not v.empty},
        zscores=rows.loc[[i for i in ranked.index if i in rows.index]], curves=curve_moves(curves),
    )
    if sectors.empty:
        review.notes.append("secteurs Stoxx 600 absents de la sélection")
    if curves is None or review.curves.empty:
        review.notes.append("courbes de taux indisponibles")
    for index in MOVERS_INDICES:
        found = (movers or {}).get(index)
        if found is not None and found.session is not None and not found.counted.empty:
            review.stock_lines.append(index_line(found, top))
    if not review.stock_lines:
        review.notes.append("cours des titres non chargés : market-monitor equities refresh")
    return review


def index_line(movers: IndexMovers, top: int = 3) -> IndexLine:
    def moves(frame: pd.DataFrame) -> tuple[StockMove, ...]:
        return tuple(StockMove(str(r["ticker"]), str(r["name"]), float(r["return_pct"]))
                     for _, r in frame.iterrows())
    return IndexLine(movers.index, movers.name, movers.index_change_pct, movers.session,
                     moves(movers.top(top)), moves(movers.flop(top)), len(movers.counted),
                     len(movers.checks))


def _rows(table: pd.DataFrame) -> pd.DataFrame:
    if table.empty:
        empty = pd.DataFrame(columns=ROW_COLUMNS[1:]).rename_axis("id")
        return empty.astype({"stale": bool, "check": bool, "level": float, "change": float, "z": float})
    out = pd.DataFrame({
        "name": table["name"], "asset_class": table["asset_class"], "level": table["level"],
        "change": table["chg_1d"],
        "change_unit": table["change_unit"], "z": table["z_1d"], "unit": table["unit"],
        "quote": table["quote"], "stale": table["stale"].astype(bool),
        "check": table["suspect"].astype(bool) if "suspect" in table else False,
    }, index=table.index)
    return out


def _session(table: pd.DataFrame, band: Sequence[str]) -> date | None:
    """Most common last-close date of the index band (the session being reviewed)."""
    if "level_date" not in table:
        return None
    dates = table.reindex([i for i in band if i in table.index])["level_date"].dropna()
    if dates.empty:
        dates = table["level_date"].dropna()
    return pd.Timestamp(dates.mode().iloc[0]).date() if not dates.empty else None


def curve_moves(curves: CurveSet | None, tenor: float = CURVE_TENOR) -> pd.DataFrame:
    """Per country: 10-year yield and J-1 change, 2s10s and its change, spread to the Bund."""
    columns = ["country", "label", "level", "chg", "slope", "slope_chg", "spread", "spread_chg"]
    if curves is None or curves.points.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for country in curves.countries:
        pts = curves.points[(curves.points["country"] == country) & (curves.points["tenor"] == tenor)]
        if pts.empty:
            continue
        slope = curves.slopes[(curves.slopes["country"] == country) & (curves.slopes["slope"] == "2s10s")]
        spread = curves.spreads[(curves.spreads["country"] == country) & (curves.spreads["tenor"] == tenor)]
        rows.append({
            "country": country, "label": SHORT_LABELS.get(country, country),
            "level": float(pts["level"].iloc[0]), "chg": float(pts["chg_J-1"].iloc[0]),
            "slope": float(slope["level"].iloc[0]) if not slope.empty else math.nan,
            "slope_chg": float(slope["chg_J-1"].iloc[0]) if not slope.empty else math.nan,
            "spread": float(spread["level"].iloc[0]) if not spread.empty else math.nan,
            "spread_chg": float(spread["chg_J-1"].iloc[0]) if not spread.empty else math.nan,
        })
    return pd.DataFrame(rows, columns=columns)


def movers_sentence(line: IndexLine, decimals: int = 1,
                    percent: Callable[[float], str] | None = None) -> str:
    """``CAC 40 (+0,6 %) : en tête Kering (+3,6 %), … ; en queue LVMH (−3,0 %), …``

    ``percent`` writes a signed % change (the daily macro passes its own number style);
    default: French format, narrow no-break space before %.
    """
    from market_monitor.text_format import NNBSP, fr_number

    def default(value: float) -> str:
        return f"{fr_number(value, decimals, sign=True)}{NNBSP}%"
    pct = percent or default

    def part(moves: tuple[StockMove, ...]) -> str:
        return ", ".join(f"{m.name} ({pct(m.return_pct)})" for m in moves)
    head = line.name
    if not math.isnan(line.change_pct):
        head += f" ({pct(line.change_pct)})"
    pieces = []
    if line.risers:
        pieces.append(f"en tête {part(line.risers)}")
    if line.fallers:
        pieces.append(f"en queue {part(line.fallers)}")
    return f"{head} : {' ; '.join(pieces)}." if pieces else f"{head} : aucun titre comptable."
