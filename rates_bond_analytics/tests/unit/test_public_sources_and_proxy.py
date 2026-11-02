"""Public sources (CNBC / Bundesbank / Stooq), proxy swap curves and benchmark sovereign curves."""

from __future__ import annotations

import dataclasses
from datetime import date

import numpy as np
import pandas as pd
import pytest

from rates_analytics import api
from rates_analytics.config import load_settings
from rates_analytics.curves import build_eur_curves
from rates_analytics.curves.interpolation import CurveError
from rates_analytics.curves.nss import NSSParams
from rates_analytics.curves.proxy import (
    ProxyCurveConfig,
    benchmark_bonds,
    complete_with_proxy,
    proxy_curves,
    proxy_swap_quotes,
    term_structure,
)
from rates_analytics.data.mapping import MarketDataMap
from rates_analytics.data.providers.public import (
    PublicProvider,
    parse_cnbc_bars,
    parse_sdmx_csv,
    parse_stooq_csv,
)
from rates_analytics.exceptions import DataProviderError

AS_OF = date(2026, 10, 6)
AAA = NSSParams(0.012, 0.008, -0.01, 2.5, beta3=0.03, tau2=12.0)


# ------------------------------------------------------------------ parsers
def test_parsers():
    cnbc = {"barData": {"priceBars": [{"tradeTime": "20261005000000", "close": "2.61"},
                                      {"tradeTimeinMills": "1791244800000", "close": 2.65},
                                      {"tradeTime": "bad", "close": 1}]}}
    s = parse_cnbc_bars(cnbc, "DE10Y-DE")
    assert list(s.round(2)) == [2.61, 2.65] and s.index[0] == pd.Timestamp("2026-10-05")
    with pytest.raises(DataProviderError):
        parse_cnbc_bars({"oops": 1})
    sdmx = "KEY;TIME_PERIOD;OBS_VALUE\nX;2026-10-05;2.58\nX;2026-10-06;2.6\n"
    assert parse_sdmx_csv(sdmx).iloc[-1] == 2.6 and parse_sdmx_csv("").empty
    assert parse_stooq_csv("Date,Open,High,Low,Close\n2026-10-06,1,1,1,2.62\n").iloc[0] == 2.62
    assert parse_stooq_csv("No data").empty
    with pytest.raises(DataProviderError, match="anti-bot"):
        parse_stooq_csv("<html><script>challenge</script></html>")


def test_mapping_placeholders_for_public_and_bloomberg():
    m = MarketDataMap({"EUR.GOVT.DE.{tenor}": {
        "tenors": ["2Y", "30Y"],
        "public": {"ticker": "cnbc:DE{tenor}-DE|bbk:BBSIS/A.{bbk_tenor}|stooq:{years}dey.b"},
        "bloomberg": {"ticker": "GDBR{years} Index"}}})
    assert m.spec("EUR.GOVT.DE.2Y", "public")["ticker"] == "cnbc:DE2Y-DE|bbk:BBSIS/A.R02XX|stooq:2dey.b"
    assert m.spec("EUR.GOVT.DE.30Y", "bloomberg")["ticker"] == "GDBR30 Index"
    project = MarketDataMap(load_settings().market_data)
    assert project.spec("EUR.GOVT.IT.10Y", "public")["ticker"].startswith("cnbc:IT10Y-IT")


class _Resp:
    def __init__(self, status, text="", payload=None):
        self.status_code, self.text, self._p = status, text, payload

    def json(self):
        if self._p is None:
            raise ValueError("no json")
        return self._p


class _Session:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(url)
        return self.handler(url, params or {})


def _provider(handler):
    mapping = MarketDataMap({"EUR.GOVT.DE.{tenor}": {"tenors": ["2Y", "10Y"], "public": {
        "ticker": "cnbc:DE{tenor}-DE|bbk:BBSIS/D.{bbk_tenor}|stooq:{years}dey.b", "scale": 0.01}}})
    return PublicProvider(mapping, retries=0, session=_Session(handler))


