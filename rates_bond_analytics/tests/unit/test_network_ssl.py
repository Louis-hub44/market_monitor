"""Corporate SSL-proxy support: settings, sessions, diagnostics (no network)."""

from __future__ import annotations

import ssl

import pytest
import requests

from rates_analytics.config import load_settings
from rates_analytics.data import build_market_data_service
from rates_analytics.exceptions import ConfigurationError
from rates_analytics.network import (
    NetworkSettings,
    _SystemTrustAdapter,
    build_session,
    check_connectivity,
    check_endpoint,
    configure_session,
    count_ssl_failures,
    discover_ca_bundles,
    looks_like_proxy_block,
    looks_like_ssl_error,
    proxy_env,
)

SSL_MSG = ("SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
           "self signed certificate in certificate chain'))")


def test_settings_from_config_and_environment(tmp_path):
    pem = tmp_path / "corp.pem"
    pem.write_text("-----BEGIN CERTIFICATE-----\n")
    assert NetworkSettings.from_config(None, env={}).mode == "standard"
    assert NetworkSettings.from_config({"mode": "system"}, env={}).mode == "system"
    ca = NetworkSettings.from_config({"mode": "ca", "ca_bundle": str(pem)}, env={})
    assert ca.mode == "ca" and ca.ca_bundle == pem and ca.label == "Certificat · corp.pem"
    # environment wins; Market Monitor variables are shared by the whole terminal
    assert NetworkSettings.from_config({"mode": "system"}, env={"RATES_CA_BUNDLE": str(pem)}).mode == "ca"
    assert NetworkSettings.from_config({}, env={"MARKET_MONITOR_INSECURE_SSL": "1"}).mode == "insecure"
    assert NetworkSettings.from_config({}, env={"MARKET_MONITOR_CA_BUNDLE": str(pem)}).ca_bundle == pem
    assert NetworkSettings.from_config({"mode": "ca"}, env={"REQUESTS_CA_BUNDLE": str(pem)}).ca_bundle == pem
    assert NetworkSettings.from_config({"mode": "ca", "ca_bundle": str(pem)},
                                       env={"RATES_NETWORK_MODE": "insecure"}).mode == "insecure"
    with pytest.raises(ConfigurationError):
        NetworkSettings(mode="vpn")
    with pytest.raises(ConfigurationError):
        NetworkSettings(mode="ca")


def test_sessions_per_mode(tmp_path):
    assert build_session().verify is True
    insecure = build_session(NetworkSettings(mode="insecure"))
    assert insecure.verify is False and "Chrome" in insecure.headers["User-Agent"]
    pem = tmp_path / "corp.pem"
    pem.write_text("x")
    assert build_session(NetworkSettings(mode="ca", ca_bundle=pem)).verify == str(pem)
    with pytest.raises(ConfigurationError, match="not found"):
        build_session(NetworkSettings(mode="ca", ca_bundle=tmp_path / "missing.pem"))
    system = build_session(NetworkSettings(mode="system"))
    adapter = system.get_adapter("https://data-api.ecb.europa.eu")
    assert isinstance(adapter, _SystemTrustAdapter) and isinstance(adapter._ctx, ssl.SSLContext)
    assert adapter.poolmanager.connection_pool_kw["ssl_context"] is adapter._ctx
    # re-configuring a session restores verification
    assert configure_session(insecure, NetworkSettings()).verify is True


def test_settings_loaded_and_session_shared_by_rest_providers(monkeypatch):
    monkeypatch.setenv("RATES_NETWORK_MODE", "insecure")
    s = load_settings(overrides={"provider": "free", "fallback": ["public"]})
    assert s.network.mode == "insecure"
    svc = build_market_data_service(s)
    sessions = {id(p.http.session) for p in svc.providers}
    assert len(sessions) == 1 and svc.providers[0].http.session.verify is False
    svc2 = build_market_data_service(s, network=NetworkSettings(mode="system"))
    assert svc2.providers[1].http.session.verify is True


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
    assert kinds == {"ok": "ok", "key": "ok", "ssl": "ssl", "blk": "proxy", "403": "blocked", "to": "timeout",
                     "px": "proxy"}
    all_ssl = check_connectivity(probes={"A": "ssl", "B": "ssl"}, session=s)
    assert all_ssl["kind"] == "ssl" and "proxy d'inspection" in all_ssl["message"]
    partial = check_connectivity(probes={"A": "ok", "B": "to"}, session=s)
    assert partial["kind"] == "partial" and "B" in partial["message"]
    assert check_connectivity(probes={"A": "ok"}, session=s)["kind"] == "ok"
