"""Client e server SSH temporanei, esclusivamente per il container di collaudo."""

import os
from pathlib import Path
import secrets
import select
import subprocess

from factoryvpn_common.network import run


class SSHLab:
    def __init__(self, root):
        if os.environ.get("FACTORYVPN_NETWORK_TEST") != "1":
            raise RuntimeError("SSHLab requires the disposable network test container")
        self.root = Path(root)
        self.processes = []
        # Sono credenziali del solo container, distrutte alla fine del test.
        # La password casuale sblocca l'account; sshd accetta esclusivamente la chiave.
        run(["chpasswd"], input="root:" + secrets.token_urlsafe(32) + "\n")
        Path("/run/sshd").mkdir(exist_ok=True)
        for name in ("host", "client"):
            run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.root / name)])
        (self.root / "known_hosts").write_text(
            "10.100.0.2,10.100.0.3 " + (self.root / "host.pub").read_text()
        )
        (self.root / "sshd_config").write_text(
            f"HostKey {self.root / 'host'}\n"
            f"AuthorizedKeysFile {self.root / 'client.pub'}\n"
            "ListenAddress 0.0.0.0\n"
            "PermitRootLogin prohibit-password\n"
            "PasswordAuthentication no\n"
            "KbdInteractiveAuthentication no\n"
            "UsePAM no\n"
            "PidFile none\n"
        )

    def start_server(self, gateway):
        log = self.root / f"{gateway}.log"
        process = subprocess.Popen(
            [
                "ip",
                "netns",
                "exec",
                gateway,
                "/usr/sbin/sshd",
                "-D",
                "-e",
                "-E",
                str(log),
                "-f",
                str(self.root / "sshd_config"),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.processes.append(process)

    def command(self, technician, remote_command, *, keepalive_seconds=1):
        return [
            "ip",
            "netns",
            "exec",
            technician,
            "ssh",
            "-i",
            str(self.root / "client"),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.root / 'known_hosts'}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "ConnectTimeout=2",
            "-o",
            f"ServerAliveInterval={keepalive_seconds}",
            "-o",
            "ServerAliveCountMax=1",
            f"root@10.100.0.{int(technician.removeprefix('tech'))+1}",
            remote_command,
        ]

    def execute(self, technician, remote_command, *, check=True):
        result = run(self.command(technician, remote_command), check=False)
        if check and result.returncode:
            print(result.stderr)
            for log in self.root.glob("*.log"):
                print(log.read_text())
            result.check_returncode()
        return result

    def open_shell(self, technician):
        # Manteniamo lo stesso TCP aperto per verificare che anche una shell già
        # autenticata smetta di trasferire dati alla scadenza o alla chiusura.
        process = subprocess.Popen(
            self.command(technician, 'while read line; do echo "reply:$line"; done'),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.processes.append(process)
        return process

    @staticmethod
    def probe(shell):
        try:
            shell.stdin.write("check\n")
            shell.stdin.flush()
        except BrokenPipeError:
            return "blocked"
        if select.select([shell.stdout], [], [], 3)[0]:
            return shell.stdout.readline().strip() or "blocked"
        return "blocked"

    def close(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
