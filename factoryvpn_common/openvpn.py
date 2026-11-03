"""Bounded OpenVPN management over a local Unix socket, including complete status."""

import csv
import io
import socket
import re
from factoryvpn_common.protocol import parse_peer_name


class ManagementClient:
    def __init__(self, path="/run/openvpn/management.sock", timeout=5):
        self.path, self.timeout = path, timeout
        self.version = None

    def command(self, command, status=False):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout)
            sock.connect(self.path)
            sock.sendall((command + "\n").encode())
            with sock.makefile("r", encoding="utf-8") as stream:
                lines = []
                size = 0
                while True:
                    line = stream.readline(65536)
                    if not line:
                        raise RuntimeError("OpenVPN management disconnected before response")
                    size += len(line)
                    if size > 4 * 1024 * 1024:
                        raise RuntimeError("OpenVPN management response too large")
                    line = line.rstrip("\r\n")
                    if line.startswith(">"):
                        continue  # greeting and asynchronous notifications are not command results
                    lines.append(line)
                    if (status and line == "END") or line.startswith(("SUCCESS:", "ERROR:")):
                        return lines

    def clients(self):
        lines = self.command("status 2", status=True)
        if not lines or lines[-1] != "END":
            raise RuntimeError("Incomplete OpenVPN status response")
        clients = {}
        version = None
        for row in csv.reader(io.StringIO("\n".join(lines))):
            if len(row) > 1 and row[0] == "TITLE":
                match = re.match(r"OpenVPN ([0-9][\w.+-]{0,63})(?:\s|$)", row[1])
                version = match.group(1) if match else None
            if row and row[0] == "CLIENT_LIST":
                if len(row) < 4:
                    raise ValueError("Malformed OpenVPN client status")
                parse_peer_name(row[1])
                clients[row[1]] = row[3]
        self.version = version
        return clients

    def kill(self, common_name):
        parse_peer_name(common_name)
        return self.command("kill " + common_name)[-1].startswith("SUCCESS:")
