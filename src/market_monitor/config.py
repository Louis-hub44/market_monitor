"""Settings loading: ``config.yaml`` (parameters) + ``.env`` / environment (secrets).

Precedence: real environment variables > ``.env`` > ``config.yaml`` > defaults.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import load_dotenv

from market_monitor.exceptions import ConfigError
from market_monitor.network import env_ca_bundle, env_insecure

KNOWN_PROVIDERS = ("bloomberg", "fmp", "free")
ENV_CONFIG_PATH = "MARKET_MONITOR_CONFIG"
ENV_PROVIDERS = "MARKET_MONITOR_PROVIDERS"
ENV_FMP_KEY = "FMP_API_KEY"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


@dataclass(frozen=True)
class BloombergSettings:
    host: str = "localhost"
    port: int = 8194
    timeout_ms: int = 30_000


@dataclass(frozen=True)
class FMPSettings:
    api_key: str | None = field(default=None, repr=False)  # never printed
    base_url: str = "https://financialmodelingprep.com/stable"
    timeout_s: float = 15.0
    max_retries: int = 3


@dataclass(frozen=True)
class FreeSettings:
    ecb_base_url: str = "https://data-api.ecb.europa.eu/service/data"
    timeout_s: float = 20.0


@dataclass(frozen=True)
class NetworkSettings:
    """Corporate SSL-inspection proxy workaround (see :mod:`market_monitor.network`)."""

    insecure_ssl: bool = False        # verify=False - trusted corporate network only
    ca_bundle: Path | None = None     # corporate root CA (PEM): wins over insecure_ssl

    @property
    def customised(self) -> bool:
        return self.insecure_ssl or self.ca_bundle is not None


@dataclass(frozen=True)
class CacheSettings:
    enabled: bool = True
    directory: Path = PROJECT_ROOT / ".cache" / "market_data"
    intraday_ttl_minutes: int = 15
    refresh_lookback_days: int = 5


@dataclass(frozen=True)
class AnalyticsSettings:
    """Performance-engine parameters (see README, § Moteur de performances)."""

    zscore_window: int = 252          # daily changes used to estimate mu / sigma
    zscore_min_obs: int = 60          # below this, z-scores are NaN
    zscore_demean: bool = True        # z = (x - n.mu) / (sigma.sqrt(n)) ; False -> mu = 0
    stale_bdays: int = 2              # last print older than this (business days) -> stale
    max_reference_gap_days: int = 7   # reference print too far from its target date -> NaN

    def __post_init__(self) -> None:
        if self.zscore_min_obs > self.zscore_window:
            raise ConfigError("analytics.zscore_min_obs cannot exceed zscore_window")


@dataclass(frozen=True)
class UiSettings:
    """Dashboard parameters."""

    default_watchlist: str = "home"
    cache_ttl_minutes: int = 5        # Streamlit-side cache of the computed report
    heatmap_columns: int = 8          # tiles per row in the heatmap
    zscore_clip: float = 3.0          # colour saturation at |z| = clip
    top_movers: int = 6
    network_gate: bool = True         # start screen: no data request before "Lancer"


@dataclass(frozen=True)
class Settings:
    priority: tuple[str, ...] = KNOWN_PROVIDERS
    bloomberg: BloombergSettings = BloombergSettings()
    fmp: FMPSettings = FMPSettings()
    free: FreeSettings = FreeSettings()
    network: NetworkSettings = NetworkSettings()
    cache: CacheSettings = CacheSettings()
    analytics: AnalyticsSettings = AnalyticsSettings()
    ui: UiSettings = UiSettings()
    daily_macro_path: Path = PROJECT_ROOT / "config" / "daily_macro.yaml"
    alerts_path: Path = PROJECT_ROOT / "config" / "alerts.yaml"
    export_dir: Path = PROJECT_ROOT / "exports"
    instruments_path: Path = PROJECT_ROOT / "config" / "instruments.yaml"
    watchlists_path: Path = PROJECT_ROOT / "config" / "watchlists.yaml"
    timezone: str = "Europe/Paris"
    log_level: str = "INFO"
    log_file: Path | None = None


def load_settings(
    config_path: str | Path | None = None, env_file: str | Path | None = None
) -> Settings:
    """Load ``.env`` then the YAML file, and build validated :class:`Settings`."""
    env_path = Path(env_file) if env_file else DEFAULT_ENV_PATH
    if env_path.is_file():
        load_dotenv(env_path, override=False)
    path = Path(config_path or os.environ.get(ENV_CONFIG_PATH) or DEFAULT_CONFIG_PATH)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    return settings_from_dict(raw, base_dir=path.parent, env=os.environ)


def settings_from_dict(
    raw: Mapping[str, Any], *, base_dir: Path, env: Mapping[str, str]
) -> Settings:
    """Pure builder (no I/O) - relative paths are resolved against ``base_dir``."""
    if not isinstance(raw, Mapping):
        raise ConfigError("config root must be a mapping")
    providers = _section(raw, "providers")
    cache = _section(raw, "cache")
    bbg, fmp, free = (_section(providers, k) for k in ("bloomberg", "fmp", "free"))
    timezone = str(_section(raw, "market").get("timezone", "Europe/Paris"))
    _check_timezone(timezone)
    referential = _section(raw, "referential")
    analytics = _section(raw, "analytics")
    ui = _section(raw, "ui")
    export = _section(raw, "export")
    network = _section(raw, "network")
    env_insecure_ssl = env_insecure(env)
    ca_bundle = env_ca_bundle(env) or str(network.get("ca_bundle") or "").strip()
    return Settings(
        priority=_priority(env.get(ENV_PROVIDERS) or providers.get("priority", KNOWN_PROVIDERS)),
        bloomberg=BloombergSettings(
            host=str(bbg.get("host", "localhost")),
            port=_int(bbg, "port", 8194, low=1, high=65535),
            timeout_ms=_int(bbg, "timeout_ms", 30_000, low=1),
        ),
        fmp=FMPSettings(
            api_key=(env.get(ENV_FMP_KEY) or "").strip() or None,
            base_url=str(fmp.get("base_url", FMPSettings.base_url)),
            timeout_s=_float(fmp, "timeout_s", 15.0),
            max_retries=_int(fmp, "max_retries", 3, low=0),
        ),
        free=FreeSettings(
            ecb_base_url=str(free.get("ecb_base_url", FreeSettings.ecb_base_url)),
            timeout_s=_float(free, "timeout_s", 20.0),
        ),
        network=NetworkSettings(
            insecure_ssl=(bool(network.get("insecure_ssl", False)) if env_insecure_ssl is None
                          else env_insecure_ssl),
            ca_bundle=_path(ca_bundle, base_dir) if ca_bundle else None,
        ),
        cache=CacheSettings(
            enabled=bool(cache.get("enabled", True)),
            directory=_path(cache.get("directory", CacheSettings.directory), base_dir),
            intraday_ttl_minutes=_int(cache, "intraday_ttl_minutes", 15, low=0),
            refresh_lookback_days=_int(cache, "refresh_lookback_days", 5, low=0),
        ),
        analytics=AnalyticsSettings(
            zscore_window=_int(analytics, "zscore_window", 252, low=2),
            zscore_min_obs=_int(analytics, "zscore_min_obs", 60, low=2),
            zscore_demean=bool(analytics.get("zscore_demean", True)),
            stale_bdays=_int(analytics, "stale_bdays", 2, low=0),
            max_reference_gap_days=_int(analytics, "max_reference_gap_days", 7, low=1),
        ),
        ui=UiSettings(
            default_watchlist=str(ui.get("default_watchlist", "home")),
            cache_ttl_minutes=_int(ui, "cache_ttl_minutes", 5, low=0),
            heatmap_columns=_int(ui, "heatmap_columns", 8, low=2, high=20),
            zscore_clip=_float(ui, "zscore_clip", 3.0),
            top_movers=_int(ui, "top_movers", 6, low=0, high=20),
            network_gate=bool(ui.get("network_gate", True)),
        ),
        daily_macro_path=_path(export.get("layout", "daily_macro.yaml"), base_dir),
        alerts_path=_path(_section(raw, "alerts").get("rules", "alerts.yaml"), base_dir),
        export_dir=_path(export.get("output_dir", "../exports"), base_dir),
        instruments_path=_path(referential.get("instruments", "instruments.yaml"), base_dir),
        watchlists_path=_path(referential.get("watchlists", "watchlists.yaml"), base_dir),
        timezone=timezone,
        log_level=_log_level(_section(raw, "logging").get("level", "INFO")),
        log_file=(_path(_section(raw, "logging")["file"], base_dir)
                  if _section(raw, "logging").get("file") else None),
    )


LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s - %(message)s"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def configure_logging(settings: Settings) -> None:
    """Console logging, plus a rotating UTF-8 file (1 MB x 5) when ``logging.file`` is set.

    Idempotent: calling it twice (Streamlit reruns) does not duplicate handlers.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level))
    if not any(getattr(h, "_market_monitor", False) for h in root.handlers):
        handlers: list[logging.Handler] = [logging.StreamHandler()]
        if settings.log_file is not None:
            settings.log_file.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(RotatingFileHandler(settings.log_file, maxBytes=1_000_000,
                                                backupCount=5, encoding="utf-8"))
        for handler in handlers:
            handler.setFormatter(logging.Formatter(LOG_FORMAT))
            handler._market_monitor = True  # type: ignore[attr-defined]
            root.addHandler(handler)


