"""Machine protocol shared by the API, gateway and OpenVPN hooks."""

import hashlib
import hmac
import time
from uuid import UUID


def peer_name(kind, identifier):
    if kind not in {"gateway", "technician"}:
        raise ValueError("Unknown VPN peer kind")
    return f"{kind}-{UUID(str(identifier))}"


def parse_peer_name(value):
    kind, identifier = value.split("-", 1)
    canonical = peer_name(kind, identifier)
    if canonical != value:
        raise ValueError("Invalid VPN common name")
    return kind, UUID(identifier)


def activation_message(session_id, expires_at, key):
    payload = f"{UUID(str(session_id))} {int(expires_at)}"
    signature = hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"FACTORYVPN: activate {payload} {signature}"


def verify_activation(message, key, now=None):
    try:
        prefix, verb, identifier, deadline, signature = message.strip().split()
        if prefix != "FACTORYVPN:" or verb != "activate":
            return None
        expiry = int(deadline)
        if expiry <= (time.time() if now is None else now):
            return None
        expected = activation_message(identifier, expiry, key).split()[-1]
        if not hmac.compare_digest(signature, expected):
            return None
        return str(UUID(identifier))
    except (ValueError, TypeError):
        return None
