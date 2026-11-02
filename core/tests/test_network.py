"""Session HTTP unique : proxy, confiance TLS, refus de verify=False, diagnostic."""

from __future__ import annotations

import re
from pathlib import Path

import certifi
import pytest
import requests
from requests.adapters import BaseAdapter

from terminal_core import network as net
from terminal_core.check_network import (
    Probe,
    ProbeResult,
    classify_exception,
    dominant_cause,
    main,
    probe,
    run_checks,
)
from terminal_core.network import InsecureTLSError, NetworkConfigurationError, NetworkSettings, SecureSession


@pytest.fixture
def ca_file(tmp_path: Path) -> Path:
    """Un vrai certificat racine (le premier de certifi) joue le rôle du CA d'entreprise."""
    text = Path(certifi.where()).read_text()
    first = re.search(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", text, re.S)
    assert first
    path = tmp_path / "corporate.pem"
    path.write_text(first.group(0) + "\n")
    return path


@pytest.fixture(autouse=True)
def _clean_sessions():
    yield
    net.reset_sessions()


# --------------------------------------------------------------------------- réglages
def test_defaults_from_empty_environment():
    s = NetworkSettings.from_env(env={})
    assert (s.trust, s.https_proxy, s.ca_bundle, s.notes) == ("auto", None, None, ())


def test_environment_overrides_configuration(ca_file):
    env = {"TERMINAL_HTTPS_PROXY": "http://proxy.corp:8080", "TERMINAL_CA_BUNDLE": str(ca_file),
           "TERMINAL_TRUST": "ca", "TERMINAL_TIMEOUT_S": "7"}
    s = NetworkSettings.from_env(env=env, config={"https_proxy": "http://other:1", "trust": "system"})
    assert s.https_proxy == "http://proxy.corp:8080"
    assert s.http_proxy == "http://proxy.corp:8080"  # repli sur le proxy HTTPS
    assert (s.trust, s.ca_bundle, s.timeout_s) == ("ca", ca_file, 7.0)


def test_configuration_block_is_used_without_environment():
    s = NetworkSettings.from_env(env={}, config={"proxy": "http://p:3128", "no_proxy": "localhost"})
    assert (s.https_proxy, s.no_proxy) == ("http://p:3128", "localhost")


@pytest.mark.parametrize("var", ["PI_CA_BUNDLE", "RATES_CA_BUNDLE", "MARKET_MONITOR_CA_BUNDLE",
                                 "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"])
def test_project_and_standard_ca_variables_are_honoured(var, ca_file):
    assert NetworkSettings.from_env(env={var: str(ca_file)}).ca_bundle == ca_file


def test_project_ca_variable_wins_over_standard(ca_file, tmp_path):
    other = tmp_path / "other.pem"
    other.write_text(ca_file.read_text())
    s = NetworkSettings.from_env(env={"REQUESTS_CA_BUNDLE": str(other), "PI_CA_BUNDLE": str(ca_file)})
    assert s.ca_bundle == ca_file


@pytest.mark.parametrize("var", ["PI_INSECURE_SSL", "MARKET_MONITOR_INSECURE_SSL"])
def test_legacy_insecure_switches_are_ignored(var):
    s = NetworkSettings.from_env(env={var: "true"})
    assert s.trust == "auto"
    assert any(var in n for n in s.notes)
    assert SecureSession(s).verify is not False


def test_legacy_rates_mode_insecure_is_ignored_and_others_are_mapped():
    assert NetworkSettings.from_env(env={"RATES_NETWORK_MODE": "insecure"}).trust == "auto"
    assert NetworkSettings.from_env(env={"RATES_NETWORK_MODE": "standard"}).trust == "certifi"
    assert NetworkSettings.from_env(env={"RATES_NETWORK_MODE": "system"}).trust == "system"


@pytest.mark.parametrize("value", ["insecure", "off", "none"])
def test_explicit_insecure_trust_is_rejected(value):
    with pytest.raises(InsecureTLSError):
        NetworkSettings(trust=value)


def test_unknown_trust_mode_is_rejected():
    with pytest.raises(NetworkConfigurationError):
        NetworkSettings(trust="windows")


def test_missing_ca_is_ignored_in_auto_mode_but_fatal_in_ca_mode(tmp_path):
    missing = str(tmp_path / "absent.pem")
    s = NetworkSettings.from_env(env={"TERMINAL_CA_BUNDLE": missing})
    assert s.ca_bundle is None and any("introuvable" in n for n in s.notes)
    with pytest.raises(NetworkConfigurationError):
        NetworkSettings.from_env(env={"TERMINAL_CA_BUNDLE": missing, "TERMINAL_TRUST": "ca"})


# --------------------------------------------------------------------------- confiance
def test_auto_prefers_truststore_and_adds_the_corporate_ca(ca_file):
    plan = net.resolve_trust(NetworkSettings(ca_bundle=ca_file))
    assert plan.kind == "system" and plan.verify is True and plan.ssl_context is not None
    assert ca_file.name in plan.description


def test_auto_falls_back_to_ca_then_certifi_without_truststore(monkeypatch, ca_file):
    monkeypatch.setattr(net, "truststore_available", lambda: False)
    plan = net.resolve_trust(NetworkSettings(ca_bundle=ca_file))
    assert plan.kind == "ca"
    merged = Path(str(plan.verify)).read_text()
    assert ca_file.read_text().strip() in merged
    assert Path(certifi.where()).read_text().strip()[:200] in merged  # certifi conservé
    assert net.resolve_trust(NetworkSettings()).kind == "certifi"


def test_system_mode_without_truststore_is_an_error(monkeypatch):
    monkeypatch.setattr(net, "truststore_available", lambda: False)
    with pytest.raises(NetworkConfigurationError):
        net.resolve_trust(NetworkSettings(trust="system"))


def test_system_mode_mounts_the_truststore_adapter():
    s = SecureSession(NetworkSettings(trust="system"))
    assert isinstance(s.get_adapter("https://example.org"), net._ContextAdapter)


# --------------------------------------------------------------------------- verify=False
def test_session_refuses_verify_false_assignment():
    s = SecureSession(NetworkSettings(trust="certifi"))
    with pytest.raises(InsecureTLSError):
        s.verify = False


def test_session_refuses_verify_false_per_request():
    s = SecureSession(NetworkSettings(trust="certifi"))
    with pytest.raises(InsecureTLSError):
        s.get("https://example.org", verify=False)


def test_environment_ca_bundle_cannot_override_session_trust(monkeypatch, ca_file):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_file))
    s = SecureSession(NetworkSettings(trust="certifi"))
    settings = s.merge_environment_settings("https://example.org", {}, False, None, None)
    assert settings["verify"] == certifi.where()


