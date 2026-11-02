"""Corporate SSL-proxy support: settings, sessions, diagnostics (no network)."""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest
import requests
from terminal_core import network as core
from terminal_core.guards import insecure_tls_uses

from rates_analytics.config import load_settings
from rates_analytics.data import build_market_data_service
from rates_analytics.exceptions import ConfigurationError
from rates_analytics.network import (
    NetworkSettings,
    build_session,
    check_connectivity,
    check_endpoint,
    count_ssl_failures,
    discover_ca_bundles,
    looks_like_proxy_block,
    looks_like_ssl_error,
    proxy_env,
)

SSL_MSG = ("SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
           "self signed certificate in certificate chain'))")


@pytest.fixture(autouse=True)
def _fresh_sessions():
    yield
    core.reset_sessions()


@pytest.fixture
def pem(tmp_path):
    """A real root certificate (first of certifi) standing for the corporate CA."""
    import re

    import certifi

    first = re.search(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----",
                      Path(certifi.where()).read_text(), re.S)
    path = tmp_path / "corp.pem"
    path.write_text(first.group(0) + "\n")
    return path


def test_settings_from_config_and_environment(pem):
    assert NetworkSettings.from_config(None, env={}).mode == "auto"
    assert NetworkSettings.from_config({"mode": "system"}, env={}).mode == "system"
    ca = NetworkSettings.from_config({"mode": "ca", "ca_bundle": str(pem)}, env={})
    assert ca.mode == "ca" and ca.ca_bundle == pem and ca.label == "Certificat · corp.pem"
    # environment wins; the variables are shared by the whole terminal
    assert NetworkSettings.from_config({"mode": "system"}, env={"RATES_CA_BUNDLE": str(pem)}).ca_bundle == pem
    assert NetworkSettings.from_config({}, env={"MARKET_MONITOR_CA_BUNDLE": str(pem)}).ca_bundle == pem
    assert NetworkSettings.from_config({"mode": "ca"}, env={"REQUESTS_CA_BUNDLE": str(pem)}).ca_bundle == pem
    assert NetworkSettings.from_config({}, env={"RATES_NETWORK_MODE": "standard"}).mode == "standard"
    assert NetworkSettings.from_config({}, env={"TERMINAL_TRUST": "certifi"}).mode == "standard"
    proxied = NetworkSettings.from_config({"proxy": "http://proxy.corp:8080", "no_proxy": "intranet"}, env={})
    assert (proxied.proxy, proxied.no_proxy) == ("http://proxy.corp:8080", "intranet")
    with pytest.raises(ConfigurationError):
        NetworkSettings(mode="vpn")
    with pytest.raises(ConfigurationError):
        NetworkSettings(mode="ca")


def test_insecure_is_refused_in_code_and_ignored_in_configuration(pem):
    with pytest.raises(ConfigurationError, match="insecure"):
        NetworkSettings(mode="insecure")
    assert NetworkSettings.from_config({"mode": "insecure"}, env={}).mode == "auto"
    assert NetworkSettings.from_config({}, env={"MARKET_MONITOR_INSECURE_SSL": "1"}).mode == "auto"
    assert NetworkSettings.from_config({"mode": "ca", "ca_bundle": str(pem)},
                                       env={"RATES_NETWORK_MODE": "insecure"}).mode == "ca"


def test_sessions_per_mode(tmp_path, pem):
    default = build_session()
    assert isinstance(default, core.SecureSession) and default.verify is True
    assert "Chrome" in default.headers["User-Agent"]
    with pytest.raises(core.InsecureTLSError):
        default.verify = False
    ca = build_session(NetworkSettings(mode="ca", ca_bundle=pem))
    assert pem.read_text().strip() in Path(ca.verify).read_text()   # corporate CA merged with certifi
    with pytest.raises(ConfigurationError, match="not found"):
        build_session(NetworkSettings(mode="ca", ca_bundle=tmp_path / "missing.pem"))
    system = build_session(NetworkSettings(mode="system"))
    adapter = system.get_adapter("https://data-api.ecb.europa.eu")
    assert isinstance(adapter, core._ContextAdapter) and isinstance(adapter._ctx, ssl.SSLContext)
    assert adapter.poolmanager.connection_pool_kw["ssl_context"] is adapter._ctx
    assert build_session(NetworkSettings(mode="system")) is system          # one shared session


def test_settings_loaded_and_session_shared_by_rest_providers(monkeypatch):
    monkeypatch.setenv("RATES_NETWORK_MODE", "standard")
    s = load_settings(overrides={"provider": "free", "fallback": ["public"]})
    assert s.network.mode == "standard"
    svc = build_market_data_service(s)
    sessions = {id(p.http.session) for p in svc.providers}
    assert len(sessions) == 1 and isinstance(svc.providers[0].http.session, core.SecureSession)
    svc2 = build_market_data_service(s, network=NetworkSettings(mode="system"))
    assert svc2.providers[1].http.session.trust.kind == "system"


def test_no_insecure_tls_in_the_sources():
    root = Path(__file__).resolve().parents[2]
    assert insecure_tls_uses([root / "src", root / "app", root / "scripts"]) == []


def test_static_chain_needs_no_session(tmp_path):
    s = load_settings(overrides={"provider": "static", "fallback": []})
    bad = NetworkSettings(mode="ca", ca_bundle=tmp_path / "missing.pem")
    assert build_market_data_service(s, network=bad).providers[0].name == "static"   # offline: no SSL needed


def test_error_classifiers():
    assert looks_like_ssl_error(SSL_MSG) and not looks_like_ssl_error("HTTP 500") and not looks_like_ssl_error(None)
    assert looks_like_proxy_block("<html><title>Zscaler</title> This site is blocked</html>")
    assert not looks_like_proxy_block('{"barData": {}}')
    assert count_ssl_failures({"a": SSL_MSG, "b": "timeout", "c": SSL_MSG}) == 2
    assert proxy_env({"HTTPS_PROXY": "http://p:8080", "PATH": "x"}) == {"HTTPS_PROXY": "http://p:8080"}
    assert discover_ca_bundles({"RATES_CA_BUNDLE": __file__})[0] == __file__


class _Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


class _Session:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    def get(self, url, timeout=None):
        b = self.behaviour[url]
        if isinstance(b, Exception):
            raise b
        return b

    def close(self):
        pass


def test_check_endpoint_and_dominant_cause():
    s = _Session({"ok": _Resp(200), "key": _Resp(401), "ssl": requests.exceptions.SSLError(SSL_MSG),
                  "blk": _Resp(200, "<html>Zscaler: access denied</html>"), "403": _Resp(403),
                  "to": requests.exceptions.ConnectTimeout("Connection timed out"),
                  "px": requests.exceptions.ProxyError("Unable to connect to proxy")})
    kinds = {u: check_endpoint(u, s)["kind"] for u in ("ok", "key", "ssl", "blk", "403", "to", "px")}
    assert kinds == {"ok": "ok", "key": "ok", "ssl": "ssl", "blk": "blocked", "403": "blocked", "to": "timeout",
                     "px": "proxy"}
    all_ssl = check_connectivity(probes={"A": "ssl", "B": "ssl"}, session=s)
    assert all_ssl["kind"] == "ssl" and "proxy d'inspection" in all_ssl["message"]
    partial = check_connectivity(probes={"A": "ok", "B": "to"}, session=s)
    assert partial["kind"] == "partial" and "B" in partial["message"]
    assert check_connectivity(probes={"A": "ok"}, session=s)["kind"] == "ok"
