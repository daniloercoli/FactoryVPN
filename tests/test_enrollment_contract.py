"""Confini del primo accesso senza credenziali operative o accesso amministrativo."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from api.config import settings
from api.schemas.enrollment import EnrollmentData, FactoryCreateRequest
from api.services.credentials import digest
from api.services import enrollment_auth
from api.services.enrollment import incomplete, valid_delivery


@pytest.mark.parametrize(
    "field", ["gateway_id", "serial_number", "group_id", "secret_hash", "approved_at", "lan_cidr"]
)
def test_gateway_cannot_claim_identity_or_approval(field):
    with pytest.raises(ValidationError):
        EnrollmentData(**{field: "untrusted"})


def test_factory_can_prepare_identity_without_active_sim_or_lan():
    request = FactoryCreateRequest(
        gateway_id=uuid4(), serial_number=" line-001 ", secret_hash=digest("random-initial-key")
    )
    assert request.serial_number == "line-001" and incomplete(request) == ["activation_mode"]
    sms = EnrollmentData(activation_mode="sms")
    assert incomplete(sms) == ["phone_number"]
    assert not incomplete(EnrollmentData(activation_mode="wifi", mac_address="aa-bb-cc-dd-ee-ff"))


async def test_station_hash_is_not_a_bearer_credential(monkeypatch):
    key = "station-" + "k" * 40
    monkeypatch.setattr(settings, "FACTORY_STATIONS", {"line-1": digest(key)})
    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await enrollment_auth.factory_station(digest(key), db)
    assert exc.value.status_code == 401
    db.execute.assert_not_awaited()
    monkeypatch.setattr(enrollment_auth, "allocation_lock", AsyncMock())
    limiter = AsyncMock()
    monkeypatch.setattr(enrollment_auth, "rate_limit", limiter)
    assert await enrollment_auth.factory_station(key, db) == "line-1"
    limiter.assert_awaited_once()


async def test_consumed_or_foreign_initial_secret_never_reaches_delivery(monkeypatch):
    identifier = uuid4()
    key = "initial-" + "k" * 40
    monkeypatch.setattr(enrollment_auth, "allocation_lock", AsyncMock())
    limiter = AsyncMock()
    monkeypatch.setattr(enrollment_auth, "rate_limit", limiter)
    for secret_hash in (None, digest("someone else")):
        db = AsyncMock()
        db.get.return_value = SimpleNamespace(
            secret_hash=secret_hash, blocked_at=None, completed_at=None
        )
        with pytest.raises(HTTPException) as exc:
            await enrollment_auth.gateway_enrollment(identifier, key, db)
        assert exc.value.status_code == 401
    limiter.assert_not_awaited()


async def test_revocation_racing_with_limit_commit_is_rechecked(monkeypatch):
    identifier = uuid4()
    key = "initial-" + "k" * 40
    row = SimpleNamespace(secret_hash=digest(key), blocked_at=None, completed_at=None)
    db = AsyncMock()
    db.get.return_value = row
    monkeypatch.setattr(enrollment_auth, "allocation_lock", AsyncMock())

    async def rate(*args):
        row.secret_hash = None

    monkeypatch.setattr(enrollment_auth, "rate_limit", rate)
    with pytest.raises(HTTPException) as exc:
        await enrollment_auth.gateway_enrollment(identifier, key, db)
    assert exc.value.status_code == 401


async def test_machine_delivery_uses_approval_not_approver_liveness(monkeypatch):
    from api.models.enrollment import GatewayEnrollment
    from api.models.gateway import Gateway
    from api.models.group import Group
    from api.models.user import User
    from api.services import provisioning

    identifier, group = uuid4(), uuid4()
    row = SimpleNamespace(
        id=identifier,
        secret_hash="hash",
        approved_at=True,
        completed_at=None,
        blocked_at=None,
        delivery_mode="automatic",
        group_id=group,
        configuration_version=3,
    )
    gateway = SimpleNamespace(group_id=group)
    values = {GatewayEnrollment: row, Gateway: gateway, Group: object()}
    db = AsyncMock()
    db.get.side_effect = lambda model, *args, **kwargs: values[model]
    monkeypatch.setattr(provisioning, "gateway_parameters", Mock(return_value={"mode": "wifi"}))
    job = SimpleNamespace(
        enrollment_id=identifier,
        peer_id=identifier,
        group_id=group,
        configuration_version=3,
        parameters={"mode": "wifi"},
    )
    assert await valid_delivery(db, job)
    assert all(call.args[0] is not User for call in db.get.call_args_list)
    row.configuration_version = 4
    assert not await valid_delivery(db, job)
