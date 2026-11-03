"""Small typed snapshots; no hostnames, processes, log text or caller-selected identity."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, model_validator
from factoryvpn_common.runtime_info import RuntimeInfo

ByteCount = Annotated[int, Field(strict=True, ge=0, le=2**60)]
ServiceState = Literal["ok", "error", "idle", "unknown"]


class ResourceMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["linux_system"] = "linux_system"
    disk_scope: Literal["collector_filesystem"] = "collector_filesystem"
    cpu_percent: Annotated[float, Field(strict=True, ge=0, le=100, allow_inf_nan=False)] | None = (
        None
    )
    memory_total_bytes: ByteCount | None = None
    memory_available_bytes: ByteCount | None = None
    disk_total_bytes: ByteCount | None = None
    disk_available_bytes: ByteCount | None = None
    uptime_seconds: Annotated[int, Field(strict=True, ge=0, le=10**12)] | None = None

    @model_validator(mode="after")
    def validate_pairs(self):
        for kind in ("memory", "disk"):
            total, available = getattr(self, kind + "_total_bytes"), getattr(
                self, kind + "_available_bytes"
            )
            if (total is None) != (available is None) or (
                total is not None and (total == 0 or available > total)
            ):
                raise ValueError("Total and available must be a consistent pair")
        return self


class GatewayServices(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: ServiceState
    control: ServiceState
    openvpn: ServiceState
    network: ServiceState


class ServerServices(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker: ServiceState
    openvpn: ServiceState


class GatewayTelemetry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observed_at: AwareDatetime
    metrics: ResourceMetrics
    services: GatewayServices


class ServerTelemetry(GatewayTelemetry):
    services: ServerServices
    runtime: RuntimeInfo | None = None


class TelemetryView(BaseModel):
    gateway_id: UUID | None = None
    display_name: str | None = None
    mac_address: str | None = None
    activation_mode: Literal["sms", "wifi"] | None = None
    freshness: Literal["missing", "fresh", "stale"]
    valid_for_seconds: float = Field(ge=0, le=90)
    observed_at: datetime | None = None
    received_at: datetime | None = None
    metrics: ResourceMetrics | None = None
    services: dict[str, ServiceState]


class MetricAggregate(BaseModel):
    mean: float | None
    peak: float | None
    samples: int


class HistoryPoint(BaseModel):
    at: datetime
    first_observed_at: datetime | None
    last_observed_at: datetime | None
    samples: int
    cpu: MetricAggregate
    memory: MetricAggregate
    disk: MetricAggregate


class TelemetryHistory(BaseModel):
    since: datetime
    until: datetime
    resolution_seconds: int
    retention_days: int
    available_from: datetime | None
    available_until: datetime | None
    points: list[HistoryPoint]
