"""Deployment-owned OIDC trust configuration; never derived from login input."""

import hashlib
import json
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from typing import Literal


def validate_url(value, *, loopback=False):
    parsed = urlsplit(value)
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (
            parsed.scheme != "https"
            and not (
                loopback
                and parsed.scheme == "http"
                and parsed.hostname in ("127.0.0.1", "localhost")
            )
        )
    ):
        raise ValueError("Expected an HTTPS URL without credentials, query or fragment")
    # Reject invalid ports at configuration time.
    _ = parsed.port
    return value


def origin(value):
    parsed = urlsplit(value)
    return f"{parsed.scheme}://{parsed.netloc}"


class OIDCProvider(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    kind: Literal["oidc", "cognito"] = "oidc"
    label: str = Field(min_length=1, max_length=100)
    issuer: str = Field(max_length=512)
    client_id: str = Field(min_length=1, max_length=255)
    client_secret: SecretStr
    endpoint_origins: list[str] = Field(default_factory=list, max_length=10)
    enabled: bool = True
    mfa_amr_values: list[str] = Field(default_factory=lambda: ["mfa"], max_length=20)
    mfa_acr_values: list[str] = Field(default_factory=list, max_length=20)
    mfa_request_acr: str = Field(default="", max_length=255)
    # Only opt in after verifying the provider rejects revoked tokens at UserInfo.
    revocation_check: Literal["expiry", "userinfo"] = "expiry"
    token_endpoint_auth_method: Literal["client_secret_basic", "client_secret_post"] = (
        "client_secret_basic"
    )
    logout_endpoint: str | None = None

    @field_validator("issuer", "logout_endpoint")
    @classmethod
    def urls(cls, value):
        return validate_url(value) if value is not None else value

    @field_validator("client_secret")
    @classmethod
    def nonempty_secret(cls, value):
        if not value.get_secret_value():
            raise ValueError("A confidential OIDC client secret is required")
        return value

    @field_validator("endpoint_origins")
    @classmethod
    def origins(cls, values):
        for value in values:
            validate_url(value)
            if origin(value) != value:
                raise ValueError("Endpoint allowlist entries must be exact origins")
        return values

    @property
    def checks_revocation(self):
        return self.kind == "cognito" or self.revocation_check == "userinfo"

    def fingerprint(self):
        values = self.model_dump(mode="json")
        values["client_secret"] = self.client_secret.get_secret_value()
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    def check_endpoint(self, value):
        validate_url(value)
        if origin(value) not in {origin(self.issuer), *self.endpoint_origins}:
            raise ValueError("OIDC endpoint origin is not trusted")
        return value


def validate_providers(values):
    for key, provider in values.items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", key):
            raise ValueError("Invalid SSO provider identifier")
        if provider.logout_endpoint:
            provider.check_endpoint(provider.logout_endpoint)
        if provider.kind == "cognito" and not provider.logout_endpoint:
            raise ValueError("Cognito requires its explicit managed-login logout endpoint")
        if provider.kind == "cognito" and not provider.logout_endpoint.endswith("/logout"):
            raise ValueError("Cognito logout endpoint must end with /logout")
    return values
