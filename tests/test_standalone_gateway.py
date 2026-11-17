"""Standalone configuration, bounded demo response and agent supervision."""

from http.client import HTTPConnection
import json
import os
import signal
import threading
from unittest.mock import Mock

import pytest

from scripts import standalone_gateway as standalone


@pytest.fixture
def configuration(tmp_path):
    (tmp_path / "gateway.env").write_text(
        "SERVER_URL=https://factoryvpn.example.test\n"
        "GATEWAY_ACTIVATION_MODE=wifi\n"
        "GATEWAY_API_KEY=package-secret-${literal}\n"
        "GATEWAY_ID=10de6095-14c5-4529-91db-15c75df541a9\n"
        "MAC_ADDRESS=02:00:00:00:00:03\n"
        "OPENVPN_CONFIG=/old/path/gateway.ovpn\n"
    )
    (tmp_path / "gateway.ovpn").write_text("private-profile-material")
    return tmp_path


def test_package_identity_is_authoritative_and_profile_is_local(configuration, monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "different-container-key")
    monkeypatch.setenv("SSL_CERT_FILE", "/old/laboratory/ca.pem")
    monkeypatch.setenv("SSL_CERT_DIR", "/old/laboratory")
    monkeypatch.setenv("FACTORYVPN_ENROLLMENT_FILE", "/other/enrollment.json")
    result = standalone.gateway_environment(configuration)
    assert result["GATEWAY_API_KEY"] == "package-secret-${literal}"
    assert result["OPENVPN_CONFIG"] == str(configuration / "gateway.ovpn")
    assert result["FACTORYVPN_ENROLLMENT_FILE"] == str(configuration / "enrollment.json")
    assert "SSL_CERT_FILE" not in result and "SSL_CERT_DIR" not in result


def test_missing_package_field_is_not_recovered_from_another_gateway_environment(
    configuration, monkeypatch
):
    path = configuration / "gateway.env"
    path.write_text(path.read_text().replace("MAC_ADDRESS=02:00:00:00:00:03\n", ""))
    monkeypatch.setenv("MAC_ADDRESS", "02:00:00:00:00:99")
    assert "MAC_ADDRESS" not in standalone.gateway_environment(configuration)


def test_sms_package_is_rejected_without_exposing_credentials(configuration):
    path = configuration / "gateway.env"
    path.write_text(path.read_text().replace("=wifi", "=sms"))
    with pytest.raises(ValueError, match="Wi-Fi/polling") as error:
        standalone.gateway_environment(configuration)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("missing", ["gateway.env", "gateway.ovpn"])
def test_both_provisioned_files_are_required(configuration, missing):
    (configuration / missing).unlink()
    with pytest.raises(ValueError, match="Provide gateway.env and gateway.ovpn"):
        standalone.gateway_environment(configuration)


def test_does_not_accept_factory_enrollment(configuration):
    (configuration / "enrollment.json").write_text("{}")
    with pytest.raises(ValueError, match="manually provisioned"):
        standalone.gateway_environment(configuration)


def test_rejects_environment_injection(configuration):
    with (configuration / "gateway.env").open("a") as stream:
        stream.write("PYTHONPATH=/untrusted\n")
    with pytest.raises(ValueError, match="unsupported"):
        standalone.gateway_environment(configuration)


def test_https_bundle_must_be_valid_and_is_used_if_present(configuration, monkeypatch):
    certificate = configuration / "https-ca.pem"
    certificate.write_text("not a PEM certificate")
    with pytest.raises(ValueError, match="HTTPS CA bundle"):
        standalone.gateway_environment(configuration)
    verify = Mock()
    monkeypatch.setattr(standalone.ssl, "create_default_context", verify)
    assert standalone.gateway_environment(configuration)["SSL_CERT_FILE"] == str(certificate)
    verify.assert_called_once_with(cafile=str(certificate))


def test_demo_never_serves_files_even_when_requested():
    server = standalone.ThreadingHTTPServer(("127.0.0.1", 0), standalone.DemoHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        for path in ("/", "/gateway.env", "/../gateway.ovpn"):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request("GET", path)
            result = connection.getresponse()
            assert result.status == 200
            assert result.read() == standalone.DEMO_RESPONSE
            connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_healthcheck_requires_live_agent_and_reports_no_vpn_state(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"runner_pid": 12, "agent_pid": 34, "http_port": 0}))
    processes = Mock()
    monkeypatch.setattr(standalone.os, "kill", processes)
    assert standalone.healthcheck(state)
    assert processes.call_args_list[0].args == (12, 0)
    assert processes.call_args_list[1].args == (34, 0)
    processes.side_effect = ProcessLookupError
    assert not standalone.healthcheck(state)
    state.unlink()
    assert not standalone.healthcheck(state)


def test_agent_exit_is_propagated_and_state_is_removed(configuration, tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    child = Mock(pid=os.getpid(), returncode=17)
    child.poll.return_value = 17
    launch = Mock(return_value=child)
    monkeypatch.setattr(standalone.subprocess, "Popen", launch)
    assert standalone.serve(configuration, http_port=0, state_file=state) == 17
    assert launch.call_args.args[0][-2:] == ["-m", "gateway-agent.main"]
    assert launch.call_args.kwargs["env"]["OPENVPN_CONFIG"] == str(configuration / "gateway.ovpn")
    assert not state.exists()


def test_stop_signal_reaches_agent(configuration, tmp_path, monkeypatch):
    child = Mock(pid=os.getpid(), returncode=0)
    child.poll.side_effect = [None, None, 0, 0]
    handlers = {}
    monkeypatch.setattr(standalone.subprocess, "Popen", Mock(return_value=child))

    def install(signum, handler):
        previous = handlers.get(signum)
        handlers[signum] = handler
        return previous

    def wait(**_kwargs):
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    child.wait.side_effect = wait
    monkeypatch.setattr(standalone.signal, "signal", install)
    assert standalone.serve(configuration, http_port=0, state_file=tmp_path / "state") == 0
    child.send_signal.assert_called_once_with(signal.SIGTERM)


def test_http_port_environment_can_disable_demo(configuration, monkeypatch):
    monkeypatch.setenv("FACTORYVPN_SIMULATOR_HTTP_PORT", "0")
    monkeypatch.setattr(
        standalone.sys, "argv", ["standalone_gateway", "serve", "--config-dir", str(configuration)]
    )
    serve = Mock(return_value=0)
    monkeypatch.setattr(standalone, "serve", serve)
    assert standalone.main() == 0
    serve.assert_called_once_with(configuration, 0)


def test_invalid_http_port_is_rejected_without_traceback(monkeypatch, capsys):
    monkeypatch.setenv("FACTORYVPN_SIMULATOR_HTTP_PORT", "invalid")
    monkeypatch.setattr(standalone.sys, "argv", ["standalone_gateway", "serve"])
    with pytest.raises(SystemExit) as error:
        standalone.main()
    assert error.value.code == 2
    assert "Traceback" not in capsys.readouterr().err
