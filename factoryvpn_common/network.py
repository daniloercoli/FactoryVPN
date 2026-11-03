"""Deterministic resources for isolated IPv4 sessions (including overlapping LANs)."""

from dataclasses import asdict, dataclass
from ipaddress import IPv4Address, IPv4Network
import re
import subprocess
from uuid import UUID

VPN_NETWORK = IPv4Network("10.100.0.0/16")
TRANSIT_NETWORK = IPv4Network("10.250.0.0/16")
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
    slot: int
    technician_ip: str
    gateway_ip: str
    lan_cidr: str

    def __post_init__(self):
        object.__setattr__(self, "session_id", str(UUID(self.session_id)))
        if not isinstance(self.slot, int) or not 1 <= self.slot <= 16383:
            raise ValueError("Network slot out of range")
        tech, gateway = IPv4Address(self.technician_ip), IPv4Address(self.gateway_ip)
        if tech not in IPv4Network("10.100.128.0/17") or gateway not in IPv4Network(
            "10.100.0.0/17"
        ):
            raise ValueError("VPN endpoints are outside their assigned pools")
        if gateway == IPv4Address(VPN_SERVER_IP):
            raise ValueError("Gateway cannot use the server address")
        lan = IPv4Network(self.lan_cidr, strict=True)
        if lan.overlaps(VPN_NETWORK) or lan.overlaps(TRANSIT_NETWORK) or lan.prefixlen == 0:
            raise ValueError("LAN overlaps the VPN transport networks")
        object.__setattr__(self, "lan_cidr", str(lan))

    @property
    def namespace(self):
        return "factoryvpn_" + UUID(self.session_id).hex

    @property
    def host_interface(self):
        return f"fvh{self.slot:04x}"

    @property
    def host_ip(self):
        return str(IPv4Address(int(TRANSIT_NETWORK.network_address) + 4 * self.slot + 1))

    @property
    def peer_ip(self):
        return str(IPv4Address(int(TRANSIT_NETWORK.network_address) + 4 * self.slot + 2))

    @property
    def table(self):
        return str(10000 + self.slot)

    def to_dict(self):
        return asdict(self)


def lease_ttl(seconds):
    if not isinstance(seconds, int) or not 1 <= seconds <= 60:
        raise ValueError("Lease must be between 1 and 60 seconds")
    return seconds
