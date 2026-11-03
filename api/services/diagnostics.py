"""Diagnostic facts never authorize a connection or extend its lifetime."""

from datetime import datetime, timedelta
from fastapi import HTTPException
from api.services.session_events import event
from api.time_utils import utc_now


async def observe(db, session, source, report):
    """Caller holds the session row lock. Repeats refresh facts, not history."""
    now = utc_now()
    if report.observed_at > now + timedelta(seconds=30):
        raise HTTPException(422, "Observation timestamp is in the future")
    if report.observed_at < now - timedelta(minutes=5):
        return False
    if session.cleaned_at:
        return False
    # In-flight success reports must not present a closed session as ready.
    cleanup = report.code in ("cleanup_failed", "peer_disconnect_pending")
    if session.status not in ("REQUESTED", "ACTIVE") and not cleanup:
        return False
    key = source if source == "worker" else "gateway_" + report.component
    previous = (session.diagnostic_state or {}).get(key, {})
    if previous and report.observed_at <= datetime.fromisoformat(previous["observed_at"]):
        return False
    values = report.model_dump(mode="json", exclude={"observed_at"})
    old_values = {k: v for k, v in previous.items() if k not in ("observed_at", "received_at")}
    if values != old_values:
        event(db, session, key + "_changed", source=source, details=values)
    session.diagnostic_state = {
        **(session.diagnostic_state or {}),
        key: {
            **values,
            "observed_at": report.observed_at.isoformat(),
            "received_at": now.isoformat(),
        },
    }
    return True


