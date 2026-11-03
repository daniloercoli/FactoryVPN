import asyncio
from unittest.mock import AsyncMock, Mock

import pytest


def test_sms_parses_standard_optional_alpha_and_multiline(load_script):
    handler = load_script("gateway-agent", "sms_handler").SMSHandler
    raw = (
        '\r\n+CMGL: 1,"REC UNREAD","+393331234567","","26/10/01,01:00:00+08"\r\n'
        "FACTORYVPN: activate\r\nsecond line\r\n"
        '+CMGL: 2,"REC READ","+393339999999","26/10/01,01:01:00+08"\r\n'
        "another message\r\n\r\nOK\r\n"
    )
    messages = handler._parse_sms_list(raw)
    assert len(messages) == 2
    assert messages[0].sender == "+393331234567"
    assert messages[0].body == "FACTORYVPN: activate\nsecond line"
    assert messages[0].timestamp == "26/10/01,01:00:00+08"
    assert messages[1].body == "another message"


def test_sms_slot_is_not_consumed_before_ack_or_when_reused(load_script):
    module = load_script("gateway-agent", "sms_handler")
    handler = module.SMSHandler.__new__(module.SMSHandler)
    first = module.SMS(1, "+393331234567", "first", "timestamp")
    second = module.SMS(1, "+393331234567", "second", "new timestamp")
    handler.list_sms = Mock(side_effect=[[first], [first], [second]])
    assert handler.check_new_sms() == first
    assert handler.check_new_sms() == first
    assert handler.check_new_sms() == second


def test_modem_waits_for_fragmented_reply_and_reports_errors(load_script):
    module = load_script("gateway-agent", "sms_handler")
    handler = module.SMSHandler.__new__(module.SMSHandler)
    handler.timeout = 1
    handler.modem = Mock(in_waiting=0)
    handler.modem.read.side_effect = [b"", b"\r\n", b"O", b"K\r\n"]
    assert handler._read_response() == "\r\nOK\r\n"
    handler.modem.read.side_effect = [b"\r\n+CMS ERROR: 500\r\n"]
    with pytest.raises(RuntimeError):
        handler._read_response()
    handler.timeout = 0
    with pytest.raises(TimeoutError):
        handler._read_response()


def test_fragmented_sms_error_body_is_not_a_modem_error(load_script):
    module = load_script("gateway-agent", "sms_handler")
    handler = module.SMSHandler.__new__(module.SMSHandler)
    handler.timeout = 1
    handler.modem = Mock(in_waiting=0)
    chunks = [b'\r\n+CMGL: 1,"REC UNREAD","FactoryVPN","",""\r\n', b"ERROR\r\n", b"\r\nOK\r\n"]
    handler.modem.read.side_effect = chunks
    assert handler._read_response() == b"".join(chunks).decode()


class Process:
    def __init__(self, ready=True):
        self.returncode = None
        self.done = asyncio.Event()
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        if ready:
            self.stdout.feed_data(b"Initialization Sequence Completed\n")

    async def wait(self):
        await self.done.wait()
        return self.returncode

    def terminate(self):
        self.returncode = 0
        self.done.set()

    kill = terminate


async def test_openvpn_drains_streams_restarts_exited_child_and_stops(load_script, monkeypatch):
    module = load_script("gateway-agent", "vpn_client")
    first, second = Process(), Process()
    create = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create)
    vpn = module.OpenVPNClient()
    await vpn.start()
    await vpn.start()
    assert create.await_count == 1
    first.terminate()
    assert not vpn.is_running()
    await vpn.start()
    assert create.await_count == 2
    assert vpn.is_running()
    await vpn.stop()
    assert vpn.process is None and not vpn._readers
    assert second.returncode == 0


async def test_openvpn_timeout_reaps_child(load_script, monkeypatch):
    module = load_script("gateway-agent", "vpn_client")
    process = Process(ready=False)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    vpn = module.OpenVPNClient(connect_timeout=0.01)
    with pytest.raises(TimeoutError):
        await vpn.start()
    assert process.returncode == 0
    assert vpn.process is None


async def test_openvpn_does_not_reuse_ready_flag_after_internal_restart(load_script, monkeypatch):
    module = load_script("gateway-agent", "vpn_client")
    first, second = Process(), Process()
    create = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create)
    vpn = module.OpenVPNClient()
    try:
        await vpn.start()
        first.stdout.feed_data(b"SIGUSR1[soft,ping-restart] received, process restarting\n")
        await asyncio.sleep(0)  # Let the stream reader observe the lost connection.
        await vpn.start()
        assert create.await_count == 2
        assert first.returncode == 0
    finally:
        await vpn.stop()


async def test_cancelling_openvpn_start_reaps_the_child(load_script, monkeypatch):
    module = load_script("gateway-agent", "vpn_client")
    process = Process(ready=False)
    create = AsyncMock(return_value=process)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create)
    vpn = module.OpenVPNClient()
    task = asyncio.create_task(vpn.start())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode == 0
    assert vpn.process is None and not vpn._readers
