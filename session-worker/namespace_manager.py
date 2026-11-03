"""Per-session policy routing and an IP-in-IP path to the selected gateway.

IPIP is carried inside the encrypted OpenVPN tunnel. Routing the original LAN
packet inside this point-to-point path permits identical LAN prefixes at sites.
"""

import sys

from factoryvpn_common.network import SessionNetwork, lease_ttl, replace_table, run


class NamespaceManager:
    def __init__(self, runner=run):
        self.run = runner

    def _inside(self, spec, command, **kwargs):
        if command[:2] == ["sysctl", "-w"]:
            command = [sys.executable, "-m", "factoryvpn_common.net_sysctl", *command[2:]]
        return self.run(["ip", "netns", "exec", spec.namespace, *command], **kwargs)

    def ensure(self, spec: SessionNetwork, lease_seconds=30):
        ttl = lease_ttl(lease_seconds)
        existing = self.run(["ip", "netns", "list"]).stdout.splitlines()
        present = any(line.split()[0] == spec.namespace for line in existing if line.strip())
        if present:
            probes = [
                self.run(["ip", "link", "show", spec.host_interface], check=False),
                self._inside(spec, ["ip", "link", "show", "uplink"], check=False),
                self._inside(spec, ["ip", "link", "show", "site0"], check=False),
            ]
            present = all(probe.returncode == 0 for probe in probes)
        if not present:
            # A worker/container restart can leave routes while losing namespace mounts.
            # Clear only this API-owned slot before rebuilding, so policy rules cannot duplicate.
            self.delete(spec)
            try:
                self.run(["ip", "netns", "add", spec.namespace])
                self._inside(spec, ["sysctl", "-w", "net.ipv4.ip_forward=0"])
                self.run(
                    [
                        "ip",
                        "link",
                        "add",
                        spec.host_interface,
                        "type",
                        "veth",
                        "peer",
                        "name",
                        "uplink",
                        "netns",
                        spec.namespace,
                    ]
                )
                self.run(["ip", "addr", "add", spec.host_ip + "/30", "dev", spec.host_interface])
                self.run(["ip", "link", "set", spec.host_interface, "up"])
                self._inside(spec, ["ip", "addr", "add", spec.peer_ip + "/30", "dev", "uplink"])
                for link in ("lo", "uplink"):
                    self._inside(spec, ["ip", "link", "set", link, "up"])
                self._inside(
                    spec,
                    [
                        "sysctl",
                        "-w",
                        "net.ipv4.conf.all.rp_filter=0",
                        "net.ipv4.conf.default.rp_filter=0",
                    ],
                )
                self._inside(
                    spec,
                    ["ip", "route", "replace", "default", "via", spec.host_ip, "dev", "uplink"],
                )
                # The ioctl path avoids the IPIP rtnetlink creation panic in
                # LinuxKit 7.0.14; tunnel endpoints and policy remain unchanged.
                self._inside(
                    spec,
                    [
                        "ip",
                        "tunnel",
                        "add",
                        "site0",
                        "mode",
                        "ipip",
                        "local",
                        spec.peer_ip,
                        "remote",
                        spec.gateway_ip,
                        "ttl",
                        "64",
                    ],
                )
                self._inside(spec, ["ip", "link", "set", "site0", "mtu", "1360", "up"])
                self._inside(spec, ["ip", "route", "add", spec.lan_cidr, "dev", "site0"])
                self.run(
                    [
                        "ip",
                        "route",
                        "replace",
                        spec.lan_cidr,
                        "via",
                        spec.peer_ip,
                        "dev",
                        spec.host_interface,
                        "table",
                        spec.table,
                    ]
                )
                self.run(
                    [
                        "ip",
                        "rule",
                        "add",
                        "pref",
                        spec.table,
                        "from",
                        spec.technician_ip + "/32",
                        "to",
                        spec.lan_cidr,
                        "lookup",
                        spec.table,
                    ]
                )
            except Exception:
                self.delete(spec)
                raise
        # The lease expires in the kernel even if the worker is killed or loses API access.
        replace_table(
            lambda cmd, **kw: self._inside(spec, cmd, **kw),
            "factoryvpn_session",
            f"""
 set technician {{ type ipv4_addr; flags timeout; elements = {{ {spec.technician_ip} timeout {ttl}s }}; }}
 chain forward {{ type filter hook forward priority 0; policy drop;
  iifname "uplink" oifname "site0" ip saddr @technician ip daddr {spec.lan_cidr} accept
  iifname "site0" oifname "uplink" ip saddr {spec.lan_cidr} ip daddr @technician ct state established,related accept
 }}""",
        )
        # Forwarding is enabled only after the expiring policy exists. A crash
        # during creation must leave a namespace that cannot forward indefinitely.
        self._inside(spec, ["sysctl", "-w", "net.ipv4.ip_forward=1"])
        return spec.namespace

    def delete(self, spec: SessionNetwork):
        rules = self.run(["ip", "rule", "list"]).stdout
        for line in rules.splitlines():
            if line.split(":", 1)[0].strip() == spec.table:
                self.run(["ip", "rule", "del", "pref", spec.table])
        routes = self.run(["ip", "route", "show", "table", spec.table], check=False)
        if routes.returncode == 0 and routes.stdout.strip():
            self.run(["ip", "route", "flush", "table", spec.table])
        elif routes.returncode and "FIB table does not exist" not in routes.stderr:
            routes.check_returncode()
        namespaces = self.run(["ip", "netns", "list"]).stdout
        if any(
            line.split()[0] == spec.namespace for line in namespaces.splitlines() if line.strip()
        ):
            self.run(["ip", "netns", "delete", spec.namespace])
        if self.run(["ip", "link", "show", spec.host_interface], check=False).returncode == 0:
            self.run(["ip", "link", "delete", spec.host_interface])
