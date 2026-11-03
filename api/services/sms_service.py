"""SMS acceptance adapters. Acceptance is distinct from handset delivery."""

import logging
import re
from dataclasses import dataclass
from typing import Literal
import httpx
from api.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SMSResult:
    outcome: Literal["accepted", "rejected", "unconfirmed", "not_configured"]

    def __bool__(self):
        return self.outcome == "accepted"


async def send_sms(phone_number: str, message: str) -> bool:
    """Compatibility API for callers that need only acceptance."""
    return bool(await send_sms_detailed(phone_number, message))


async def send_sms_detailed(phone_number: str, message: str) -> SMSResult:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if settings.SMS_PROVIDER == "twilio":
                if not (
                    re.fullmatch(r"AC[0-9a-fA-F]{32}", settings.TWILIO_ACCOUNT_SID)
                    and settings.TWILIO_AUTH_TOKEN
                    and settings.TWILIO_FROM
                ):
                    logger.warning("Twilio is not configured; no message was sent")
                    return SMSResult("not_configured")
                response = await client.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json",
                    auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
                    data={"To": phone_number, "From": settings.TWILIO_FROM, "Body": message},
                )
                if not response.is_success:
                    logger.warning("Twilio rejected SMS: HTTP %s", response.status_code)
                    return SMSResult("rejected" if response.status_code < 500 else "unconfirmed")
                payload = response.json()
                if not isinstance(payload, dict):
                    return SMSResult("unconfirmed")
                accepted = bool(
                    payload.get("sid")
                    and payload.get("status")
                    in {"accepted", "queued", "sending", "sent", "delivered"}
                )
                return SMSResult(
                    "accepted"
                    if accepted
                    else (
                        "rejected"
                        if payload.get("status") in {"failed", "undelivered", "canceled"}
                        else "unconfirmed"
                    )
                )
            if not settings.SMS_PROVIDER_API_KEY or not settings.SMS_PROVIDER_URL:
                logger.warning("SMS provider is not configured; no message was sent")
                return SMSResult("not_configured")
            response = await client.post(
                settings.SMS_PROVIDER_URL,
                headers={"Authorization": f"Bearer {settings.SMS_PROVIDER_API_KEY}"},
                json={"to": phone_number, "message": message},
            )
            if not response.is_success:
                logger.warning("SMS provider rejected message: HTTP %s", response.status_code)
            return SMSResult(
                "accepted"
                if response.is_success
                else "rejected" if response.status_code < 500 else "unconfirmed"
            )
    except (httpx.HTTPError, ValueError, TypeError):
        logger.warning("SMS acceptance could not be confirmed")
        return SMSResult("unconfirmed")
