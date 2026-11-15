"""Accesso ai servizi del gateway; nessun inoltro della LAN nella VPN."""

from factoryvpn_common.network import (
    GATEWAY_PORTS,
    SessionNetwork,
    interface_name,
    lease_ttl,
    replace_table,
    run,
)


class NetworkManager:
    def __init__(self, runner=run):
        self.run = runner

    def configure_session(self, spec: SessionNetwork, vpn_interface="tun0", lease_seconds=30):
        vpn_interface = interface_name(vpn_interface)
        ttl = lease_ttl(lease_seconds)
        ports = ", ".join(map(str, GATEWAY_PORTS))
        # La scadenza controlla anche le risposte di connessioni già aperte.
        replace_table(
            self.run,
            "factoryvpn_gateway",
            f"""
 set technician {{ type ipv4_addr; flags timeout; elements = {{ {spec.technician_ip} timeout {ttl}s }}; }}
 chain input {{ type filter hook input priority -10; policy accept;
  iifname "{vpn_interface}" ip saddr @technician ip daddr {spec.gateway_ip} tcp dport {{ {ports} }} ct state new,established accept
  iifname "{vpn_interface}" drop
 }}
 chain output {{ type filter hook output priority -10; policy accept;
  oifname "{vpn_interface}" ip saddr {spec.gateway_ip} ip daddr @technician tcp sport {{ {ports} }} ct state established accept
  oifname "{vpn_interface}" drop
 }}
 chain forward {{ type filter hook forward priority -10; policy accept;
  iifname "{vpn_interface}" drop
  oifname "{vpn_interface}" drop
 }}""",
        )

    def disable(self, vpn_interface="tun0", spec=None):
        vpn_interface = interface_name(vpn_interface)
        replace_table(
            self.run,
            "factoryvpn_gateway",
            f"""
 chain input {{ type filter hook input priority -10; policy accept; iifname "{vpn_interface}" drop; }}
 chain output {{ type filter hook output priority -10; policy accept; oifname "{vpn_interface}" drop; }}
 chain forward {{ type filter hook forward priority -10; policy accept;
  iifname "{vpn_interface}" drop
  oifname "{vpn_interface}" drop
 }}""",
        )
