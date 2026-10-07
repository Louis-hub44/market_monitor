"""Market Monitor - écran d'accueil multi-actifs du mini terminal maison.

Entry points for the terminal (lazy, so ``import market_monitor`` stays cheap)::

    from market_monitor import MarketMonitor, load_settings
    monitor = MarketMonitor.from_settings(load_settings())

The Streamlit page lives in ``market_monitor.ui.page.render_market_monitor``.
"""

from __future__ import annotations

from typing import Any

__version__ = "1.5.3"
__all__ = ["MarketMonitor", "load_settings", "__version__"]


def __getattr__(name: str) -> Any:
    if name == "MarketMonitor":
        from market_monitor.monitor import MarketMonitor

        return MarketMonitor
    if name == "load_settings":
        from market_monitor.config import load_settings

        return load_settings
    raise AttributeError(f"module 'market_monitor' has no attribute {name!r}")
