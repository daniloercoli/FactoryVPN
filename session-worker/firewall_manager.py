"""Atomic VPN forwarding policy; replacing one session retains all other sessions."""

from factoryvpn_common.network import interface_name, replace_table, run


class FirewallManager:
    def __init__(self, vpn_interface="tun0", runner=run):
        self.vpn_interface = interface_name(vpn_interface)
        self.run = runner

    def apply(self, sessions):
        # RFC 2003 IPv4-in-IPv4 is protocol 4. The /etc/protocols name "ipip"
        # resolves to 94 and would silently reject the actual tunnel packets.
        forward = []
        for spec in sessions:
            veth = spec.host_interface
            forward.extend(
                [
                    f'iifname "{self.vpn_interface}" oifname "{veth}" ip saddr {spec.technician_ip} ip daddr {spec.lan_cidr} accept',
                    f'iifname "{veth}" oifname "{self.vpn_interface}" ip saddr {spec.lan_cidr} ip daddr {spec.technician_ip} ct state established,related accept',
                    f'iifname "{veth}" oifname "{self.vpn_interface}" ip daddr {spec.technician_ip} ip protocol icmp accept',
                    f'iifname "{veth}" oifname "{self.vpn_interface}" ip saddr {spec.peer_ip} ip daddr {spec.gateway_ip} ip protocol 4 accept',
                    f'iifname "{self.vpn_interface}" oifname "{veth}" ip saddr {spec.gateway_ip} ip daddr {spec.peer_ip} ip protocol 4 accept',
                ]
            )
        forward.extend(
            [
                f'iifname "{self.vpn_interface}" drop',
                f'oifname "{self.vpn_interface}" drop',
                'iifname "fvh*" drop',
                'oifname "fvh*" drop',
            ]
        )
        replace_table(
            self.run,
            "factoryvpn_support",
            f'chain input {{ type filter hook input priority -10; policy accept; iifname "{self.vpn_interface}" drop; }}\n'
            + "chain forward { type filter hook forward priority -10; policy accept;\n"
            + "\n".join(forward)
            + "\n}",
        )

    def create_table(self):
        self.apply([])
