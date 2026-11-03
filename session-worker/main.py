"""Authenticated session maintenance and conservative namespace cleanup."""

import asyncio
import logging
import os
import re
import subprocess
import signal
import time
from contextlib import suppress
from factoryvpn_common.telemetry import telemetry_loop
from factoryvpn_common.technical_logs import LogQueue
from factoryvpn_common.runtime_info import local_runtime
from datetime import datetime, timezone
from importlib import import_module
from typing import Literal
from pydantic import Field, TypeAdapter, model_validator
from factoryvpn_common.network import SessionNetwork
from factoryvpn_common.openvpn import ManagementClient
from factoryvpn_common.protocol import parse_peer_name

import httpx
from pydantic import BaseModel, field_validator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

API_URL = os.getenv("API_URL", "http://api:8000").rstrip("/")
POLL_INTERVAL = int(os.getenv("WORKER_POLL_INTERVAL", "5"))
WORKER_API_KEY = os.getenv("WORKER_API_KEY", "")


class NamespaceStates(BaseModel):
    protected: list[str]
    terminal: list[str]

    @field_validator("protected", "terminal")
    @classmethod
    def validate_names(cls, names: list[str]) -> list[str]:
        if any(not re.fullmatch(r"factoryvpn_[0-9a-f]{8}", name) for name in names):
            raise ValueError("Invalid namespace name in API response")
        return names


def worker_headers() -> dict[str, str]:
    if not WORKER_API_KEY:
        raise RuntimeError("WORKER_API_KEY must be configured")
    return {"X-Worker-Key": WORKER_API_KEY}


