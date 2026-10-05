"""Historique: multi-asset comparison (base 100 / cumulative bp / points)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from market_monitor.analytics.comparison import PERIODS, ComparisonResult
from market_monitor.config import UiSettings
from market_monitor.exceptions import MarketMonitorError
from market_monitor.monitor import MarketMonitor
from market_monitor.referential import ChangeUnit, Referential
from market_monitor.text_format import (
    CHANGE_UNIT_LABELS,
    NNBSP,
    format_change,
    fr_number,
    french_date,
    level_decimals,
)
from market_monitor.ui.caching import cached, refresh_nonce
from market_monitor.ui.charts import comparison_figure

MAX_SERIES = 10
DEFAULT_SERIES = 4


def _compute(_monitor: MarketMonitor, monitor_id: int, ids: tuple[str, ...], period: str,
             as_of: str, nonce: int) -> ComparisonResult:
    return _monitor.comparison(ids, period, as_of)


def render_history(monitor: MarketMonitor, ui: UiSettings, watchlist: str, as_of: date, key: str) -> None:
    ref = monitor.referential
    default = list(ref.resolve(ref.watchlist(watchlist).instruments))[:DEFAULT_SERIES]
    ids = st.multiselect(
        "Instruments à comparer", options=list(ref.ids), default=default, max_selections=MAX_SERIES,
        format_func=lambda i: ref.get(i).name, key=f"{key}-hist-ids",
    )
    period = st.segmented_control("Période", PERIODS, default="1A", required=True,
                                  key=f"{key}-hist-period") or "1A"
    if not ids:
        st.info("Choisissez au moins un instrument à comparer.")
        return
    compute = cached(_compute, ui, "Chargement des historiques…")
    try:
        result = compute(monitor, id(monitor), tuple(ids), period, as_of.isoformat(), refresh_nonce(key))
    except MarketMonitorError as exc:
        st.error(f"Chargement impossible : {exc}")
        return
    if result.base_date is None:
        st.warning("Aucune donnée sur la période pour cette sélection.")
        return
    st.caption(
        f"Origine commune : {french_date(result.base_date.date(), weekday=False)}. Les prix sont "
        "en base 100, les taux et spreads en variation cumulée en pb, la volatilité en points."
    )
    names = {i: ref.get(i).name for i in ids}
    st.plotly_chart(comparison_figure(result, names), width="stretch",
                    config={"displayModeBar": False}, key=f"{key}-hist-chart")
    st.dataframe(stats_table(result, ref), hide_index=True, width="stretch")
    for label, issues in (("Sans données", result.errors), ("À noter", result.warnings)):
        for instrument_id, message in issues.items():
            if instrument_id in names:
                st.caption(f"{label} : {names[instrument_id]} ({message}).")


def stats_table(result: ComparisonResult, ref: Referential) -> pd.DataFrame:
    """French display of the period statistics."""
    rows = []
    for instrument_id, s in result.stats.iterrows():
        inst = ref.get(str(instrument_id))
        decimals = level_decimals(inst.asset_class.value, inst.quote.value, s["end"])
        unit = CHANGE_UNIT_LABELS[inst.change.value]
        rows.append({
            "Instrument": inst.name,
            "Début": fr_number(s["start"], decimals),
            "Fin": fr_number(s["end"], decimals),
            "Variation": format_change(s["change"], inst.change.value),
            "Vol. annualisée": f"{fr_number(s['ann_vol'], 1)}{NNBSP}{unit}",
            "Perte max.": (f"{fr_number(s['max_drawdown'], 1)}{NNBSP}%"
                           if inst.change is ChangeUnit.PCT else "–"),
            "Plus bas": fr_number(s["low"], decimals),
            "Plus haut": fr_number(s["high"], decimals),
        })
    return pd.DataFrame(rows)
