"""Command line interface.

    market-monitor doctor [--online]                 # vérifie l'installation
    market-monitor ui                                # lance le dashboard Streamlit
    market-monitor perf --watchlist daily_macro [--as-of 2026-10-02]
    market-monitor daily-macro [--as-of 2026-10-02] [--out exports]
    market-monitor alerts [--as-of ...] [--json alerts.json] [--fail-on critical]
    market-monitor fetch --provider free --tickers ^STOXX50E --start 2026-01-01
    market-monitor snapshot --provider fmp --tickers ^GSPC EURUSD
    market-monitor check-referential
    market-monitor cache-clear [--provider fmp]

Exit codes: 0 OK, 1 data missing, 2 configuration / usage error, 3 alert threshold hit.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from market_monitor.alerts import AlertRule, Severity, load_rules
from market_monitor.config import (
    KNOWN_PROVIDERS,
    PROJECT_ROOT,
    Settings,
    configure_logging,
    load_settings,
)
from market_monitor.data.cache import ParquetCache
from market_monitor.data.factory import build_provider, with_cache
from market_monitor.doctor import run_checks
from market_monitor.exceptions import MarketMonitorError
from market_monitor.export import export_daily_macro, load_layout
from market_monitor.export.text import alert_lines
from market_monitor.monitor import MarketMonitor, PerformanceReport
from market_monitor.referential import load_referential

EXIT_OK, EXIT_MISSING_DATA, EXIT_ERROR, EXIT_ALERT = 0, 1, 2, 3
Command = Callable[[Settings, argparse.Namespace], int]


# ===================================================================== parser
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market-monitor", description="Market Monitor")
    parser.add_argument("--config", help="path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="check the installation").add_argument(
        "--online", action="store_true", help="also fetch one ticker per provider")
    sub.add_parser("ui", help="launch the Streamlit dashboard")
    sub.add_parser("check-referential", help="validate the YAML referential")
    perf = sub.add_parser("perf", help="performance table in the console")
    perf.add_argument("--watchlist", default="home")
    perf.add_argument("--as-of", default=None)
    daily = sub.add_parser("daily-macro", help="write the Excel / PNG / text export")
    daily.add_argument("--as-of", default=None)
    daily.add_argument("--out", default=None, help="output folder (default: export.output_dir)")
    alerts = sub.add_parser("alerts", help="evaluate the alert rules")
    alerts.add_argument("--as-of", default=None)
    alerts.add_argument("--json", default=None, help="write the alerts to this JSON file")
    alerts.add_argument("--fail-on", choices=["warning", "critical"], default=None,
                        help=f"exit code {EXIT_ALERT} if an alert of this severity or above fires")
    for name in ("fetch", "snapshot"):
        cmd = sub.add_parser(name, help=f"raw {name} from one provider")
        cmd.add_argument("--provider", choices=KNOWN_PROVIDERS, required=True)
        cmd.add_argument("--tickers", nargs="+", required=True, help="native tickers")
    fetch = sub.choices["fetch"]
    fetch.add_argument("--start", default=(date.today() - timedelta(days=30)).isoformat())
    fetch.add_argument("--end", default=date.today().isoformat())
    fetch.add_argument("--no-cache", action="store_true")
    clear = sub.add_parser("cache-clear", help="delete the local parquet cache")
    clear.add_argument("--provider", choices=KNOWN_PROVIDERS)
    return parser


# =================================================================== commands
def _doctor(settings: Settings, args: argparse.Namespace) -> int:
    checks = run_checks(settings, online=args.online)
    for check in checks:
        print(f"[{'OK' if check.ok else '!!'}] {check.name} : {check.detail}")
    providers_ok = any(c.ok for c in checks if c.name.startswith("Provider"))
    config_ok = all(c.ok for c in checks if not c.name.startswith("Provider"))
    return EXIT_OK if providers_ok and config_ok else EXIT_ERROR


def _check_referential(settings: Settings, args: argparse.Namespace) -> int:
    ref = load_referential(settings.instruments_path, settings.watchlists_path)
    print(f"OK: {len(ref)} instruments, watchlists {list(ref.watchlist_names)}")
    return EXIT_OK


def _perf(settings: Settings, args: argparse.Namespace) -> int:
    report = MarketMonitor.from_settings(settings).performance(args.watchlist, args.as_of)
    _print_perf(report)
    return EXIT_MISSING_DATA if report.errors else EXIT_OK


def _daily_macro(settings: Settings, args: argparse.Namespace) -> int:
    monitor = MarketMonitor.from_settings(settings)
    layout = load_layout(settings.daily_macro_path, monitor.referential)
    result = export_daily_macro(monitor, layout, args.out or settings.export_dir, args.as_of,
                                _rules_if_any(settings, monitor))
    print(result.text.read_text(encoding="utf-8"))
    print(f"Fichiers : {result.excel}, {result.png}, {result.text}")
    return EXIT_OK


def _alerts(settings: Settings, args: argparse.Namespace) -> int:
    monitor = MarketMonitor.from_settings(settings)
    report = monitor.alerts(load_rules(settings.alerts_path, monitor.referential), args.as_of)
    print("\n".join(alert_lines(report)))
    if args.json:
        frame = report.frame()
        frame["level_date"] = frame["level_date"].astype(str)
        Path(args.json).write_text(frame.to_json(orient="records", force_ascii=False, indent=2),
                                   encoding="utf-8")
    worst = report.max_severity
    if args.fail_on and worst is not None and worst.rank >= Severity(args.fail_on).rank:
        return EXIT_ALERT
    return EXIT_OK


def _raw(settings: Settings, args: argparse.Namespace) -> int:
    provider = build_provider(args.provider, settings)
    if args.command == "fetch":
        if not args.no_cache:
            provider = with_cache(provider, settings)
        history = provider.get_history(args.tickers, args.start, args.end)
        with pd.option_context("display.width", 160, "display.max_columns", 20):
            print(history.data.tail(10))
        errors, warnings = history.errors, history.warnings
    else:
        snapshot = provider.get_snapshot(args.tickers)
        print(snapshot.data)
        errors, warnings = snapshot.errors, snapshot.warnings
    for label, issues in (("ERROR", errors), ("WARNING", warnings)):
        for ticker, message in issues.items():
            print(f"{label} {ticker}: {message}", file=sys.stderr)
    return EXIT_MISSING_DATA if errors else EXIT_OK


def _cache_clear(settings: Settings, args: argparse.Namespace) -> int:
    ParquetCache(settings.cache.directory).clear(args.provider)
    print(f"cache cleared: {settings.cache.directory}")
    return EXIT_OK


COMMANDS: dict[str, Command] = {
    "doctor": _doctor, "check-referential": _check_referential, "perf": _perf,
    "daily-macro": _daily_macro, "alerts": _alerts, "fetch": _raw, "snapshot": _raw,
    "cache-clear": _cache_clear,
}


# ===================================================================== helpers
def _print_perf(report: PerformanceReport) -> None:
    columns = ["name", "level", "level_date", "chg_1d", "chg_1w", "chg_mtd", "chg_ytd",
               "z_1d", "change_unit", "source"]
    print(f"Market Monitor - as of {report.as_of}")
    with pd.option_context("display.width", 200, "display.max_columns", 20,
                           "display.float_format", "{:,.2f}".format):
        for asset_class, table in report.by_asset_class().items():
            print(f"\n== {asset_class.value.upper()} ==")
            print(table[columns].to_string())
    for label, issues in (("ERROR", report.errors), ("WARNING", report.warnings)):
        for key, message in issues.items():
            print(f"{label} {key}: {message}", file=sys.stderr)


def _rules_if_any(settings: Settings, monitor: MarketMonitor) -> tuple[AlertRule, ...]:
    return load_rules(settings.alerts_path, monitor.referential) if settings.alerts_path.is_file() else ()


def _launch_ui(config: str | None) -> int:
    """Run the Streamlit app from the project root (so .streamlit/config.toml applies)."""
    app = Path(__file__).resolve().parent / "ui" / "streamlit_app.py"
    env = dict(os.environ)
    if config:
        env["MARKET_MONITOR_CONFIG"] = str(Path(config).resolve())
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(app)], cwd=PROJECT_ROOT, env=env)


def _utf8_console() -> None:
    """Redirected output on Windows defaults to cp1252, which cannot encode "−" or narrow spaces."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    args = _parser().parse_args(argv)
    if args.command == "ui":
        return _launch_ui(args.config)
    try:
        settings = load_settings(args.config)
        configure_logging(settings)
        return COMMANDS[args.command](settings, args)
    except MarketMonitorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
