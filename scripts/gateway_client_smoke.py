"""Client fault checks against the already running, isolated gateway simulator.

Uses only its reserved demonstration accounts; run separately from manual demos.
Every created session is closed in finally. No host process or route is changed.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json

from scripts.local_lab import Lab, ROOT, command, save, wait_for


def main():
    lab = Lab(ROOT / ".factoryvpn-simulator")
    if not lab.state.get("simulator"):
        raise RuntimeError("Client checks require the dedicated gateway simulator")
    credentials = json.loads((lab.root / "credentials.json").read_text())
    clients, checks = [], []

    def record(label):
        checks.append(label)
        print("PASS: " + label, flush=True)

    def python(code, *args):
        return lab.docker("exec", "-T", "peers", "python", "-c", code, *args).decode().strip()

    def traffic(number, expected):
        for protocol in ("tcp", "udp"):
            observed = lab.peer("query", number, protocol)
            if observed != expected:
                raise RuntimeError(f"Site {number} {protocol}: expected {expected}, got {observed}")

    def vpn_pids(namespace):
        return json.loads(
            python(
                "import json, subprocess, sys; from pathlib import Path; "
                "pids=subprocess.check_output(['ip','netns','pids',sys.argv[1]],text=True).split(); "
                "print(json.dumps([int(p) for p in pids "
                "if Path('/proc',p,'exe').resolve().name == 'openvpn']))",
                namespace,
            )
        )

    try:
        for number in (1, 2):
            client = lab.login(credentials[f"tech{number}"])
            clients.append(client)
            lab.disconnect(client, number)
        # Ordinary, unsigned SMS must not prevent the following signed command
        # from reaching the real agent, even when its body looks like an AT error.
        python(
            "from scripts.simulator_sms import Inbox, PHONE; "
            "inbox=Inbox(); "
            "[inbox.accept(PHONE, body) for body in ['ERROR', '+CMS ERROR: 500']]"
        )
        for number, client in enumerate(clients, 1):
            lab.connect(client, number)
            traffic(number, f"site{number}")
        record("Unsigned SMS error text is discarded; signed SMS still opens the real VPN")

        pidfile = lab.root / "peers/site1/technician.pid"
        original = pidfile.read_bytes()
        other = (lab.root / "peers/site2/technician.pid").read_bytes()
        try:
            pidfile.write_bytes(other)
            lab.peer("client-stop", 1)
            traffic(1, "site1")
            traffic(2, "site2")
        finally:
            pidfile.write_bytes(original)
        record("Stale PID referring to site 2 does not stop either technician")

        with ThreadPoolExecutor(max_workers=2) as pool:
            starts = [pool.submit(lab.peer, "client-start", 1) for _ in range(2)]
            for started in starts:
                started.result()
        if len(vpn_pids("tech1")) != 1:
            raise RuntimeError("Concurrent starts left multiple technician processes")
        wait_for("technician reconnect", lambda: lab.peer("query", 1) == "site1")
        traffic(1, "site1")
        lab.peer("client-stop", 1)
        if vpn_pids("tech1"):
            raise RuntimeError("Client-stop returned before the technician process exited")
        traffic(1, "blocked")
        traffic(2, "site2")
        lab.peer("client-start", 1)
        wait_for("technician restart", lambda: lab.peer("query", 1) == "site1")
        record("Concurrent starts leave one client; stop waits for exit and preserves site 2")

        (old_pid,) = vpn_pids("gw1")
        python(
            "import os, signal, sys; "
            "fd=os.pidfd_open(int(sys.argv[1])); "
            "signal.pidfd_send_signal(fd, signal.SIGTERM); os.close(fd)",
            str(old_pid),
        )
        wait_for(
            "gateway OpenVPN replacement",
            lambda: bool(vpn_pids("gw1") and old_pid not in vpn_pids("gw1")),
        )
        wait_for("gateway traffic recovery", lambda: lab.peer("query", 1) == "site1")
        traffic(1, "site1")
        traffic(2, "site2")
        if json.loads(lab.peer("sms-status", 1)) != {"pending": 0, "delivered": 0}:
            raise RuntimeError("Unexpected SMS retry during live gateway recovery")
        record("Gateway replaces exited OpenVPN and restores traffic without a new SMS")
        save(
            lab.root / "client-review-report.json",
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "git_head": command(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).decode().strip(),
                "checks": checks,
            },
        )
    finally:
        # Attempt cleanup for both sites even if one of the API calls fails.
        errors = []
        for number, client in enumerate(clients, 1):
            try:
                lab.disconnect(client, number)
            except Exception as exc:
                errors.append(exc)
            finally:
                client.close()
        for number in (1, 2):
            try:
                lab.peer("client-stop", number)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError(
                "Client review cleanup incomplete; inspect the simulator"
            ) from errors[0]


if __name__ == "__main__":
    main()
