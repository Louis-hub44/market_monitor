"""End-to-end runs on the repository configuration with synthetic data."""

import logging
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from market_monitor.alerts import load_rules
from market_monitor.analytics.correlation import MatrixOrder
from market_monitor.cli import main
from market_monitor.config import configure_logging, settings_from_dict
from market_monitor.export import export_daily_macro, load_layout
from market_monitor.monitor import MarketMonitor
from market_monitor.referential import ChangeUnit, load_referential
from tests.conftest import FakeProvider, bday_series
from tests.synthetic import synthetic_monitor

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "config.yaml"
AS_OF = "2026-10-05"


@pytest.fixture(autouse=True)
def _restore_from_settings(monkeypatch):
    """ui_main_app.py replaces MarketMonitor.from_settings: restore it for the other tests."""
    monkeypatch.setattr(MarketMonitor, "from_settings", MarketMonitor.__dict__["from_settings"])


@pytest.fixture(scope="module")
def monitor():
    ref = load_referential(ROOT / "config" / "instruments.yaml", ROOT / "config" / "watchlists.yaml")
    return synthetic_monitor(ref, end="2026-10-02")


def test_full_universe_performance(monitor):
    report = monitor.performance("home", AS_OF)
    assert len(report.table) == 84
    assert report.table["level"].notna().all() and not report.errors
    assert report.table.loc["OAT_BUND_10Y", "source"] == "derived"
    assert report.table["z_1d"].notna().sum() > 60


def test_full_daily_macro_with_alerts(monitor, tmp_path):
    layout = load_layout(ROOT / "config" / "daily_macro.yaml", monitor.referential)
    rules = load_rules(ROOT / "config" / "alerts.yaml", monitor.referential)
    result = export_daily_macro(monitor, layout, tmp_path, AS_OF, rules)
    text = result.text.read_text(encoding="utf-8")
    for title in ("Indices", "Taux 10 ans", "Marchés clés", "Spreads", "Alertes du jour"):
        assert title in text
    assert result.png.stat().st_size > 20_000 and result.excel.stat().st_size > 5_000


def test_correlations_and_history_on_full_basket(monitor):
    basket = list(monitor.referential.watchlist("cross_asset").instruments)
    corr = monitor.correlation(basket, AS_OF, order=MatrixOrder.CLUSTER)
    assert corr.matrix.shape == (17, 17) and not corr.errors
    comp = monitor.comparison(["SX5E", "BUND_10Y", "VIX"], "YTD", AS_OF)
    assert list(comp.groups) == [ChangeUnit.PCT, ChangeUnit.BP, ChangeUnit.ABS]


def test_real_main_page_with_repository_config():
    at = AppTest.from_file(str(Path(__file__).with_name("ui_main_app.py")), default_timeout=90)
    at.run()
    assert not at.exception, at.exception
    # start screen: nothing is loaded before "Lancer le chargement"
    assert "Choisissez la connexion" in " ".join(m.value for m in at.markdown)
    assert not at.dataframe
    at.segmented_control(key="mm-net-mode").set_value("insecure").run()
    at.button(key="mm-net-start").click().run()
    assert not at.exception, at.exception
    assert "SSL contourné" in " ".join(m.value for m in at.markdown)
    assert at.dataframe
    for view in ("history", "correlations", "alerts", "daily"):
        at.segmented_control(key="mm-view").set_value(view).run()
        assert not at.exception, (view, at.exception)
    # daily macro charts: one Plotly figure per configured chart, period changed in place
    assert len(at.get("plotly_chart")) >= 6
    text = " ".join(m.value for m in at.markdown)
    assert "OAS High Yield" in text and "VIX" in text and "6 mois" in text
    at.selectbox(key="mm-chart-5-period").set_value("1M").run()
    assert not at.exception, at.exception
    assert "1 mois" in " ".join(m.value for m in at.markdown)
    at.selectbox(key="mm-chart-0-period").set_value("dates").run()
    assert not at.exception, at.exception
    assert at.date_input(key="mm-chart-0-start")


# ------------------------------------------------------------------------- CLI
@pytest.fixture
def patched(monkeypatch, monitor):
    monkeypatch.setattr(MarketMonitor, "from_settings", classmethod(lambda cls, s: monitor))
    fake = FakeProvider("free", {"X": bday_series("2026-09-01", "2026-10-02")})
    monkeypatch.setattr("market_monitor.cli.build_provider", lambda name, settings: fake)
    return fake


def test_cli_commands(patched, capsys, tmp_path):
    base = ["--config", str(CONFIG)]
    assert main([*base, "perf", "--watchlist", "daily_macro", "--as-of", AS_OF]) == 0
    assert "== EQUITY ==" in capsys.readouterr().out
    assert main([*base, "fetch", "--provider", "free", "--tickers", "X", "Y", "--no-cache",
                 "--start", "2026-09-01", "--end", "2026-10-02"]) == 1  # Y has no data
    assert "ERROR Y" in capsys.readouterr().err
    assert main([*base, "snapshot", "--provider", "free", "--tickers", "X"]) in (0, 1)
    assert main([*base, "daily-macro", "--as-of", AS_OF, "--out", str(tmp_path)]) == 0


