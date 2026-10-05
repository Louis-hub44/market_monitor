import pandas as pd
import pytest
import requests

from market_monitor import network
from market_monitor.config import settings_from_dict
from market_monitor.data.factory import build_provider, rest_session
from market_monitor.data.models import Field
from market_monitor.data.providers.free import YahooClient, _yfinance_download
from market_monitor.doctor import run_checks
from tests.conftest import FakeResponse, FakeSession

SSL_ERROR = requests.exceptions.SSLError(
    "certificate verify failed: self signed certificate in certificate chain")


@pytest.fixture
def ca_file(tmp_path):
    path = tmp_path / "corporate.pem"
    path.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    return path


def test_configure_session_prefers_ca_bundle_over_insecure(ca_file):
    session = network.configure_session(requests.Session(), insecure_ssl=True, ca_bundle=ca_file)
    assert session.verify == str(ca_file)
    assert session.headers["User-Agent"] == network.CHROME_USER_AGENT


def test_configure_session_insecure_then_back_to_verified(tmp_path):
    session = network.configure_session(requests.Session(), insecure_ssl=True,
                                        ca_bundle=tmp_path / "missing.pem")
    assert session.verify is False
    assert network.configure_session(session).verify is True


def test_build_session_is_none_without_customisation(tmp_path, ca_file):
    assert network.build_session() is None
    assert network.build_session(ca_bundle=tmp_path / "missing.pem") is None
    assert network.build_session(ca_bundle=ca_file).verify == str(ca_file)


def test_settings_from_yaml_and_env(tmp_path, ca_file):
    raw = {"network": {"insecure_ssl": True, "ca_bundle": ca_file.name}}
    net = settings_from_dict(raw, base_dir=tmp_path, env={}).network
    assert (net.insecure_ssl, net.ca_bundle) == (True, ca_file)

    env = {"MARKET_MONITOR_INSECURE_SSL": "0", "REQUESTS_CA_BUNDLE": str(ca_file)}
    net = settings_from_dict({"network": {"insecure_ssl": True}}, base_dir=tmp_path, env=env).network
    assert (net.insecure_ssl, net.ca_bundle) == (False, ca_file)

    assert not settings_from_dict({}, base_dir=tmp_path, env={}).network.customised


def test_factory_applies_policy_to_every_source(tmp_path):
    settings = settings_from_dict({}, base_dir=tmp_path, env={"MARKET_MONITOR_INSECURE_SSL": "oui",
                                                              "FMP_API_KEY": "k"})
    assert rest_session(settings).verify is False
    free = build_provider("free", settings)
    assert free._ecb._session.verify is False
    assert build_provider("fmp", settings)._session.verify is False
    assert rest_session(settings_from_dict({}, base_dir=tmp_path, env={})) is None


def test_yahoo_session_reaches_yfinance(monkeypatch):
    yf = pytest.importorskip("yfinance")
    seen = {}

    def fake_download(**kwargs):
        seen.update(kwargs)
        return pd.DataFrame()

    applied = []
    monkeypatch.setattr(yf, "download", fake_download)
    monkeypatch.setattr("market_monitor.data.providers.free.apply_to_yfinance", applied.append)
    session = requests.Session()
    YahooClient(session=session).fetch(["^VIX"], pd.Timestamp("2026-10-01").date(),
                                       pd.Timestamp("2026-10-02").date(), Field.LAST)
    assert seen["session"] is session and applied == [session]

    _yfinance_download(["^VIX"], pd.Timestamp("2026-10-01").date(), pd.Timestamp("2026-10-02").date())
    assert seen["session"] is None and applied == [session]  # native curl_cffi untouched


def test_apply_to_yfinance():
    pytest.importorskip("yfinance")
    from yfinance.data import YfData

    previous = YfData()._session
    session = requests.Session()
    try:
        assert network.apply_to_yfinance(session)
    finally:
        YfData()._set_session(previous)
    assert not network.apply_to_yfinance(None)


@pytest.mark.parametrize("message, expected", [
    ("SSLError: certificate verify failed", True),
    ("curl: (60) SSL certificate problem: self-signed certificate", True),
    ("HTTP 404", False),
    (None, False),
])
def test_looks_like_ssl_error(message, expected):
    assert network.looks_like_ssl_error(message) is expected


def test_count_ssl_failures():
    assert network.count_ssl_failures({"a": str(SSL_ERROR), "b": "HTTP 500"}) == 1


@pytest.mark.parametrize("item, kind", [
    (FakeResponse(200), "ok"),
    (FakeResponse(403), "blocked"),
    (FakeResponse(429), "blocked"),
    (SSL_ERROR, "ssl"),
    (requests.exceptions.ProxyError("Unable to connect to proxy"), "proxy"),
    (requests.exceptions.ConnectTimeout("timed out"), "timeout"),
    (requests.exceptions.ConnectionError("getaddrinfo failed"), "dns"),
    (requests.exceptions.ConnectionError("reset"), "error"),
])
def test_check_endpoint_qualifies_failures(item, kind):
    diag = network.check_endpoint("https://x", FakeSession([item]))
    assert diag["kind"] == kind and diag["ok"] is (kind == "ok")


def test_check_connectivity_points_at_the_proxy():
    probes = {"A": "https://a", "B": "https://b"}
    diag = network.check_connectivity(FakeSession([SSL_ERROR]), probes=probes)
    assert diag["ssl_blocked"] and diag["kind"] == "ssl"
    assert network.ENV_CA_BUNDLE in diag["hint"]

    partial = network.check_connectivity(FakeSession([FakeResponse(200), SSL_ERROR]), probes=probes)
    assert partial["kind"] == "partial" and "B" in partial["message"]
    assert network.check_connectivity(FakeSession([FakeResponse(200)]), probes=probes)["kind"] == "ok"


def test_doctor_reports_network(tmp_path, ca_file):
    raw = {"providers": {"priority": ["free"]}, "cache": {"directory": "c"},
           "network": {"ca_bundle": str(ca_file)}}
    checks = {c.name: c for c in run_checks(settings_from_dict(raw, base_dir=tmp_path, env={}))}
    assert checks["Réseau"].ok and "corporate.pem" in checks["Réseau"].detail

    raw["network"] = {"ca_bundle": "missing.pem"}
    checks = {c.name: c for c in run_checks(settings_from_dict(raw, base_dir=tmp_path, env={}))}
    assert not checks["Réseau"].ok and "introuvable" in checks["Réseau"].detail
