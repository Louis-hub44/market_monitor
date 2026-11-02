"""Manual quotes: CSV store, provider, configuration, CLI and the Daily macro form."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from market_monitor.cli import main
from market_monitor.config import ConfigError, settings_from_dict
from market_monitor.data.cached_provider import CachedProvider
from market_monitor.data.factory import build_service
from market_monitor.data.providers.manual import (
    NOT_ENTERED,
    ManualProvider,
    ManualQuotes,
    previous_business_day,
)
from market_monitor.exceptions import InvalidRequestError

ROOT = Path(__file__).resolve().parents[1]


def test_store_set_replaces_and_delete(tmp_path):
    store = ManualQuotes(tmp_path / "q" / "quotes.csv")
    assert store.frame().empty and store.series("X").empty
    store.set("ITRX_XOVER", "2026-10-05", 287.5)
    store.set("ITRX_XOVER", date(2026, 10, 5), 288.0)  # same day: replaced
    store.set("ITRX_XOVER", "2026-10-06", 290.0)
    assert store.series("ITRX_XOVER").tolist() == [288.0, 290.0]
    assert store.delete("ITRX_XOVER", "2026-10-05") and not store.delete("ITRX_XOVER", "2026-10-05")
    assert store.path.read_text().splitlines() == ["date,ticker,value", "2026-10-06,ITRX_XOVER,290"]
    with pytest.raises(InvalidRequestError):
        store.set("X", "2026-10-05", float("nan"))


def test_import_wide_french_csv_and_long_csv(tmp_path):
    store = ManualQuotes(tmp_path / "quotes.csv")
    store.set("ITRX_MAIN", "2026-10-01", 1.0)
    wide = "date;ITRX_MAIN;ITRX_XOVER\n2026-10-01;55,1;281\n2026-10-02;55,4;\n"
    assert store.import_csv(wide) == 3  # empty cell ignored
    assert store.series("ITRX_MAIN").tolist() == [55.1, 55.4]  # 1.0 replaced
    assert store.import_csv("date,ticker,value\n2026-10-05,ITRX_XOVER,287.5\n") == 1
    assert store.series("ITRX_XOVER").tolist() == [281.0, 287.5]
    with pytest.raises(InvalidRequestError, match="date"):
        store.import_csv("day,value\n1,2\n")


def test_previous_business_day():
    assert previous_business_day("2026-10-05") == date(2026, 10, 2)  # Monday -> Friday
    assert previous_business_day("2026-10-07") == date(2026, 10, 6)


def test_provider_serves_entries_and_reports_missing(tmp_path):
    store = ManualQuotes(tmp_path / "quotes.csv")
    store.set("ITRX_MAIN", "2026-09-01", 50.0)
    store.set("ITRX_XOVER", "2026-10-05", 287.5)
    tickers = ["ITRX_XOVER", "ITRX_MAIN", "NONE"]
    result = ManualProvider(store).get_history(tickers, "2026-10-01", "2026-10-06")
    assert result.data["ITRX_XOVER"].dropna().tolist() == [287.5]
    assert result.errors["NONE"] == NOT_ENTERED
    assert "ITRX_MAIN" in result.errors  # entries exist, none in the window


def _settings(tmp_path, priority):
    return settings_from_dict({"providers": {"priority": priority, "manual": {"file": "quotes.csv"}},
                               "cache": {"directory": "cache"}}, base_dir=tmp_path, env={})


def test_priority_appends_manual_and_service_does_not_cache_it(tmp_path):
    settings = _settings(tmp_path, ["free"])
    assert settings.priority == ("free", "manual")
    assert settings.manual.file == tmp_path / "quotes.csv"
    service = build_service(settings)
    manual = service.providers[-1]
    assert isinstance(manual, ManualProvider) and isinstance(service.providers[0], CachedProvider)
    with pytest.raises(ConfigError):
        _settings(tmp_path, ["manual"])  # manual quotes alone are not a market data source


def _repo_config(tmp_path) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        "providers:\n  priority: [free]\n  manual:\n    file: quotes.csv\n"
        f"referential:\n  instruments: {ROOT / 'config' / 'instruments.yaml'}\n"
        f"  watchlists: {ROOT / 'config' / 'watchlists.yaml'}\n"
        "cache:\n  directory: cache\n", encoding="utf-8")
    return config


def test_cli_quote_commands(tmp_path, capsys):
    config = str(_repo_config(tmp_path))
    assert main(["--config", config, "quote", "set", "ITRX_XOVER", "287,5", "--date", "2026-10-05"]) == 0
    assert main(["--config", config, "quote", "set", "SPX", "1"]) == 2
    assert "saisissables : ITRX_MAIN, ITRX_XOVER" in capsys.readouterr().err
    history = tmp_path / "h.csv"
    history.write_text("date;ITRX_MAIN\n2026-10-02;55,4\n", encoding="utf-8")
    assert main(["--config", config, "quote", "import", str(history)]) == 0
    capsys.readouterr()
    assert main(["--config", config, "quote", "list", "ITRX_XOVER"]) == 0
    out = capsys.readouterr().out
    assert "287.5" in out and "ITRX_MAIN" not in out
    assert main(["--config", config, "quote", "delete", "ITRX_MAIN", "2026-10-02"]) == 0
    assert main(["--config", config, "quote", "delete", "ITRX_MAIN", "2026-10-02"]) == 1
    assert main(["--config", config, "quote", "set", "ITRX_MAIN", "abc"]) == 2
    store = ManualQuotes(tmp_path / "quotes.csv")
    assert store.frame()["ticker"].tolist() == ["ITRX_XOVER"]


def test_cli_doctor_reports_manual_without_counting_it(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("market_monitor.doctor.importlib.util.find_spec", lambda name: None)
    config = str(_repo_config(tmp_path))
    ManualQuotes(tmp_path / "quotes.csv").set("ITRX_MAIN", "2026-10-02", 55.4)
    code = main(["--config", config, "doctor"])
    out = capsys.readouterr().out
    assert "[OK] Provider manual : ITRX_MAIN au 02/10" in out
    assert code == 2  # free unusable (no yfinance): manual quotes do not make the setup OK


def test_daily_macro_form_saves_quotes(tmp_path, monkeypatch):
    monkeypatch.setenv("MM_TEST_QUOTES", str(tmp_path / "quotes.csv"))
    at = AppTest.from_file(str(Path(__file__).with_name("ui_manual_app.py")), default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    assert "2 à saisir" in at.expander[0].label
    at.number_input(key="mm-mq-ITRX_XOVER").set_value(287.5)
    at.button[0].click().run()
    assert not at.exception, at.exception
    frame = ManualQuotes(tmp_path / "quotes.csv").frame()
    assert frame["ticker"].tolist() == ["ITRX_XOVER"] and frame["value"].tolist() == [287.5]
    assert frame["date"].iloc[0] == pd.Timestamp(previous_business_day(date(2026, 10, 7)))
