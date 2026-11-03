"""SMTP probes never use arbitrary credentials or silently downgrade TLS."""

from contextlib import contextmanager
import smtplib
import ssl
from unittest.mock import Mock
import pytest
from api.config import settings
from api.services import server_status, mail_service
from factoryvpn_common.openvpn import ManagementClient
from portal_mail import smtp_sink, test_deliveries as deliveries


def test_real_smtp_connection_and_explicit_delivery(monkeypatch):
    recipient = "server-probe@example.test"
    with smtp_sink() as port:
        for name, value in {
            "SMTP_HOST": "127.0.0.1",
            "SMTP_PORT": port,
            "SMTP_SECURITY": "plain",
            "SMTP_USERNAME": "",
            "SMTP_CA_FILE": "",
            "SMTP_FROM": "factoryvpn@example.test",
        }.items():
            monkeypatch.setattr(settings, name, value)
        assert server_status.probe() == "connected"
        assert not deliveries.get(recipient)
        assert server_status.probe(recipient) == "accepted"
        assert deliveries[recipient] == 1
        monkeypatch.setattr(settings, "SMTP_SECURITY", "starttls")
        assert server_status.probe(recipient) == "tls_failed"
        assert deliveries[recipient] == 1


@pytest.mark.parametrize(
    "error,code",
    [
        (ssl.SSLCertVerificationError("secret-path"), "tls_failed"),
        (smtplib.SMTPAuthenticationError(535, b"secret-credentials"), "authentication_failed"),
        (
            smtplib.SMTPRecipientsRefused({"private@example.test": (550, b"blocked")}),
            "recipient_rejected",
        ),
        (TimeoutError("private-host"), "timeout"),
        (OSError("private-diagnostic"), "connection_failed"),
    ],
)
def test_probe_errors_are_safe_codes(monkeypatch, error, code):
    monkeypatch.setattr(server_status, "smtp_configured", lambda: True)

    @contextmanager
    def broken(**kwargs):
        assert kwargs["timeout"] == 3
        raise error
        yield

    monkeypatch.setattr(mail_service, "transport", broken)
    assert server_status.probe("self@example.test") == code


def test_management_runtime_uses_server_title_and_bounds_version():
    client = ManagementClient()
    client.command = Mock(
        return_value=["TITLE,OpenVPN 2.6.15 x86_64-pc-linux-gnu [SSL] [LZO]", "END"]
    )
    assert client.clients() == {} and client.version == "2.6.15"
    client.command.return_value = ["TITLE,private text", "END"]
    assert client.clients() == {} and client.version is None
    client.command.return_value = ["TITLE,OpenVPN " + "1" * 200, "END"]
    assert client.clients() == {} and client.version is None
