"""Load the two script packages without altering their production entry points."""

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture
def load_script():
    def load(folder, name):
        key = folder.replace("-", "_") + "_" + name
        spec = importlib.util.spec_from_file_location(
            key, Path(__file__).parents[1] / folder / (name + ".py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
        return module

    return load


@pytest.fixture(autouse=True)
def provisioning_encryption(monkeypatch):
    from cryptography.fernet import Fernet
    from pydantic import SecretStr
    from api.config import settings

    monkeypatch.setattr(
        settings, "PROVISIONING_SECRET_KEY", SecretStr(Fernet.generate_key().decode())
    )


@pytest.fixture(autouse=True)
def local_email_delivery(monkeypatch):
    from api.config import settings
    from api.services import mail_service
    from pydantic import SecretStr
    import email_helpers

    email_helpers.messages.clear()
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(settings, "SMTP_FROM", "factoryvpn@example.test")
    monkeypatch.setattr(settings, "EMAIL_CODE_KEY", SecretStr("test-mail-key-" + "k" * 40))
    monkeypatch.setattr(mail_service, "send_code", email_helpers.capture)
