"""Gateway overlay, source-restricted NAT and kernel-enforced session leases."""

import json
from ipaddress import IPv4Address
from factoryvpn_common.network import (
    SessionNetwork,
    VPN_SERVER_IP,
    TRANSIT_NETWORK,
    interface_name,
    lease_ttl,
    replace_table,
    run,
)


class NetworkManager:
    def __init__(self, runner=run):
        self.run = runner

    def configure_session(
        self, spec: SessionNetwork, vpn_interface="tun0", lan_interface="eth0", lease_seconds=30
    ):
        vpn_interface, lan_interface = interface_name(vpn_interface), interface_name(lan_interface)
        ttl = lease_ttl(lease_seconds)
        self.run(
            [
                "sysctl",
                "-w",
                "net.ipv4.ip_forward=1",
                "net.ipv4.conf.all.rp_filter=0",
                "net.ipv4.conf.default.rp_filter=0",
            ]
        )
        existing = self.run(["ip", "-j", "-d", "link", "show", "dev", "fvsite0"], check=False)
        if existing.returncode == 0:
            link = json.loads(existing.stdout)[0].get("linkinfo", {}).get("info_data", {})
            if link.get("remote") != spec.peer_ip or link.get("local") != spec.gateway_ip:
                self._remove_peer_route(link.get("remote"), vpn_interface)
                self.run(["ip", "link", "delete", "fvsite0"])
                existing = None
        if existing is None or existing.returncode != 0:
            # Use the ioctl creation path, as on the server, to avoid the
            # LinuxKit 7.0.14 IPIP rtnetlink panic.
            self.run(
                [
                    "ip",
                    "tunnel",
                    "add",
                    "fvsite0",
                    "mode",
                    "ipip",
                    "local",
                    spec.gateway_ip,
                    "remote",
                    spec.peer_ip,
                    "ttl",
                    "64",
                ]
            )
        self.run(["ip", "link", "set", "fvsite0", "mtu", "1360", "up"])
        self.run(
            [
                "ip",
                "route",
                "replace",
                spec.peer_ip + "/32",
                "via",
                VPN_SERVER_IP,
                "dev",
                vpn_interface,
            ]
        )
        self.run(["ip", "route", "replace", spec.technician_ip + "/32", "dev", "fvsite0"])
        # Match RFC 2003's protocol number, not the ambiguous "ipip" name (94).
        replace_table(
            self.run,
            "factoryvpn_gateway",
            f"""
 set technician {{ type ipv4_addr; flags timeout; elements = {{ {spec.technician_ip} timeout {ttl}s }}; }}
 set peer {{ type ipv4_addr; flags timeout; elements = {{ {spec.peer_ip} timeout {ttl}s }}; }}
 chain input {{ type filter hook input priority -10; policy accept;
  iifname "{vpn_interface}" ip protocol 4 ip saddr @peer ip daddr {spec.gateway_ip} accept
  iifname "{vpn_interface}" ip protocol 4 drop
 }}
 chain forward {{ type filter hook forward priority -10; policy accept;
  iifname "fvsite0" oifname "{lan_interface}" ip saddr @technician ip daddr {spec.lan_cidr} accept
  iifname "{lan_interface}" oifname "fvsite0" ip daddr @technician ct state established,related accept
  iifname "fvsite0" drop
  oifname "fvsite0" drop
  iifname "{vpn_interface}" oifname "{lan_interface}" drop
  iifname "{lan_interface}" oifname "{vpn_interface}" drop
 }}
 chain postrouting {{ type nat hook postrouting priority 100; policy accept;
  oifname "{lan_interface}" ip saddr @technician ip daddr {spec.lan_cidr} masquerade
 }}""",
        )

    def disable_nat(self, vpn_interface="tun0", lan_interface="eth0", spec=None):
        vpn_interface, lan_interface = interface_name(vpn_interface), interface_name(lan_interface)
        # Keep a guard after teardown: raw VPN packets must never reach the LAN.
        replace_table(
            self.run,
            "factoryvpn_gateway",
            f"""
 chain input {{ type filter hook input priority -10; policy accept;
  iifname "{vpn_interface}" ip protocol 4 drop
 }}
 chain forward {{ type filter hook forward priority -10; policy accept;
  iifname "fvsite0" drop
  oifname "fvsite0" drop
  iifname "{vpn_interface}" oifname "{lan_interface}" drop
  iifname "{lan_interface}" oifname "{vpn_interface}" drop
 }}""",
        )
        existing = self.run(["ip", "-j", "-d", "link", "show", "dev", "fvsite0"], check=False)
        if existing.returncode == 0:
            link = json.loads(existing.stdout)[0].get("linkinfo", {}).get("info_data", {})
            self._remove_peer_route(link.get("remote"), vpn_interface)
            self.run(["ip", "link", "delete", "fvsite0"])
        if spec:
            self._remove_peer_route(spec.peer_ip, vpn_interface)

    def _remove_peer_route(self, address, vpn_interface):
        if not address or IPv4Address(address) not in TRANSIT_NETWORK:
            return
        routes = self.run(["ip", "-j", "route", "show", address + "/32"])
        if any(route.get("dev") == vpn_interface for route in json.loads(routes.stdout)):
            self.run(["ip", "route", "del", address + "/32", "dev", vpn_interface])

    def get_lan_devices(self):
        result = self.run(["ip", "-j", "neigh", "show"])
        return [item for item in json.loads(result.stdout) if item.get("lladdr")]
