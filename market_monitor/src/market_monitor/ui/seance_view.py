"""« La séance d'hier » : the previous session on one screen (default view).

Index band, best and worst Stoxx 600 sectors, three risers and three fallers per index,
curve moves, FX / commodities / volatility, largest z-scores and alerts. Each block degrades
on its own: without index members, curves or a block's instruments, the rest still shows.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date
from functools import partial
from html import escape

import pandas as pd
import streamlit as st

from market_monitor.alerts import AlertRule
from market_monitor.analytics.session_review import (
    MOVERS_INDICES,
    IndexLine,
    SessionReview,
    build_review,
)
from market_monitor.config import UiSettings
from market_monitor.equities.movers import IndexMovers
from market_monitor.equities.universe import EquityUniverse
from market_monitor.exceptions import MarketMonitorError
from market_monitor.monitor import MarketMonitor, PerformanceReport
from market_monitor.sessions import ClosingRule
from market_monitor.text_format import (
    MISSING,
    NNBSP,
    format_change,
    fr_number,
    french_date,
    level_decimals,
)
from market_monitor.ui import theme
from market_monitor.ui.alerts_view import alerts_strip, load_alerts
from market_monitor.ui.curves_view import load_curves
from market_monitor.ui.instrument_view import open_instrument
from market_monitor.ui.movers_view import load_movers

ROW_HEIGHT_PX = 35
UNIT_SUFFIX = {"yield": " %", "spread": " pb"}
BAND_LABELS = {"SX5E": "Stoxx 50", "SXXP": "Stoxx 600", "FTSEMIB": "FTSE MIB", "NKY": "Nikkei"}


def render_session(monitor: MarketMonitor, equities: EquityUniverse | None, ui: UiSettings,
                   report: PerformanceReport, as_of: date, key: str,
                   rules: Sequence[AlertRule] | None = None, closing: ClosingRule | None = None) -> None:
    review = build_review(report.table, report.as_of, curves=_curves(monitor, ui, as_of, key, closing),
                          movers=_movers(equities, monitor, ui, as_of, key), zscores=max(ui.top_movers, 6))
    when = french_date(review.session) if review.session else french_date(report.as_of)
    st.markdown(f'<div class="mm-section" style="font-size:1.25rem">La séance du {escape(when)}</div>',
                unsafe_allow_html=True)
    alerts_strip(load_alerts(monitor, rules, ui, as_of, key))
    _band(review)
    left, middle, right = st.columns([5, 8, 6], gap="medium")
    with left:
        _sectors(review)
    with middle:
        _curves_block(review)
    with right:
        _zscores(review, key)
    _stock_lines(review)
    _cross_asset(review)
    for note in review.notes:
        st.caption(note.capitalize() + ".")
    st.caption("Clôtures uniquement ; variations sur la séance (1J), taux et spreads en pb. Détail dans "
               "les vues Vue d'ensemble, Top movers et Courbes de taux.")


# ------------------------------------------------------------------- loading
def _curves(monitor: MarketMonitor, ui: UiSettings, as_of: date, key: str, closing: ClosingRule | None):
    try:
        return load_curves(monitor, ui, as_of, key, closing)
    except MarketMonitorError:
        return None


def _movers(equities: EquityUniverse | None, monitor: MarketMonitor, ui: UiSettings, as_of: date,
            key: str) -> dict[str, IndexMovers]:
    if equities is None:
        return {}
    equities.panel.stamp()
    if equities.panel.field("last").empty:
        return {}
    known = {c.index for c in equities.compositions}
    out = {}
    for index in MOVERS_INDICES:
        if index not in known:
            continue
        try:
            out[index] = load_movers(equities, monitor, ui, index, as_of, key)
        except MarketMonitorError:
            continue
    return out


# -------------------------------------------------------------------- blocks
def _section(title: str) -> None:
    st.markdown(f'<div class="mm-section">{escape(title)}</div>', unsafe_allow_html=True)


def _level(row: pd.Series) -> str:
    value = row["level"]
    if value is None or pd.isna(value):
        return MISSING
    text = fr_number(float(value), level_decimals(str(row["asset_class"]), str(row["quote"]), float(value)))
    return text + UNIT_SUFFIX.get(str(row["quote"]), "").replace(" ", NNBSP)


def _change(row: pd.Series) -> tuple[str, str]:
    value = row["change"]
    text = format_change(value, str(row["change_unit"]))
    if bool(row.get("check", False)):
        text += " †"
    colour = theme.MUTED if bool(row.get("stale", False)) else theme.sign_color(value)
    return text, colour


def _band(review: SessionReview) -> None:
    if review.indices.empty:
        return
    tiles = []
    for _, row in review.indices.iterrows():
        text, colour = _change(row)
        name = BAND_LABELS.get(str(row.name), str(row["name"]))
        full = escape(str(row["name"]))
        tiles.append(f'<div class="mm-kpi"><div class="lbl" title="{full}">{escape(name)}</div>'
                     f'<div class="val" style="color:{colour}">{escape(text)}</div>'
                     f'<div class="sub">{escape(_level(row))}</div></div>')
    st.markdown(f'<div class="mm-band">{"".join(tiles)}</div>', unsafe_allow_html=True)


def _rows_html(rows: pd.DataFrame, show_level: bool = True) -> str:
    out = []
    for _, row in rows.iterrows():
        text, colour = _change(row)
        level = f'<span class="v">{escape(_level(row))}</span>' if show_level else ""
        out.append(f'<div class="mm-row"><span class="n">{escape(str(row["name"]))}</span>{level}'
                   f'<span class="c" style="color:{colour}">{escape(text)}</span></div>')
    return "".join(out)


def _card(title: str, body: str, head_value: str = "", head_colour: str = theme.TEXT) -> str:
    value = f'<span class="c" style="color:{head_colour}">{escape(head_value)}</span>' if head_value else ""
    return (f'<div class="mm-card"><div class="hd"><span class="t">{escape(title)}</span>{value}</div>'
            f'{body}</div>')


def _sectors(review: SessionReview) -> None:
    _section("Secteurs Stoxx 600")
    if review.sectors_best.empty:
        st.caption("Secteurs absents de la sélection.")
        return
    cards = (_card("En tête", _rows_html(review.sectors_best, show_level=False))
             + _card("En queue", _rows_html(review.sectors_worst, show_level=False)))
    st.markdown(f'<div class="mm-cards">{cards}</div>', unsafe_allow_html=True)


def _bp(value: float) -> str:
    return fr_number(value, 1, sign=True) if not math.isnan(value) else MISSING


def _curves_block(review: SessionReview) -> None:
    _section("Taux souverains (10 ans)")
    if review.curves.empty:
        st.caption("Courbes indisponibles.")
        return
    table = pd.DataFrame({
        "Pays": review.curves["label"],
        "10 ans": [f"{fr_number(v, 3)}{NNBSP}%" for v in review.curves["level"]],
        "Δ pb": [_bp(v) for v in review.curves["chg"]],
        "2s10s": [fr_number(v, 0) if not math.isnan(v) else MISSING for v in review.curves["slope"]],
        "Δ 2s10s": [_bp(v) for v in review.curves["slope_chg"]],
        "vs Bund": [fr_number(v, 0) if not math.isnan(v) else MISSING for v in review.curves["spread"]],
        "Δ spread": [_bp(v) for v in review.curves["spread_chg"]],
    })

    def colours(_: pd.DataFrame) -> pd.DataFrame:
        styles = pd.DataFrame("", index=table.index, columns=table.columns)
        for column, source in (("Δ pb", "chg"), ("Δ 2s10s", "slope_chg"), ("Δ spread", "spread_chg")):
            styles[column] = [f"color: {theme.sign_color(v)}; font-weight: 600;"
                              for v in review.curves[source]]
        return styles
    narrow = st.column_config.TextColumn(width=48)
    st.dataframe(table.style.apply(colours, axis=None), hide_index=True, width="stretch",
                 height=(len(table) + 1) * ROW_HEIGHT_PX + 3,
                 column_config={"Pays": st.column_config.TextColumn(width=50),
                                "10 ans": st.column_config.TextColumn(width=62), "Δ pb": narrow,
                                "2s10s": narrow, "Δ 2s10s": narrow, "vs Bund": narrow,
                                "Δ spread": narrow})


def _zscores(review: SessionReview, key: str) -> None:
    _section("Plus forts z-scores")
    rows = review.zscores
    if rows.empty:
        st.caption("Aucun z-score fiable.")
        return
    table = pd.DataFrame({
        "Instrument": list(rows["name"]),
        "Var. 1J": [_change(r)[0] for _, r in rows.iterrows()],
        "z": [fr_number(v, 1, sign=True) for v in rows["z"]],
    })

    def colours(_: pd.DataFrame) -> pd.DataFrame:
        styles = pd.DataFrame("", index=table.index, columns=table.columns)
        styles["Var. 1J"] = [f"color: {theme.sign_color(v)}; font-weight: 600;" for v in rows["change"]]
        styles["z"] = [f"color: {theme.MUTED};" for _ in rows["z"]]
        return styles
    table_key = f"{key}-seance-z"
    st.dataframe(table.style.apply(colours, axis=None), hide_index=True, width="stretch",
                 height=(len(table) + 1) * ROW_HEIGHT_PX + 3, key=table_key, selection_mode="single-row",
                 on_select=partial(_open_selected, key, table_key, [str(i) for i in rows.index]),
                 column_config={"Instrument": st.column_config.TextColumn(width=140),
                                "Var. 1J": st.column_config.TextColumn(width=68),
                                "z": st.column_config.TextColumn(width=40)})


def _open_selected(key: str, table_key: str, ids: list[str]) -> None:
    state = st.session_state.get(table_key)
    rows = list(state["selection"]["rows"]) if state else []
    if rows and rows[0] < len(ids):
        open_instrument(key, ids[rows[0]])
        st.session_state.pop(table_key, None)


def _stock_html(line: IndexLine) -> str:
    def rows(moves, arrow: str) -> str:
        return "".join(
            f'<div class="mm-row"><span class="n">{arrow} {escape(m.name)}</span>'
            f'<span class="c" style="color:{theme.sign_color(m.return_pct)}">'
            f'{escape(fr_number(m.return_pct, 2, sign=True))}{NNBSP}%</span></div>' for m in moves)
    change = (f"{fr_number(line.change_pct, 2, sign=True)}{NNBSP}%" if not math.isnan(line.change_pct)
              else MISSING)
    body = rows(line.risers, "▲") + rows(line.fallers, "▼")
    if line.checks:
        body += f'<div class="note">{line.checks} titre(s) à vérifier, hors classement</div>'
    return _card(line.name, body, change, theme.sign_color(line.change_pct))


def _stock_lines(review: SessionReview) -> None:
    _section("Valeurs marquantes par indice")
    if not review.stock_lines:
        st.caption("Cours des titres non chargés : vue Top movers, « Actualiser les cours des titres », "
                   "ou market-monitor equities refresh.")
        return
    other = [line for line in review.stock_lines if review.session and line.session
             and line.session != review.session]
    cards = "".join(_stock_html(line) for line in review.stock_lines)
    st.markdown(f'<div class="mm-cards" style="grid-template-columns:repeat(auto-fill,minmax(230px,1fr))">'
                f'{cards}</div>', unsafe_allow_html=True)
    if other:
        names = ", ".join(f"{line.name} ({line.session:%d/%m})" for line in other if line.session)
        st.caption(f"Séance différente pour : {names}.")


def _cross_asset(review: SessionReview) -> None:
    if not review.cross_asset:
        return
    _section("Change, matières premières, volatilité")
    cards = "".join(_card(label, _rows_html(rows)) for label, rows in review.cross_asset.items())
    st.markdown(f'<div class="mm-cards" style="grid-template-columns:repeat(auto-fit,minmax(260px,1fr))">'
                f'{cards}</div>', unsafe_allow_html=True)

