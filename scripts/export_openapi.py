"""Export the customer OpenAPI contract without loading deployment credentials.

Run from the repository root: python -m scripts.export_openapi [--check]
No database, running API, .env file or production credentials are needed.
"""

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from factoryvpn_common.branding import DEFAULT_APP_NAME, normalize_app_name

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "website/dist/api/openapi.json"
HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options", "trace"}


def load_schema() -> dict:
    # A clean cwd avoids Settings.env_file, and a minimal environment avoids
    # importing the caller's database, provider, or signing configuration.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        # Export starts from a deterministic label, independent of deployment
        # settings; the selected build label is applied by customer_schema.
        "APP_NAME": DEFAULT_APP_NAME,
    }
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    with tempfile.TemporaryDirectory(prefix="factoryvpn-openapi-") as directory:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json; from api.main import app; print(json.dumps(app.openapi()))",
            ],
            cwd=directory,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
    return json.loads(result.stdout)


def customer_schema(source: dict, app_name: str = DEFAULT_APP_NAME) -> dict:
    app_name = normalize_app_name(app_name)
    schema = deepcopy(source)
    # Select using the generated security requirements. Machine-only operations
    # never belong to a customer client, even when added to an existing router.
    schema["paths"] = {
        path: {
            method: operation
            for method, operation in methods.items()
            if method in HTTP_METHODS
            and (
                path
                in {
                    "/api/v1/auth/login",
                    "/api/v1/auth/login/verify",
                    "/api/v1/auth/login/resend",
                    "/api/v1/auth/sso/providers",
                    "/api/v1/auth/sso/start",
                    "/api/v1/auth/sso/exchange",
                }
                or path.startswith("/api/v1/web/")
                or {"HTTPBearer": []} in operation.get("security", [])
            )
        }
        for path, methods in schema["paths"].items()
        if path.startswith("/api/v1/") and not path.startswith("/api/v1/internal/")
    }
    schema["paths"] = {path: methods for path, methods in schema["paths"].items() if methods}
    schema["info"]["title"] = f"{app_name} Customer API"
    schema["info"]["description"] = (
        "HTTP API for customer clients and custom connectors. Authenticate with JSON at "
        "POST /api/v1/auth/login for local accounts, or use the SSO start/browser/exchange flow. "
        "When challenge_required is true, complete /auth/login/verify with the emailed code before receiving a JWT. "
        f"Pass the returned {app_name} JWT as a bearer token. "
        "Access is restricted to assigned resource groups. Delegated administration requires "
        "the operation's capability within those groups; global administrators manage the instance. "
        "The API controls sessions; machine traffic requires a separately provisioned OpenVPN client. "
        "Use your deployment's API origin, not the public documentation website. "
        "Worker and gateway machine operations are intentionally excluded."
    )
    schema["servers"] = [
        {
            "url": "https://vpn-api.example.com",
            "description": "Placeholder: replace with your API origin",
        }
    ]
    schema["externalDocs"] = {
        "description": "Integration and client development guide",
        "url": "https://factoryvpn.ercoliconsulting.eu/developers.html",
    }
    schema["tags"] = [
        {
            "name": "Authentication",
            "description": "JSON login and current identity. Registration requires an administrator.",
        },
        {
            "name": "Sessions",
            "description": "Request assistance with connect in an assigned group. Read and close owned sessions, or use sessions.manage within their historical group.",
        },
        {
            "name": "Diagnostics",
            "description": "Session observations, lifecycle history and CSV exports, restricted to the session owner's current historical group or sessions.manage.",
        },
        {
            "name": "Gateways",
            "description": "Discover gateways in assigned groups using a user bearer token.",
        },
        {
            "name": "Devices",
            "description": "Read group-scoped inventory; mutations require gateways.manage.",
        },
        {
            "name": "Admin",
            "description": "Group-scoped administration with granular permissions; global administrators can manage all resources.",
        },
        {"name": "Groups", "description": "Resource groups and explicitly authorized membership."},
    ]
    schemas = schema["components"]["schemas"]
    schemas["APIError"] = {
        "type": "object",
        "properties": {"detail": {"type": "string"}},
        "required": ["detail"],
    }
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if path.startswith("/api/v1/web/"):
                description = "Same-origin portal endpoint. No Bearer tokens are returned. "
                if path in {"/api/v1/web/login/verify", "/api/v1/web/login/resend"}:
                    operation["security"] = [{"VerificationCookie": []}]
                    description += (
                        "Requires the verification cookie issued by /web/login and the challenge_token "
                        "in the request body, plus Origin and X-FactoryVPN-Portal: 1. "
                        "No authenticated portal session or CSRF token is required."
                    )
                elif path == "/api/v1/web/login":
                    operation["security"] = []
                    description += "Requires Origin and X-FactoryVPN-Portal: 1."
                elif path == "/api/v1/web/sso/start":
                    operation["security"] = [{}, {"PortalCookie": []}]
                    description += (
                        "Requires Origin and X-FactoryVPN-Portal: 1. Initial login needs no session. "
                        "When reauthenticate=true, an authenticated global SSO administrator's "
                        "portal session and X-CSRF-Token are also required."
                    )
                elif path == "/api/v1/web/logout":
                    operation["security"] = [{}, {"PortalCookie": []}]
                    description += (
                        "Requires Origin. With a portal cookie, even expired or revoked, "
                        "X-CSRF-Token is required. Without that cookie, "
                        "X-FactoryVPN-Portal: 1 is required instead."
                    )
                else:
                    operation["security"] = [{"PortalCookie": []}]
                    description += (
                        "Requires an authenticated portal session. "
                        "Mutations require Origin and X-CSRF-Token."
                    )
                operation["description"] = operation.get("description", "") + "\n\n" + description
            elif path.startswith("/api/v1/backups"):
                operation["security"] = [{"PortalCookie": []}]
                operation["description"] = operation.get("description", "") + (
                    "\n\nGlobal administrators with an authenticated portal session only. "
                    "Mutations require Origin, X-CSRF-Token and identity confirmation within five minutes. "
                    "Queued operations require a UUID Idempotency-Key. Import accepts an encrypted "
                    f"{app_name} binary archive, up to the configured limit (maximum 512 MiB). "
                    "A missing recent identity confirmation returns 428; maintenance returns 503."
                )
            elif path in {"/api/v1/auth/password", "/api/v1/auth/register"}:
                operation["security"] = [{"HTTPBearer": []}]
            errors = {}
            if path.startswith("/api/v1/web/") or operation.get("security"):
                errors.update(
                    {
                        "401": "Invalid, expired or revoked credentials, or an inactive account.",
                        "403": (
                            "Missing or malformed authentication, insufficient permissions, "
                            "or a failed portal Origin/header/CSRF check."
                        ),
                    }
                )
            else:
                errors["401"] = "Invalid username or password, or an inactive account."
            if "{" in path or (
                method == "post" and path in {"/api/v1/sessions/", "/api/v1/devices/"}
            ):
                errors["404"] = "Resource not found or session not visible to this user."
            if method in {"post", "put", "patch", "delete"}:
                errors["409"] = (
                    "Resource conflict, occupied peers, pending cleanup or incompatible operation."
                )
            if path == "/api/v1/sessions/" and method == "post":
                errors["503"] = "No VPN addresses available."
            if path in {
                f"/api/v1/{surface}/login/{action}"
                for surface in ("auth", "web")
                for action in ("verify", "resend")
            }:
                errors["400"] = "Invalid or expired challenge, code or browser binding."
                errors["429"] = (
                    "Email verification locked, resend cooldown or sending limit reached."
                )
                if path.endswith("/resend"):
                    errors["503"] = "Email verification or mail delivery is unavailable."
            if (
                path.startswith("/api/v1/admin/users") and method in {"post", "delete"}
            ) or path == "/api/v1/auth/register":
                errors["400"] = "Request rejected by an account management rule."
            for code, description in errors.items():
                operation["responses"].setdefault(
                    code,
                    {
                        "description": description,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/APIError"}
                            }
                        },
                    },
                )
            admin_only = (
                path
                in {
                    "/api/v1/auth/register",
                    "/api/v1/sessions/expire-stale",
                    "/api/v1/telemetry/server",
                    "/api/v1/telemetry/server/history",
                    "/api/v1/admin/server",
                    "/api/v1/admin/server/smtp-check",
                }
                or path.endswith("/authentication")
                or "/identities" in path
                or path.startswith("/api/v1/admin/enrollments")
            )
            if admin_only:
                operation["description"] = (
                    operation.get("description", "") + "\n\nRequires an administrator account."
                )
                operation["x-required-role"] = "admin"
            required = None
            if path.startswith("/api/v1/telemetry/gateways"):
                required = "telemetry.read"
            if path.startswith("/api/v1/gateways/") and path.endswith(
                ("/technical-logs", "/technical-logs.csv")
            ):
                required = "logs.read"
            for resource, permission in {
                "users": "users.manage",
                "groups": "groups.manage",
                "gateways": "gateways.manage",
                "sessions": "sessions.manage",
                "audit": "audit.read",
                "stats": "audit.read",
            }.items():
                if path in {
                    f"/api/v1/admin/{resource}",
                    f"/api/v1/admin/{resource}.csv",
                } or path.startswith(f"/api/v1/admin/{resource}/"):
                    required = permission
            if path == "/api/v1/devices/" and method == "post":
                required = "gateways.manage"
            if required and not admin_only:
                operation["x-required-permission"] = required
                operation["description"] = operation.get("description", "") + (
                    f"\n\nRequires {required} within the caller's assigned groups, or global administration."
                )

    # Keep only schemas reachable from published operations, including nested
    # model references. Do not ship machine request models or credential schemes.
    retained = set()

    def collect(value):
        if isinstance(value, dict):
            ref = value.get("$ref", "")
            if ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[1]
                if name not in retained:
                    retained.add(name)
                    collect(schemas[name])
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(schema["paths"])
    schema["components"]["schemas"] = {name: schemas[name] for name in sorted(retained)}
    schema["components"]["securitySchemes"] = {
        "HTTPBearer": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
        "PortalCookie": {
            "type": "apiKey",
            "in": "cookie",
            "name": "__Host-factoryvpn_portal",
            "description": "Portal session cookie. Local HTTP development uses factoryvpn_portal.",
        },
        "VerificationCookie": {
            "type": "apiKey",
            "in": "cookie",
            "name": "__Host-factoryvpn_verification",
            "description": (
                "Browser binding issued by /web/login for email verification, before a session exists. "
                "Local HTTP development uses factoryvpn_verification."
            ),
        },
    }
    return schema


def render_schema(schema: dict) -> str:
    return json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--app-name",
        type=normalize_app_name,
        default=DEFAULT_APP_NAME,
        help="Display label (default: FactoryVPN); deployment APP_NAME is deliberately ignored",
    )
    parser.add_argument(
        "--check", action="store_true", help="Fail if the published specification is stale"
    )
    args = parser.parse_args()
    content = render_schema(customer_schema(load_schema(), app_name=args.app_name))
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != content:
            print("OpenAPI export is stale. Run python -m scripts.export_openapi.", file=sys.stderr)
            return 1
        print("OpenAPI export is up to date.")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    print(f"Wrote customer OpenAPI specification to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
