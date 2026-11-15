"""Limiti dei tempi condivisi da API, gateway e worker."""

import os

# È un limite del protocollo, non la durata concessa a ogni sessione.
MAX_LEASE_SECONDS = 900


def env_seconds(name, default, minimum, maximum):
    """Rifiuta subito valori fuori intervallo, prima di avviare la rete."""
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name}: usare un valore tra {minimum} e {maximum} secondi")
    return value