def test_chain_falls_back_and_scales():
    def handler(url, params):
        if "cnbc" in url:
            return _Resp(200, payload={"barData": {"priceBars": []}})        # CNBC: no bar
        if "bundesbank" in url:
            return _Resp(200, "TIME_PERIOD,OBS_VALUE\n2026-10-06,2.55\n")
        raise AssertionError("Stooq must not be called once the Bundesbank answered")
    q = _provider(handler).get_quotes(["EUR.GOVT.DE.10Y"], AS_OF)
    assert q.loc["EUR.GOVT.DE.10Y", "value"] == pytest.approx(0.0255)


def test_circuit_breaker_and_total_failure():
    def handler(url, params):
        if "stooq" in url:
            return _Resp(200, "<html>anti-bot</html>")
        return _Resp(404)
    prov = _provider(handler)
    assert prov.get_history(["EUR.GOVT.DE.2Y", "EUR.GOVT.DE.10Y"], date(2026, 9, 26), AS_OF).empty
    assert sum("stooq" in u for u in prov.http.session.calls) == 1        # anti-bot page: Stooq skipped after

    def down(url, params):
        raise __import__("requests").exceptions.SSLError("certificate verify failed")
    with pytest.raises(DataProviderError, match="public sources failed"):
        _provider(down).get_history(["EUR.GOVT.DE.2Y"], date(2026, 9, 26), AS_OF)
    with pytest.raises(DataProviderError, match="unknown public source"):
        prov.fetch_ticker("yahoo:^TNX", AS_OF, AS_OF)


# ------------------------------------------------------------------- proxy
def test_proxy_curves_shape():
    cfg = ProxyCurveConfig()
    ois, e6 = proxy_curves(AS_OF, 0.0193, AAA, cfg)
    z_estr = np.log1p(0.0193 / 360) * 365
    assert float(ois.zero_rate(1e-4)) == pytest.approx(z_estr, abs=2e-6)        # short end on the €STR fixing
    t = 30.0
    expected = AAA.zero_rate(t) + term_structure(cfg.ois_minus_aaa_bp)(t)
    assert float(ois.zero_rate(t)) == pytest.approx(expected, abs=1e-6)          # long end: AAA + spread
    assert float(e6.zero_rate(10.0) - ois.zero_rate(10.0)) == pytest.approx(10e-4, abs=1e-9)


def test_proxy_quotes_bootstrap_and_recover_the_basis():
    q = proxy_swap_quotes(AS_OF, 0.0193, AAA)
    assert len([k for k in q if ".OIS." in k]) == 27 and len([k for k in q if ".SWAP." in k]) == 16
    curves = build_eur_curves(AS_OF, q)
    b = curves.basis_spreads(("2Y", "10Y", "30Y")).set_index("tenor")["basis_bp"]
    assert b["2Y"] == pytest.approx(17, abs=0.5) and b["10Y"] == pytest.approx(10, abs=0.5)
    assert b["30Y"] == pytest.approx(6, abs=1.0)


def test_complete_with_proxy_rules():
    market = {f"EUR.ESTR.OIS.{t}": 0.02 for t in ("1Y", "2Y", "5Y", "10Y")} | \
             {f"EUR.EURIBOR6M.SWAP.{t}": 0.022 for t in ("2Y", "5Y", "10Y")}
    same, rep = complete_with_proxy(AS_OF, market, {})
    assert same == market and not rep.used                                     # enough market quotes: untouched
    inputs = {"EUR.ESTR.FIX": 0.0193, **{f"EUR.ECB.AAA.NSS.{k.upper()}": v for k, v in
                                         {"beta0": 0.012, "beta1": 0.008, "beta2": -0.01, "beta3": 0.03,
                                          "tau1": 2.5, "tau2": 12.0}.items()}}
    only_ois = {k: v for k, v in market.items() if ".OIS." in k} | {"EUR.EURIBOR6M.FRA.1X7": 0.021}
    out, rep = complete_with_proxy(AS_OF, only_ois, inputs)
    assert rep.used and all(not k.startswith("EUR.ESTR.OIS.") for k in rep.proxied_ids)   # OIS kept from market
    assert out["EUR.ESTR.OIS.10Y"] == 0.02 and "EUR.EURIBOR6M.FRA.1X7" not in out
    assert "PROXY" in rep.summary()
    with pytest.raises(CurveError, match="Svensson"):
        complete_with_proxy(AS_OF, {}, {"EUR.ESTR.FIX": 0.0193})
    with pytest.raises(CurveError, match="disabled"):
        complete_with_proxy(AS_OF, {}, inputs, ProxyCurveConfig(enabled=False))