# ------------------------------------------------------------------ validators
def _section(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"'{key}' must be a mapping")
    return value


def _priority(value: Any) -> tuple[str, ...]:
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        raise ConfigError("providers.priority must be a list")
    names = tuple(dict.fromkeys(str(v).strip().lower() for v in items if str(v).strip()))
    unknown = [n for n in names if n not in KNOWN_PROVIDERS]
    if not names or unknown:
        raise ConfigError(f"invalid provider priority {list(items)} (known: {KNOWN_PROVIDERS})")
    return names


def _int(section: Mapping[str, Any], key: str, default: int, *, low: int, high: int | None = None) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high and value > high):
        raise ConfigError(f"'{key}' must be an integer in [{low}, {high or '+inf'}], got {value!r}")
    return value


def _float(section: Mapping[str, Any], key: str, default: float) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"'{key}' must be a positive number, got {value!r}")
    return float(value)


def _path(value: Any, base_dir: Path) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else (base_dir / path).resolve()


def _log_level(value: Any) -> str:
    level = str(value).upper()
    if level not in LOG_LEVELS:
        raise ConfigError(f"logging.level must be one of {LOG_LEVELS}, got {value!r}")
    return level


def _check_timezone(name: str) -> None:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"unknown timezone {name!r} (on Windows: pip install tzdata)") from exc
