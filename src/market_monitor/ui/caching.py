"""Streamlit-side caching helpers shared by the views."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import streamlit as st

from market_monitor.config import UiSettings


def cached(fn: Callable[..., Any], ui: UiSettings, spinner: str) -> Callable[..., Any]:
    """Wrap ``fn`` with ``st.cache_data`` (TTL from settings); identity if TTL is 0.

    Arguments whose name starts with ``_`` (the monitor) are excluded from the key, so
    callers pass ``id(monitor)`` and a refresh nonce explicitly.
    """
    if not ui.cache_ttl_minutes:
        return fn
    return st.cache_data(ttl=timedelta(minutes=ui.cache_ttl_minutes), show_spinner=spinner)(fn)


def refresh_nonce(key: str) -> int:
    """Counter bumped by the "refresh" button: changing it invalidates cached results."""
    return int(st.session_state.get(f"{key}-nonce", 0))


def bump_refresh_nonce(key: str) -> None:
    st.session_state[f"{key}-nonce"] = refresh_nonce(key) + 1
