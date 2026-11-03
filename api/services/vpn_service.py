"""Compatibility facade for the shared Unix-socket OpenVPN management client."""

from factoryvpn_common.openvpn import ManagementClient
from factoryvpn_common.protocol import parse_peer_name
from ipaddress import IPv4Address


def generate_client_config(common_name, vpn_ip):
    parse_peer_name(common_name)
    return f"ifconfig-push {IPv4Address(vpn_ip)} 255.255.0.0\n"


def get_active_vpn_clients():
    return [
        {"common_name": name, "virtual_address": address}
        for name, address in ManagementClient().clients().items()
    ]


def kill_client(common_name):
    return ManagementClient().kill(common_name)
