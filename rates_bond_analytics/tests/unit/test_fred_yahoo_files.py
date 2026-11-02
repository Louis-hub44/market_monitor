"""FRED / FRED API / Yahoo prefixes of the public provider and the CSV / Excel file provider (no network)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from rates_analytics.config import load_settings
from rates_analytics.data import FileProvider, MarketDataMap, MarketDataService, build_market_data_service
from rates_analytics.data.providers.files import extract_series, read_table
from rates_analytics.data.providers.public import (
    PublicProvider,
    SourceUnavailable,
    parse_fred_csv,
    parse_fred_observations,
    parse_yahoo_chart,
)
from rates_analytics.exceptions import DataProviderError

AS_OF = date(2026, 10, 9)


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
        self.calls.append((url, dict(params or {})))
        return self.handler(url, params or {})


YAHOO = {"chart": {"result": [{"meta": {"gmtoffset": 7200},
                               "timestamp": [1791442800, 1791529200],          # 2026-10-08 / 09 07:00 UTC
                               "indicators": {"quote": [{"close": [6126.7, 6180.3]}],
                                              "adjclose": [{"adjclose": [6126.73, 6180.29]}]}}],
                   "error": None}}


# ------------------------------------------------------------------ parsers
def test_fred_csv_and_api_parsers():
    s = parse_fred_csv("observation_date,BAA10Y\n2026-10-07,1.71\n2026-10-08,.\n2026-10-09,1.74\n", "BAA10Y")
    assert list(s) == [1.71, 1.74] and s.index[-1] == pd.Timestamp("2026-10-09")
    assert parse_fred_csv("DATE,VIXCLS\n2026-10-09,15.41\n").iloc[0] == 15.41     # older header
    assert parse_fred_csv("").empty
    with pytest.raises(SourceUnavailable):
        parse_fred_csv("<html>blocked</html>")
    api = parse_fred_observations({"observations": [{"date": "2026-10-08", "value": "3.26"},
                                                    {"date": "2026-10-09", "value": "."}]})
    assert list(api) == [3.26]
    with pytest.raises(DataProviderError):
        parse_fred_observations({"error_message": "bad key"})


def test_yahoo_chart_parser_uses_adjusted_close_and_local_dates():
    s = parse_yahoo_chart(YAHOO, "^STOXX50E")
    assert list(s) == [6126.73, 6180.29]
    assert list(s.index) == [pd.Timestamp("2026-10-08"), pd.Timestamp("2026-10-09")]
    no_adj = {"chart": {"result": [{**YAHOO["chart"]["result"][0], "indicators": {"quote": [{"close": [1.0, None]}]}}]}}
    assert list(parse_yahoo_chart(no_adj)) == [1.0]
    with pytest.raises(DataProviderError, match="refused"):
        parse_yahoo_chart({"chart": {"result": None, "error": {"description": "No data found"}}}, "XX")
    assert parse_yahoo_chart({"chart": {"result": [], "error": None}}).empty


# ------------------------------------------------------------------ chains
def _provider(ticker, handler, key=""):
    mapping = MarketDataMap({"X": {"public": {"ticker": ticker, "scale": 0.01}}})
    return PublicProvider(mapping, retries=0, session=_Session(handler), fred_api_key=key)


def test_fred_then_fred_api_and_scale():
    def handler(url, params):
        if "fredgraph" in url:
            return _Resp(200, "<html>Zscaler: site blocked</html>")
        assert params["api_key"] == "KEY123" and params["series_id"] == "BAMLHE00EHYIOAS"
        return _Resp(200, payload={"observations": [{"date": "2026-10-09", "value": "3.22"}]})
    prov = _provider("fred:BAMLHE00EHYIOAS|fredapi:BAMLHE00EHYIOAS", handler, key="KEY123")
    q = prov.get_quotes(["X"], AS_OF)
    assert q.loc["X", "value"] == pytest.approx(0.0322)


def test_fred_api_without_key_is_skipped_and_key_is_masked():
    prov = _provider("fredapi:BAA10Y", lambda u, p: _Resp(200, payload={}))
    with pytest.raises(DataProviderError, match="FRED_API_KEY"):
        prov.get_history(["X"], date(2026, 10, 1), AS_OF)

    def leak(url, params):
        return _Resp(400, f"bad request api_key={params['api_key']}")
    with pytest.raises(DataProviderError) as exc:
        _provider("fredapi:BAA10Y", leak, key="SECRETKEY99").get_history(["X"], date(2026, 10, 1), AS_OF)
    assert "SECRETKEY99" not in str(exc.value)


def test_yahoo_prefix_quotes_symbol_and_window():
    seen = {}

    def handler(url, params):
        seen.update(url=url, **params)
        return _Resp(200, payload=YAHOO)
    prov = _provider("yahoo:^STOXX50E", handler)
    prov.urls["yahoo"] = "https://y.test/chart"
    h = prov.get_history(["X"], date(2026, 10, 9), AS_OF)
    assert seen["url"].endswith("/%5ESTOXX50E") and seen["interval"] == "1d"
    assert list(h["X"].round(4)) == [61.8029]                     # window applied, scale 0.01


def test_yahoo_html_answer_breaks_the_source():
    prov = _provider("yahoo:^GSPC|stooq:^spx", lambda u, p: _Resp(200, "<html>consent</html>")
                     if "chart" in u else _Resp(200, "Date,Open,High,Low,Close\n2026-10-09,1,1,1,7765.36\n"))
    s, used = prov.fetch_ticker("yahoo:^GSPC|stooq:^spx", date(2026, 10, 1), AS_OF)
    assert used == "stooq" and s.iloc[-1] == 7765.36


# ------------------------------------------------------------------ files
BANNERS = (
    "date,instrument,niveau,variation,source\n"
    "2026-10-08,HY Europe OAS,342,11,ICE BofA via FRED (20/10)\n"
    "2026-10-09,HY Europe OAS,322,7,ICE BofA via FRED (07/10)\n"
    "2026-10-09,Allemagne 10Y,3.494,1,Investing\n"
    "2026-10-09,HY Europe OAS,324,7,correction du jour\n"
    "2026-10-12,OAT-Bund 10Y,138,-2,Calculé\n"
)


def test_long_banner_file_is_read_with_corrections(tmp_path):
    (tmp_path / "bandeaux.csv").write_text(BANNERS, encoding="utf-8")
    mapping = MarketDataMap({
        "EUR.HY.OAS": {"file": {"path": "bandeaux.csv", "column": "HY Europe OAS", "scale": 0.0001}},
        "EUR.GOVT.DE.10Y": {"file": {"path": "bandeaux.csv", "column": "Allemagne 10Y", "scale": 0.01}},
        "NOT.A.FILE.ID": {"public": {"ticker": "stooq:x"}},
    })
    prov = FileProvider(mapping, root=tmp_path)
    h = prov.get_history(["EUR.HY.OAS", "EUR.GOVT.DE.10Y", "NOT.A.FILE.ID"], date(2026, 10, 1), date(2026, 10, 31))
    assert list(h.columns) == ["EUR.HY.OAS", "EUR.GOVT.DE.10Y"]
    assert h.loc["2026-10-09", "EUR.HY.OAS"] == pytest.approx(0.0324)       # later row of the day wins
    assert h.loc["2026-10-09", "EUR.GOVT.DE.10Y"] == pytest.approx(0.03494)
    assert prov.get_quotes(["EUR.HY.OAS"], date(2026, 10, 12)).loc["EUR.HY.OAS", "obs_date"] == date(2026, 10, 9)


def test_wide_semicolon_decimal_comma_and_day_first(tmp_path):
    (tmp_path / "export.csv").write_text("Date;iTraxx Main;iTraxx Xover\n08/10/2026;61,5;290,25\n09/10/2026;62;295\n",
                                         encoding="utf-8")
    s = extract_series(read_table(tmp_path / "export.csv"), "iTraxx Xover")
    assert list(s) == [290.25, 295.0] and s.index[0] == pd.Timestamp("2026-10-08")


def test_excel_sheet(tmp_path):
    pytest.importorskip("openpyxl")
    pd.DataFrame({"date": ["2026-10-08", "2026-10-09"], "Main": [61.5, 62.0]}).to_excel(
        tmp_path / "credit.xlsx", sheet_name="itraxx", index=False)
    mapping = MarketDataMap({"ITRX.MAIN": {"file": {"path": "credit.xlsx", "sheet": "itraxx", "column": "Main"}}})
    h = FileProvider(mapping, root=tmp_path).get_history(["ITRX.MAIN"], date(2026, 10, 1), AS_OF)
    assert list(h["ITRX.MAIN"]) == [61.5, 62.0]


def test_file_errors_are_reported(tmp_path):
    mapping = MarketDataMap({"A": {"file": {"path": "absent.csv", "column": "x"}},
                             "B": {"file": {"path": "absent.csv"}}})
    with pytest.raises(DataProviderError, match="not found"):
        FileProvider(mapping, root=tmp_path).get_history(["A", "B"], date(2026, 10, 1), AS_OF)


def test_file_provider_in_the_configured_chain(tmp_path):
    (tmp_path / "bandeaux.csv").write_text(BANNERS, encoding="utf-8")
    settings = load_settings(overrides={"provider": "static", "fallback": ["file"]})
    assert "file" in [settings.data.provider, *settings.data.fallback]
    mapping = MarketDataMap({"EUR.HY.OAS": {"file": {"path": str(tmp_path / "bandeaux.csv"),
                                                     "column": "HY Europe OAS", "scale": 0.0001}}})
    svc = build_market_data_service(settings, mapping=mapping)
    assert isinstance(svc, MarketDataService) and [p.name for p in svc.providers] == ["static", "file"]
    h = svc.history(["EUR.HY.OAS"], date(2026, 10, 1), date(2026, 10, 31))
    assert h["EUR.HY.OAS"].dropna().iloc[-1] == pytest.approx(0.0324)
    assert svc.last_report.served_by["EUR.HY.OAS"] == "file"
