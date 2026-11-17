"""
Application configuration
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator, model_validator
from sqlalchemy.engine import make_url
from typing import List, Literal
from pydantic import Field, SecretStr
from api.sso_config import OIDCProvider, validate_providers, validate_url, origin
from factoryvpn_common.timing import MAX_LEASE_SECONDS
from factoryvpn_common.branding import DEFAULT_APP_NAME, normalize_app_name
import re


def _normalize_database_url(url: str) -> str:
    """Ensure the database URL uses the asyncpg driver for async SQLAlchemy."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


class Settings(BaseSettings):
    # Display label only; protocol names and technical identifiers stay stable.
    APP_NAME: str = DEFAULT_APP_NAME

    @field_validator("APP_NAME")
    @classmethod
    def app_name(cls, value: str) -> str:
        return normalize_app_name(value)

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://factoryvpn:password@localhost:5432/factoryvpn"

    # JWT
    JWT_SECRET: str = "change-this-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 30

    # Empty disables browser authentication; never infer trust from Host headers.
    PORTAL_PUBLIC_URL: str = ""
    # Il login dura fino a 12 ore; logout e revoche restano immediatamente validi.
    PORTAL_SESSION_MINUTES: int = Field(default=720, ge=1, le=720)

    # Durata totale e autorizzazione temporanea del firewall hanno scopi diversi.
    VPN_SESSION_MINUTES: int = Field(default=180, ge=1, le=720)
    VPN_LEASE_SECONDS: int = Field(default=300, ge=30, le=MAX_LEASE_SECONDS)
    # Questi valori vengono copiati nei nuovi pacchetti gateway.
    GATEWAY_POLL_INTERVAL: int = Field(default=5, ge=1, le=10)
    GATEWAY_WIFI_CHECK_INTERVAL: int = Field(default=30, ge=5, le=300)
    CONTROL_API_TIMEOUT_SECONDS: int = Field(default=10, ge=1, le=60)
    GATEWAY_CONNECT_TIMEOUT_SECONDS: int = Field(default=30, ge=5, le=300)

    SMTP_HOST: str = ""
    SMTP_PORT: int = Field(default=587, ge=1, le=65535)
    SMTP_SECURITY: Literal["starttls", "tls", "plain"] = "starttls"
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: SecretStr = SecretStr("")
    SMTP_FROM: str = ""
    SMTP_CA_FILE: str = ""
    EMAIL_CODE_KEY: SecretStr = SecretStr("")

    # Empty disables recovery integration. These are server paths, never API inputs.
    BACKUP_CONTROL_DIR: str = ""
    BACKUP_ARCHIVE_DIR: str = ""
    BACKUP_MAX_BYTES: int = Field(default=512 * 1024 * 1024, ge=1024 * 1024, le=512 * 1024 * 1024)

    # Public PEMs only; no request can choose a path or a network destination.
    STATUS_HTTPS_CERTIFICATE_FILE: str = ""
    STATUS_VPN_CERTIFICATE_FILE: str = ""
    STATUS_PKI_ROOT: str = ""
    STATUS_VPN_CA_FILE: str = ""

    SSO_PROVIDERS: dict[str, OIDCProvider] = {}
    SSO_PUBLIC_URL: str = ""
    SSO_TOKEN_KEY: SecretStr = SecretStr("")
    SSO_CLIENT_REDIRECT_URIS: list[str] = ["http://127.0.0.1:8765/callback"]
    SSO_SESSION_MINUTES: int = Field(default=720, ge=1, le=720)
    SSO_CHECK_INTERVAL_SECONDS: int = Field(default=60, ge=5, le=300)
    SSO_MAX_STALENESS_SECONDS: int = Field(default=120, ge=10, le=600)

    @field_validator("SSO_PROVIDERS")
    @classmethod
    def sso_providers(cls, values):
        return validate_providers(values)

    # Separate machine credentials; gateway keys are scoped to a stable gateway UUID.
    WORKER_API_KEY: str = ""
    PROVISIONING_SECRET_KEY: SecretStr = SecretStr("")
    VPN_GATEWAY_CERTIFICATE_DAYS: int = Field(default=3650, ge=1, le=3650)
    VPN_TECHNICIAN_CERTIFICATE_DAYS: int = Field(default=3650, ge=1, le=3650)
    PROVISIONING_DOWNLOAD_MINUTES: int = Field(default=15, ge=1, le=60)

    # Solo hash SHA-256: la chiave in chiaro resta sulla postazione di fine linea.
    # Gli IP fidati vengono filtrati da Nginx prima di raggiungere queste API.
    FACTORY_STATIONS: dict[str, str] = Field(default_factory=dict)
    FACTORY_REQUESTS_PER_MINUTE: int = Field(default=60, ge=1, le=600)
    ENROLLMENT_REQUESTS_PER_MINUTE: int = Field(default=30, ge=1, le=300)
    ENROLLMENT_PACKAGES_PER_HOUR: int = Field(default=6, ge=1, le=60)

    @field_validator("FACTORY_STATIONS")
    @classmethod
    def factory_stations(cls, values):
        if any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", station)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            for station, digest in values.items()
        ):
            raise ValueError("Invalid factory station identifier or SHA-256 hash")
        if len(set(values.values())) != len(values):
            raise ValueError("Factory stations must have distinct keys")
        return values

    @field_validator("WORKER_API_KEY")
    @classmethod
    def validate_worker_key(cls, value: str) -> str:
        if value and len(value) < 32:
            raise ValueError("WORKER_API_KEY must contain at least 32 characters")
        return value

    @field_validator("PROVISIONING_SECRET_KEY")
    @classmethod
    def provisioning_secret(cls, value):
        if value.get_secret_value():
            from cryptography.fernet import Fernet

            Fernet(value.get_secret_value().encode())
        return value

    @model_validator(mode="after")
    def validate_deployment(self):
        if self.PORTAL_PUBLIC_URL:
            validate_url(self.PORTAL_PUBLIC_URL, loopback=self.ENVIRONMENT != "production")
            if origin(self.PORTAL_PUBLIC_URL) != self.PORTAL_PUBLIC_URL:
                raise ValueError(
                    "PORTAL_PUBLIC_URL must be an origin without path or trailing slash"
                )
            if self.SSO_PROVIDERS and self.SSO_PUBLIC_URL != self.PORTAL_PUBLIC_URL:
                raise ValueError("Portal and SSO must share the same public origin")
        if self.SSO_PROVIDERS:
            from cryptography.fernet import Fernet

            Fernet(self.SSO_TOKEN_KEY.get_secret_value().encode())
            validate_url(self.SSO_PUBLIC_URL, loopback=self.ENVIRONMENT != "production")
            if origin(self.SSO_PUBLIC_URL) != self.SSO_PUBLIC_URL:
                raise ValueError("SSO_PUBLIC_URL must be an origin without path or trailing slash")
            if not self.SSO_CLIENT_REDIRECT_URIS:
                raise ValueError("SSO requires at least one client redirect URI")
            for uri in self.SSO_CLIENT_REDIRECT_URIS:
                validate_url(uri, loopback=True)
            if self.SSO_MAX_STALENESS_SECONDS < self.SSO_CHECK_INTERVAL_SECONDS * 2:
                raise ValueError("SSO stale window must be at least twice its check interval")
        if self.OPENVPN_NETWORK != "10.100.0.0/16":
            raise ValueError("OPENVPN_NETWORK is fixed by the shared network allocation contract")
        if self.ENVIRONMENT == "production":
            if len(self.JWT_SECRET) < 32 or "change" in self.JWT_SECRET.lower():
                raise ValueError(
                    "Production requires a random JWT_SECRET of at least 32 characters"
                )
            if not self.WORKER_API_KEY:
                raise ValueError("Production requires WORKER_API_KEY")
            if make_url(self.DATABASE_URL).password in (
                None,
                "",
                "password",
                "your_secure_password",
            ):
                raise ValueError("Production requires a configured database password")
        return self

    # SMS Provider
    SMS_PROVIDER: Literal["generic", "twilio"] = "generic"
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_FROM: str = ""
    SMS_PROVIDER_API_KEY: str = ""
    SMS_PROVIDER_URL: str = ""

    # OpenVPN
    OPENVPN_SERVER_HOST: str = "localhost"
    OPENVPN_SERVER_PORT: int = 1194
    OPENVPN_NETWORK: str = "10.100.0.0/16"

    # Server
    SERVER_HOST: str = "0.0.0.0"
    SERVER_PORT: int = 8000
    ENVIRONMENT: str = "development"

    # CORS
    CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost:8080"]

    # Logging
    LOG_LEVEL: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8-sig",
        case_sensitive=True,
        extra="ignore",
        hide_input_in_errors=True,
    )


settings = Settings()
settings.DATABASE_URL = _normalize_database_url(settings.DATABASE_URL)
