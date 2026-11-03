"""Reject unsafe trust configuration before serving authentication traffic."""

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from api.config import Settings
from api.sso_config import OIDCProvider, validate_url


def provider(**changes):
    return OIDCProvider(
        label="Test",
        issuer="https://idp.example/realm",
        client_id="factoryvpn",
        client_secret="private-value",
        **changes,
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://idp.example",
        "https://user:secret@idp.example",
        "https://idp.example?issuer=x",
        "https://idp.example#fragment",
        "file:///etc/passwd",
    ],
)
def test_unsafe_provider_urls_rejected(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_endpoint_origin_allowlist():
    p = provider(endpoint_origins=["https://login.example"])
    assert p.check_endpoint("https://login.example/token")
    for url in (
        "https://evil.example/token",
        "https://login.example.evil.test/token",
        "https://login.example:8443/token",
        "http://login.example/token",
    ):
        with pytest.raises(ValueError):
            p.check_endpoint(url)


@pytest.mark.parametrize(
    "changes",
    [
        {"SSO_TOKEN_KEY": "bad-key"},
        {"SSO_PUBLIC_URL": "https://api.example/prefix"},
        {"SSO_CLIENT_REDIRECT_URIS": ["https://client.example/callback?next=https://evil.test"]},
        {"SSO_CHECK_INTERVAL_SECONDS": 90, "SSO_MAX_STALENESS_SECONDS": 120},
    ],
)
def test_invalid_deployment_config_rejected(changes):
    values = {
        "SSO_PROVIDERS": {"example": provider()},
        "SSO_PUBLIC_URL": "https://api.example",
        "SSO_TOKEN_KEY": Fernet.generate_key().decode(),
        **changes,
    }
    with pytest.raises((ValueError, ValidationError)):
        Settings(_env_file=None, **values)


def test_secrets_not_exposed_in_configuration_errors():
    with pytest.raises(ValidationError) as result:
        OIDCProvider(
            label="Test",
            issuer="http://insecure.example",
            client_id="factoryvpn",
            client_secret="do-not-log-this",
        )
    assert "do-not-log-this" not in str(result.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://portal.example",
        "https://portal.example/",
        "https://portal.example/app",
        "https://portal.example?x=y",
        "https://user@portal.example",
    ],
)
def test_portal_requires_an_exact_trusted_origin(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, PORTAL_PUBLIC_URL=url)


def test_portal_and_sso_share_origin():
    with pytest.raises(ValidationError, match="same public origin"):
        Settings(
            _env_file=None,
            PORTAL_PUBLIC_URL="https://portal.example",
            SSO_PUBLIC_URL="https://api.example",
            SSO_PROVIDERS={"example": provider()},
            SSO_TOKEN_KEY=Fernet.generate_key().decode(),
        )


@pytest.mark.parametrize("minutes", [0, 481])
def test_portal_lifetime_is_bounded(minutes):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, PORTAL_SESSION_MINUTES=minutes)


def test_loopback_portal_allowed_only_in_development():
    assert Settings(
        _env_file=None, ENVIRONMENT="development", PORTAL_PUBLIC_URL="http://127.0.0.1:5173"
    ).PORTAL_PUBLIC_URL
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None, ENVIRONMENT="production", PORTAL_PUBLIC_URL="http://127.0.0.1:5173"
        )
