"""OIDC protocol adapter with a Cognito logout/revocation profile.

The rest of FactoryVPN consumes verified identities, never vendor-specific roles.
HTTP requests do not follow redirects and endpoints must belong to configured origins.
"""

import asyncio
import time
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oidc.core import CodeIDToken
from joserfc import jwt
from joserfc.jwk import KeySet
from joserfc.jws import JWSRegistry
from joserfc.errors import InvalidKeyIdError

from api.config import settings
from api.time_utils import utc_now


class IdentityRejected(Exception):
    """Definitive authentication failure, without provider secrets in the message."""


class ProviderUnavailable(Exception):
    """Transient error; never interpreted as a successful identity verification."""


def get_provider(identifier):
    provider = settings.SSO_PROVIDERS.get(identifier)
    if not provider or not provider.enabled:
        raise IdentityRejected("Provider unavailable")
    return provider


def http_client():
    return httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False)


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
    )


class OIDCAdapter:
    # Bounded by configured provider count; no user-supplied cache keys.
    metadata_cache = {}
    jwks_cache = {}

    def __init__(self, identifier):
        self.identifier = identifier
        self.provider = get_provider(identifier)
        self.fingerprint = self.provider.fingerprint()
        self.callback_uri = f"{settings.SSO_PUBLIC_URL}/api/v1/auth/sso/{identifier}/callback"

    async def fetch_json(self, url):
        self.provider.check_endpoint(url)
        try:
            async with http_client() as client:
                response = await client.get(url)
            response.raise_for_status()
            if len(response.content) > 1_000_000:
                raise ValueError("Oversized provider response")
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid provider response")
            return data
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderUnavailable() from exc

    async def metadata(self):
        cached = self.metadata_cache.get(self.identifier)
        if cached and cached[0] == self.fingerprint and cached[1] > time.monotonic():
            return cached[2]
        data = await self.fetch_json(
            self.provider.issuer.rstrip("/") + "/.well-known/openid-configuration"
        )
        if data.get("issuer") != self.provider.issuer:
            raise IdentityRejected("Issuer mismatch")
        try:
            for key in (
                "authorization_endpoint",
                "token_endpoint",
                "jwks_uri",
                "userinfo_endpoint",
            ):
                self.provider.check_endpoint(data[key])
            for key in ("revocation_endpoint", "end_session_endpoint"):
                if data.get(key):
                    self.provider.check_endpoint(data[key])
        except (ValueError, KeyError, TypeError) as exc:
            raise IdentityRejected("Invalid provider metadata") from exc
        self.metadata_cache[self.identifier] = (self.fingerprint, time.monotonic() + 300, data)
        return data

    async def authorization_url(self, secrets):
        metadata = await self.metadata()
        parameters = {"prompt": "login", "max_age": 0} if secrets.get("reauth_user_id") else {}
        if self.provider.mfa_request_acr:
            parameters["acr_values"] = self.provider.mfa_request_acr
        async with oauth_client(self.provider, self.callback_uri) as client:
            url, _ = client.create_authorization_url(
                metadata["authorization_endpoint"],
                state=secrets["state"],
                nonce=secrets["nonce"],
                code_verifier=secrets["verifier"],
                **parameters,
            )
        return url

    async def jwks(self, *, force=False):
        cached = self.jwks_cache.get(self.identifier)
        if not force and cached and cached[0] == self.fingerprint and cached[1] > time.monotonic():
            return cached[2]
        data = await self.fetch_json((await self.metadata())["jwks_uri"])
        # Asymmetric signing only. Never allow a provider's symmetric key as a JWT verifier.
        data = {
            "keys": [
                key
                for key in data.get("keys", [])
                if key.get("kty") == "RSA" and key.get("use", "sig") == "sig"
            ]
        }
        self.jwks_cache[self.identifier] = (self.fingerprint, time.monotonic() + 300, data)
        return data

    async def validate_id_token(self, tokens, nonce):
        try:
            keys = KeySet.import_key_set(await self.jwks())
            try:
                decoded = jwt.decode(
                    tokens["id_token"], keys, registry=JWSRegistry(algorithms=["RS256"])
                )
            except InvalidKeyIdError:
                keys = KeySet.import_key_set(await self.jwks(force=True))
                decoded = jwt.decode(
                    tokens["id_token"], keys, registry=JWSRegistry(algorithms=["RS256"])
                )
            claims = CodeIDToken(
                decoded.claims,
                decoded.header,
                {
                    "iss": {"essential": True, "value": self.provider.issuer},
                    "aud": {"essential": True, "value": self.provider.client_id},
                },
                {
                    "nonce": nonce,
                    "client_id": self.provider.client_id,
                    "access_token": tokens["access_token"],
                },
            )
            claims.validate(leeway=30)
            if not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 255:
                raise ValueError("Invalid subject")
            if self.provider.kind == "cognito" and claims.get("token_use") != "id":
                raise ValueError("Not an ID token")
            return dict(claims)
        except ProviderUnavailable:
            raise
        except Exception as exc:
            raise IdentityRejected("Invalid ID token") from exc

    async def exchange(self, code, secrets):
        metadata = await self.metadata()
        try:
            async with oauth_client(self.provider, self.callback_uri) as client:
                # Anchor expires_in before the request, not after JWKS/UserInfo latency.
                # The provider may give its access token a shorter life than its ID token.
                requested_at = utc_now()
                tokens = await client.fetch_token(
                    metadata["token_endpoint"], code=code, code_verifier=secrets["verifier"]
                )
            if str(tokens.get("token_type", "")).lower() != "bearer":
                raise ValueError("Unsupported token type")
            lifetime = int(tokens["expires_in"])
            if not 0 < lifetime <= 86400:
                raise ValueError("Invalid token lifetime")
        except httpx.HTTPError as exc:
            raise ProviderUnavailable() from exc
        except Exception as exc:
            raise IdentityRejected("Code rejected") from exc
        claims = await self.validate_id_token(tokens, secrets["nonce"])
        await self.userinfo(tokens["access_token"], claims["sub"])
        return dict(tokens), claims, requested_at + timedelta(seconds=lifetime)

    async def userinfo(self, access_token, subject):
        metadata = await self.metadata()
        try:
            async with http_client() as client:
                response = await client.get(
                    metadata["userinfo_endpoint"],
                    headers={"Authorization": "Bearer " + access_token},
                )
            if response.status_code in (400, 401, 403):
                raise IdentityRejected("Provider session rejected")
            response.raise_for_status()
            if len(response.content) > 100_000:
                raise ValueError("Oversized userinfo")
            if response.json().get("sub") != subject:
                raise IdentityRejected("UserInfo subject mismatch")
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise ProviderUnavailable() from exc

    async def logout_url(self):
        destination = settings.SSO_PUBLIC_URL + "/api/v1/auth/sso/signed-out"
        if self.provider.kind == "cognito":
            endpoint = self.provider.logout_endpoint
            parameters = {"client_id": self.provider.client_id, "logout_uri": destination}
        else:
            endpoint = (await self.metadata()).get("end_session_endpoint")
            parameters = {
                "client_id": self.provider.client_id,
                "post_logout_redirect_uri": destination,
            }
        if not endpoint:
            return None
        self.provider.check_endpoint(endpoint)
        return endpoint + "?" + urlencode(parameters)

    async def revoke(self, encrypted_tokens):
        from api.services.sso_service import decrypt

        tokens = decrypt(encrypted_tokens)
        token = tokens.get("refresh_token")
        if not token:
            return
        metadata = await self.metadata()
        endpoint = metadata.get("revocation_endpoint")
        if not endpoint and self.provider.kind == "cognito":
            endpoint = self.provider.logout_endpoint.removesuffix("/logout") + "/oauth2/revoke"
        if not endpoint:
            return
        self.provider.check_endpoint(endpoint)
        async with oauth_client(self.provider, self.callback_uri) as client:
            response = await client.revoke_token(
                endpoint, token=token, token_type_hint="refresh_token"
            )
            response.raise_for_status()


async def best_effort_revoke(provider_id, encrypted_tokens):
    # Local revocation always commits first. Remote failures must not undo it.
    try:
        async with asyncio.timeout(10):
            await OIDCAdapter(provider_id).revoke(encrypted_tokens)
        return True
    except Exception:
        return False
