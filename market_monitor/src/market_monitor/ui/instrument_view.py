"""Instrument page: real level history of one instrument, statistics, daily closes."""

from __future__ import annotations

import io
import math
from collections.abc import Callable
from datetime import date
from html import escape

import pandas as pd
import streamlit as st

from market_monitor.analytics.history import source_label
from market_monitor.analytics.instrument import (
    PROFILE_PERIODS,
    STAT_HORIZONS,
    InstrumentProfile,
    spread_changes,
)
from market_monitor.analytics.performance import daily_changes
from market_monitor.config import UiSettings
from market_monitor.data.models import ohlc_share
from market_monitor.exceptions import MarketMonitorError
from market_monitor.monitor import MarketMonitor
from market_monitor.referential import ChangeUnit, Instrument
from market_monitor.sessions import ClosingRule
from market_monitor.text_format import (
    ASSET_CLASS_LABELS,
    CHANGE_UNIT_LABELS,
    MISSING,
    NNBSP,
    SOURCE_LABELS,
    format_change,
    fr_number,
    french_date,
    level_decimals,
)
from market_monitor.ui import theme
from market_monitor.ui.caching import cached, refresh_nonce
from market_monitor.ui.charts import instrument_figure, spread_figure

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DEFAULT_PERIOD = "1A"
CHART_MODES = {"auto": "Auto", "candles": "Chandeliers", "line": "Ligne"}
UNIT_SUFFIX = {"yield": "%", "spread": "pb"}
NO_OVERLAY = ""


def view_key(key: str) -> str:
    return f"{key}-view"


def instrument_key(key: str) -> str:
    return f"{key}-fiche-id"


def open_instrument(key: str, instrument_id: str) -> None:
    """Switch the dashboard to the page of ``instrument_id`` (use as a widget callback)."""
    st.session_state[view_key(key)] = "instrument"
    st.session_state[instrument_key(key)] = instrument_id


def apply_query_params(monitor: MarketMonitor, key: str,
                       known: Callable[[str], bool] | None = None) -> None:
    """``?fiche=SX5E`` (or ``?fiche=EQ:MC.PA``, an index member) opens that page."""
    wanted = st.query_params.get("fiche")
    listed = bool(wanted) and (wanted in monitor.referential or (known is not None and known(str(wanted))))
    if wanted and listed and st.session_state.get(f"{key}-fiche-qp") != wanted:
        st.session_state[f"{key}-fiche-qp"] = wanted
        open_instrument(key, wanted)


def _compute(_monitor: MarketMonitor, monitor_id: int, instrument_id: str, period: str, as_of: str,
             overlay: str | None, provisional: tuple[str, ...], nonce: int) -> InstrumentProfile:
    return _monitor.instrument_profile(instrument_id, period, as_of, overlay=overlay,
                                       provisional=provisional)


