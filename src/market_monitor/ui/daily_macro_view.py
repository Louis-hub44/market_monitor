"""Daily macro: preview of the export and downloads (Excel, PNG, text)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import streamlit as st

from market_monitor.alerts import AlertRule
from market_monitor.config import UiSettings
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
from market_monitor.ui.caching import cached, refresh_nonce

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
