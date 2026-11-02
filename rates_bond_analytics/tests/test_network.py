"""Live smoke tests against the real APIs (skipped by default).

Run them on your machine with:  pytest -m network
Behind a corporate proxy, set the SSL policy first, e.g. ``RATES_NETWORK_MODE=system`` in .env.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

from rates_analytics.config import load_settings
from rates_analytics.data import EcbProvider, FmpProvider, MarketDataMap, PublicProvider
from rates_analytics.network import build_session, check_connectivity

pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def mapping() -> MarketDataMap:
    return MarketDataMap(load_settings().market_data)


@pytest.fixture(scope="module")
def session():
    return build_session(load_settings().network)


def test_connectivity_diagnostic():
    diag = check_connectivity(load_settings().network)
    assert diag["kind"] in ("ok", "partial"), diag["message"]


def test_ecb_estr_and_aaa_curve(mapping, session):
    prov = EcbProvider(mapping, session=session)
    end = date.today()
    q = prov.get_quotes(["EUR.ESTR.FIX", "EUR.ECB.AAA.SPOT.10Y"], end)
    assert {"EUR.ESTR.FIX", "EUR.ECB.AAA.SPOT.10Y"} <= set(q.index)
    assert -0.01 < q.loc["EUR.ESTR.FIX", "value"] < 0.10


def test_fmp_treasuries(mapping):
    key = os.environ.get("FMP_API_KEY") or load_settings().provider_options("fmp").get("api_key")
    if not key:
        pytest.skip("FMP_API_KEY not set")
    prov = FmpProvider(mapping, api_key=key)
    h = prov.get_history(["USD.UST.CMT.10Y"], date.today() - timedelta(days=15), date.today())
    assert not h.empty and 0.0 < h.iloc[-1, 0] < 0.15


def test_public_generic_yields(mapping, session):
    prov = PublicProvider(mapping, session=session)
    q = prov.get_quotes(["EUR.GOVT.DE.10Y", "EUR.GOVT.IT.10Y"], date.today())
    assert 0.0 < q.loc["EUR.GOVT.DE.10Y", "value"] < q.loc["EUR.GOVT.IT.10Y", "value"] < 0.10


def test_free_snapshot_end_to_end():
    from rates_analytics import api

    s = load_settings(overrides={"provider": "free", "fallback": ["public"]})
    snap = api.market_snapshot(date.today() - timedelta(days=1), s)
    assert snap.curves is not None and set(snap.sovereigns) >= {"BUND"}