def render_instrument(monitor: MarketMonitor, ui: UiSettings, as_of: date, key: str,
                      closing: ClosingRule | None = None, default_id: str | None = None) -> None:
    ref = monitor.referential
    options = list(monitor.available(ref.ids))
    if not options:
        st.info("Aucun instrument disponible avec les sources actives.")
        return
    id_key = instrument_key(key)
    if st.session_state.get(id_key) not in options:
        st.session_state[id_key] = default_id if default_id in options else options[0]
    top = st.columns([3, 5, 3], vertical_alignment="bottom")
    instrument_id = top[0].selectbox("Instrument", options, key=id_key,
                                     format_func=lambda i: _option_label(ref.get(i)))
    period = top[1].segmented_control("Période", PROFILE_PERIODS, default=DEFAULT_PERIOD, required=True,
                                      key=f"{key}-fiche-period") or DEFAULT_PERIOD
    overlay = top[2].selectbox(
        "Superposer", [NO_OVERLAY, *[i for i in options if i != instrument_id]], key=f"{key}-fiche-overlay",
        format_func=lambda i: "Aucun instrument" if i == NO_OVERLAY else ref.get(i).name)
    ids = [i for i in (instrument_id, overlay, ref.benchmark_of(instrument_id)) if i]
    provisional: tuple[str, ...] = ()
    if as_of >= monitor.today():
        provisional = tuple(sorted((closing or ClosingRule()).open_sessions(ref, ids, monitor.data_time())))
    compute = cached(_compute, ui, "Chargement de l'historique…")
    try:
        profile = compute(monitor, id(monitor), instrument_id, period, as_of.isoformat(),
                          overlay or None, provisional, refresh_nonce(key))
    except MarketMonitorError as exc:
        st.error(f"Historique indisponible : {exc}")
        return
    if profile.bars.empty:
        st.warning(f"Aucune donnée pour {profile.instrument.name} : {profile.error or 'source muette'}")
        return
    _header(profile)
    window = profile.window
    share = ohlc_share(window)
    controls = st.columns([3, 3, 4], vertical_alignment="center")
    mode = controls[0].segmented_control("Graphique", list(CHART_MODES), default="auto", required=True,
                                         format_func=lambda m: CHART_MODES[m],
                                         key=f"{key}-fiche-mode") or "auto"
    averages = controls[1].toggle("Moyennes mobiles 50 / 200", value=True, key=f"{key}-fiche-ma")
    candles = mode == "candles" or (mode == "auto" and share >= 0.9)
    if mode == "candles" and share < 0.9:
        controls[2].caption("Pas d'OHLC complet pour cette source : courbe des clôtures.")
        candles = False
    st.plotly_chart(instrument_figure(profile, candles=candles, averages=averages), width="stretch",
                    config={"displayModeBar": False}, key=f"{key}-fiche-chart")
    pair = spread_figure(profile, pair=True)
    if pair is not None and profile.pair_spread is not None:
        st.markdown(f'<div class="mm-section">Écart {escape(profile.instrument.name)} − '
                    f'{escape(str(profile.overlay_name))} (pb)</div>', unsafe_allow_html=True)
        st.markdown(_spread_line(profile.pair_spread, profile.as_of), unsafe_allow_html=True)
        st.plotly_chart(pair, width="stretch", config={"displayModeBar": False}, key=f"{key}-fiche-pair")
    spread = spread_figure(profile)
    if spread is not None and profile.overlay_id != profile.benchmark_id:  # else: the chart above
        bench = monitor.referential.get(str(profile.benchmark_id)).name
        st.markdown(f'<div class="mm-section">Spread contre le {escape(bench)} (pb)</div>',
                    unsafe_allow_html=True)
        st.plotly_chart(spread, width="stretch", config={"displayModeBar": False},
                        key=f"{key}-fiche-spread")
    _stats(profile)
    _closes_table(profile, key)
    _sources(profile)


# ---------------------------------------------------------------------- blocks
def _spread_line(spread: pd.Series, as_of: date) -> str:
    """``−40,2 pb · 1J +1,3 · 1S −2,0 · 1M −5,0 · YTD +12,4 · 1A +20,1``."""
    values = spread_changes(spread, as_of)
    if not values:
        return ""
    parts = [f'<span class="lvl">{escape(fr_number(values["level"], 1))} pb</span>']
    for label in ("1J", "1S", "1M", "YTD", "1A"):
        move = values.get(label, math.nan)
        parts.append(f'<span class="lbl">{label}</span> <span class="chg" style="color:'
                     f'{theme.sign_color(move)}">{escape(fr_number(move, 1, sign=True))}</span>')
    return ('<div class="mm-chart-head"><div class="vals">'
            + '<span class="sep"> · </span>'.join(parts) + "</div></div>")


def _option_label(inst: Instrument) -> str:
    return f"{inst.name} · {ASSET_CLASS_LABELS.get(inst.asset_class.value, inst.asset_class.value)}"


def level_text(value: float, inst: Instrument) -> str:
    if value is None or math.isnan(value):
        return MISSING
    text = fr_number(value, level_decimals(inst.asset_class.value, inst.quote.value, value))
    suffix = UNIT_SUFFIX.get(inst.quote.value, "")
    return f"{text}{NNBSP}{suffix}" if suffix else text


