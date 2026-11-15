"""Opaque content revisions, checked under the existing allocation lock.

Revisions include membership and identity links, even when a legacy client or a
different endpoint changed them. They are separate from authentication revocation.
"""

import hashlib
import hmac
import json
from fastapi import HTTPException
from sqlalchemy import select
from api.config import settings
from api.models.sso import ExternalIdentity
from api.models.group import UserGroup
from api.models.gateway import Gateway


def revision(value):
    return hmac.new(
        settings.JWT_SECRET.encode(),
        json.dumps(value, sort_keys=True, default=str).encode(),
        hashlib.sha256,
    ).hexdigest()


async def user_revision(db, user, groups=None):
    if groups is None:
        groups = (
            await db.scalars(select(UserGroup.group_id).where(UserGroup.user_id == user.id))
        ).all()
    identities = (
        await db.scalars(select(ExternalIdentity).where(ExternalIdentity.user_id == user.id))
    ).all()
    return revision(
        {
            "type": "user",
            "id": str(user.id),
            "username": user.username,
            "email": user.email,
            "email_verified_at": user.email_verified_at,
            "email_two_step": user.email_two_step,
            "full_name": user.full_name,
            "is_active": user.is_active,
            "is_admin": user.is_admin,
            "auth_mode": user.auth_mode,
            "token_version": user.token_version,
            "permissions": sorted(user.permissions or []),
            "groups": sorted(str(g) for g in groups),
            "identities": sorted((i.provider_id, i.issuer, i.subject) for i in identities),
        }
    )


async def group_revision(db, group):
    members = (
        await db.scalars(select(UserGroup.user_id).where(UserGroup.group_id == group.id))
    ).all()
    gateways = (await db.scalars(select(Gateway.id).where(Gateway.group_id == group.id))).all()
    return revision(
        {
            "type": "group",
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "members": sorted(str(m) for m in members),
            "gateways": sorted(str(g) for g in gateways),
        }
    )


def gateway_revision(gateway):
    return revision(
        {
            "type": "gateway",
            **{
                field: getattr(gateway, field)
                for field in (
                    "id",
                    "display_name",
                    "group_id",
                    "mac_address",
                    "serial_number",
                    "imei",
                    "iccid",
                    "phone_number",
                    "activation_mode",
                )
            },
        }
    )


def device_revision(device, gateway):
    return revision(
        {
            "type": "device",
            "gateway": gateway_revision(gateway),
            **{
                field: getattr(device, field)
                for field in (
                    "id",
                    "gateway_id",
                    "name",
                    "ip_address",
                    "mac_address",
                    "device_type",
                )
            },
        }
    )


def check_revision(actor, supplied, current):
    # Existing Bearer clients may omit If-Match. Browser forms must supply it.
    if supplied is None:
        if getattr(actor, "_web_session_id", None):
            raise HTTPException(428, "Reload the resource before editing; If-Match is required")
        return
    if not hmac.compare_digest(supplied.encode(), ('"' + current + '"').encode()):
        raise HTTPException(412, "Resource changed; reload before saving")
