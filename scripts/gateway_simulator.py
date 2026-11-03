"""FactoryVPN Alpine demo: SMS gateway, Wi-Fi gateway and real VPN traffic.

Reuse the lab lifecycle with a separate directory, database and certificates.
Run `python -m scripts.gateway_simulator --help` for the available commands.
"""

from scripts.local_lab import ROOT, main


if __name__ == "__main__":
    main(ROOT / ".factoryvpn-simulator", simulator=True)