def test_benchmark_bonds_are_par_and_zero():
    from rates_analytics.conventions.market import get_convention

    bonds, prices = benchmark_bonds(AS_OF, "DE", get_convention("BUND"), {"10Y": 0.026}, {"5Y": 0.022})
    par = next(b for b in bonds if b.isin == "GEN-DE-10Y")
    assert par.yield_from_clean_price(prices[par.isin], par.settlement_date(AS_OF)) == pytest.approx(0.026, abs=1e-9)
    zc = next(b for b in bonds if b.isin == "ZC-DE-5Y")
    assert zc.coupon == 0.0 and prices[zc.isin] == pytest.approx(100 / 1.022 ** 5)


# ---------------------------------------------- free-mode snapshot end to end
@pytest.fixture
def free_like_settings(tmp_path):
    """Static provider holding only what free sources publish (no swap quote, no bond data)."""
    days = pd.bdate_range("2026-09-21", "2026-10-06")
    cols = {"EUR.ESTR.FIX": 0.0193, "EUR.ECB.AAA.NSS.BETA0": 0.012, "EUR.ECB.AAA.NSS.BETA1": 0.008,
            "EUR.ECB.AAA.NSS.BETA2": -0.01, "EUR.ECB.AAA.NSS.BETA3": 0.03, "EUR.ECB.AAA.NSS.TAU1": 2.5,
            "EUR.ECB.AAA.NSS.TAU2": 12.0}
    for cc, add in (("DE", 0.0), ("FR", 0.006), ("IT", 0.011)):
        for t, y in (("2Y", 0.0205), ("5Y", 0.0225), ("10Y", 0.0265), ("30Y", 0.031)):
            cols[f"EUR.GOVT.{cc}.{t}"] = y + add
    for t, z in (("1Y", 0.0195), ("3Y", 0.021), ("7Y", 0.024), ("20Y", 0.030)):
        cols[f"EUR.BBK.DE.ZERO.{t}"] = z
    pd.DataFrame({k: [v] * len(days) for k, v in cols.items()},
                 index=pd.Index(days.date, name="date")).to_csv(tmp_path / "history.csv")
    s = load_settings(overrides={"provider": "static", "fallback": []})
    return dataclasses.replace(s, providers={**s.providers, "static": {"directory": str(tmp_path)}})


def test_snapshot_without_swap_quotes_uses_proxies_and_benchmarks(free_like_settings):
    snap = api.market_snapshot(AS_OF, free_like_settings)
    assert snap.proxy.used and snap.sources["swap curves"] == "proxy" and not snap.warnings
    assert {snap.sources[n] for n in ("BUND", "OAT", "BTP")} == {"benchmarks"}
    assert snap.sources["EURIBOR6M fixings"].startswith("proxy")
    bund, btp = snap.sovereigns["BUND"], snap.sovereigns["BTP"]
    assert float(btp.curve.zero_rate(10.0) - bund.curve.zero_rate(10.0)) * 1e4 == pytest.approx(110, abs=15)
    assert snap.sovereigns["OAT"].rmse_bp < 3.0                                    # NS through 4 generics
    t = api.bond_table(snap, snap.bonds)
    assert len(t) == len(snap.bonds) >= 12
    risk = api.swap_risk(snap, "IRS", date(2026, 10, 8), date(2036, 10, 8), 0.02, 1e7)
    assert risk["dv01"] > 0 and np.isfinite(risk["npv"])
