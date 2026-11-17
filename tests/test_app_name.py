"""A deployment label reaches browser-facing output without changing protocols."""

from contextlib import contextmanager
from html import escape
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
import zipfile

from httpx import ASGITransport, AsyncClient
import pytest
from pydantic import ValidationError

from api.config import Settings, settings
from api.routers.sso import signed_out
from api.services import mail_service, server_status, telemetry


def test_display_name_default_and_whitespace(monkeypatch):
    monkeypatch.delenv("APP_NAME", raising=False)
    assert Settings(_env_file=None).APP_NAME == "FactoryVPN"
    assert Settings(_env_file=None, APP_NAME="  Assistenza VPN  ").APP_NAME == "Assistenza VPN"
    assert Settings(_env_file=None, APP_NAME="🛠" * 80).APP_NAME == "🛠" * 80


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "x" * 81,
        "VPN\n",
        "VPN\rName",
        "VPN\tName",
        "VPN\x00",
        "VPN\x7f",
        "VPN\x85",
        "VPN\u2028",
        "VPN\u2029",
    ],
)
def test_display_name_rejects_empty_multiline_and_control_values(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, APP_NAME=value)


def test_custom_name_is_applied_to_both_mail_templates(monkeypatch):
    label = 'Assistenza & "VPN"'
    monkeypatch.setattr(settings, "APP_NAME", label)
    smtp = Mock()
    smtp.noop.return_value = (250, b"OK")
    smtp.send_message.return_value = {}

    @contextmanager
    def transport(**kwargs):
        yield smtp

    monkeypatch.setattr(mail_service, "transport", transport)
    mail_service.deliver("tech@example.test", "123456", "login_web")
    assert server_status.probe("tech@example.test") == "accepted"
    code, probe = [call.args[0] for call in smtp.send_message.call_args_list]
    assert str(code["Subject"]) == f"{label} — codice di verifica"
    assert f"Il codice {label} per accedere al portale è: 123456" in code.get_content()
    assert str(probe["Subject"]) == f"{label} — verifica SMTP"
    assert f"Stato server di {label}." in probe.get_content()
    assert "FactoryVPN" not in code.get_content() + probe.get_content()


async def test_logout_escapes_custom_name_and_server_label_uses_it(monkeypatch):
    label = 'VPN <script>alert("name")</script> & assistenza'
    monkeypatch.setattr(settings, "APP_NAME", label)
    monkeypatch.setattr(settings, "PORTAL_PUBLIC_URL", "")
    response = await signed_out()
    assert response.body.decode() == (
        f"<p>{escape(label)}: signed out. You can close this window.</p>"
    )
    assert "<script>" not in response.body.decode()
    assert telemetry.project(None, server=True)["display_name"] == f"Server {label}"


@pytest.mark.parametrize("root_path", ["", "/factoryvpn"])
async def test_api_docs_escape_title_and_preserve_schema_and_mount_urls(monkeypatch, root_path):
    from api.main import app

    label = 'VPN </title><script>alert("brand")</script> & assistenza'
    title = f"{label} API"
    monkeypatch.setattr(app, "title", title)
    monkeypatch.setattr(app, "openapi_schema", None)
    async with AsyncClient(
        transport=ASGITransport(app=app, root_path=root_path), base_url="http://test"
    ) as client:
        for path, suffix in [("/docs", "Swagger UI"), ("/redoc", "ReDoc")]:
            response = await client.get(path)
            assert response.status_code == 200
            assert f"<title>{escape(title + ' - ' + suffix)}</title>" in response.text
            assert '<script>alert("brand")</script>' not in response.text
            assert f"{root_path}/openapi.json" in response.text
            assert (await client.head(path)).status_code == 200
            if path == "/docs":
                assert f"{root_path}/docs/oauth2-redirect" in response.text
        assert (await client.get("/docs/oauth2-redirect")).status_code == 200
        schema = app.openapi()
    assert schema["info"]["title"] == title
    assert not {"/docs", "/redoc", "/docs/oauth2-redirect"}.intersection(schema["paths"])


def test_new_api_process_uses_configured_label_in_metadata_and_root(tmp_path):
    label = "Assistenza Aziendale"
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "APP_NAME": label,
        "ENVIRONMENT": "development",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio,json; from api.main import app,root; "
            "print(json.dumps({'info':app.openapi()['info'],'root':asyncio.run(root())}))",
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        text=True,
        capture_output=True,
    )
    output = json.loads(result.stdout)
    assert output["info"]["title"] == f"{label} API"
    assert output["info"]["description"].startswith(label)
    assert output["root"]["name"] == f"{label} API"


def test_downloaded_gateway_instructions_use_label_and_keep_technical_paths(monkeypatch):
    from scripts.provisioning_worker import gateway_bundle

    label = "Assistenza Aziendale"
    monkeypatch.setattr(settings, "APP_NAME", label)
    monkeypatch.setattr(settings, "PORTAL_PUBLIC_URL", "https://vpn.example.test")
    job = SimpleNamespace(
        peer_id=uuid4(),
        parameters={
            "activation_mode": "wifi",
            "phone_number": None,
            "mac_address": "00:11:22:33:44:55",
        },
    )
    bundle = gateway_bundle(job, b"profile", "api-key", "sms-key")
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        instructions = archive.read("LEGGIMI.txt").decode()
        assert instructions.startswith(f"{label} — installazione gateway")
        assert f"installare l'agente {label}." in instructions
        assert "FactoryVPN" not in instructions
        assert "/etc/factoryvpn/" in instructions
        assert "factoryvpn-agent.service" in instructions
        assert archive.read("gateway.ovpn") == b"profile"
