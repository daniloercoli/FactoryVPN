"""CLI di fine linea: identità locale durabile, export device e creazione idempotente."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from factoryvpn_common.certificates import read_bounded, write_private


def enroll(
    record_path,
    identity_path,
    server_url,
    serial_number,
    key,
    *,
    client=None,
    display_name=None,
    external_reference=None,
):
    record_path, identity_path = Path(record_path), Path(identity_path)
    url = urlsplit(server_url)
    url.port  # Rifiuta porte malformate prima di scrivere l'identità.
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):
        raise ValueError("FactoryVPN requires an HTTPS origin")
    server_url = server_url.rstrip("/")
    serial_number = serial_number.strip()
    if not 1 <= len(serial_number) <= 100 or not key:
        raise ValueError("Serial number and station API key are required")
    expected = {
        "serial_number": serial_number,
        "display_name": display_name,
        "external_reference": external_reference,
    }
    if record_path.exists():
        record = json.loads(read_bounded(record_path))
        if (
            record.get("server_url") != server_url
            or record.get("identity_path") != str(identity_path.absolute())
            or any(record["payload"].get(k) != v for k, v in expected.items())
        ):
            raise ValueError("Existing factory record differs; do not regenerate its identity")
    else:
        secret = secrets.token_urlsafe(32)
        gateway_id = str(uuid4())
        record = {
            "format": 1,
            "server_url": server_url,
            "operation_id": str(uuid4()),
            "identity_path": str(identity_path.absolute()),
            "enrollment_key": secret,
            "payload": {
                "gateway_id": gateway_id,
                "secret_hash": hashlib.sha256(secret.encode()).hexdigest(),
                **expected,
            },
            "confirmed": False,
        }
        # Prima di qualunque export o HTTP, il retry possiede identità e chiave originali.
        write_private(record_path, json.dumps(record))
    if identity_path.exists():
        identity = json.loads(read_bounded(identity_path))
    else:
        if not record.get("enrollment_key"):
            raise ValueError("Factory secret removed after success; use authorized recovery")
        identity = {
            "format": 1,
            "gateway_id": record["payload"]["gateway_id"],
            "server_url": server_url,
            "enrollment_key": record["enrollment_key"],
        }
        write_private(identity_path, json.dumps(identity))
        identity = json.loads(read_bounded(identity_path))
    if (
        identity.get("gateway_id") != record["payload"]["gateway_id"]
        or identity.get("server_url") != server_url
        or hashlib.sha256(identity.get("enrollment_key", "").encode()).hexdigest()
        != record["payload"]["secret_hash"]
    ):
        raise ValueError("Device identity verification failed; refusing to overwrite it")
    if client is None:
        with httpx.Client(timeout=30, follow_redirects=False) as connection:
            return enroll(
                record_path,
                identity_path,
                server_url,
                serial_number,
                key,
                client=connection,
                display_name=display_name,
                external_reference=external_reference,
            )
    response = client.post(
        server_url + "/api/v1/factory/gateways",
        headers={"X-Factory-Key": key, "Idempotency-Key": record["operation_id"]},
        json=record["payload"],
    )
    response.raise_for_status()
    result = response.json()
    if result.get("id") != record["payload"]["gateway_id"]:
        raise ValueError("Server returned a different factory identity")
    record.pop("enrollment_key", None)
    record["confirmed"] = True
    write_private(record_path, json.dumps(record), replace=True)
    return record["payload"]["gateway_id"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", required=True, type=Path)
    parser.add_argument(
        "--identity-file",
        required=True,
        type=Path,
        help="File persistente sul dispositivo montato (verificato dopo scrittura)",
    )
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--serial-number", required=True)
    parser.add_argument("--display-name")
    parser.add_argument("--external-reference")
    parser.add_argument("--key-file", type=Path, help="Altrimenti usa FACTORYVPN_FACTORY_KEY")
    args = parser.parse_args(argv)
    key = (
        read_bounded(args.key_file).decode().strip()
        if args.key_file
        else os.getenv("FACTORYVPN_FACTORY_KEY", "")
    )
    try:
        identifier = enroll(
            args.record,
            args.identity_file,
            args.server_url,
            args.serial_number,
            key,
            display_name=args.display_name,
            external_reference=args.external_reference,
        )
    except httpx.HTTPStatusError as exc:
        parser.exit(
            1,
            f"FactoryVPN HTTP {exc.response.status_code}; conservare registro e identità per il retry.\n",
        )
    except (httpx.TransportError, OSError, ValueError):
        parser.exit(
            1,
            "Arruolamento non completato; verificare registro, dispositivo e HTTPS senza rigenerare l'identità.\n",
        )
    print("FactoryVPN gateway registered: " + identifier)


if __name__ == "__main__":
    main()
