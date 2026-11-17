"""Deployment-owned SMTP transport. No provider API or automatic fallback."""

import asyncio
from contextlib import contextmanager, suppress
from email.message import EmailMessage
import re
import smtplib
import ssl
from api.config import settings


def email_address(value):
    value = (value or "").strip().lower()
    if len(value) > 255 or not re.fullmatch(
        r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value
    ):
        raise ValueError("Invalid email address")
    local, domain = value.split("@")
    if (
        local.startswith(".")
        or local.endswith(".")
        or ".." in value
        or any(label.startswith("-") or label.endswith("-") for label in domain.split("."))
    ):
        raise ValueError("Invalid email address")
    return value


def configured():
    return bool(
        settings.SMTP_HOST
        and settings.SMTP_FROM
        and len(settings.EMAIL_CODE_KEY.get_secret_value()) >= 32
    )


@contextmanager
def transport(timeout=10):
    context = ssl.create_default_context(cafile=settings.SMTP_CA_FILE or None)
    implementation = smtplib.SMTP_SSL if settings.SMTP_SECURITY == "tls" else smtplib.SMTP
    options = {"context": context} if settings.SMTP_SECURITY == "tls" else {}
    smtp = implementation(settings.SMTP_HOST, settings.SMTP_PORT, timeout=timeout, **options)
    try:
        smtp.ehlo()
        if settings.SMTP_SECURITY == "starttls":
            smtp.starttls(context=context)
            smtp.ehlo()
        if settings.SMTP_USERNAME:
            smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD.get_secret_value())
        yield smtp
    finally:
        # QUIT is cleanup: its failure cannot undo a successful DATA/NOOP or
        # replace the original TLS/authentication/delivery exception.
        try:
            with suppress(OSError, smtplib.SMTPException):
                smtp.quit()
        finally:
            with suppress(OSError, smtplib.SMTPException):
                smtp.close()


def deliver(recipient, code, purpose):
    message = EmailMessage()
    message["From"] = email_address(settings.SMTP_FROM)
    message["To"] = email_address(recipient)
    message["Subject"] = f"{settings.APP_NAME} — codice di verifica"
    labels = {
        "login_web": "accedere al portale",
        "login_api": "accedere alle API",
        "confirm": "confermare un'operazione sensibile",
        "enable": "attivare la verifica in due passaggi",
        "disable": "disattivare la verifica in due passaggi",
        "change_old": "autorizzare il cambio email",
        "change_new": "confermare il nuovo indirizzo",
    }
    message.set_content(
        f"Il codice {settings.APP_NAME} per {labels[purpose]} è: {code}\n\nScade entro 5 minuti ed è utilizzabile una sola volta.\nNon condividerlo. Se non hai richiesto questa operazione, ignora il messaggio e contatta il tuo amministratore.\n"
    )
    with transport() as smtp:
        if smtp.send_message(message):
            raise OSError("Mail rejected")


async def send_code(recipient, code, purpose):
    await asyncio.to_thread(deliver, recipient, code, purpose)
