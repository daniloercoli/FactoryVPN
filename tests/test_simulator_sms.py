"""Exercise the simulator through the production serial reader and agent."""

from http.server import HTTPServer
from importlib import import_module
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from factoryvpn_common.protocol import activation_message
from scripts.simulator_sms import Inbox, Modem, PHONE, provider_handler

SMSHandler = import_module("gateway-agent.sms_handler").SMSHandler
GatewayAgent = import_module("gateway-agent.main").GatewayAgent
KEY = "test-simulator-key-" + "a" * 32


@pytest.fixture
def serial_pair(tmp_path):
    inbox = Inbox(tmp_path / "sms.sqlite3")
    modem = Modem(inbox)
    reader = SMSHandler(modem.path, timeout=1)
    try:
        yield inbox, reader
    finally:
        reader.close()
        modem.close()


def test_delivery_persistence_and_ack_through_real_serial(serial_pair):
    inbox, reader = serial_pair
    body = activation_message(uuid4(), time.time() + 120, KEY)
    inbox.accept(PHONE, body)
    inbox.accept(PHONE, body)  # Provider retry must not duplicate a queued SMS.
    assert reader.list_sms() == []
    assert inbox.counts(PHONE) == {"pending": 1, "delivered": 0}
    assert inbox.deliver("+15550100002") == 0
    assert inbox.deliver(PHONE) == 1
    restarted = Inbox(inbox.path)
    assert restarted.counts(PHONE) == {"pending": 0, "delivered": 1}
    (message,) = reader.list_sms()
    assert message.body == body
    assert reader.list_sms()[0].id == message.id
    assert reader.inventory()["imei"] == "990000000000001"
    restarted.delete("+15550100002", message.id)
    assert reader.list_sms()
    reader.delete_sms(message.id)
    assert reader.list_sms() == []


@pytest.mark.parametrize("kind", ["valid", "expired", "wrong_key", "malformed"])
async def test_actual_agent_only_wakes_for_authentic_current_sms(serial_pair, kind):
    inbox, reader = serial_pair
    body = activation_message(
        uuid4(),
        time.time() - 10 if kind == "expired" else time.time() + 120,
        "other-key" if kind == "wrong_key" else KEY,
    )
    if kind == "malformed":
        body = "activate VPN"
    inbox.accept(PHONE, body)
    agent = GatewayAgent(
        reader,
        Mock(),
        Mock(),
        Mock(),
        SimpleNamespace(
            ACTIVATION_MODE="sms",
            GATEWAY_SMS_KEY=KEY,
        ),
    )
    await agent.collect_wakeups()
    assert not agent.awake
    inbox.deliver(PHONE)
    await agent.collect_wakeups()
    assert agent.awake == (kind == "valid")
    # Valid SMS remains until the real handshake; invalid SMS is discarded.
    assert len(reader.list_sms()) == (1 if kind == "valid" else 0)
    agent.vpn.start.assert_not_called()


@pytest.mark.parametrize("noise", ["ERROR", "+CMS ERROR: 500", "+CME ERROR: 10"])
async def test_sms_body_cannot_poison_modem_listing(serial_pair, noise):
    inbox, reader = serial_pair
    body = activation_message(uuid4(), time.time() + 120, KEY)
    inbox.accept(PHONE, noise)
    inbox.accept(PHONE, body)
    inbox.deliver(PHONE)
    agent = GatewayAgent(
        reader,
        Mock(),
        Mock(),
        Mock(),
        SimpleNamespace(ACTIVATION_MODE="sms", GATEWAY_SMS_KEY=KEY),
    )
    await agent.collect_wakeups()
    assert agent.activation_token == body
    assert [sms.body for sms in reader.list_sms()] == [body]


@pytest.mark.parametrize(
    "phone,body",
    [
        ("+15550100002", "body"),
        (PHONE, "AT\r\nOK"),
        (PHONE, "x" * 201),
        (PHONE, None),
        (PHONE, ""),
        (PHONE, "\x1b"),
    ],
)
def test_queue_rejects_invalid_destination_or_serial_injection(tmp_path, phone, body):
    with pytest.raises(ValueError):
        Inbox(tmp_path / "sms.sqlite3").accept(phone, body)


def test_provider_authentication_and_acceptance_do_not_deliver(tmp_path):
    inbox = Inbox(tmp_path / "sms.sqlite3")
    server = HTTPServer(("127.0.0.1", 0), provider_handler(inbox, KEY))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False
        ) as client:
            payload = {"to": PHONE, "message": "test SMS"}
            assert client.post("/send", json=payload).status_code == 401
            assert inbox.counts(PHONE)["pending"] == 0
            client.headers["Authorization"] = "Bearer " + KEY
            assert client.post("/send", json=payload).status_code == 202
            assert inbox.counts(PHONE) == {"pending": 1, "delivered": 0}
            assert client.post("/send", json=[]).status_code == 400
            assert client.post("/send", content="x" * 1025).status_code == 413
            assert client.post("/send", json={**payload, "to": "external"}).status_code == 400
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_queue_capacity_and_old_message_removal(tmp_path):
    inbox = Inbox(tmp_path / "sms.sqlite3")
    for number in range(100):
        inbox.accept(PHONE, f"message {number}")
    with pytest.raises(OverflowError):
        inbox.accept(PHONE, "overflow")
    with inbox.connection() as db:
        db.execute("UPDATE messages SET created=?", (time.time() - 901,))
    inbox.accept(PHONE, "new")
    assert inbox.counts(PHONE) == {"pending": 1, "delivered": 0}
