from pathlib import Path

import pytest

from market_monitor.cli import main
from market_monitor.config import load_settings, settings_from_dict
from market_monitor.data.cached_provider import CachedProvider
from market_monitor.data.factory import build_service
from market_monitor.exceptions import ConfigError

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config" / "config.yaml"


def test_repo_config_loads(monkeypatch, tmp_path):
    monkeypatch.delenv("MARKET_MONITOR_PROVIDERS", raising=False)
    settings = load_settings(REPO_CONFIG, env_file=tmp_path / "absent.env")
    assert settings.priority == ("bloomberg", "fmp", "free")
    assert settings.cache.directory.is_absolute()
    assert settings.cache.directory.name == "market_data"


def test_env_overrides_and_secret_hidden(tmp_path):
    env = {"FMP_API_KEY": " k3y ", "MARKET_MONITOR_PROVIDERS": "FMP, free"}
    settings = settings_from_dict({}, base_dir=tmp_path, env=env)
    assert settings.priority == ("fmp", "free")
    assert settings.fmp.api_key == "k3y"
    assert "k3y" not in repr(settings)


@pytest.mark.parametrize("raw", [
    {"providers": {"priority": ["reuters"]}},
    {"providers": {"priority": []}},
    {"providers": {"bloomberg": {"port": 0}}},
    {"providers": {"fmp": {"timeout_s": -1}}},
    {"cache": {"intraday_ttl_minutes": "15"}},
    {"market": {"timezone": "Mars/Olympus"}},
    {"cache": []},
])
def test_invalid_configs(raw, tmp_path):
    with pytest.raises(ConfigError):
        settings_from_dict(raw, base_dir=tmp_path, env={})


def test_build_service_skips_unavailable(tmp_path):
    raw = {"providers": {"priority": ["fmp", "free"]}, "cache": {"directory": "c"}}
    service = build_service(settings_from_dict(raw, base_dir=tmp_path, env={}))
    assert service.provider_names == ("free",)  # no FMP key
    assert isinstance(service.providers[0], CachedProvider)


def test_build_service_fails_when_nothing_available(tmp_path):
    settings = settings_from_dict({"providers": {"priority": ["fmp"]}}, base_dir=tmp_path, env={})
    with pytest.raises(ConfigError):
        build_service(settings)


def test_cli_cache_clear(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("cache:\n  directory: cache\n", encoding="utf-8")
    assert main(["--config", str(config), "cache-clear"]) == 0
    assert "cache cleared" in capsys.readouterr().out
    assert main(["--config", str(tmp_path / "missing.yaml"), "cache-clear"]) == 2


def test_analytics_and_referential_paths(tmp_path):
    raw = {"referential": {"instruments": "ref/i.yaml"}, "analytics": {"zscore_window": 120}}
    settings = settings_from_dict(raw, base_dir=tmp_path, env={})
    assert settings.instruments_path == (tmp_path / "ref" / "i.yaml").resolve()
    assert settings.analytics.zscore_window == 120
    with pytest.raises(ConfigError):
        settings_from_dict({"analytics": {"zscore_window": 30, "zscore_min_obs": 60}},
                           base_dir=tmp_path, env={})


def test_cli_check_referential(capsys):
    assert main(["--config", str(REPO_CONFIG), "check-referential"]) == 0
    assert "instruments" in capsys.readouterr().out
