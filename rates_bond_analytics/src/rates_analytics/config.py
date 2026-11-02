"""Configuration loading (``config/config.yaml`` + ``.env``).

Secrets (``FMP_API_KEY``, Bloomberg host...) live in ``.env`` or in the process
environment, never in the YAML file or in code. YAML values of the form
``${VAR}`` or ``${VAR:-default}`` are substituted from the environment.

Lookup order for the YAML file:

1. explicit ``path`` argument;
2. ``RATES_ANALYTICS_CONFIG`` environment variable;
3. ``./config/config.yaml`` then ``<project root>/config/config.yaml``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from rates_analytics.exceptions import ConfigurationError
from rates_analytics.network import NetworkSettings

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
VALID_PROVIDERS = ("bloomberg", "fmp", "free", "public", "static")
VALID_POLICIES = ("raise", "warn", "drop")


def _substitute_env(value: Any) -> Any:
    """Recursively replace ``${VAR}`` / ``${VAR:-default}`` placeholders."""
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    return value


@dataclass(frozen=True)
class CacheSettings:
    """Parquet cache settings."""

    enabled: bool = True
    directory: Path = PROJECT_ROOT / "data" / "cache"
    ttl_hours_live: float = 4.0          # data whose window includes today
    ttl_days_static: float = 30.0        # bond reference data


@dataclass(frozen=True)
class DataSettings:
    """Market-data layer settings."""

    provider: str = "free"
    fallback: tuple[str, ...] = ()
    missing_policy: str = "warn"
    quote_lookback_days: int = 10
    plausible_rate_range: tuple[float, float] = (-0.05, 0.30)
    cache: CacheSettings = field(default_factory=CacheSettings)


@dataclass(frozen=True)
class Settings:
    """Top-level settings object (immutable)."""

    data: DataSettings
    providers: dict[str, dict[str, Any]]
    market_data: dict[str, dict[str, Any]]
    source_path: Path | None = None
    sovereign_curves: dict[str, dict[str, Any]] = field(default_factory=dict)
    network: NetworkSettings = field(default_factory=NetworkSettings)
    proxy_curves: dict[str, Any] = field(default_factory=dict)
    benchmark_curves: dict[str, str] = field(default_factory=dict)

    def provider_options(self, name: str) -> dict[str, Any]:
        """Options block of a provider (empty dict if absent)."""
        return dict(self.providers.get(name, {}) or {})


def _find_config(path: str | Path | None) -> Path | None:
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    elif os.environ.get("RATES_ANALYTICS_CONFIG"):
        candidates.append(Path(os.environ["RATES_ANALYTICS_CONFIG"]))
    else:
        candidates += [Path.cwd() / "config" / "config.yaml", PROJECT_ROOT / "config" / "config.yaml"]
    for c in candidates:
        if c.is_file():
            return c
    if path is not None or os.environ.get("RATES_ANALYTICS_CONFIG"):
        raise ConfigurationError(f"Config file not found: {candidates[0]}")
    return None


def _load_dotenv(env_file: str | Path | None) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - optional convenience
        logger.debug("python-dotenv not installed; relying on process environment")
        return
    target = Path(env_file) if env_file else None
    if target is None:
        for c in (Path.cwd() / ".env", PROJECT_ROOT / ".env"):
            if c.is_file():
                target = c
                break
    if target is not None and target.is_file():
        load_dotenv(target, override=False)


def _parse_data(raw: dict[str, Any], base_dir: Path) -> DataSettings:
    provider = str(raw.get("provider", "free")).lower()
    if provider not in VALID_PROVIDERS:
        raise ConfigurationError(f"data.provider must be one of {VALID_PROVIDERS}, got {provider!r}")
    fallback = tuple(str(p).lower() for p in (raw.get("fallback") or []))
    unknown = [p for p in fallback if p not in VALID_PROVIDERS]
    if unknown:
        raise ConfigurationError(f"Unknown fallback provider(s): {unknown}")
    policy = str(raw.get("missing_policy", "warn")).lower()
    if policy not in VALID_POLICIES:
        raise ConfigurationError(f"data.missing_policy must be one of {VALID_POLICIES}, got {policy!r}")
    c = raw.get("cache") or {}
    directory = Path(c.get("directory", "data/cache")).expanduser()
    if not directory.is_absolute():
        directory = (base_dir / directory).resolve()
    rng = raw.get("plausible_rate_range", [-0.05, 0.30])
    if len(rng) != 2 or float(rng[0]) >= float(rng[1]):
        raise ConfigurationError(f"Invalid plausible_rate_range {rng!r}")
    lookback = int(raw.get("quote_lookback_days", 10))
    if lookback < 0:
        raise ConfigurationError("quote_lookback_days must be >= 0")
    return DataSettings(
        provider=provider,
        fallback=tuple(p for p in fallback if p != provider),
        missing_policy=policy,
        quote_lookback_days=lookback,
        plausible_rate_range=(float(rng[0]), float(rng[1])),
        cache=CacheSettings(
            enabled=bool(c.get("enabled", True)),
            directory=directory,
            ttl_hours_live=float(c.get("ttl_hours_live", 4.0)),
            ttl_days_static=float(c.get("ttl_days_static", 30.0)),
        ),
    )


def load_settings(path: str | Path | None = None, env_file: str | Path | None = None,
                  overrides: dict[str, Any] | None = None) -> Settings:
    """Load and validate settings.

    Args:
        path: YAML file (see module docstring for the default lookup).
        env_file: ``.env`` file (default: ``./.env`` then project root).
        overrides: shallow overrides of the ``data`` block (e.g. ``{"provider": "fmp"}``),
            convenient for the UI and for tests.

    Raises:
        ConfigurationError: on invalid content.
    """
    _load_dotenv(env_file)
    cfg_path = _find_config(path)
    raw: dict[str, Any] = {}
    if cfg_path is not None:
        try:
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigurationError(f"Invalid YAML in {cfg_path}: {exc}") from exc
    else:
        logger.warning("No config.yaml found, using defaults (free provider, no mapping)")
    raw = _substitute_env(raw)
    data_raw = dict(raw.get("data") or {})
    data_raw.update(overrides or {})
    base_dir = cfg_path.parent.parent if cfg_path is not None else PROJECT_ROOT
    return Settings(
        data=_parse_data(data_raw, base_dir),
        providers=dict(raw.get("providers") or {}),
        market_data=dict(raw.get("market_data") or {}),
        source_path=cfg_path,
        sovereign_curves=dict(raw.get("sovereign_curves") or {}),
        network=NetworkSettings.from_config(raw.get("network")),
        proxy_curves=dict(raw.get("proxy_curves") or {}),
        benchmark_curves={str(k).upper(): str(v).upper() for k, v in (raw.get("benchmark_curves") or {}).items()},
    )
