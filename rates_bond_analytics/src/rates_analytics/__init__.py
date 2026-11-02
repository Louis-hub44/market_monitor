"""Rates & Bond Analytics – module of the home-made market terminal.

Layers (the engine never imports Streamlit):

* :mod:`rates_analytics.conventions` – day counts, calendars, schedules, market conventions;
* :mod:`rates_analytics.data` – abstract market-data layer (ECB / Bundesbank / Stooq / CNBC / FMP / files) + parquet cache;
* :mod:`rates_analytics.curves` – €STR OIS and EURIBOR 6M dual-curve bootstrap, NSS sovereign fits;
* :mod:`rates_analytics.bonds` – YAS analytics, G / I / Z / ASW spreads;
* :mod:`rates_analytics.swaps` – SWPM pricer and bucketed risk;
* :mod:`rates_analytics.scenarios` – curve scenarios and portfolio P&L;
* :mod:`rates_analytics.api` – stable integration API for the other terminal modules;
* :mod:`rates_analytics.network` – corporate SSL-proxy policy shared by the REST providers.
"""

__version__ = "1.1.0"
