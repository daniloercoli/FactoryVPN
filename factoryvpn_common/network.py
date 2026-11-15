"""Indirizzi VPN e servizi autorizzati delle sessioni Gateway Only."""

from dataclasses import asdict, dataclass
from ipaddress import IPv4Address, IPv4Network
import re
import subprocess
from uuid import UUID
from factoryvpn_common.timing import MAX_LEASE_SECONDS

VPN_NETWORK = IPv4Network("10.100.0.0/16")
GATEWAY_PORTS = (22, 80, 443)
VPN_SERVER_IP = "10.100.0.1"


def interface_name(value):
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", value):
        raise ValueError("Invalid interface name")
    return value


def run(command, **kwargs):
    return subprocess.run(
        command,
        check=kwargs.pop("check", True),
        capture_output=True,
        text=True,
        timeout=10,
        **kwargs,
    )


def replace_table(runner, table, body):
    exists = runner(["nft", "list", "table", "inet", table], check=False).returncode == 0
    script = (
        f"delete table inet {table}\n" if exists else ""
    ) + f"table inet {table} {{\n{body}\n}}\n"
    runner(["nft", "-f", "-"], input=script)


@dataclass(frozen=True)
class SessionNetwork:
    session_id: str
    technician_ip: str
    gateway_ip: str

    def __post_init__(self):
        object.__setattr__(self, "session_id", str(UUID(self.session_id)))
        tech, gateway = IPv4Address(self.technician_ip), IPv4Address(self.gateway_ip)
        if tech not in IPv4Network("10.100.128.0/17") or gateway not in IPv4Network(
            "10.100.0.0/17"
        ):
            raise ValueError("VPN endpoints are outside their assigned pools")
        if gateway in (IPv4Address(VPN_SERVER_IP), IPv4Address("10.100.0.0")):
            raise ValueError("Gateway cannot use a reserved address")
        object.__setattr__(self, "technician_ip", str(tech))
        object.__setattr__(self, "gateway_ip", str(gateway))

    @property
    def lease_set(self):
        return "lease_" + UUID(self.session_id).hex

    def to_dict(self):
        return asdict(self)


def lease_ttl(seconds):
    if not isinstance(seconds, int) or not 1 <= seconds <= MAX_LEASE_SECONDS:
        raise ValueError(f"Lease must be between 1 and {MAX_LEASE_SECONDS} seconds")
    return seconds
