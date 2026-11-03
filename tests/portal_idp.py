"""Local HTTPS OIDC simulator; real PKCE, signed ID tokens, one-use codes and revocation."""

import os
import secrets
import time
from html import escape
from urllib.parse import urlencode, parse_qs
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from joserfc import jwt
from joserfc.jwk import RSAKey
import base64
import hashlib

if not os.getenv("FACTORYVPN_TEST_DATABASE_URL"):
    raise RuntimeError("Test fixture only")

app = FastAPI()
issuer = os.environ["FACTORYVPN_TEST_IDP_URL"]
portal = os.environ["PORTAL_PUBLIC_URL"]
key = RSAKey.generate_key(2048, parameters={"kid": "browser-test"})
codes = {}
tokens = {}
refresh_tokens = {}


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/.well-known/openid-configuration")
async def metadata():
    return {
        "issuer": issuer,
        **{
            key: issuer + path
            for key, path in {
                "authorization_endpoint": "/authorize",
                "token_endpoint": "/token",
                "userinfo_endpoint": "/userinfo",
                "jwks_uri": "/keys",
                "revocation_endpoint": "/revoke",
            }.items()
        },
    }


@app.get("/keys")
async def keys():
    return {"keys": [key.as_dict(private=False)]}


@app.get("/authorize", response_class=HTMLResponse)
async def authorize(request: Request):
    params = dict(request.query_params)
    if (
        params.get("redirect_uri") != portal + "/api/v1/auth/sso/test/callback"
        or params.get("client_id") != "factoryvpn-browser"
        or params.get("code_challenge_method") != "S256"
    ):
        raise HTTPException(400)
    inputs = "".join(
        f'<input type="hidden" name="{escape(k)}" value="{escape(v)}">' for k, v in params.items()
    )
    return (
        '<html lang="it"><title>Provider OIDC di test</title><h1>Provider di test</h1><form action="/approve" method="get">'
        + inputs
        + "<button>Continua come utente SSO</button></form></html>"
    )


@app.get("/approve")
async def approve(request: Request):
    params = dict(request.query_params)
    if params.get("redirect_uri") != portal + "/api/v1/auth/sso/test/callback":
        raise HTTPException(400)
    code = secrets.token_urlsafe(32)
    codes[code] = params
    return RedirectResponse(
        params["redirect_uri"] + "?" + urlencode({"code": code, "state": params["state"]}),
        status_code=302,
    )


@app.post("/token")
async def token(request: Request):
    body = parse_qs((await request.body()).decode())
    params = codes.pop(body.get("code", [""])[0], None)
    challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(body.get("code_verifier", [""])[0].encode()).digest()
        )
        .rstrip(b"=")
        .decode()
    )
    expected = (
        "Basic " + base64.b64encode(b"factoryvpn-browser:isolated-test-client-secret").decode()
    )
    if (
        not params
        or params["code_challenge"] != challenge
        or request.headers.get("authorization") != expected
    ):
        raise HTTPException(400)
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    tokens[access] = "browser-user"
    refresh_tokens[refresh] = access
    now = int(time.time())
    claims = {
        "iss": issuer,
        "aud": "factoryvpn-browser",
        "sub": "browser-user",
        "iat": now,
        "auth_time": now,
        "exp": now + 3600,
        "nonce": params["nonce"],
        "token_use": "id",
        "amr": ["pwd", "mfa"],
    }
    return {
        "token_type": "Bearer",
        "expires_in": 3600,
        "access_token": access,
        "refresh_token": refresh,
        "id_token": jwt.encode({"alg": "RS256", "kid": key.kid}, claims, key),
    }


@app.get("/userinfo")
async def userinfo(request: Request):
    access = request.headers.get("authorization", "").removeprefix("Bearer ")
    if access not in tokens:
        raise HTTPException(401)
    return {"sub": tokens[access]}


@app.post("/revoke")
async def revoke(request: Request):
    body = parse_qs((await request.body()).decode())
    value = body.get("token", [""])[0]
    tokens.pop(refresh_tokens.pop(value, value), None)
    return {}


@app.get("/logout")
async def logout(logout_uri: str):
    if logout_uri != portal + "/api/v1/auth/sso/signed-out":
        raise HTTPException(400)
    return RedirectResponse(logout_uri, status_code=302)