def project(session):
    now = utc_now()
    state = session.diagnostic_state or {}
    live = session.status in ("REQUESTED", "ACTIVE")
    steps = []

    def add(key, title, status, message, action=None, at=None, source=None):
        steps.append(
            dict(
                id=key,
                title=title,
                status=status,
                message=message,
                action=action,
                observed_at=at,
                source=source,
            )
        )

    def fresh(key, include_terminal=False):
        value = state.get(key, {})
        if not value.get("observed_at") or not value.get("received_at"):
            return {}
        # Use both timestamps: delayed reports cannot make old evidence look current.
        at = min(
            datetime.fromisoformat(value["observed_at"]),
            datetime.fromisoformat(value["received_at"]),
        )
        return value if (live or include_terminal) and now - at <= timedelta(seconds=30) else {}

    sms = state.get("sms", {})
    if session.activation_mode == "wifi":
        add("sms", "Attivazione SMS", "not_applicable", "Il gateway usa il controllo Wi-Fi.")
    else:
        outcome = sms.get("outcome")
        messages = {
            "accepted": ("ok", "Richiesta accettata dal provider; la consegna non è certificata."),
            "rejected": ("error", "Il provider ha rifiutato la richiesta."),
            "not_configured": ("error", "Il servizio SMS non è configurato."),
            "unconfirmed": ("unknown", "L'esito della richiesta al provider non è confermato."),
            "pending": ("waiting", "Tentativo registrato; risposta del provider non disponibile."),
        }
        status, message = messages.get(outcome, ("unknown", "Esito del provider non disponibile."))
        if not outcome and session.sms_sent_at:
            status, message = "ok", "È registrata un'accettazione del provider."
        add(
            "sms",
            "Provider SMS",
            status,
            message,
            (
                "Verificare il servizio SMS con l'amministratore."
                if status in ("error", "unknown")
                else None
            ),
            sms.get("received_at") or session.sms_sent_at,
            "api",
        )
    add(
        "gateway",
        "Conferma del gateway",
        "ok" if session.activation_received_at else "waiting" if live else "unknown",
        (
            "Il gateway ha preso in carico la richiesta."
            if session.activation_received_at
            else "Non è registrata una conferma di attivazione."
        ),
        (
            None
            if session.activation_received_at
            else "Verificare alimentazione e connettività del gateway."
        ),
        session.activation_received_at,
        "gateway",
    )
    worker = fresh("worker")
    add(
        "worker",
        "Verifica del server",
        "error" if worker.get("code") == "vpn_unavailable" else "ok" if worker else "unknown",
        (
            "Il worker non riesce a leggere i collegamenti OpenVPN."
            if worker.get("code") == "vpn_unavailable"
            else (
                "Il worker sta comunicando le osservazioni della sessione."
                if worker
                else "Non sono disponibili osservazioni recenti del worker."
            )
        ),
        (
            "Verificare worker e servizio OpenVPN con l'amministratore."
            if not worker or worker.get("code") == "vpn_unavailable"
            else None
        ),
        worker.get("observed_at"),
        "worker",
    )
    for key, title, field in (
        ("tunnel", "Tunnel del gateway", "gateway_connected"),
        ("technician", "Client del tecnico", "technician_connected"),
    ):
        connected = worker.get(field)
        report = fresh("gateway_tunnel") if key == "tunnel" else {}
        gateway_error = report.get("state") == "error" and (
            not worker or report["received_at"] >= worker["received_at"]
        )
        if connected is True and not gateway_error:
            add(
                key,
                title,
                "ok",
                "Collegamento osservato sul server OpenVPN.",
                at=worker["observed_at"],
                source="worker",
            )
        elif gateway_error:
            add(
                key,
                title,
                "error",
                "Il gateway segnala un errore di apertura del tunnel.",
                "Verificare profilo OpenVPN, certificati e raggiungibilità del server.",
                report["observed_at"],
                "gateway",
            )
        elif connected is False:
            add(
                key,
                title,
                "waiting",
                "Il client non risulta collegato al server.",
                (
                    "Avviare manualmente OpenVPN con il profilo FactoryVPN."
                    if key == "technician"
                    else "Verificare connettività e configurazione VPN del gateway."
                ),
                worker["observed_at"],
                "worker",
            )
        else:
            add(
                key,
                title,
                "unknown",
                "Nessuna osservazione recente del collegamento.",
                "Attendere un aggiornamento; verificare il servizio VPN se l'attesa continua.",
            )
    network = fresh("gateway_network")
    newer_gateway_error = network.get("state") == "error" and (
        not worker or network["received_at"] >= worker["received_at"]
    )
    if worker.get("network") == "ready" and session.status == "ACTIVE" and not newer_gateway_error:
        add(
            "network",
            "Rete di assistenza",
            "ok",
            "Il server conferma la configurazione della rete di assistenza.",
            "La raggiungibilità del singolo PLC va verificata con gli strumenti di assistenza.",
            worker["observed_at"],
            "worker",
        )
    elif worker.get("code") in ("network_failed", "firewall_failed") or newer_gateway_error:
        report = worker if worker.get("code") in ("network_failed", "firewall_failed") else network
        add(
            "network",
            "Rete di assistenza",
            "error",
            "È stato segnalato un errore di configurazione della rete.",
            "Verificare LAN, interfacce e regole di rete con l'amministratore.",
            report["observed_at"],
            "worker" if report is worker else "gateway",
        )
    else:
        add(
            "network",
            "Rete di assistenza",
            "waiting" if worker else "unknown",
            "Configurazione corrente della rete non confermata.",
        )
    if session.cleaned_at:
        add(
            "cleanup",
            "Rilascio delle risorse",
            "ok",
            "Il server ha confermato la pulizia.",
            at=session.cleaned_at,
            source="worker",
        )
    elif not live and fresh("worker", True).get("code") in (
        "cleanup_failed",
        "peer_disconnect_pending",
    ):
        cleanup = fresh("worker", True)
        add(
            "cleanup",
            "Rilascio delle risorse",
            "error",
            "Il worker segnala un problema di pulizia o un client ancora collegato.",
            "Verificare il server; le risorse restano prenotate fino al rilascio.",
            cleanup["observed_at"],
            "worker",
        )
    elif not live:
        add(
            "cleanup",
            "Rilascio delle risorse",
            "waiting",
            "La sessione è terminata; il rilascio non è ancora confermato.",
            "Attendere la pulizia prima di aprire un'altra VPN.",
        )
    else:
        add(
            "cleanup",
            "Rilascio delle risorse",
            "not_applicable",
            "Le risorse rimangono prenotate durante la sessione.",
        )
    if not live:
        for step in steps:
            if step["id"] in ("worker", "tunnel", "technician", "network"):
                step["message"] = (
                    "La sessione è terminata. Le osservazioni precedenti sono nella cronologia."
                )
                step["action"] = None
    return dict(
        session_id=session.id,
        status=session.status,
        generated_at=now,
        history_complete=session.diagnostics_started_at is not None,
        steps=steps,
    )
