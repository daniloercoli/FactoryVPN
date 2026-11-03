# ruff: noqa: E402
"""Real application, trusting only the ephemeral local test provider certificate."""
import os
import ssl
import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client

if (
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL")
    or os.environ.get("DATABASE_URL") != os.environ["FACTORYVPN_TEST_DATABASE_URL"]
):
    raise RuntimeError("A disposable test database is required")

from api.services import oidc

context = ssl.create_default_context(cafile=os.environ["FACTORYVPN_TEST_CA"])


def http_client():
    return httpx.AsyncClient(verify=context, timeout=5, follow_redirects=False, trust_env=False)


def oauth_client(provider, redirect_uri):
    return AsyncOAuth2Client(
        provider.client_id,
        provider.client_secret.get_secret_value(),
        token_endpoint_auth_method=provider.token_endpoint_auth_method,
        revocation_endpoint_auth_method=provider.token_endpoint_auth_method,
        scope="openid profile email",
        redirect_uri=redirect_uri,
        code_challenge_method="S256",
        timeout=5,
        follow_redirects=False,
        trust_env=False,
        verify=context,
    )


oidc.http_client = http_client
oidc.oauth_client = oauth_client
from api.main import app  # noqa: E402, F401
from api.routers import sessions


async def accept_test_sms(**kwargs):
    # Browser fixtures simulate provider acceptance, never contact an SMS service.
    return True


sessions.send_sms = accept_test_sms
