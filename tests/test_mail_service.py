"""SMTP transport uses deployment configuration, verified TLS and no cloud API."""

import pytest
from api.config import settings
from api.services import mail_service
from portal_mail import smtp_sink, messages, SMTPHandler


@pytest.mark.parametrize(
    "value",
    ["x@example.test\r\nBcc: victim@test", "", "a..b@test", ".a@test", "a@-test", "a@te..st"],
)
def test_reject_invalid_addresses(value):
    with pytest.raises(ValueError):
        mail_service.email_address(value)


def test_real_local_smtp_delivery_without_ses(monkeypatch):
    with smtp_sink() as port:
        monkeypatch.setattr(settings, "SMTP_HOST", "127.0.0.1")
        monkeypatch.setattr(settings, "SMTP_PORT", port)
        monkeypatch.setattr(settings, "SMTP_SECURITY", "plain")
        monkeypatch.setattr(settings, "SMTP_USERNAME", "")
        monkeypatch.setattr(settings, "SMTP_CA_FILE", "")
        mail_service.deliver("local@example.test", "042163", "login_web")
    assert messages["local@example.test"] == "042163"


def test_starttls_is_required_without_cleartext_fallback(monkeypatch):
    import smtplib

    with smtp_sink() as port:
        monkeypatch.setattr(settings, "SMTP_HOST", "127.0.0.1")
        monkeypatch.setattr(settings, "SMTP_PORT", port)
        monkeypatch.setattr(settings, "SMTP_SECURITY", "starttls")
        monkeypatch.setattr(settings, "SMTP_CA_FILE", "")
        with pytest.raises(smtplib.SMTPNotSupportedError):
            mail_service.deliver("tls-required@example.test", "123456", "login_web")
    assert "tls-required@example.test" not in messages


@pytest.mark.parametrize("mode", ["connection", "send", "code"])
def test_smtp_quit_error_does_not_reverse_success(monkeypatch, mode):
    from api.services.server_status import probe
    from portal_mail import test_deliveries as deliveries

    recipient = f"quit-{mode}@example.test"
    monkeypatch.setattr(SMTPHandler, "quit_reply", b"500 QUIT unavailable\r\n")
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
        if mode == "code":
            mail_service.deliver(recipient, "123456", "login_web")
            assert messages[recipient] == "123456"
        elif mode == "send":
            assert probe(recipient) == "accepted"
            assert deliveries[recipient] == 1
        else:
            assert probe() == "connected"
