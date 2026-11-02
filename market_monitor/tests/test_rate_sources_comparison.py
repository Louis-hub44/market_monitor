"""v1.6.3: diagnostic-taux compares CNBC with Stooq and the Bundesbank (one session late?)."""

from datetime import date

import pandas as pd

from market_monitor.diagnostics import lag_verdict, source_comparison_report

DAYS = pd.bdate_range("2026-10-19", "2026-10-27")
CLOSES = pd.Series([4.95, 4.918, 4.994, 5.089, 5.019, 4.907, 4.924], index=DAYS)


def test_same_day_sources_agree():
    cnbc = CLOSES + 0.004
    assert lag_verdict(cnbc, CLOSES).startswith("CNBC concorde au jour près")


def test_one_session_late_is_named():
    late = CLOSES.shift(1).dropna()              # CNBC's value for J is the close of J-1
    assert "EN RETARD D'UNE SÉANCE" in lag_verdict(late, CLOSES)


def test_few_common_sessions():
    assert "pas assez" in lag_verdict(CLOSES.head(2), CLOSES)


class _Response:
    def __init__(self, payload=None, text=""):
        self._payload, self.text, self.status_code = payload, text, 200

    def json(self):
        return self._payload


class _Session:
    def get(self, url, params=None, headers=None, timeout=None):
        if "cnbc" in url:
            bars = [{"tradeTime": f"{d:%Y%m%d}000000", "close": str(v)}
                    for d, v in CLOSES.shift(1).dropna().items()]
            return _Response({"barData": {"priceBars": bars}})
        if "stooq" in url:
            rows = "\n".join(f"{d:%Y-%m-%d},{v},{v},{v},{v}" for d, v in CLOSES.items())
            return _Response(text="Date,Open,High,Low,Close\n" + rows)
        return _Response(text="")


def test_report_shows_raw_stamps_and_verdict():
    lines = source_comparison_report(_Session(), date(2026, 10, 28))
    text = "\n".join(lines)
    assert "tradeTime=20261027000000 -> classé au Tue 27/10 ; close=4.907" in text
    assert "EN RETARD D'UNE SÉANCE" in text
    assert "Stooq" in text and "Bund 10 ans" in text and "OAT 10 ans" in text


# ------------------------------------------------------------- v1.6.4 cross-check
def test_stooq_and_cnbc_quotes_are_checked_to_the_basis_point():
    from market_monitor.config import QualitySettings
    from market_monitor.data.service import MarketDataService
    from market_monitor.monitor import MarketMonitor
    from market_monitor.referential import referential_from_dicts
    from tests.conftest import FakeProvider

    days = pd.bdate_range("2026-01-02", "2026-10-27")
    stooq = pd.Series(3.0, index=days)
    stooq.iloc[-1] = 3.355
    cnbc = stooq.copy()
    cnbc.iloc[-1] = 3.340                         # the bar that missed the close (1,5 pb)
    ref = referential_from_dicts(
        {"instruments": {"BUND_10Y": {"name": "Bund 10 ans", "class": "rates", "quote": "yield",
                                      "unit": "%", "tickers": {"free": "stooq:10dey.b|cnbc:DE10Y-DE"}}}},
        {"watchlists": {"all": {"instruments": ["*"]}}})

    class Chain(FakeProvider):
        def _fetch_history(self, request):
            result = super()._fetch_history(request)
            if "stooq:10dey.b|cnbc:DE10Y-DE" in request.tickers:
                result.origins["stooq:10dey.b|cnbc:DE10Y-DE"] = "stooq:10dey.b"
            return result
    free = Chain("free", {"stooq:10dey.b|cnbc:DE10Y-DE": stooq, "cnbc:DE10Y-DE": cnbc})
    for tolerance, expected in ((1.0, True), (2.0, False)):
        monitor = MarketMonitor(MarketDataService([free]), ref,
                                quality=QualitySettings(cross_check_quote_tol_bp=tolerance))
        report = monitor.verify(monitor.performance("all", date(2026, 10, 27)), ["BUND_10Y"])
        row = report.table.loc["BUND_10Y"]
        assert bool(row["suspect"]) is expected, (tolerance, row["cross_check"], row["suspect_reason"])
        if expected:
            assert "3,340" in row["suspect_reason"] and "CNBC" in row["suspect_reason"].upper()
