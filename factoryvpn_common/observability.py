"""Bounded wire contracts shared by gateway, worker and API. No free-form logs."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


Label = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[\w .()+,:@-]+$")]
ComponentName = Literal["agent", "openvpn", "system", "hardware", "modem", "wifi"]
CollectionState = Literal["reported", "not_applicable", "unsupported", "collection_failed"]


class Component(StrictModel):
    state: CollectionState
    origin: Literal["detected", "configured", "build"] = "detected"
    version: Label | None = None
    build: Label | None = None
    model: Label | None = None
    manufacturer: Label | None = None
    revision: Label | None = None
    kernel: Label | None = None
    architecture: Label | None = None
    imei: Annotated[str, Field(pattern=r"^[0-9]{15}$")] | None = None
    iccid: Annotated[str, Field(pattern=r"^[0-9]{18,22}$")] | None = None
    interface: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_.-]{1,15}$")] | None = None
    mac_address: Annotated[str, Field(pattern=r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")] | None = None

    @model_validator(mode="after")
    def state_matches_data(self):
        data = self.model_dump(exclude={"state", "origin"}, exclude_none=True)
        if bool(data) != (self.state == "reported"):
            raise ValueError("Only reported components contain data")
        return self


COMPONENT_FIELDS = {
    "agent": {"version", "build"},
    "openvpn": {"version"},
    "system": {"model", "version", "kernel", "architecture"},
    "hardware": {"model", "revision"},
    "modem": {"imei", "iccid", "manufacturer", "model", "version"},
    "wifi": {"interface", "mac_address"},
}


class SystemReport(StrictModel):
    report_id: UUID
    observed_at: AwareDatetime
    components: dict[ComponentName, Component] = Field(min_length=1, max_length=6)
    logs_supported: Literal[True] = True

    @model_validator(mode="after")
    def typed_components(self):
        for name, value in self.components.items():
            fields = value.model_dump(exclude={"state", "origin"}, exclude_none=True)
            if set(fields) - COMPONENT_FIELDS[name]:
                raise ValueError("Invalid fields for component")
        return self


# code: (component, severity, needs session, allowed origins, Italian public message)
LOG_CODES = {
    "agent_started": ("agent", "info", False, {"agent"}, "Agente avviato"),
    "registration_failed": ("api", "error", False, {"agent"}, "Registrazione non riuscita"),
    "api_unavailable": ("api", "warning", False, {"agent"}, "Contatto API non riuscito"),
    "modem_failed": ("modem", "warning", False, {"agent"}, "Lettura modem non riuscita"),
    "collection_failed": ("collector", "warning", False, {"agent"}, "Raccolta componenti parziale"),
    "tunnel_failed": ("openvpn", "error", True, {"agent"}, "Avvio del tunnel non riuscito"),
    "tunnel_timeout": ("openvpn", "error", True, {"agent"}, "Tempo di avvio del tunnel esaurito"),
    "tunnel_exited": ("openvpn", "error", True, {"agent"}, "Processo OpenVPN terminato"),
    "address_mismatch": ("openvpn", "error", True, {"agent"}, "Indirizzo del tunnel inatteso"),
    "network_failed": (
        "network",
        "error",
        True,
        {"agent", "worker"},
        "Configurazione della rete non riuscita",
    ),
    "cleanup_failed": (
        "network",
        "error",
        True,
        {"agent", "worker"},
        "Pulizia delle risorse non riuscita",
    ),
    "vpn_unavailable": ("openvpn", "error", True, {"worker"}, "Controllo OpenVPN non disponibile"),
    "firewall_failed": (
        "network",
        "error",
        True,
        {"worker"},
        "Aggiornamento firewall non riuscito",
    ),
    "peer_disconnect_pending": (
        "openvpn",
        "warning",
        True,
        {"worker"},
        "Disconnessione dei client ancora in attesa",
    ),
    "worker_api_failed": (
        "api",
        "warning",
        True,
        {"worker"},
        "Aggiornamento della sessione non riuscito",
    ),
    "sms_failed": ("sms", "error", True, {"api"}, "Invio di attivazione SMS non riuscito"),
    "logs_dropped": (
        "collector",
        "warning",
        False,
        {"agent", "worker", "api"},
        "Eventi tecnici scartati dalla coda",
    ),
}
LogSource = Literal["agent", "worker", "api"]


class LogParameters(StrictModel):
    http_status: Annotated[int, Field(strict=True, ge=100, le=599)] | None = None
    exit_code: Annotated[int, Field(strict=True, ge=-255, le=255)] | None = None
    dropped: Annotated[int, Field(strict=True, ge=1, le=1000000)] | None = None


class TechnicalEvent(StrictModel):
    event_id: UUID
    observed_at: AwareDatetime
    first_observed_at: AwareDatetime
    session_id: UUID | None = None
    code: str = Field(min_length=1, max_length=40)
    occurrences: int = Field(default=1, strict=True, ge=1, le=1000000)
    parameters: LogParameters = Field(default_factory=LogParameters)

    @model_validator(mode="after")
    def validate_code(self):
        definition = LOG_CODES.get(self.code)
        if not definition or (definition[2] and self.session_id is None):
            raise ValueError("Unknown code or missing session")
        if self.first_observed_at > self.observed_at:
            raise ValueError("Invalid event interval")
        allowed = (
            {"http_status"}
            if self.code
            in {"registration_failed", "api_unavailable", "worker_api_failed", "sms_failed"}
            else (
                {"exit_code"}
                if self.code == "tunnel_exited"
                else {"dropped"} if self.code == "logs_dropped" else set()
            )
        )
        if set(self.parameters.model_dump(exclude_none=True)) - allowed:
            raise ValueError("Invalid parameters for code")
        if len(self.model_dump_json().encode()) > 2048:
            raise ValueError("Event too large")
        return self


class EventBatch(StrictModel):
    events: list[TechnicalEvent] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def bounded(self):
        if len(self.model_dump_json().encode()) > 65536:
            raise ValueError("Batch too large")
        return self


class BatchResult(StrictModel):
    accepted: int
    duplicates: int
    discarded: int


class ComponentView(StrictModel):
    latest: Component
    observed_at: datetime
    last_success: Component | None = None
    last_success_at: datetime | None = None


class SystemView(StrictModel):
    gateway_id: UUID
    received_at: datetime | None = None
    components: dict[ComponentName, ComponentView] = Field(default_factory=dict)
    logs_supported: bool | None = None


class TechnicalLogView(StrictModel):
    id: int
    event_id: UUID
    observed_at: datetime
    first_observed_at: datetime
    received_at: datetime
    source: LogSource
    component: str
    severity: str
    code: str
    message: str
    session_id: UUID | None
    occurrences: int
    parameters: LogParameters
