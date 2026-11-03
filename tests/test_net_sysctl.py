"""Prevent the session helper from changing the container/host mount namespace."""

from unittest.mock import Mock

import pytest

from factoryvpn_common import net_sysctl


@pytest.mark.parametrize("kind", ["mnt", "net"])
def test_sysctl_requires_both_private_namespaces(monkeypatch, kind):
    monkeypatch.setattr(
        net_sysctl, "namespace_id", lambda process, ns: 1 if ns == kind or process == "1" else 2
    )
    run = Mock()
    monkeypatch.setattr(net_sysctl.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="private"):
        net_sysctl.configure(["net.ipv4.ip_forward=1"])
    run.assert_not_called()


@pytest.mark.parametrize(
    "assignment", ["kernel.panic=1", "net.ipv4.ip_forward=2", "net.ipv4.ip_forward=1;reboot", "-w"]
)
def test_sysctl_rejects_unrelated_or_invalid_assignments(monkeypatch, assignment):
    run = Mock()
    monkeypatch.setattr(net_sysctl.subprocess, "run", run)
    with pytest.raises(ValueError):
        net_sysctl.configure([assignment])
    run.assert_not_called()
