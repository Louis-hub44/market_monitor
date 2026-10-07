"""Daily macro: preview of the export and downloads (Excel, PNG, text)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape

import pandas as pd
import streamlit as st

from market_monitor.alerts import AlertRule
from market_monitor.analytics.comparison import PERIODS
from market_monitor.config import MANUAL_PROVIDER, UiSettings
from market_monitor.data.providers.manual import ManualQuotes, previous_business_day
from market_monitor.exceptions import MarketMonitorError
from market_monitor.export import (
    DailyMacroData,
    DailyMacroLayout,
    build_daily_macro,
    excel_bytes,
    png_bytes,
    render_text,
)
from market_monitor.export.charts import (
    PERIOD_LABELS,
    ChartData,
    build_charts,
    chart_window,
    charts_png_bytes,
)
from market_monitor.export.layout import ChartSpec
from market_monitor.monitor import MarketMonitor
from market_monitor.text_format import fr_number
from market_monitor.ui import theme
from market_monitor.ui.caching import bump_refresh_nonce, cached, refresh_nonce
from market_monitor.ui.charts import daily_chart_figure

FORM_COLUMNS = 4   # manual-entry inputs per row
CHART_COLUMNS = 2  # charts per row
CUSTOM = "dates"   # period option: free start date
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@dataclass
class _Bundle:
    data: DailyMacroData
    xlsx: bytes
    png: bytes
    text: str


def _compute(_monitor: MarketMonitor, _layout: DailyMacroLayout, _rules: Sequence[AlertRule],
             monitor_id: int, layout_hash: int, rules_hash: int, as_of: str, nonce: int) -> _Bundle:
    data = build_daily_macro(_monitor, _layout, as_of, _rules)
    return _Bundle(data, excel_bytes(data), png_bytes(data), render_text(data))


def render_daily_macro(monitor: MarketMonitor, ui: UiSettings, layout: DailyMacroLayout | None,
                       as_of: date, key: str, rules: Sequence[AlertRule] | None = None) -> None:
    if layout is None:
        st.info("Aucune mise en page d'export n'est configurée (config/daily_macro.yaml).")
        return
    _manual_entry(monitor, layout, as_of, key)
    compute = cached(_compute, ui, "Préparation de la daily macro…")
    try:
        rule_set = tuple(rules or ())
        bundle = compute(monitor, layout, rule_set, id(monitor), hash(layout), hash(rule_set),
                         as_of.isoformat(), refresh_nonce(key))
    except MarketMonitorError as exc:
        st.error(f"Export impossible : {exc}")
        return
    stem = f"{layout.file_prefix}_{bundle.data.as_of.isoformat()}"
    c1, c2, c3 = st.columns(3)
    c1.download_button("Télécharger l'Excel", bundle.xlsx, f"{stem}.xlsx", XLSX_MIME, key=f"{key}-dl-xlsx")
    c2.download_button("Télécharger le PNG", bundle.png, f"{stem}.png", "image/png", key=f"{key}-dl-png")
    c3.download_button("Télécharger le texte", bundle.text.encode("utf-8"), f"{stem}.txt",
                       "text/plain", key=f"{key}-dl-txt")
    st.image(bundle.png, width="stretch")
    _charts(monitor, ui, layout, bundle.data.as_of, key)
    st.markdown("**Texte prêt à coller**")
    st.code(bundle.text, language=None, wrap_lines=True)


def _manual_entry(monitor: MarketMonitor, layout: DailyMacroLayout, as_of: date, key: str) -> None:
    """Form for the lines without a reachable source (iTraxx outside Bloomberg)."""
    store = monitor.manual_quotes()
    if store is None:
        return
    day = previous_business_day(as_of)
    ids = _manual_candidates(monitor, layout, day)
    if not ids:
        return
    frame = store.frame()
    missing = [i for i in ids if not _entered(frame, _ticker(monitor, i), day)]
    label = "Saisie manuelle" + (f" — {len(missing)} à saisir pour le {day:%d/%m}" if missing else "")
    with st.expander(label, icon=":material/edit_note:", expanded=bool(missing)):
        st.caption("Cotations sans source accessible (ex. iTraxx hors Bloomberg) : niveau de clôture "
                   "lu sur écran ou note broker. Bloomberg reste prioritaire lorsqu'il est connecté.")
        with st.form(f"{key}-manual"):
            when = st.date_input("Date de clôture", value=day, format="DD/MM/YYYY", key=f"{key}-mq-date")
            values: dict[str, float | None] = {}
            columns = [c for k in range(0, len(ids), FORM_COLUMNS)
                       for c in st.columns(FORM_COLUMNS)][:len(ids)]
            for col, instrument_id in zip(columns, ids, strict=True):
                inst = monitor.referential.get(instrument_id)
                last = _last(frame, _ticker(monitor, instrument_id))
                values[instrument_id] = col.number_input(
                    f"{inst.name} ({inst.unit or 'niveau'})", value=None, step=0.1, format="%.2f",
                    key=f"{key}-mq-{instrument_id}",
                    help=f"Dernière saisie : {last}" if last else "Aucune saisie",
                )
            if st.form_submit_button("Enregistrer", icon=":material/save:"):
                saved = _save(monitor, store, values, when)
                if saved:
                    bump_refresh_nonce(key)
                    st.rerun()
                st.warning("Aucune valeur saisie.")


def _manual_candidates(monitor: MarketMonitor, layout: DailyMacroLayout,
                       day: date) -> tuple[str, ...]:
    """Raw lines (legs of spreads included) the user may have to type in for ``day``.

    A line that only has a manual ticker is always offered; one that also has another
    source (OAS via FRED...) only when that source did not serve it.
    """
    ref = monitor.referential
    deps = monitor.manual_ids(ref.raw_dependencies(monitor.available(layout.section_ids)))
    automatic = [i for i in deps if any(p in ref.get(i).tickers
                                        for p in monitor.service.provider_names if p != MANUAL_PROVIDER)]
    if not automatic:
        return deps
    try:
        hist = monitor.history(automatic, day - timedelta(days=10), day)
    except MarketMonitorError:
        return deps
    served = {i for i in automatic if hist.sources.get(i) not in (None, MANUAL_PROVIDER)}
    return tuple(i for i in deps if i not in served)


def _ticker(monitor: MarketMonitor, instrument_id: str) -> str:
    return monitor.referential.get(instrument_id).tickers["manual"].ticker


def _entered(frame: pd.DataFrame, ticker: str, day: date) -> bool:
    return bool(((frame["ticker"] == ticker) & (frame["date"] == pd.Timestamp(day))).any())


def _last(frame: pd.DataFrame, ticker: str) -> str:
    rows = frame[frame["ticker"] == ticker]
    if rows.empty:
        return ""
    row = rows.iloc[-1]
    return f"{fr_number(float(row['value']), 2)} au {row['date']:%d/%m/%Y}"


def _save(monitor: MarketMonitor, store: ManualQuotes, values: dict[str, float | None],
          when: object) -> int:
    if not isinstance(when, date):
        return 0
    saved = 0
    for instrument_id, value in values.items():
        if value is not None:
            store.set(_ticker(monitor, instrument_id), when, float(value))
            saved += 1
    return saved


# ====================================================================== charts
def _compute_charts(_monitor: MarketMonitor, monitor_id: int, specs: tuple[ChartSpec, ...],
                    as_of: str, nonce: int) -> tuple[list[ChartData], bytes]:
    charts = build_charts(_monitor, specs, date.fromisoformat(as_of))
    return charts, charts_png_bytes(charts)


def _charts(monitor: MarketMonitor, ui: UiSettings, layout: DailyMacroLayout, as_of: date,
            key: str) -> None:
    """Grid of charts; each has its own period (or start date), changed in place."""
    if not layout.charts:
        return
    st.markdown('<div class="mm-section">Graphiques</div>', unsafe_allow_html=True)
    cells = [c for k in range(0, len(layout.charts), CHART_COLUMNS)
             for c in st.columns(CHART_COLUMNS, gap="large")][:len(layout.charts)]
    specs, slots = [], []
    for k, (cell, spec) in enumerate(zip(cells, layout.charts, strict=True)):
        with cell:
            header = st.empty()
            specs.append(_chart_controls(spec, k, as_of, key))
            slots.append((header, st.empty()))
    compute = cached(_compute_charts, ui, "Préparation des graphiques…")
    try:
        charts, png = compute(monitor, id(monitor), tuple(specs), as_of.isoformat(), refresh_nonce(key))
    except MarketMonitorError as exc:
        st.error(f"Graphiques impossibles : {exc}")
        return
    for k, (chart, (header, body)) in enumerate(zip(charts, slots, strict=True)):
        header.markdown(chart_header_html(chart), unsafe_allow_html=True)
        if chart.series:
            body.plotly_chart(daily_chart_figure(chart), width="stretch",
                              config={"displayModeBar": False}, key=f"{key}-chart-{k}")
        else:
            body.info("Pas de donnée sur la période : "
                      + "; ".join(f"{i} ({m})" for i, m in chart.errors.items()))
    st.download_button("Télécharger les graphiques (PNG)", png,
                       f"{layout.file_prefix}_{as_of.isoformat()}_graphiques.png", "image/png",
                       icon=":material/download:", key=f"{key}-dl-charts")


def _chart_controls(spec: ChartSpec, k: int, as_of: date, key: str) -> ChartSpec:
    """Period of chart ``k`` (default from the layout), or a free start date."""
    options = [*PERIODS, CUSTOM]
    default = CUSTOM if spec.start is not None else spec.period
    left, right = st.columns([3, 2], vertical_alignment="bottom")
    period = left.selectbox(
        "Période", options, index=options.index(default), key=f"{key}-chart-{k}-period",
        format_func=_period_option,
        label_visibility="collapsed",
    )
    if period != CUSTOM:
        return ChartSpec(spec.title, spec.instruments, period, None, spec.labels)
    start = right.date_input("Depuis le", value=chart_window(spec, as_of)[0], max_value=as_of,
                             format="DD/MM/YYYY", key=f"{key}-chart-{k}-start",
                             label_visibility="collapsed")
    first = start if isinstance(start, date) else chart_window(spec, as_of)[0]
    return ChartSpec(spec.title, spec.instruments, spec.period, first, spec.labels)


def _period_option(period: str) -> str:
    if period == CUSTOM:
        return "Depuis une date…"
    return "YTD · depuis le 1er janvier" if period == "YTD" else f"{period} · {PERIOD_LABELS[period]}"


def chart_header_html(chart: ChartData) -> str:
    """Title, then each series' last level and 1-day change (in its sign colour)."""
    parts = []
    for s in chart.series:
        move = s.change_1d
        tone = theme.sign_color(round(move, 2) if not pd.isna(move) else move)
        name = f'<span class="lbl">{escape(s.label)}</span> ' if len(chart.series) > 1 else ""
        parts.append(f'{name}<span class="lvl">{escape(s.level_text())}</span> '
                     f'<span class="chg" style="color:{tone}">{escape(s.change_text())}</span>')
    values = '<span class="sep"> · </span>'.join(parts)
    return (f'<div class="mm-chart-head"><div class="ttl">{escape(chart.title)}'
            f'<span class="per"> · {escape(chart.period_label)}</span></div>'
            f'<div class="vals">{values}</div></div>')