def test_cli_doctor_offline(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text(
        f"providers:\n  priority: [fmp, free]\n"
        f"referential:\n  instruments: {ROOT / 'config' / 'instruments.yaml'}\n"
        f"  watchlists: {ROOT / 'config' / 'watchlists.yaml'}\n"
        f"export:\n  layout: {ROOT / 'config' / 'daily_macro.yaml'}\n"
        f"alerts:\n  rules: missing.yaml\n"
        f"cache:\n  directory: cache\n", encoding="utf-8")
    code = main(["--config", str(config), "doctor"])
    out = capsys.readouterr().out
    assert "[OK] Référentiel : 84 instruments" in out
    assert "[!!] Alertes" in out and "[!!] Provider fmp : FMP_API_KEY absente" in out
    assert code == 2  # alert rules file missing


def test_cli_ui_launcher(monkeypatch):
    calls = {}
    monkeypatch.setattr("market_monitor.cli.subprocess.call",
                        lambda cmd, cwd, env: calls.update(cmd=cmd, cwd=cwd, env=env) or 0)
    assert main(["--config", str(CONFIG), "ui"]) == 0
    assert calls["cmd"][1:4] == ["-m", "streamlit", "run"]
    assert calls["env"]["MARKET_MONITOR_CONFIG"] == str(CONFIG.resolve())


def test_logging_file_and_idempotence(tmp_path):
    settings = settings_from_dict({"logging": {"level": "DEBUG", "file": "logs/mm.log"}},
                                  base_dir=tmp_path, env={})
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        for h in [h for h in root.handlers if getattr(h, "_market_monitor", False)]:
            root.removeHandler(h)
        configure_logging(settings)
        configure_logging(settings)  # second call adds nothing
        ours = [h for h in root.handlers if getattr(h, "_market_monitor", False)]
        assert len(ours) == 2
        logging.getLogger("market_monitor.test").info("hello")
        for h in ours:
            h.flush()
        assert "hello" in (tmp_path / "logs" / "mm.log").read_text(encoding="utf-8")
    finally:
        for h in [h for h in root.handlers if getattr(h, "_market_monitor", False)]:
            root.removeHandler(h)
            h.close()
        for h in before:
            if h not in root.handlers:
                root.addHandler(h)


def test_doctor_online_probe(monkeypatch, tmp_path):
    from market_monitor.doctor import run_checks

    good = FakeProvider("free", {"ecb:EST/B.EU000A2X2A25.WT": bday_series("2026-01-01", "2030-01-01", 1.9)})
    empty = FakeProvider("fmp", {})
    monkeypatch.setattr("market_monitor.doctor.build_provider",
                        lambda name, settings: good if name == "free" else empty)
    monkeypatch.setattr("market_monitor.doctor.importlib.util.find_spec", lambda name: object())
    settings = settings_from_dict({"providers": {"priority": ["fmp", "free"]},
                                   "cache": {"directory": "c"}}, base_dir=tmp_path, env={})
    checks = {c.name: c for c in run_checks(settings, online=True)}
    assert checks["Provider free"].ok and "ecb:EST" in checks["Provider free"].detail
    assert not checks["Provider fmp"].ok and "^GSPC" in checks["Provider fmp"].detail


def test_main_page_change_connection_returns_to_start_screen():
    at = AppTest.from_file(str(Path(__file__).with_name("ui_main_app.py")), default_timeout=90)
    at.run()
    at.segmented_control(key="mm-net-mode").set_value("standard").run()
    at.button(key="mm-net-start").click().run()
    assert "Connexion standard" in " ".join(m.value for m in at.markdown)
    at.sidebar.button(key="mm-net-change").click().run()
    assert not at.exception, at.exception
    assert "Choisissez la connexion" in " ".join(m.value for m in at.markdown)


def test_bloomberg_only_lines_hidden_without_bloomberg():
    from market_monitor.data.service import MarketDataService

    ref = load_referential(CONFIG.parent / "instruments.yaml", CONFIG.parent / "watchlists.yaml")
    no_bbg = MarketMonitor(MarketDataService([FakeProvider("fmp", {}), FakeProvider("free", {})]), ref)
    assert not no_bbg.is_available("ITRX_MAIN") and not no_bbg.is_available("ITRX_XOVER")
    assert no_bbg.is_available("OAT_BUND_10Y")  # derived: both legs have a free ticker
    rates = no_bbg.select("rates")
    assert "ITRX_MAIN" not in rates and "BUND_10Y" in rates
    assert "ITRX_XOVER" not in no_bbg.select("cross_asset")
    with_bbg = MarketMonitor(MarketDataService([FakeProvider("bloomberg", {})]), ref)
    assert "ITRX_MAIN" in with_bbg.select("rates")
