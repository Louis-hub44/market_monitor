"""Daily macro: preview of the export and downloads (Excel, PNG, text)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import pandas as pd
import streamlit as st

from market_monitor.alerts import AlertRule
from market_monitor.config import UiSettings
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
from market_monitor.monitor import MarketMonitor
from market_monitor.text_format import fr_number
from market_monitor.ui.caching import bump_refresh_nonce, cached, refresh_nonce

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
    st.markdown("**Texte prêt à coller**")
    st.code(bundle.text, language=None, wrap_lines=True)


def _manual_entry(monitor: MarketMonitor, layout: DailyMacroLayout, as_of: date, key: str) -> None:
    """Form for the lines without a reachable source (iTraxx outside Bloomberg)."""
    store = monitor.manual_quotes()
    ids = monitor.manual_ids(layout.section_ids)
    if store is None or not ids:
        return
    frame = store.frame()
    day = previous_business_day(as_of)
    missing = [i for i in ids if not _entered(frame, _ticker(monitor, i), day)]
    label = "Saisie manuelle" + (f" — {len(missing)} à saisir pour le {day:%d/%m}" if missing else "")
    with st.expander(label, icon=":material/edit_note:", expanded=bool(missing)):
        st.caption("Cotations sans source accessible (ex. iTraxx hors Bloomberg) : niveau de clôture "
                   "lu sur écran ou note broker. Bloomberg reste prioritaire lorsqu'il est connecté.")
        with st.form(f"{key}-manual"):
            when = st.date_input("Date de clôture", value=day, format="DD/MM/YYYY", key=f"{key}-mq-date")
            values: dict[str, float | None] = {}
            for col, instrument_id in zip(st.columns(len(ids)), ids, strict=True):
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
