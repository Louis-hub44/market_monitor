"""Tests of the market-data layer (no network: HTTP and Bloomberg are faked)."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

import pandas as pd
import pytest

from rates_analytics.config import load_settings
from rates_analytics.conventions.period import Period
from rates_analytics.data import (
    BloombergProvider,
    EcbProvider,
    FmpProvider,
    MarketDataMap,
    MarketDataService,
    ParquetCache,
    StaticProvider,
    build_market_data_service,
)
from rates_analytics.data.mapping import bbg_tenor_code, ecb_tenor_code, fmp_tenor_code
from rates_analytics.exceptions import (
    ConfigurationError,
    DataProviderError,
    MissingDataError,
)

TODAY = date(2026, 10, 7)

MAPPING_RAW = {
    "EUR.ESTR.FIX": {"ecb": {"key": "EST/B.EU000A2X2A25.WT", "scale": 0.01},
                     "bloomberg": {"ticker": "ESTRON Index", "scale": 0.01}},
    "EUR.ESTR.OIS.{tenor}": {"tenors": ["6M", "1Y", "18M", "10Y"],
                             "bloomberg": {"ticker": "EESWE{bbg_tenor} Curncy", "scale": 0.01}},
    "USD.UST.CMT.{tenor}": {"tenors": ["3M", "10Y"],
                            "fmp": {"endpoint": "treasury-rates", "field": "{fmp_tenor}", "scale": 0.01}},
}


@pytest.fixture
def mapping() -> MarketDataMap:
    return MarketDataMap(MAPPING_RAW)


# ------------------------------------------------------------------ fakes
class FakeResponse:
    def __init__(self, status: int, text: str = "", payload=None):
        self.status_code, self.text, self._payload = status, text, payload

    def json(self):
        return self._payload


class FakeSession:
    """Records calls and replays scripted responses."""

    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return self.handler(url, params or {})


class FakeBlp:
    def __init__(self):
        self.ref_calls, self.hist_calls = [], []

    def reference(self, securities, fields):
        self.ref_calls.append((list(securities), list(fields)))
        data = {
            "EESWE10 Curncy": {"PX_LAST": 2.45},
            "EESWE1 Curncy": {"PX_LAST": 1.95},
            "/isin/DE0001102580": {"SECURITY_DES": "DBR 0 02/15/32", "CNTRY_OF_RISK": "DE", "CRNCY": "EUR",
                                   "CPN": 0.0, "CPN_FREQ": 1, "DAY_CNT_DES": "ACT/ACT",
                                   "ISSUE_DT": date(2022, 1, 12), "INT_ACC_DT": date(2022, 1, 12),
                                   "FIRST_CPN_DT": date(2023, 2, 15), "MATURITY": date(2032, 2, 15),
                                   "PX_BID": 84.10, "PX_ASK": 84.14, "YLD_YTM_MID": 2.61},
        }
        return {s: {f: v for f, v in data[s].items() if f in fields} for s in securities if s in data}

    def historical(self, securities, fields, start, end):
        self.hist_calls.append((list(securities), list(fields), start, end))
        idx = pd.date_range(start, end, freq="B")
        return {s: pd.DataFrame(dict.fromkeys(fields, 2.0), index=idx) for s in securities}

    def close(self):
        pass


# ---------------------------------------------------------------- mapping
def test_tenor_codes():
    assert bbg_tenor_code(Period.parse("6M")) == "F"
    assert bbg_tenor_code(Period.parse("18M")) == "1F"
    assert bbg_tenor_code(Period.parse("10Y")) == "10"
    assert ecb_tenor_code(Period.parse("18M")) == "1Y6M"
    assert ecb_tenor_code(Period.parse("3M")) == "3M"
    assert fmp_tenor_code(Period.parse("3M")) == "month3"
    assert fmp_tenor_code(Period.parse("10Y")) == "year10"


def test_mapping_expansion(mapping):
    assert len(mapping) == 1 + 4 + 2
    assert mapping.spec("EUR.ESTR.OIS.18M", "bloomberg")["ticker"] == "EESWE1F Curncy"
    assert mapping.spec("USD.UST.CMT.10Y", "fmp")["field"] == "year10"
    assert mapping.spec("EUR.ESTR.OIS.1Y", "ecb") is None
    assert mapping.ids("EUR.ESTR.OIS") == ["EUR.ESTR.OIS.10Y", "EUR.ESTR.OIS.18M", "EUR.ESTR.OIS.1Y", "EUR.ESTR.OIS.6M"]


def test_mapping_errors():
    with pytest.raises(ConfigurationError):
        MarketDataMap({"X.{tenor}": {"bloomberg": {"ticker": "A{tenor}"}}})       # no tenors list
    with pytest.raises(ConfigurationError):
        MarketDataMap({"X.{tenor}": {"tenors": ["1Y"], "bloomberg": {"ticker": "{oops}"}}})


# ----------------------------------------------------------------- config
def test_load_settings_defaults_and_env(tmp_path, monkeypatch):
    cfg = tmp_path / "config" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("data:\n  provider: fmp\n  fallback: [free, fmp]\n  cache: {directory: cache}\n"
                   "providers:\n  fmp: {api_key: '${MY_KEY}'}\n", encoding="utf-8")
    monkeypatch.setenv("MY_KEY", "secret123")
    s = load_settings(cfg, env_file=tmp_path / "none.env")
    assert s.data.provider == "fmp" and s.data.fallback == ("free",)
    assert s.provider_options("fmp")["api_key"] == "secret123"
    assert s.data.cache.directory == (tmp_path / "cache").resolve()


@pytest.mark.parametrize("yaml_text", [
    "data: {provider: reuters}", "data: {missing_policy: ignore}", "data: {fallback: [bbg]}",
    "data: {plausible_rate_range: [0.3, -0.05]}", "data: [unclosed",
])
def test_load_settings_rejects_invalid(tmp_path, yaml_text):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_settings(cfg, env_file=tmp_path / "none.env")


def test_load_settings_missing_explicit_file(tmp_path):
    with pytest.raises(ConfigurationError):
        load_settings(tmp_path / "nope.yaml")


def test_project_config_is_valid():
    s = load_settings()
    m = MarketDataMap(s.market_data)
    assert "EUR.ESTR.OIS.10Y" in m and "EUR.ECB.AAA.SPOT.10Y" in m and "USD.UST.CMT.10Y" in m


# ------------------------------------------------------------------ cache
def test_parquet_cache_series_roundtrip_and_ttl(tmp_path):
    cache = ParquetCache(tmp_path, ttl_hours_live=4)
    s = pd.Series([0.019, 0.0192], index=pd.to_datetime(["2026-10-01", "2026-10-02"]))
    cache.put_series("ecb", "EUR.ESTR.FIX", s, date(2026, 9, 30), date(2026, 10, 2))
    got = cache.get_series("ecb", "EUR.ESTR.FIX", date(2026, 10, 1), date(2026, 10, 2), today=TODAY)
    assert list(got.values) == [0.019, 0.0192]
    assert cache.get_series("ecb", "EUR.ESTR.FIX", date(2026, 9, 1), date(2026, 10, 2), today=TODAY) is None
    # merge an overlapping window
    s2 = pd.Series([0.0193], index=pd.to_datetime(["2026-10-05"]))
    cache.put_series("ecb", "EUR.ESTR.FIX", s2, date(2026, 10, 2), date(2026, 10, 5))
    assert len(cache.get_series("ecb", "EUR.ESTR.FIX", date(2026, 9, 30), date(2026, 10, 5), today=TODAY)) == 3


def test_parquet_cache_live_window_expires(tmp_path):
    cache = ParquetCache(tmp_path, ttl_hours_live=0)
    s = pd.Series([0.02], index=pd.to_datetime(["2026-10-07"]))
    cache.put_series("ecb", "X", s, date(2026, 10, 7), date(2026, 10, 7))
    assert cache.get_series("ecb", "X", date(2026, 10, 7), date(2026, 10, 7), today=TODAY) is None
    assert cache.get_series("ecb", "X", date(2026, 10, 7), date(2026, 10, 7), today=date(2026, 10, 8)) is not None


def test_parquet_cache_tables(tmp_path):
    cache = ParquetCache(tmp_path)
    t1 = pd.DataFrame({"value": [1.0]}, index=pd.Index(["A"], name="id"))
    cache.upsert_table("p", "quotes", "2026-10-07", t1)
    cache.upsert_table("p", "quotes", "2026-10-07", pd.DataFrame({"value": [2.0, 3.0]}, index=["A", "B"]))
    t = cache.get_table("p", "quotes", "2026-10-07")
    assert t.loc["A", "value"] == 2.0 and t.loc["B", "value"] == 3.0
    assert cache.get_table("p", "quotes", "2026-10-07", ttl=timedelta(seconds=-1)) is None
    assert cache.clear("p") == 2


# --------------------------------------------------------------------- ECB
ECB_CSV = ("KEY,FREQ,TIME_PERIOD,OBS_VALUE\n"
           "EST.B.EU000A2X2A25.WT,B,2026-10-01,1.928\n"
           "EST.B.EU000A2X2A25.WT,B,2026-10-02,1.931\n")


def test_ecb_provider_parses_csv(mapping):
    sess = FakeSession(lambda url, params: FakeResponse(200, ECB_CSV))
    prov = EcbProvider(mapping, session=sess)
    df = prov.get_history(["EUR.ESTR.FIX", "EUR.ESTR.OIS.1Y"], date(2026, 10, 1), date(2026, 10, 2))
    assert list(df.columns) == ["EUR.ESTR.FIX"]
    assert df["EUR.ESTR.FIX"].iloc[-1] == pytest.approx(0.01931)
    url, params = sess.calls[0]
    assert url.endswith("/EST/B.EU000A2X2A25.WT") and params["format"] == "csvdata"
    q = prov.get_quotes(["EUR.ESTR.FIX"], date(2026, 10, 4))
    assert q.loc["EUR.ESTR.FIX", "obs_date"] == date(2026, 10, 2)


def test_ecb_provider_404_is_empty_and_500_raises(mapping, monkeypatch):
    prov = EcbProvider(mapping, session=FakeSession(lambda u, p: FakeResponse(404)))
    assert prov.get_history(["EUR.ESTR.FIX"], date(2026, 10, 1), date(2026, 10, 2)).empty
    monkeypatch.setattr("time.sleep", lambda s: None)
    prov = EcbProvider(mapping, session=FakeSession(lambda u, p: FakeResponse(503)), retries=1)
    with pytest.raises(DataProviderError):
        prov.get_history(["EUR.ESTR.FIX"], date(2026, 10, 1), date(2026, 10, 2))


# --------------------------------------------------------------------- FMP
def _fmp_handler(url, params):
    start, end = date.fromisoformat(params["from"]), date.fromisoformat(params["to"])
    days = pd.bdate_range(start, end)
    return FakeResponse(200, payload=[{"date": d.strftime("%Y-%m-%d"), "month3": 3.9, "year10": 4.2}
                                      for d in reversed(days)])


def test_fmp_provider_chunks_and_scales(mapping):
    sess = FakeSession(_fmp_handler)
    prov = FmpProvider(mapping, api_key="KEY", session=sess, max_days_per_call=30)
    df = prov.get_history(["USD.UST.CMT.10Y", "USD.UST.CMT.3M"], date(2026, 7, 1), date(2026, 9, 30))
    assert len(sess.calls) == 4                       # 92 days / 30-day windows
    assert df["USD.UST.CMT.10Y"].iloc[0] == pytest.approx(0.042)
    assert df.index.is_monotonic_increasing


def test_fmp_masks_api_key_and_requires_it(mapping):
    with pytest.raises(ConfigurationError):
        FmpProvider(mapping, api_key="")
    sess = FakeSession(lambda u, p: FakeResponse(200, payload={"Error Message": "Invalid API KEY: SECRETKEY"}))
    prov = FmpProvider(mapping, api_key="SECRETKEY", session=sess)
    with pytest.raises(DataProviderError) as exc:
        prov.get_history(["USD.UST.CMT.10Y"], date(2026, 10, 1), date(2026, 10, 2))
    assert "SECRETKEY" not in str(exc.value)
    sess = FakeSession(lambda u, p: FakeResponse(401, text="bad key SECRETKEY"))
    with pytest.raises(DataProviderError) as exc:
        FmpProvider(mapping, api_key="SECRETKEY", session=sess).get_history(
            ["USD.UST.CMT.10Y"], date(2026, 10, 1), date(2026, 10, 2))
    assert "SECRETKEY" not in str(exc.value)


# --------------------------------------------------------------- Bloomberg
def test_bloomberg_live_quotes_and_history(mapping):
    blp = FakeBlp()
    prov = BloombergProvider(mapping, client=blp, today=lambda: TODAY)
    q = prov.get_quotes(["EUR.ESTR.OIS.10Y", "EUR.ESTR.OIS.1Y", "USD.UST.CMT.10Y"], TODAY)
    assert q.loc["EUR.ESTR.OIS.10Y", "value"] == pytest.approx(0.0245)
    assert "USD.UST.CMT.10Y" not in q.index
    hist = prov.get_history(["EUR.ESTR.OIS.10Y"], date(2026, 9, 28), date(2026, 10, 2))
    assert len(hist) == 5 and hist.iloc[0, 0] == pytest.approx(0.02)
    past = prov.get_quotes(["EUR.ESTR.OIS.10Y"], date(2026, 10, 2))      # history-based
    assert past.loc["EUR.ESTR.OIS.10Y", "obs_date"] == date(2026, 10, 2)


def test_bloomberg_bond_static_and_prices(mapping):
    prov = BloombergProvider(mapping, client=FakeBlp(), today=lambda: TODAY)
    st = prov.get_bond_static(["DE0001102580", "XX0000000000"])
    assert list(st.index) == ["DE0001102580"]
    assert st.loc["DE0001102580", "maturity"] == date(2032, 2, 15)
    assert st.loc["DE0001102580", "frequency"] == 1
    px = prov.get_bond_prices(["DE0001102580"], TODAY)
    assert px.loc["DE0001102580", "mid"] == pytest.approx(84.12)
    assert px.loc["DE0001102580", "yield_mid"] == pytest.approx(0.0261)


def test_bloomberg_without_blpapi_raises_configuration_error(mapping):
    prov = BloombergProvider(mapping)
    try:
        import blpapi  # noqa: F401
        pytest.skip("blpapi installed")
    except ImportError:
        with pytest.raises(ConfigurationError):
            prov.get_quotes(["EUR.ESTR.OIS.10Y"], date.today())


# ----------------------------------------------------------------- service
def _static(values: dict[str, list[float]], dates=("2026-10-01", "2026-10-02")) -> StaticProvider:
    return StaticProvider(pd.DataFrame(values, index=pd.to_datetime(list(dates))))


def test_service_fallback_chain_and_report(tmp_path):
    primary = _static({"A": [0.01, 0.011]})
    backup = _static({"A": [0.5, 0.5], "B": [0.02, 0.021]})
    svc = MarketDataService([primary, backup], cache=None, today=lambda: TODAY)
    q = svc.quotes(["A", "B"], date(2026, 10, 5))
    assert q.loc["A", "value"] == pytest.approx(0.011)     # primary wins
    assert q.loc["B", "value"] == pytest.approx(0.021)     # filled by fallback
    assert svc.last_report.served_by == {"A": "static", "B": "static"}
    assert svc.last_report.ok


@pytest.mark.parametrize("policy", ["warn", "drop", "raise"])
def test_service_missing_policies(policy, caplog):
    svc = MarketDataService([_static({"A": [0.01, 0.011]})], missing_policy=policy, today=lambda: TODAY)
    if policy == "raise":
        with pytest.raises(MissingDataError) as exc:
            svc.history(["A", "Z"], date(2026, 10, 1), date(2026, 10, 2))
        assert exc.value.missing == ["Z"]
        return
    with caplog.at_level(logging.WARNING):
        h = svc.history(["A", "Z"], date(2026, 10, 1), date(2026, 10, 2))
    if policy == "warn":
        assert list(h.columns) == ["A", "Z"] and h["Z"].isna().all()
        assert "Z" in caplog.text
    else:
        assert list(h.columns) == ["A"]
    assert svc.last_report.missing == ["Z"]


def test_service_flags_stale_and_suspicious():
    old = _static({"A": [0.01, 0.02]}, dates=("2026-09-01", "2026-09-02"))
    old.quote_lookback_days = 60
    svc = MarketDataService([old], today=lambda: TODAY)
    svc.quotes(["A"], TODAY)
    assert svc.last_report.stale == {"A": date(2026, 9, 2)}
    svc = MarketDataService([_static({"A": [0.01, 0.45]})], today=lambda: TODAY)
    q = svc.quotes(["A"], date(2026, 10, 3))
    assert q.loc["A", "value"] == pytest.approx(0.45)
    assert svc.last_report.suspicious == {"A": 0.45}


def test_service_uses_cache_on_second_call(tmp_path, mapping):
    sess = FakeSession(lambda url, params: FakeResponse(200, ECB_CSV))
    svc = MarketDataService([EcbProvider(mapping, session=sess)], cache=ParquetCache(tmp_path),
                            today=lambda: TODAY)
    h1 = svc.history(["EUR.ESTR.FIX"], date(2026, 10, 1), date(2026, 10, 2))
    h2 = svc.history(["EUR.ESTR.FIX"], date(2026, 10, 1), date(2026, 10, 2))
    assert len(sess.calls) == 1 and h1.equals(h2)
    assert svc.last_report.cache_hits == ["EUR.ESTR.FIX"]


def test_service_provider_failure_falls_back(mapping, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    broken = EcbProvider(mapping, session=FakeSession(lambda u, p: FakeResponse(500)), retries=0)
    backup = _static({"EUR.ESTR.FIX": [0.019, 0.0193]})
    svc = MarketDataService([broken, backup], today=lambda: TODAY)
    h = svc.history(["EUR.ESTR.FIX"], date(2026, 10, 1), date(2026, 10, 2))
    assert h["EUR.ESTR.FIX"].iloc[-1] == pytest.approx(0.0193)
    assert svc.last_report.served_by["EUR.ESTR.FIX"] == "static" and svc.last_report.ok


def test_service_bond_static_and_prices_via_cache(tmp_path, mapping):
    blp = FakeBlp()
    svc = MarketDataService([BloombergProvider(mapping, client=blp, today=lambda: TODAY)],
                            cache=ParquetCache(tmp_path), today=lambda: TODAY)
    s1 = svc.bond_static(["DE0001102580"])
    s2 = svc.bond_static(["DE0001102580"])
    assert len(blp.ref_calls) == 1
    assert s2.loc["DE0001102580", "maturity"] == s1.loc["DE0001102580", "maturity"] == date(2032, 2, 15)
    px = svc.bond_prices(["DE0001102580"], TODAY)
    assert px.loc["DE0001102580", "mid"] == pytest.approx(84.12)


def test_service_validates_inputs():
    svc = MarketDataService([_static({"A": [0.01, 0.011]})])
    with pytest.raises(ValueError):
        svc.history(["A"], date(2026, 10, 2), date(2026, 10, 1))
    with pytest.raises(ValueError):
        svc.quotes([""], TODAY)
    with pytest.raises(ValueError):
        MarketDataService([])


def test_factory_builds_chain(tmp_path, monkeypatch):
    cfg = tmp_path / "config" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({
        "data": {"provider": "free", "fallback": ["fmp"], "cache": {"directory": "c"}},
        "market_data": MAPPING_RAW,
    }), encoding="utf-8")
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    svc = build_market_data_service(load_settings(cfg, env_file=tmp_path / "none.env"))
    assert [p.name for p in svc.providers] == ["ecb"]       # FMP skipped: no key
    monkeypatch.setenv("FMP_API_KEY", "k")
    svc = build_market_data_service(load_settings(cfg, env_file=tmp_path / "none.env"))
    assert [p.name for p in svc.providers] == ["ecb", "fmp"]
    assert svc.cache is not None