def test_system_session_keeps_verify_true_despite_environment(monkeypatch, ca_file):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_file))
    s = SecureSession(NetworkSettings(trust="system"))
    assert s.merge_environment_settings("https://example.org", {}, False, None, None)["verify"] is True


# --------------------------------------------------------------------------- proxy
def test_configured_proxy_is_applied_and_no_proxy_bypasses_it():
    s = SecureSession(NetworkSettings(trust="certifi", https_proxy="http://proxy.corp:8080",
                                      http_proxy="http://proxy.corp:8080", no_proxy="intranet.corp,localhost"))
    out = s.merge_environment_settings("https://data-api.ecb.europa.eu/x", {}, False, None, None)
    assert out["proxies"]["https"] == "http://proxy.corp:8080"
    inside = s.merge_environment_settings("https://intranet.corp/x", {}, False, None, None)
    assert "https" not in inside["proxies"]


def test_proxy_password_is_masked():
    assert net.mask_proxy("http://louis:s3cret@proxy:8080") == "http://louis:***@proxy:8080"
    d = net.describe(NetworkSettings(trust="certifi", https_proxy="http://u:p@proxy:1"))
    assert "p@" not in str(d["https_proxy"])


def test_shared_session_is_reused_per_settings():
    s = NetworkSettings(trust="certifi")
    assert net.get_session(s) is net.get_session(s)
    assert net.get_session(NetworkSettings(trust="certifi", timeout_s=5)) is not net.get_session(s)


def test_default_timeout_is_applied(monkeypatch):
    seen = {}

    class Recorder(BaseAdapter):
        def send(self, request, **kwargs):
            seen.update(kwargs)
            r = requests.Response()
            r.status_code, r._content, r.url = 200, b"ok", request.url
            return r

        def close(self):
            pass

    s = SecureSession(NetworkSettings(trust="certifi", timeout_s=3.5))
    s.mount("https://", Recorder())
    s.get("https://example.org")
    assert seen["timeout"] == 3.5 and seen["verify"] == certifi.where()


# --------------------------------------------------------------------------- diagnostic
class FakeAdapter(BaseAdapter):
    def __init__(self, outcome):
        super().__init__()
        self.outcome = outcome

    def send(self, request, **kwargs):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        status, body = self.outcome
        r = requests.Response()
        r.status_code, r._content, r.url, r.encoding = status, body.encode(), request.url, "utf-8"
        return r

    def close(self):
        pass