def vol_text(value: float, inst: Instrument) -> str:
    if value is None or math.isnan(value):
        return MISSING
    decimals = 1 if inst.change is not ChangeUnit.ABS else 2
    return f"{fr_number(value, decimals)}{NNBSP}{CHANGE_UNIT_LABELS[inst.change.value]}"


def _header(profile: InstrumentProfile) -> None:
    inst, stats = profile.instrument, profile.stats
    move = stats.changes.get("1J", math.nan)
    colour = theme.sign_color(move)
    when = french_date(stats.level_date.date()) if stats.level_date is not None else MISSING
    group = ASSET_CLASS_LABELS.get(inst.asset_class.value, inst.asset_class.value)
    st.markdown(
        f'<div class="mm-section" style="font-size:1.35rem;margin-bottom:0">{escape(inst.name)}'
        f'<span style="color:{theme.MUTED};font-size:.9rem;font-weight:400"> · {escape(group)}'
        f' · {escape(inst.group)}</span></div>'
        f'<div style="font-variant-numeric:tabular-nums;margin:.1rem 0 .6rem 0">'
        f'<span style="font-size:1.9rem;font-weight:600">{escape(level_text(stats.level, inst))}</span>'
        f'<span style="color:{colour};font-size:1.15rem;font-weight:600;margin-left:.7rem">'
        f'{escape(format_change(move, inst.change.value))}</span>'
        f'<span style="color:{theme.MUTED};margin-left:.7rem">clôture du {escape(when)}</span></div>',
        unsafe_allow_html=True,
    )
    if profile.provisional:
        st.caption("Séance du jour non close : la cotation en cours est ignorée (clôtures uniquement).")


def _stats(profile: InstrumentProfile) -> None:
    inst, s = profile.instrument, profile.stats
    unit = inst.change.value
    cells = "".join(
        f'<div class="mm-kpi"><div class="lbl">{h}</div><div class="val" style="font-size:1.02rem;'
        f'white-space:nowrap;color:{theme.sign_color(s.changes[h])}">'
        f'{escape(format_change(s.changes[h], unit))}</div></div>'
        for h in STAT_HORIZONS)
    st.markdown('<div class="mm-section">Variations</div>'
                f'<div class="mm-kpis mm-kpis-tight" style="grid-template-columns:'
                f'repeat(auto-fit,minmax(92px,1fr))">{cells}</div>', unsafe_allow_html=True)

    def day(when: pd.Timestamp | None) -> str:
        return f" le {when:%d/%m/%Y}" if when is not None else ""

    tiles = [
        ("Plus haut 52 sem.", level_text(s.high_52w, inst),
         f"{format_change(s.from_high_52w, unit)} du plus haut{day(s.high_52w_date)}"),
        ("Plus bas 52 sem.", level_text(s.low_52w, inst),
         f"{format_change(s.from_low_52w, unit)} du plus bas{day(s.low_52w_date)}"),
        ("Vol. réalisée 1M", vol_text(s.vol_1m, inst), "annualisée"),
        ("Vol. réalisée 3M", vol_text(s.vol_3m, inst), "annualisée"),
        (s.worst_move_label, format_change(s.worst_move, unit), f"sur la période ({s.sessions} séances)"),
        ("MM 50", level_text(s.ma50, inst), f"écart {format_change(s.to_ma50, unit)}"),
        ("MM 200", level_text(s.ma200, inst), f"écart {format_change(s.to_ma200, unit)}"),
    ]
    html = "".join(f'<div class="mm-kpi"><div class="lbl">{escape(label)}</div>'
                   f'<div class="val">{escape(value)}</div>'
                   f'<div style="color:{theme.MUTED};font-size:.8rem">{escape(sub)}</div></div>'
                   for label, value, sub in tiles)
    st.markdown('<div class="mm-section">Statistiques (clôtures)</div>'
                f'<div class="mm-kpis">{html}</div>', unsafe_allow_html=True)


CLOSE_COLUMNS = {"open": "Ouverture", "high": "Plus haut", "low": "Plus bas", "last": "Clôture",
                 "volume": "Volume"}


