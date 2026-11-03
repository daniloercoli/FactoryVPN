"""FactoryVPN browser SSO test client. Credentials are written only to a new private file.

python -m scripts.sso_login login --api-url https://vpn.example.com --provider aws --output .runtime/sso.json
python -m scripts.sso_login logout --session .runtime/sso.json
"""

import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

import httpx


def api_origin(value):
    url = urlsplit(value)
    if (
        not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/")
        or (
            url.scheme != "https"
            and not (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1"))
        )
    ):
        raise ValueError("Use an HTTPS API origin (HTTP is allowed only on loopback)")
    return value.rstrip("/")


def callback_code(path, expected_state):
    url = urlsplit(path)
    values = parse_qs(url.query)
    if (
        url.path != "/callback"
        or len(values.get("state", [])) != 1
        or not secrets.compare_digest(values["state"][0].encode(), expected_state.encode())
        or len(values.get("code", [])) != 1
        or not 32 <= len(values["code"][0]) <= 128
    ):
        raise ValueError("Invalid callback")
    return values["code"][0]


def save_private(path, payload):
    # Refuse symlinks and existing files; never truncate an unrelated credential file.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(payload, stream)
        stream.write("\n")


def response_json(response):
    if response.status_code != 200:
        raise RuntimeError(f"FactoryVPN rejected the request (HTTP {response.status_code})")
    return response.json()


def login(args):
    origin = api_origin(args.api_url)
    if args.output.exists():
        raise ValueError("Output file already exists; choose a new file or sign out first")
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = secrets.token_urlsafe(24)
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if self.headers.get("Host") != f"127.0.0.1:{args.port}":
                    raise ValueError("Invalid host")
                code = callback_code(self.path, state)
            except ValueError:
                self.send_error(400, "Invalid FactoryVPN login callback")
                return
            result["code"] = code
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(b"FactoryVPN login received. Return to the terminal.")

        def log_message(self, *args):
            pass  # The callback query contains a one-time credential.

    with HTTPServer(("127.0.0.1", args.port), Callback) as server:
        server.timeout = 1
        with httpx.Client(
            timeout=15, verify=args.ca_file or True, follow_redirects=False, trust_env=False
        ) as client:
            start = response_json(
                client.post(
                    origin + "/api/v1/auth/sso/start",
                    json={
                        "provider_id": args.provider,
                        "redirect_uri": f"http://127.0.0.1:{args.port}/callback",
                        "code_challenge": challenge,
                        "state": state,
                    },
                )
            )
            authorization_url = start["authorization_url"]
            if not authorization_url.startswith(origin + "/api/v1/auth/sso/authorize/"):
                raise ValueError("Unexpected authorization origin; verify the server public URL")
            print("Opening browser for FactoryVPN SSO. Complete login on this computer.")
            if not webbrowser.open(authorization_url):
                print("Open this login URL in your browser:", authorization_url)
            deadline = time.monotonic() + 300
            while not result and time.monotonic() < deadline:
                server.handle_request()
            if not result:
                raise TimeoutError("Login was not completed within five minutes")
            credentials = response_json(
                client.post(
                    origin + "/api/v1/auth/sso/exchange",
                    json={"code": result["code"], "code_verifier": verifier},
                )
            )
    save_private(args.output, {"api_url": origin, **credentials})
    print(f"FactoryVPN credentials saved in {args.output} (owner read/write only).")


def logout(args):
    credentials = json.loads(args.session.read_text())
    origin = api_origin(credentials["api_url"])
    with httpx.Client(
        timeout=30, verify=args.ca_file or True, follow_redirects=False, trust_env=False
    ) as client:
        response = response_json(
            client.post(
                origin + "/api/v1/auth/logout",
                headers={"Authorization": "Bearer " + credentials["access_token"]},
            )
        )
    args.session.unlink()
    for url in response.get("provider_logout_urls", []):
        if urlsplit(url).scheme == "https":
            webbrowser.open(url)
    print("FactoryVPN sessions revoked; provider token revocation is queued on the server.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sign_in = commands.add_parser("login")
    sign_in.add_argument("--api-url", required=True)
    sign_in.add_argument("--provider", required=True)
    sign_in.add_argument("--output", type=Path, required=True)
    sign_in.add_argument("--port", type=int, default=8765)
    sign_in.add_argument(
        "--ca-file", help="CA PEM for the FactoryVPN API; browser trust is configured separately"
    )
    sign_out = commands.add_parser("logout")
    sign_out.add_argument("--session", type=Path, required=True)
    sign_out.add_argument("--ca-file")
    args = parser.parse_args()
    try:
        (login if args.command == "login" else logout)(args)
    except (ValueError, KeyError, OSError, RuntimeError, httpx.HTTPError) as exc:
        # HTTP library messages can include a URL; expose only the exception type for transport errors.
        parser.exit(
            1,
            f"FactoryVPN SSO failed: {type(exc).__name__ if isinstance(exc, httpx.HTTPError) else exc}\n",
        )


if __name__ == "__main__":
    main()
