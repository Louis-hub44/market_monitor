"""Streamlit-side caching helpers shared by the views."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import streamlit as st

from market_monitor.config import UiSettings

#: results kept per view function in manual-refresh mode (dates, instruments, periods...)
CACHE_ENTRIES = 200


def cached(fn: Callable[..., Any], ui: UiSettings, spinner: str) -> Callable[..., Any]:
    """Wrap ``fn`` with ``st.cache_data``; identity if ``cache_ttl_minutes`` is 0.

    ``ui.refresh: manual``: no expiry, a result is recomputed only after "Actualiser les
    données" (refresh nonce); ``auto``: expires after ``cache_ttl_minutes``.

    Arguments whose name starts with ``_`` (the monitor) are excluded from the key, so
    callers pass ``id(monitor)`` and a refresh nonce explicitly.
    """
    if not ui.cache_ttl_minutes:
        return fn
    if ui.refresh == "manual":  # kept until "Actualiser les données" (new nonce) - no time limit
        return st.cache_data(ttl=None, max_entries=CACHE_ENTRIES, show_spinner=spinner)(fn)
    return st.cache_data(ttl=timedelta(minutes=ui.cache_ttl_minutes), show_spinner=spinner)(fn)


def refresh_nonce(key: str) -> int:
    """Counter bumped by the "refresh" button: changing it invalidates cached results."""
    return int(st.session_state.get(f"{key}-nonce", 0))


def bump_refresh_nonce(key: str) -> None:
    st.session_state[f"{key}-nonce"] = refresh_nonce(key) + 1
