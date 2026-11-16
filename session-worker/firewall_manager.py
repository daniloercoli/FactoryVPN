"""Firewall Gateway Only: coppie autorizzate con lease conservati nel kernel."""

import json
from factoryvpn_common.network import interface_name, lease_ttl, replace_table, run


class FirewallManager:
    table = "factoryvpn_support"

    def __init__(self, vpn_interface="tun0", runner=run):
        self.vpn_interface = interface_name(vpn_interface)
        self.run = runner

    def create_table(self):
        # Al riavvio nessun lease viene adottato senza una nuova conferma API.
        replace_table(self.run, self.table, self._chains([]))

    def _chains(self, sessions):
        rules = []
        for spec in sessions:
            pair = spec.lease_set
            rules.extend(
                [
                    f'iifname "{self.vpn_interface}" oifname "{self.vpn_interface}" ip saddr . ip daddr @{pair} accept',
                    f'iifname "{self.vpn_interface}" oifname "{self.vpn_interface}" ip daddr . ip saddr @{pair} accept',
                ]
            )
        rules += [f'iifname "{self.vpn_interface}" drop', f'oifname "{self.vpn_interface}" drop']
        return (
            f'chain input {{ type filter hook input priority -10; policy accept; iifname "{self.vpn_interface}" drop; }}\n'
            + f'chain output {{ type filter hook output priority -10; policy accept; oifname "{self.vpn_interface}" drop; }}\n'
            + "chain forward { type filter hook forward priority -10; policy accept;\n"
            + "\n".join(rules)
            + "\n}"
        )

    def apply(self, sessions):
        # Ricostruiamo solo le catene: i timeout esistenti non vengono prorogati.
        result = self.run(["nft", "-j", "list", "table", "inet", self.table])
        existing = {
            item["set"]["name"] for item in json.loads(result.stdout)["nftables"] if "set" in item
        }
        desired = {spec.lease_set for spec in sessions}
        commands = []
        for name in sorted(desired - existing):
            commands.append(
                f"add set inet {self.table} {name} {{ type ipv4_addr . ipv4_addr; flags timeout; }}"
            )
        for chain in ("input", "output", "forward"):
            commands.append(f"flush chain inet {self.table} {chain}")
            commands.append(f"delete chain inet {self.table} {chain}")
        # L'intero aggiornamento nft è una transazione: nessuna finestra permissiva.
        commands.append(f"table inet {self.table} {{\n{self._chains(sessions)}\n}}")
        for name in sorted(existing - desired):
            commands.append(f"delete set inet {self.table} {name}")
        self.run(["nft", "-f", "-"], input="\n".join(commands) + "\n")

    def ensure(self, spec, lease_seconds=30, *, renew=True):
        if not renew:
            # apply ha già creato un set vuoto per una nuova sessione.
            return
        ttl = lease_ttl(lease_seconds)
        self.run(
            ["nft", "-f", "-"],
            input=(
                f"flush set inet {self.table} {spec.lease_set}\n"
                f"add element inet {self.table} {spec.lease_set} {{ {spec.technician_ip} . {spec.gateway_ip} timeout {ttl}s }}\n"
            ),
        )

    def delete(self, spec):
        if (
            self.run(
                ["nft", "list", "set", "inet", self.table, spec.lease_set], check=False
            ).returncode
            == 0
        ):
            self.run(["nft", "flush", "set", "inet", self.table, spec.lease_set])
