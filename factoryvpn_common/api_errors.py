"""Errori API che consentono di conservare il lease già installato, senza rinnovarlo."""

import httpx


def temporary_api_error(exc):
    # Connessione interrotta, timeout o server momentaneamente indisponibile.
    # Rifiuti espliciti e risposte non valide non ricevono questa tolleranza.
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status in (408, 429) or 500 <= status <= 599
    return False
