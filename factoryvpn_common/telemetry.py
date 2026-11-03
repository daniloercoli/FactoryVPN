"""Linux resource sampling and bounded best-effort reporting, independent of VPN leases."""

import asyncio
import logging
import os
from contextlib import suppress
from pathlib import Path
from datetime import datetime, timezone
import httpx

logger = logging.getLogger(__name__)


class ResourceSampler:
    def __init__(self, proc=Path("/proc"), disk_path=None):
        self.proc = Path(proc)
        self.disk_path = disk_path or os.getenv("TELEMETRY_DISK_PATH", "/")
        self.previous_cpu = None
        self._sample_task = None

    async def sample_async(self):
        # Cancelling to_thread cannot stop its syscall. Keep at most one probe
        # in flight so repeated timeouts cannot consume the VPN executor pool.
        if self._sample_task is not None:
            if not self._sample_task.done():
                raise TimeoutError("Previous system probe is still running")
            with suppress(Exception):
                self._sample_task.result()
            self._sample_task = None
            # Discard a timed-out observation and its CPU baseline.
            self.previous_cpu = None
        self._sample_task = asyncio.create_task(asyncio.to_thread(self.sample))
        self._sample_task.add_done_callback(
            lambda task: None if task.cancelled() else task.exception()
        )
        result = await asyncio.shield(self._sample_task)
        self._sample_task = None
        return result

    def sample(self):
        result = dict(
            cpu_percent=None,
            memory_total_bytes=None,
            memory_available_bytes=None,
            disk_total_bytes=None,
            disk_available_bytes=None,
            uptime_seconds=None,
            scope="linux_system",
            disk_scope="collector_filesystem",
        )
        try:
            # guest/guest_nice are already included in user/nice; do not count twice.
            values = [int(v) for v in (self.proc / "stat").read_text().splitlines()[0].split()[1:9]]
            if len(values) != 8 or any(v < 0 for v in values):
                raise ValueError("Invalid CPU counters")
            current = (sum(values), values[3] + values[4])
            if self.previous_cpu:
                total, idle = (a - b for a, b in zip(current, self.previous_cpu))
                if total > 0 and 0 <= idle <= total:
                    result["cpu_percent"] = round(100 * (total - idle) / total, 2)
            self.previous_cpu = current
        except (OSError, ValueError, IndexError):
            self.previous_cpu = None
        try:
            memory = {
                p[0].rstrip(":"): int(p[1]) * 1024
                for line in (self.proc / "meminfo").read_text().splitlines()
                if (p := line.split()) and p[0] in ("MemTotal:", "MemAvailable:")
            }
            total, available = memory["MemTotal"], memory["MemAvailable"]
            if 0 <= available <= total and total > 0:
                result.update(memory_total_bytes=total, memory_available_bytes=available)
        except (OSError, ValueError, KeyError, IndexError):
            pass
        try:
            disk = os.statvfs(self.disk_path)
            total, available = disk.f_blocks * disk.f_frsize, disk.f_bavail * disk.f_frsize
            if 0 <= available <= total and total > 0:
                result.update(disk_total_bytes=total, disk_available_bytes=available)
        except (OSError, ValueError):
            pass
        try:
            uptime = int(float((self.proc / "uptime").read_text().split()[0]))
            if uptime >= 0:
                result["uptime_seconds"] = uptime
        except (OSError, ValueError, OverflowError, IndexError):
            pass
        return result


async def report_once(client, path, sampler, services, metadata=None):
    # Timestamp before collection: a delayed probe must not appear newly observed.
    at = datetime.now(timezone.utc).isoformat()
    metrics = await sampler.sample_async()
    response = await client.post(
        path,
        json={
            "observed_at": at,
            "metrics": metrics,
            "services": services(),
            **(metadata() if metadata else {}),
        },
    )
    response.raise_for_status()


async def telemetry_loop(base_url, headers, path, services, enabled=lambda: True, metadata=None):
    sampler = ResourceSampler()
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"), headers=headers, timeout=2
    ) as client:
        while True:
            if enabled():
                try:
                    async with asyncio.timeout(3):
                        await report_once(client, path, sampler, services, metadata)
                except Exception:
                    logger.warning("System telemetry unavailable")
            else:
                # A new activation must not average CPU across hours of SMS standby.
                sampler.previous_cpu = None
            await asyncio.sleep(30)