def closes_frame(profile: InstrumentProfile) -> pd.DataFrame:
    """Daily closes of the window, most recent first, with the day's change."""
    inst = profile.instrument
    full = profile.bars
    changes = daily_changes(full["last"], inst.change, inst.bp_factor)
    window = full.loc[pd.Timestamp(profile.start):pd.Timestamp(profile.as_of)]
    out = window.rename(columns=CLOSE_COLUMNS)
    out.insert(4, f"Variation ({CHANGE_UNIT_LABELS[inst.change.value]})", changes.reindex(window.index))
    out = out.dropna(axis=1, how="all")
    out.index = pd.DatetimeIndex(out.index).date
    out.index.name = "Date"
    return out.iloc[::-1]


def _closes_table(profile: InstrumentProfile, key: str) -> None:
    table = closes_frame(profile)
    inst = profile.instrument
    st.markdown(f'<div class="mm-section">Clôtures quotidiennes ({len(table)})</div>',
                unsafe_allow_html=True)
    st.dataframe(display_closes(table, inst, profile.stats.level), width="stretch", height=320,
                 hide_index=True)
    stem = f"{inst.id}_{profile.start:%Y%m%d}_{profile.as_of:%Y%m%d}"
    c1, c2, _ = st.columns([2, 2, 6])
    c1.download_button("CSV", csv_bytes(table), f"{stem}.csv", "text/csv", key=f"{key}-fiche-csv",
                       icon=":material/download:")
    c2.download_button("Excel", xlsx_bytes(table, inst.name), f"{stem}.xlsx", XLSX_MIME,
                       key=f"{key}-fiche-xlsx", icon=":material/download:")


def display_closes(table: pd.DataFrame, inst: Instrument, level: float) -> pd.DataFrame:
    """French formatting of the closes table (the downloads keep the raw numbers)."""
    decimals = level_decimals(inst.asset_class.value, inst.quote.value, level)
    out = pd.DataFrame({"Date": [f"{d:%d/%m/%Y}" for d in table.index]})
    for column in table.columns:
        values = table[column].to_numpy()
        if column == "Volume":
            out[column] = [fr_number(v, 0) for v in values]
        elif column.startswith("Variation"):
            out[column] = [format_change(v, inst.change.value, with_unit=False) for v in values]
        else:
            out[column] = [fr_number(v, decimals) for v in values]
    return out


def csv_bytes(table: pd.DataFrame) -> bytes:
    """French Excel CSV: ``;`` separator, decimal comma, UTF-8 with BOM."""
    return table.to_csv(sep=";", decimal=",", encoding="utf-8-sig").encode("utf-8-sig")


def xlsx_bytes(table: pd.DataFrame, title: str) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        table.to_excel(writer, sheet_name=title[:31].replace("/", "-") or "Clôtures")
    return buffer.getvalue()


def _sources(profile: InstrumentProfile) -> None:
    """Where the history comes from and how deep it goes."""
    lines = []
    if profile.source:
        origin = source_label(profile.origin) if profile.origin else ""
        label = SOURCE_LABELS.get(profile.source, profile.source)
        lines.append(f"Source : {label}{f' ({origin})' if origin else ''}"
                     + (f", proxy : {profile.proxy}" if profile.proxy else ""))
    depth = profile.depth
    if depth is not None and not depth.empty:
        served = depth[depth["provider"] == profile.source]
        rows = served if not served.empty else depth
        row = rows.iloc[0]
        reached = " — maximum de la source atteint" if bool(row["depth_reached"]) else ""
        lines.append(f"Historique en cache : depuis le {row['first']:%d/%m/%Y} "
                     f"({int(row['observations'])} séances){reached}")
    if profile.late_start:
        lines.append(f"La source ne remonte qu'au {profile.start:%d/%m/%Y} : "
                     "la période affichée commence à cette date.")
    lines += [str(n) for n in profile.notes]
    if profile.warning:
        lines.append(f"Avertissement : {profile.warning}")
    for line in lines:
        st.caption(line)
