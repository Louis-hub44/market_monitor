"""Sovereign yield curves: today against J-1 / S-1 / M-1, changes, slopes, spreads to the Bund."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pandas as pd
import streamlit as st

from market_monitor.analytics.curves import (
    COMPARISONS,
    COUNTRY_LABELS,
    DEFAULT_COUNTRIES,
    SHORT_LABELS,
    CurveSet,
    curve_ids,
    tenor_label,
)
from market_monitor.config import UiSettings
from market_monitor.exceptions import MarketMonitorError
from market_monitor.export.curves_chart import curves_png_bytes
from market_monitor.monitor import MarketMonitor
from market_monitor.sessions import ClosingRule
from market_monitor.text_format import fr_number, french_date
from market_monitor.ui.caching import cached, refresh_nonce
from market_monitor.ui.charts import curve_changes_figure, curves_today_figure, curves_vs_figure

CHART_CONFIG = {"displayModeBar": False}


def _compute(_monitor: MarketMonitor, monitor_id: int, as_of: str, provisional: tuple[str, ...],
             nonce: int) -> CurveSet:
    return _monitor.curves(as_of, provisional=provisional)


def load_curves(monitor: MarketMonitor, ui: UiSettings, as_of: date, key: str,
                closing: ClosingRule | None = None) -> CurveSet | None:
    """Curves at ``as_of`` (closes only, cached); ``None`` without any sovereign yield."""
    ref = monitor.referential
    ids = [i for i in curve_ids(ref) if monitor.is_available(i)]
    if not ids:
        return None
    provisional: tuple[str, ...] = ()
    if as_of >= monitor.today():
        provisional = tuple(sorted((closing or ClosingRule()).open_sessions(ref, ids, monitor.data_time())))
    compute = cached(_compute, ui, "Chargement des courbes…")
    result: CurveSet = compute(monitor, id(monitor), as_of.isoformat(), provisional, refresh_nonce(key))
    return result


def render_curves(monitor: MarketMonitor, ui: UiSettings, as_of: date, key: str,
                  closing: ClosingRule | None = None) -> None:
    try:
        curves = load_curves(monitor, ui, as_of, key, closing)
    except MarketMonitorError as exc:
        st.error(f"Courbes indisponibles : {exc}")
        return
    if curves is None:
        st.info("Aucun taux souverain disponible avec les sources actives.")
        return
    available = curves.countries
    controls = st.columns([5, 3, 2], vertical_alignment="bottom")
    countries = controls[0].multiselect(
        "Pays", available, default=[c for c in DEFAULT_COUNTRIES if c in available],
        format_func=lambda c: COUNTRY_LABELS.get(c, c), key=f"{key}-curves-countries")
    comparisons = controls[1].pills("Comparer à", list(COMPARISONS), default=list(COMPARISONS),
                                    selection_mode="multi", key=f"{key}-curves-vs") or []
    since = controls[2].segmented_control("Variations depuis", list(COMPARISONS), default="J-1",
                                          required=True, key=f"{key}-curves-since") or "J-1"
    if not countries:
        st.info("Choisir au moins un pays.")
        return
    last = curves.points["level_date"].max()
    st.caption(f"Clôtures au {french_date(last.date()) if pd.notna(last) else '–'} · J-1 : séance "
               "précédente, S-1 : une semaine avant, M-1 : un mois avant (même jour).")
    if curves.provisional:
        st.caption("Séance du jour non close pour une partie des points : dernière clôture utilisée.")
    _section("Courbes du jour")
    st.plotly_chart(curves_today_figure(curves, countries), width="stretch", config=CHART_CONFIG,
                    key=f"{key}-curves-today")
    _section(f"Aujourd'hui contre {', '.join(comparisons) if comparisons else '—'}")
    st.plotly_chart(curves_vs_figure(curves, countries, list(comparisons)), width="stretch",
                    config=CHART_CONFIG, key=f"{key}-curves-vs-chart")
    _section(f"Variations par maturité depuis {since} (pb)")
    st.plotly_chart(curve_changes_figure(curves, countries, since), width="stretch", config=CHART_CONFIG,
                    key=f"{key}-curves-changes")
    left, right = st.columns(2, gap="large")
    with left:
        _section("Pentes (pb)")
        slopes = slopes_table(curves, countries)
        st.dataframe(slopes, hide_index=True, width="stretch", height=_height(slopes))
    with right:
        _section(f"Spreads contre le Bund (pb, variation depuis {since})")
        spreads = spreads_table(curves, countries, since)
        st.dataframe(spreads, hide_index=True, width="stretch", height=_height(spreads))
    _section("Niveaux (%)")
    st.dataframe(levels_table(curves, countries, since), hide_index=True, width="stretch")
    for note in curves.notes:
        st.caption(f"À vérifier : {note}")
    st.download_button(
        "Télécharger le PNG des courbes", curves_png_bytes(curves, countries, comparisons=list(comparisons),
                                                           changes_since=since),
        f"courbes_{curves.as_of.isoformat()}.png", "image/png", key=f"{key}-curves-png",
        icon=":material/download:")


def _height(table: pd.DataFrame) -> int:
    return (len(table) + 1) * 35 + 3


def _section(title: str) -> None:
    st.markdown(f'<div class="mm-section">{title}</div>', unsafe_allow_html=True)


def _bp(value: float, sign: bool = True) -> str:
    return fr_number(value, 1, sign=sign)


def slopes_table(curves: CurveSet, countries: list[str]) -> pd.DataFrame:
    rows = curves.slopes[curves.slopes["country"].isin(countries)]
    out = pd.DataFrame({
        "Pays": [SHORT_LABELS.get(c, c) for c in rows["country"]],
        "Pente": rows["slope"].to_numpy(),
        "Niveau": [_bp(v, sign=True) for v in rows["level"]],
    })
    for label in COMPARISONS:
        out[f"Δ {label}"] = [_bp(v) for v in rows[f"chg_{label}"]]
    return out


def spreads_table(curves: CurveSet, countries: list[str], since: str) -> pd.DataFrame:
    """Country x maturity: ``spread (change)``, bp."""
    return _grid(curves.spreads, countries, since, lambda v: _bp(v, sign=False), "")


def levels_table(curves: CurveSet, countries: list[str], since: str) -> pd.DataFrame:
    """Country x maturity: ``yield % (change bp)``."""
    return _grid(curves.points, countries, since, lambda v: fr_number(v, 3), " %")


def _grid(frame: pd.DataFrame, countries: list[str], since: str, level: Callable[[float], str],
          unit: str) -> pd.DataFrame:
    rows = frame[frame["country"].isin(countries)]
    tenors = sorted(set(rows["tenor"]))
    out = []
    for country in [c for c in countries if c in set(rows["country"])]:
        mine = rows[rows["country"] == country]
        cells = {float(t): (float(v), float(c)) for t, v, c in
                 zip(mine["tenor"], mine["level"], mine[f"chg_{since}"], strict=True)}
        line = {"Pays": SHORT_LABELS.get(country, country)}
        for tenor in tenors:
            value = cells.get(float(tenor))
            line[tenor_label(tenor)] = (f"{level(value[0])}{unit} ({_bp(value[1])})" if value else "–")
        out.append(line)
    return pd.DataFrame(out)
