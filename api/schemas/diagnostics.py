"""Bounded machine reports and public diagnostic projections."""

from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import AwareDatetime, BaseModel, ConfigDict, StrictBool, model_validator


class WorkerObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observed_at: AwareDatetime
    gateway_connected: StrictBool | None
    technician_connected: StrictBool | None
    network: Literal["waiting", "ready", "error", "cleaned", "unknown"]
    code: Literal[
        "none",
        "vpn_unavailable",
        "firewall_failed",
        "network_failed",
        "cleanup_failed",
        "peer_disconnect_pending",
    ] = "none"

    @model_validator(mode="after")
    def consistent_evidence(self):
        if self.network == "ready" and not (
            self.gateway_connected is True and self.technician_connected is True
        ):
            raise ValueError("Ready requires both connected peers")
        if self.code != "none" and self.network not in ("error", "unknown"):
            raise ValueError("Failure codes require error or unknown network state")
        if self.network == "error" and self.code == "none":
            raise ValueError("Error requires a failure code")
        return self


class GatewayObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observed_at: AwareDatetime
    component: Literal["tunnel", "network"]
    state: Literal["starting", "ready", "error", "stopped"]
    code: Literal[
        "none",
        "tunnel_timeout",
        "tunnel_failed",
        "address_mismatch",
        "network_failed",
        "cleanup_failed",
    ] = "none"

    @model_validator(mode="after")
    def consistent_evidence(self):
        if (self.state == "error") != (self.code != "none"):
            raise ValueError("Error state and failure code must agree")
        if self.code.startswith("tunnel_") or self.code == "address_mismatch":
            if self.component != "tunnel":
                raise ValueError("Tunnel failures require the tunnel component")
        if self.code in ("network_failed", "cleanup_failed") and self.component != "network":
            raise ValueError("Network failures require the network component")
        return self


class DiagnosticStep(BaseModel):
    id: str
    title: str
    status: Literal["ok", "waiting", "error", "unknown", "not_applicable"]
    message: str
    action: str | None = None
    observed_at: datetime | None = None
    source: str | None = None


class DiagnosticsResponse(BaseModel):
    session_id: UUID
    status: str
    generated_at: datetime
    history_complete: bool
    steps: list[DiagnosticStep]


class SessionEventResponse(BaseModel):
    id: int
    session_id: UUID
    timestamp: datetime
    source: str
    event_type: str
    actor_id: UUID | None
    details: dict


class ObservationResponse(BaseModel):
    recorded: bool
