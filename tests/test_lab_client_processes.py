"""Never stop an unrelated OpenVPN process from an old technician PID file."""

import os
from pathlib import Path
from unittest.mock import Mock
import pytest

from scripts import local_lab_peers as peers


def test_stale_client_pid_does_not_stop_another_site(tmp_path, monkeypatch):
    monkeypatch.setattr(peers, "ROOT", tmp_path)
    folder = tmp_path / "site1"
    folder.mkdir()
    (folder / "technician.pid").write_text("4242")
    original = Path.read_bytes

    def read(path):
        if str(path) == "/proc/4242/cmdline":
            return b"openvpn\0--config\0/lab/site2/technician.ovpn\0"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    kill = Mock()
    monkeypatch.setattr(peers.os, "kill", kill)
    # A harmless descriptor stands in for Linux's process handle on the Mac.
    monkeypatch.setattr(
        peers.os, "pidfd_open", lambda pid: os.open(os.devnull, os.O_RDONLY), raising=False
    )
    send = Mock()
    monkeypatch.setattr(peers.signal, "pidfd_send_signal", send, raising=False)
    peers.client_stop(1)
    kill.assert_not_called()
    send.assert_not_called()
    assert not (folder / "technician.pid").exists()


@pytest.mark.parametrize("difference", [None, "profile", "executable", "namespace"])
def test_client_identity_requires_profile_executable_and_namespace(
    tmp_path, monkeypatch, difference
):
    monkeypatch.setattr(peers, "ROOT", tmp_path / "lab")
    proc = tmp_path / "proc/4242"
    (proc / "ns").mkdir(parents=True)
    namespace = tmp_path / "run/netns/tech1"
    namespace.parent.mkdir(parents=True)
    namespace.touch()
    other = namespace.with_name("tech2")
    other.touch()
    (proc / "ns/net").symlink_to(other if difference == "namespace" else namespace)
    executable = tmp_path / ("python" if difference == "executable" else "openvpn")
    executable.touch()
    (proc / "exe").symlink_to(executable)
    profile = peers.ROOT / ("site2" if difference == "profile" else "site1") / "technician.ovpn"
    (proc / "cmdline").write_bytes(b"openvpn\0--config\0" + str(profile).encode() + b"\0")
    monkeypatch.setattr(peers, "Path", lambda path: tmp_path / str(path).lstrip("/"))
    assert peers.owns_client(4242, 1) is (difference is None)


@pytest.mark.parametrize("exit_after", [1, 2, None])
def test_client_stop_waits_and_preserves_pid_if_process_cannot_stop(
    tmp_path, monkeypatch, exit_after
):
    monkeypatch.setattr(peers, "ROOT", tmp_path)
    folder = tmp_path / "site1"
    folder.mkdir()
    pidfile = folder / "technician.pid"
    pidfile.write_text("4242")
    descriptor = os.open(os.devnull, os.O_RDONLY)
    monkeypatch.setattr(peers.os, "pidfd_open", lambda pid: descriptor, raising=False)
    monkeypatch.setattr(peers, "owns_client", lambda pid, number: True)
    sent = []
    monkeypatch.setattr(
        peers.signal, "pidfd_send_signal", lambda fd, sig: sent.append((fd, sig)), raising=False
    )

    def wait(read, write, error, timeout):
        assert pidfile.exists()  # Keep ownership until confirmed exit.
        return ([descriptor] if len(sent) == exit_after else [], [], [])

    monkeypatch.setattr(peers.select, "select", wait)
    if exit_after is None:
        with pytest.raises(TimeoutError):
            peers.client_stop(1)
        assert pidfile.exists()
    else:
        peers.client_stop(1)
        assert not pidfile.exists()
    assert sent[0] == (descriptor, peers.signal.SIGTERM)
    if exit_after != 1:
        assert sent[1] == (descriptor, peers.signal.SIGKILL)
