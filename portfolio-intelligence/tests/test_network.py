"""Tests du module réseau (proxy d'inspection SSL d'entreprise)."""

from __future__ import annotations

import os

import pytest
import requests

from data.network import (
    build_session,
    check_endpoint,
    configure_session,
    count_ssl_failures,
    describe,
    discover_ca_bundles,
    env_defaults,
    looks_like_ssl_error,
    proxy_env,
)


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
# Construction de session
# ---------------------------------------------------------------- #
def test_ca_bundle_takes_precedence_over_insecure(tmp_path):
    """Le certificat l'emporte : on ne désactive jamais la vérification si on
    dispose d'un moyen de la faire réussir."""
    bundle = tmp_path / "corp.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    session = configure_session(requests.Session(), insecure_ssl=True, ca_bundle=str(bundle))
    assert session.verify == str(bundle)
    assert session.verify is not False


def test_insecure_disables_verification_only_when_no_bundle():
    session = configure_session(requests.Session(), insecure_ssl=True, ca_bundle=None)
    assert session.verify is False


def test_missing_bundle_is_ignored_not_silently_trusted(tmp_path):
    """Un chemin de certificat invalide ne doit pas dégrader en mode non vérifié."""
    session = configure_session(requests.Session(), insecure_ssl=False,
                                ca_bundle=str(tmp_path / "absent.pem"))
    assert session.verify is not False


def test_default_session_keeps_standard_verification():
    session = configure_session(requests.Session())
    assert session.verify is not False
    assert "Mozilla" in session.headers["User-Agent"]


def test_build_session_returns_none_without_customisation():
    """Sans réglage particulier, yfinance conserve curl_cffi (anti-bot)."""
    assert build_session(insecure_ssl=False, ca_bundle=None) is None
    assert build_session(insecure_ssl=True) is not None


# ---------------------------------------------------------------- #
# Environnement
# ---------------------------------------------------------------- #
def test_env_defaults_read_standard_variables(monkeypatch, tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    for name in ("PI_INSECURE_SSL", "INSECURE_SSL", "PI_CA_BUNDLE", "CA_BUNDLE",
                 "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
    monkeypatch.setenv("PI_INSECURE_SSL", "oui")
    insecure, ca = env_defaults()
    assert insecure is True
    assert ca == str(bundle)


def test_env_defaults_are_off_by_default(monkeypatch):
    for name in ("PI_INSECURE_SSL", "INSECURE_SSL", "PI_CA_BUNDLE", "CA_BUNDLE",
                 "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        monkeypatch.delenv(name, raising=False)
    assert env_defaults() == (False, "")


def test_discover_returns_existing_files_only():
    assert all(os.path.isfile(path) for path in discover_ca_bundles())


def test_proxy_env_lists_active_variables(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:8080")
    assert proxy_env()["HTTPS_PROXY"] == "http://proxy.corp:8080"


# ---------------------------------------------------------------- #
# Diagnostic
# ---------------------------------------------------------------- #
def test_describe_reflects_configuration(tmp_path):
    bundle = tmp_path / "corp.pem"
    bundle.write_text("x")
    assert "Certificat d'entreprise" in describe(False, str(bundle))
    assert "désactivée" in describe(True, None)
    assert "directe" in describe(False, None)


def test_check_endpoint_qualifies_dns_failure():
    result = check_endpoint("https://hote-inexistant.invalid/", timeout=5)
    assert result["ok"] is False
    assert result["kind"] in ("dns", "error", "proxy", "timeout")
    assert result["hint"]


# ---------------------------------------------------------------- #
# Réglages à chaud (bouton SSL, saisie de clé)
# ---------------------------------------------------------------- #
def test_ssl_toggle_is_reversible():
    """Réactiver la vérification doit vraiment la rétablir.

    Régression : une première version ne remettait jamais `verify` à True, ce
    qui rendait le bouton à sens unique — une session dégradée le restait
    jusqu'au redémarrage.
    """
    session = requests.Session()
    configure_session(session, insecure_ssl=True)
    assert session.verify is False
    configure_session(session, insecure_ssl=False)
    assert session.verify is True
    configure_session(session, insecure_ssl=True)
    assert session.verify is False


def test_service_applies_network_policy_to_all_providers():
    from data.market_data import MarketDataService

    service = MarketDataService()
    service.set_network(True, None)
    assert service.fmp._session.verify is False
    assert service.yahoo._session.verify is False
    assert "désactivée" in service.network_summary()

    service.set_network(False, None)
    assert service.fmp._session.verify is True
    assert service.yahoo._session.verify is True


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
    assert runtime.load_local_config()["fmp_api_key"] == "cle-secrete"
    assert runtime.load_local_config()["insecure_ssl"] is True

    runtime.save_local_config(insecure_ssl=False)          # fusion, pas remplacement
    assert runtime.load_local_config()["fmp_api_key"] == "cle-secrete"
    assert runtime.load_local_config()["insecure_ssl"] is False

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