def _session(outcome) -> requests.Session:
    s = SecureSession(NetworkSettings(trust="certifi"))
    s.mount("https://", FakeAdapter(outcome))
    return s


P = Probe("ECB", "https://data-api.ecb.europa.eu/x", True, "test")


@pytest.mark.parametrize("outcome,kind,ok", [
    ((200, "date,value"), "ok", True),
    ((401, "no key"), "ok", True),
    ((407, ""), "proxy_auth", False),
    ((403, "<html>Zscaler - this site is blocked</html>"), "blocked", False),
    ((200, "<html><title>Access Denied</title> Netskope</html>"), "blocked", False),
    ((503, "down"), "http", False),
    (requests.exceptions.SSLError("certificate verify failed: self signed certificate in chain"), "ssl", False),
    (requests.exceptions.ProxyError("Cannot connect to proxy"), "proxy", False),
    (requests.exceptions.ConnectTimeout("timed out"), "timeout", False),
    (requests.exceptions.ConnectionError("getaddrinfo failed"), "dns", False),
])
def test_probe_qualifies_each_failure(outcome, kind, ok):
    r = probe(P, _session(outcome), timeout=1)
    assert (r.kind, r.ok) == (kind, ok)
    assert bool(r.hint) is (not ok)


def test_classify_exception_by_message():
    assert classify_exception(OSError("CERTIFICATE_VERIFY_FAILED")) == "ssl"
    assert classify_exception(OSError("Tunnel connection failed: 407 Proxy Authentication Required")) == "proxy_auth"
    assert classify_exception(requests.exceptions.ProxyError(
        "Unable to connect to proxy', OSError('Tunnel connection failed: 403 Forbidden')")) == "blocked"


def _r(name, ok, kind="ok", critical=True):
    return ProbeResult(name, "u", critical, ok, kind, None, 1, "", "")


def test_dominant_cause_blames_the_workstation_only_when_everything_fails_alike():
    assert dominant_cause([_r("A", True), _r("B", True)]) == "ok"
    assert dominant_cause([_r("A", True), _r("B", False, "http")]) == "partial"
    assert dominant_cause([_r("A", False, "ssl"), _r("B", False, "ssl")]) == "ssl"
    assert dominant_cause([_r("A", False, "ssl"), _r("B", False, "dns")]) == "mixed"


def test_run_checks_reports_critical_failures():
    probes = (P, Probe("YAHOO", "https://query1.finance.yahoo.com/x", False, "t"))
    report = run_checks(NetworkSettings(trust="certifi"), session=_session((200, "x")), probes=probes)
    assert report["critical_ok"] and report["cause"] == "ok"
    report = run_checks(NetworkSettings(trust="certifi"), session=_session(requests.exceptions.SSLError("x")),
                        probes=probes)
    assert not report["critical_ok"] and report["cause"] == "ssl"


def test_cli_exit_codes(monkeypatch, capsys):
    import terminal_core.check_network as cn

    monkeypatch.setattr(cn, "run_checks", lambda *a, **k: {
        "config": net.describe(NetworkSettings(trust="certifi")), "ca_found": [], "cause": "partial",
        "results": [_r("ECB", True), _r("FMP", False, "http", critical=False)], "critical_ok": True})
    assert main([]) == 0
    assert "Verdict" in capsys.readouterr().out
    monkeypatch.setattr(cn, "run_checks", lambda *a, **k: {
        "config": net.describe(NetworkSettings(trust="certifi")), "ca_found": [], "cause": "ssl",
        "results": [_r("ECB", False, "ssl")], "critical_ok": False})
    assert main(["--json"]) == 1
    monkeypatch.setenv("TERMINAL_TRUST", "insecure")
    assert main([]) == 2


# --------------------------------------------------------------------------- garde-fou
def test_guard_detects_insecure_patterns(tmp_path):
    from terminal_core.guards import insecure_tls_uses

    bad = tmp_path / "bad.py"
    bad.write_text("import requests, ssl\n"
                   "requests.get('u', verify=False)\n"
                   "s = requests.Session()\ns.verify = False\n"
                   "ctx = ssl._create_unverified_context()\n")
    good = tmp_path / "good.py"
    good.write_text('"""verify=False est interdit."""\n# s.verify = False\nx = dict(verify=True)\n')
    assert [h.rsplit(":", 1)[1] for h in insecure_tls_uses([bad])] == ["2", "4", "5"]
    assert insecure_tls_uses([good]) == []


def test_no_verify_false_in_the_core_sources():
    from terminal_core.guards import insecure_tls_uses

    assert insecure_tls_uses([Path(__file__).resolve().parents[1] / "terminal_core"]) == []
