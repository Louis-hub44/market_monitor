"""Tests du module réseau (proxy d'inspection SSL d'entreprise)."""

from __future__ import annotations

import os
import re
from pathlib import Path

import certifi
import pytest
import requests
from terminal_core import network as core
from terminal_core.guards import insecure_tls_uses

from data.network import (
    check_endpoint,
    count_ssl_failures,
    describe,
    discover_ca_bundles,
    env_ca_bundle,
    looks_like_ssl_error,
    proxy_env,
    session_for,
)

NETWORK_VARS = ("PI_INSECURE_SSL", "PI_CA_BUNDLE", "TERMINAL_CA_BUNDLE", "RATES_CA_BUNDLE",
                "MARKET_MONITOR_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "TERMINAL_TRUST")


@pytest.fixture(autouse=True)
def _fresh_sessions():
    yield
    core.reset_sessions()


@pytest.fixture
def bundle(tmp_path):
    """Un vrai certificat racine (le premier de certifi) joue le rôle du CA d'entreprise."""
    first = re.search(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----",
                      Path(certifi.where()).read_text(), re.S)
    path = tmp_path / "corp.pem"
    path.write_text(first.group(0) + "\n")
    return path


# ---------------------------------------------------------------- #
# Détection d'erreur SSL
# ---------------------------------------------------------------- #
@pytest.mark.parametrize("message", [
    "SSLError: certificate verify failed: self signed certificate in certificate chain",
    "curl: (60) SSL certificate problem: self-signed certificate",
    "[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate",
    "SSLCertVerificationError",
])
def test_ssl_errors_are_detected(message):
    assert looks_like_ssl_error(message)


@pytest.mark.parametrize("message", [
    "", None, "HTTPError: 429 Too Many Requests", "ConnectionResetError",
    "Timeout: read timed out",
])
def test_non_ssl_errors_are_not_flagged(message):
    assert not looks_like_ssl_error(message)


def test_count_ssl_failures_separates_causes():
    failures = {
        "AAPL": "SSLError: certificate verify failed",
        "MSFT": "SSLError: self signed certificate in certificate chain",
        "SAN.PA": "HTTP 404 not found",
    }
    assert count_ssl_failures(failures) == 2


# ---------------------------------------------------------------- #
# Session unique du terminal
# ---------------------------------------------------------------- #
def test_providers_use_the_terminal_session():
    session = session_for()
    assert isinstance(session, core.SecureSession)
    assert session.verify is not False and "Mozilla" in session.headers["User-Agent"]
    assert session_for() is session                     # partagée dans le processus


def test_verification_cannot_be_disabled():
    with pytest.raises(core.InsecureTLSError):
        session_for().verify = False
    with pytest.raises(core.InsecureTLSError):
        session_for().get("https://example.org", verify=False)


def test_corporate_bundle_is_added_to_the_trust(bundle):
    session = session_for(str(bundle))
    assert bundle.name in session.trust.description


def test_missing_bundle_is_ignored_not_silently_trusted(tmp_path, monkeypatch):
    """Un chemin de certificat invalide ne doit pas dégrader la vérification."""
    for name in NETWORK_VARS:
        monkeypatch.delenv(name, raising=False)
    session = session_for(str(tmp_path / "absent.pem"))
    assert session.verify is not False and session.settings.ca_bundle is None


# ---------------------------------------------------------------- #
# Environnement
# ---------------------------------------------------------------- #
def test_env_ca_bundle_reads_standard_variables(monkeypatch, bundle):
    for name in NETWORK_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
    assert env_ca_bundle() == str(bundle)
    monkeypatch.setenv("PI_CA_BUNDLE", str(bundle))
    assert env_ca_bundle() == str(bundle)


def test_legacy_insecure_switch_is_ignored(monkeypatch):
    for name in NETWORK_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PI_INSECURE_SSL", "oui")
    assert env_ca_bundle() == ""
    assert session_for().verify is not False


def test_no_insecure_tls_in_the_sources():
    root = Path(__file__).resolve().parents[1]
    assert insecure_tls_uses([root / d for d in ("app.py", "config", "data", "services", "views",
                                                 "components", "utils", "models")]) == []


def test_discover_returns_existing_files_only():
    assert all(os.path.isfile(path) for path in discover_ca_bundles())


def test_proxy_env_lists_active_variables(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:8080")
    assert proxy_env()["HTTPS_PROXY"] == "http://proxy.corp:8080"


# ---------------------------------------------------------------- #
# Diagnostic
# ---------------------------------------------------------------- #
def test_describe_reflects_configuration(bundle, tmp_path):
    assert "corp.pem" in describe(str(bundle))
    assert describe(None).startswith("Vérification SSL active")
    assert "introuvable" in describe(str(tmp_path / "absent.pem"))


def test_check_endpoint_qualifies_dns_failure():
    result = check_endpoint("https://hote-inexistant.invalid/", timeout=5)
    assert result["ok"] is False
    assert result["kind"] in ("dns", "error", "proxy", "timeout", "blocked")
    assert result["hint"]


# ---------------------------------------------------------------- #
# Réglages à chaud (bouton SSL, saisie de clé)
# ---------------------------------------------------------------- #
def test_certificate_change_is_applied_to_all_providers_and_reversible(bundle):
    from data.market_data import MarketDataService

    service = MarketDataService()
    service.set_network(str(bundle))
    assert service.fmp._session.settings.ca_bundle == bundle
    assert service.yahoo._session is service.fmp._session
    assert "corp.pem" in service.network_summary()

    service.set_network(None)
    assert service.fmp._session.settings.ca_bundle != bundle
    assert service.fmp._session.verify is not False


def test_api_key_override_takes_priority_and_is_masked():
    from data.market_data import MarketDataService

    service = MarketDataService()
    service.set_api_key("abcd1234efgh5678")
    assert service.fmp.api_key == "abcd1234efgh5678"
    assert service.fmp.is_configured
    assert service.api_key_source() == "saisie dans l'interface"
    displayed = service.api_key_display()
    assert "abcd1234efgh5678" not in displayed
    assert displayed.startswith("abcd") and displayed.endswith("5678")


def test_clearing_api_key_falls_back_to_environment():
    from data.market_data import MarketDataService

    service = MarketDataService()
    service.set_api_key("temporaire123456")
    service.set_api_key(None)
    assert service.fmp.api_key_override is None


def test_local_config_roundtrip(tmp_path, monkeypatch):
    import config.runtime as runtime

    path = tmp_path / ".local_config.json"
    monkeypatch.setattr(runtime, "LOCAL_CONFIG_PATH", path)

    runtime.save_local_config(fmp_api_key="cle-secrete", insecure_ssl=True)
    assert runtime.load_local_config() == {"fmp_api_key": "cle-secrete"}   # contournement refusé

    runtime.save_local_config(ca_bundle="/chemin/corp.pem")   # fusion, pas remplacement
    assert runtime.load_local_config() == {"fmp_api_key": "cle-secrete", "ca_bundle": "/chemin/corp.pem"}

    assert runtime.clear_local_config()
    assert runtime.load_local_config() == {}


def test_local_config_ignores_unknown_keys(tmp_path, monkeypatch):
    import config.runtime as runtime

    path = tmp_path / ".local_config.json"
    path.write_text('{"fmp_api_key": "k", "arbitrary_code": "rm -rf /"}')
    monkeypatch.setattr(runtime, "LOCAL_CONFIG_PATH", path)
    config = runtime.load_local_config()
    assert config == {"fmp_api_key": "k"}


def test_corrupt_local_config_does_not_crash(tmp_path, monkeypatch):
    import config.runtime as runtime

    path = tmp_path / ".local_config.json"
    path.write_text("{ pas du json")
    monkeypatch.setattr(runtime, "LOCAL_CONFIG_PATH", path)
    assert runtime.load_local_config() == {}


def test_mask_key_never_leaks_short_keys():
    from config.runtime import mask_key

    assert mask_key("court") == "•••••"
    assert mask_key(None) == "non configurée"