async def expire_stale_sessions():
    """Expire sessions through the dedicated worker endpoint."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{API_URL}/api/v1/internal/sessions/expire-stale",
                headers=worker_headers(),
            )
            response.raise_for_status()
            expired = response.json().get("expired", 0)
            if expired:
                logger.info("Expired %s stale sessions", expired)
    except Exception:
        logger.exception("Failed to expire stale sessions")


async def cleanup_namespaces():
    """Delete only namespaces confirmed terminal by a valid API snapshot.

    Unknown namespaces and those of live sessions are preserved. API failures
    or malformed responses abort cleanup before any network command runs.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{API_URL}/api/v1/internal/sessions/namespace-states",
                headers=worker_headers(),
            )
            response.raise_for_status()
            states = NamespaceStates.model_validate(response.json())

        removable = set(states.terminal) - set(states.protected)
        if not removable:
            return
        result = subprocess.run(
            ["ip", "netns", "list"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            name = line.split()[0] if line.strip() else ""
            if name in removable:
                subprocess.run(
                    ["ip", "netns", "delete", name],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                logger.info("Deleted namespace for terminal session: %s", name)
    except Exception:
        logger.exception("Namespace cleanup failed; remaining resources preserved")


class WorkItem(BaseModel):
    session_id: str
    status: Literal["REQUESTED", "ACTIVE", "CLOSED", "EXPIRED", "FAILED"]
    network: SessionNetwork
    technician_cn: str
    gateway_cn: str
    gateway_ready: bool
    lease_seconds: int = Field(ge=0, le=30)
    activation_confirmed: bool

    @model_validator(mode="after")
    def validate_identity(self):
        if self.session_id != self.network.session_id:
            raise ValueError("Session and network identities differ")
        if (
            parse_peer_name(self.technician_cn)[0] != "technician"
            or parse_peer_name(self.gateway_cn)[0] != "gateway"
        ):
            raise ValueError("Wrong VPN peer kinds")
        return self


class SessionWorker:
    def __init__(self, client, namespaces, firewall, vpn):
        self.client, self.namespaces, self.firewall, self.vpn = client, namespaces, firewall, vpn
        self.last_cycle = self.last_vpn_probe = 0
        self.cycle_state = self.vpn_state = "unknown"
        self.logs = LogQueue("worker")

    def telemetry_services(self):
        now = time.monotonic()
        return {
            "worker": (
                self.cycle_state if self.last_cycle and now - self.last_cycle <= 30 else "unknown"
            ),
            "openvpn": (
                self.vpn_state
                if self.last_vpn_probe and now - self.last_vpn_probe <= 30
                else "unknown"
            ),
        }

    async def post(self, item, action, observation=None):
        response = await self.client.post(
            f"/api/v1/internal/sessions/{item.session_id}/{action}",
            **({"json": observation} if observation is not None else {}),
        )
        response.raise_for_status()

    def observation(self, item, clients, network, code="none", observed_at=None):
        return {
            "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
            "gateway_connected": (
                clients.get(item.gateway_cn) == item.network.gateway_ip
                if clients is not None
                else None
            ),
            "technician_connected": (
                clients.get(item.technician_cn) == item.network.technician_ip
                if clients is not None
                else None
            ),
            "network": network,
            "code": code,
        }

    async def report_failure(self, item, code, clients=None, observed_at=None):
        self.logs.emit(code, item.session_id, route=str(parse_peer_name(item.gateway_cn)[1]))
        try:
            async with asyncio.timeout(2):
                await self.post(
                    item,
                    "diagnostics",
                    self.observation(
                        item, clients, "unknown" if clients is None else "error", code, observed_at
                    ),
                )
        except Exception:
            logger.warning("Diagnostic report unavailable for session %s", item.session_id)

    async def report_all(self, items, code, clients=None, observed_at=None):
        await self.report_batch([(item, code, clients, observed_at) for item in items])

    async def report_batch(self, reports):
        semaphore = asyncio.Semaphore(4)

        async def report(args):
            async with semaphore:
                await self.report_failure(*args)

        try:
            async with asyncio.timeout(2):
                await asyncio.gather(*(report(args) for args in reports))
        except TimeoutError:
            logger.warning("Diagnostic batch timed out; lifecycle processing takes priority")

    async def reconcile(self):
        response = await self.client.get("/api/v1/internal/sessions/work")
        response.raise_for_status()
        items = TypeAdapter(list[WorkItem]).validate_python(response.json())
        if len({item.network.slot for item in items}) != len(items):
            raise ValueError("API returned colliding network slots")
        try:
            clients = await asyncio.to_thread(self.vpn.clients)
        except Exception:
            self.last_vpn_probe, self.vpn_state = time.monotonic(), "error"
            await self.report_all(items, "vpn_unavailable")
            raise
        self.last_vpn_probe, self.vpn_state = time.monotonic(), "ok"
        observed_at = datetime.now(timezone.utc).isoformat()
        ready = [
            item
            for item in items
            if item.status in ("REQUESTED", "ACTIVE")
            and item.activation_confirmed
            and item.gateway_ready
            and item.lease_seconds > 0
            and clients.get(item.technician_cn) == item.network.technician_ip
            and clients.get(item.gateway_cn) == item.network.gateway_ip
        ]
        # Withdraw authorization before cleanup. Unknown resources are never adopted/deleted.
        try:
            await asyncio.to_thread(self.firewall.apply, [item.network for item in ready])
        except Exception:
            await self.report_all(items, "firewall_failed", clients, observed_at)
            raise
        failures = []
        cycle_failed = False
        for item in items:
            failure_code = "network_failed"
            try:
                if item.status in ("CLOSED", "EXPIRED", "FAILED"):
                    failure_code = "cleanup_failed"
                    await asyncio.to_thread(self.namespaces.delete, item.network)
                    for name in (item.technician_cn, item.gateway_cn):
                        if name in clients:
                            await asyncio.to_thread(self.vpn.kill, name)
                    remaining = await asyncio.to_thread(self.vpn.clients)
                    if item.technician_cn in remaining or item.gateway_cn in remaining:
                        failure_code = "peer_disconnect_pending"
                        raise RuntimeError("VPN peers have not disconnected; retaining reservation")
                    await self.post(item, "cleaned")
                elif item in ready:
                    await asyncio.to_thread(
                        self.namespaces.ensure, item.network, item.lease_seconds
                    )
                    try:
                        await self.post(
                            item,
                            "ready",
                            self.observation(item, clients, "ready", observed_at=observed_at),
                        )
                    except Exception:
                        await asyncio.to_thread(self.namespaces.delete, item.network)
                        raise
                else:
                    await asyncio.to_thread(self.namespaces.delete, item.network)
                    await self.post(
                        item,
                        "waiting",
                        self.observation(item, clients, "waiting", observed_at=observed_at),
                    )
            except Exception as exc:
                cycle_failed = True
                logger.exception("Reconciliation failed for session %s", item.session_id)
                if not isinstance(exc, httpx.HTTPError):
                    failures.append((item, failure_code, clients, observed_at))
                else:
                    self.logs.emit(
                        "worker_api_failed",
                        item.session_id,
                        route=str(parse_peer_name(item.gateway_cn)[1]),
                    )
                # Other independent sessions still receive their lease renewal.
        await self.report_batch(failures)
        self.last_cycle, self.cycle_state = time.monotonic(), "error" if cycle_failed else "ok"


async def main():
    headers = worker_headers()
    if not 1 <= POLL_INTERVAL <= 10:
        raise ValueError("WORKER_POLL_INTERVAL must be between 1 and 10 seconds")
    namespaces = import_module("session-worker.namespace_manager").NamespaceManager()
    firewall = import_module("session-worker.firewall_manager").FirewallManager()
    vpn = ManagementClient(os.getenv("OPENVPN_MANAGEMENT_SOCKET", "/run/openvpn/management.sock"))
    task = asyncio.current_task()
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, task.cancel)
    await asyncio.to_thread(firewall.create_table)
    try:
        async with httpx.AsyncClient(base_url=API_URL, headers=headers, timeout=10) as client:
            worker = SessionWorker(client, namespaces, firewall, vpn)
            logs = asyncio.create_task(
                worker.logs.run(
                    API_URL, headers, "/api/v1/internal/gateways/{route}/technical-logs"
                )
            )
            monitor = asyncio.create_task(
                telemetry_loop(
                    API_URL,
                    headers,
                    "/api/v1/internal/telemetry/server",
                    worker.telemetry_services,
                    metadata=lambda: {"runtime": {**local_runtime(), "openvpn": vpn.version}},
                )
            )
            try:
                while True:
                    try:
                        await worker.reconcile()
                        await cleanup_namespaces()  # Conservative cleanup of legacy eight-character names.
                    except Exception:
                        worker.last_cycle, worker.cycle_state = time.monotonic(), "error"
                        logger.exception("Worker reconciliation failed; kernel leases will expire")
                    await asyncio.sleep(POLL_INTERVAL)
            finally:
                logs.cancel()
                with suppress(asyncio.CancelledError):
                    await logs
                monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor
    finally:
        await asyncio.to_thread(firewall.create_table)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
