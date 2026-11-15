"""Retry di fine linea: stesso UUID/segreto e nessuna credenziale nel payload."""

import hashlib
import json

import httpx
import pytest

from scripts.factory_station import enroll


def test_station_lost_response_reuses_identity_and_removes_secret_after_success(tmp_path):
    record, device = tmp_path / "record.json", tmp_path / "device/enrollment.json"
    requests = []

    def handler(request):
        requests.append(request)
        assert record.exists() and device.exists()
        if len(requests) == 1:
            raise httpx.ReadTimeout("lost response", request=request)
        return httpx.Response(200, json={"id": json.loads(request.content)["gateway_id"]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        args = (record, device, "https://vpn.example.test", "SN-001", "station-secret")
        with pytest.raises(httpx.ReadTimeout):
            enroll(*args, client=client)
        identity = json.loads(device.read_text())
        pending = json.loads(record.read_text())
        assert pending["enrollment_key"] == identity["enrollment_key"]
        assert len(identity["enrollment_key"]) >= 43
        assert enroll(*args, client=client) == identity["gateway_id"]
        assert enroll(*args, client=client) == identity["gateway_id"]
    assert len({request.headers["Idempotency-Key"] for request in requests}) == 1
    assert len({request.content for request in requests}) == 1
    payload = json.loads(requests[0].content)
    assert payload["secret_hash"] == hashlib.sha256(identity["enrollment_key"].encode()).hexdigest()
    assert identity["enrollment_key"].encode() not in requests[0].content
    assert "enrollment_key" not in json.loads(record.read_text())
    assert json.loads(record.read_text())["confirmed"]
    assert record.stat().st_mode & 0o777 == device.stat().st_mode & 0o777 == 0o600


def test_station_refuses_changed_operation_or_existing_device(tmp_path):
    record, device = tmp_path / "record.json", tmp_path / "identity.json"

    def handler(request):
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            enroll(record, device, "https://vpn.example.test", "ONE", "key", client=client)
        with pytest.raises(ValueError, match="differs"):
            enroll(record, device, "https://vpn.example.test", "TWO", "key", client=client)
        identity = json.loads(device.read_text())
        identity["enrollment_key"] = "different"
        device.write_text(json.dumps(identity))
        with pytest.raises(ValueError, match="verification"):
            enroll(record, device, "https://vpn.example.test", "ONE", "key", client=client)


@pytest.mark.parametrize(
    "url", ["http://localhost", "https://user:pass@host", "https://host/path", "https://host?q=1"]
)
def test_station_requires_https_origin_before_writing(tmp_path, url):
    with pytest.raises(ValueError):
        enroll(tmp_path / "record", tmp_path / "identity", url, "SN", "key")
    assert not list(tmp_path.iterdir())
