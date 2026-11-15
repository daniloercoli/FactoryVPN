"""Configure trusted production stations on the FactoryVPN server, without printing keys."""

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import secrets

from dotenv import dotenv_values
from factoryvpn_common.certificates import pki_lock, write_private


def station_name(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise ValueError("Invalid station identifier")
    return value


def load_stations(env_file):
    env_file = Path(env_file)
    if env_file.is_symlink() or not env_file.is_file():
        raise ValueError("An existing, regular deployment environment file is required")
    values = json.loads(dotenv_values(env_file).get("FACTORY_STATIONS") or "{}")
    if not isinstance(values, dict) or any(
        station_name(name) != name
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        for name, digest in values.items()
    ):
        raise ValueError("Invalid factory station configuration")
    return values


def save_stations(env_file, stations):
    # Aggiornamento atomico: conserva le altre opzioni e non espone il contenuto.
    env_file = Path(env_file)
    setting = "FACTORY_STATIONS='" + json.dumps(stations, sort_keys=True) + "'"
    lines = env_file.read_text().splitlines()
    result, replaced = [], False
    for line in lines:
        if re.match(r"^\s*(?:export\s+)?FACTORY_STATIONS\s*=", line):
            if not replaced:
                result.append(setting)
                replaced = True
        else:
            result.append(line)
    if not replaced:
        result.append(setting)
    write_private(env_file, "\n".join(result) + "\n", replace=True)


def add_station(root, env_file, station, key_file, *, rotate=False):
    station = station_name(station)
    with pki_lock(root):
        stations = load_stations(env_file)
        if station in stations and not rotate:
            raise ValueError("Station already configured; rotation requires --rotate")
        secret = secrets.token_urlsafe(32)
        # Non sovrascrive una chiave locale: usare un nuovo file anche per la rotazione.
        write_private(key_file, secret + "\n")
        stations[station] = hashlib.sha256(secret.encode()).hexdigest()
        save_stations(env_file, stations)


def revoke_station(root, env_file, station):
    station = station_name(station)
    with pki_lock(root):
        stations = load_stations(env_file)
        if station not in stations:
            raise ValueError("Station is not configured")
        del stations[station]
        save_stations(env_file, stations)


def allow_ips(root, addresses):
    networks = [ipaddress.ip_network(value, strict=True) for value in addresses]
    if any(network.prefixlen == 0 for network in networks):
        raise ValueError("Trust specific station IPs or networks, not the entire Internet")
    content = "# IP sorgente delle postazioni; gli header del client non concedono accesso.\n"
    content += "".join(f"allow {network};\n" for network in dict.fromkeys(networks))
    content += "deny all;\n"
    with pki_lock(root):
        write_private(Path(root) / "factory/allow.conf", content, replace=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".runtime"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    actions = parser.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add-station")
    add.add_argument("--station", required=True)
    add.add_argument("--key-file", type=Path, required=True)
    add.add_argument("--rotate", action="store_true")
    revoke = actions.add_parser("revoke-station")
    revoke.add_argument("--station", required=True)
    allow = actions.add_parser("allow-ips")
    allow.add_argument("addresses", nargs="*", help="Complete allowlist; empty denies all")
    args = parser.parse_args()
    try:
        if args.action == "add-station":
            add_station(args.root, args.env_file, args.station, args.key_file, rotate=args.rotate)
        elif args.action == "revoke-station":
            revoke_station(args.root, args.env_file, args.station)
        else:
            allow_ips(args.root, args.addresses)
    except (ValueError, OSError):
        # Errori di parsing possono contenere dati della configurazione: non stamparli.
        parser.exit(1, "Configuration failed. Check arguments, files and existing station.\n")
    print("Factory access configuration saved. Recreate api and nginx to apply it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
