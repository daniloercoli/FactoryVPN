"""Selected local facts; never execute commands received from the server."""

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from importlib.metadata import version, PackageNotFoundError
import os
from pathlib import Path
import platform
import re
import subprocess
import time
import tomllib
from uuid import uuid4
import httpx
from factoryvpn_common.observability import Component, SystemReport


def project_version():
    manifest = Path(__file__).resolve().parents[1] / "pyproject.toml"
    try:
        return tomllib.loads(manifest.read_text())["project"]["version"]
    except (OSError, KeyError, ValueError):
        try:
            return version("factoryvpn")
        except PackageNotFoundError:
            return None


def component(**values):
    values = {key: value for key, value in values.items() if value is not None}
    try:
        return Component(state="reported", **values)
    except ValueError:
        return Component(state="collection_failed")


class SystemCollector:
    def __init__(
        self,
        *,
        wifi_interface="",
        modem=None,
        can_probe_modem=lambda: True,
        hardware_model="",
        hardware_revision="",
        sys_root=Path("/sys"),
    ):
        self.wifi_interface, self.modem = wifi_interface, modem
        self.can_probe_modem = can_probe_modem
        self.hardware_model, self.hardware_revision = hardware_model, hardware_revision
        self.sys_root = Path(sys_root)
        self._task = None
        self.modem_cache = None
        self.modem_cached_at = 0.0

    def collect(self):
        components = {}
        components["agent"] = component(
            version=project_version(),
            build=os.getenv("FACTORYVPN_BUILD_ID", "unknown"),
            origin="build",
        )
        try:
            result = subprocess.run(
                ["openvpn", "--version"], capture_output=True, text=True, timeout=1
            )
            match = re.match(r"OpenVPN ([0-9][\w.+-]*)", result.stdout)
            components["openvpn"] = (
                component(version=match.group(1)) if match else Component(state="collection_failed")
            )
        except FileNotFoundError:
            components["openvpn"] = Component(state="unsupported")
        except (OSError, subprocess.SubprocessError):
            components["openvpn"] = Component(state="collection_failed")
        try:
            release = platform.freedesktop_os_release()
            components["system"] = component(
                model=release.get("ID"),
                version=release.get("VERSION_ID"),
                kernel=platform.release(),
                architecture=platform.machine(),
            )
        except OSError:
            components["system"] = Component(state="unsupported")
        components["hardware"] = (
            component(
                model=self.hardware_model,
                revision=self.hardware_revision or None,
                origin="configured",
            )
            if self.hardware_model
            else Component(state="unsupported")
        )
        if not self.wifi_interface:
            components["wifi"] = Component(state="unsupported")
        elif not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", self.wifi_interface):
            components["wifi"] = Component(state="collection_failed")
        else:
            interface = self.sys_root / "class/net" / self.wifi_interface
            try:
                components["wifi"] = (
                    component(
                        interface=self.wifi_interface,
                        mac_address=(interface / "address").read_text().strip().upper(),
                    )
                    if (interface / "wireless").is_dir()
                    else Component(state="unsupported")
                )
            except OSError:
                components["wifi"] = Component(state="collection_failed")
        if self.modem is None:
            components["modem"] = Component(state="not_applicable")
        else:
            if (
                self.modem_cache is None or time.monotonic() - self.modem_cached_at >= 3600
            ) and self.can_probe_modem():
                try:
                    inventory = component(**self.modem.inventory(can_probe=self.can_probe_modem))
                    if inventory.state == "reported":
                        self.modem_cache = inventory
                        self.modem_cached_at = time.monotonic()
                except Exception:
                    components["modem"] = Component(state="collection_failed")
            components.setdefault("modem", self.modem_cache or Component(state="collection_failed"))
        return SystemReport(
            report_id=uuid4(), observed_at=datetime.now(timezone.utc), components=components
        )

    async def collect_async(self):
        if self._task is not None:
            if not self._task.done():
                raise TimeoutError("Previous component probe is still running")
            with suppress(Exception):
                self._task.result()
            self._task = None
        self._task = asyncio.create_task(asyncio.to_thread(self.collect))
        self._task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        result = await asyncio.shield(self._task)
        self._task = None
        return result


async def system_info_loop(base_url, headers, collector, queue, enabled=lambda: True):
    first, previously_enabled, next_probe = True, False, 0
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"), headers=headers, timeout=2
    ) as client:
        while True:
            active = enabled()
            if first or (active and (not previously_enabled or time.monotonic() >= next_probe)):
                initial = first
                first = False
                next_probe = time.monotonic() + 3600
                try:
                    # Probe timestamp precedes I/O, preventing delayed observations looking fresh.
                    observed = datetime.now(timezone.utc)
                    async with asyncio.timeout(3):
                        report = await collector.collect_async()
                    report.observed_at = observed
                    if any(c.state == "collection_failed" for c in report.components.values()):
                        queue.emit("collection_failed")
                    # Initial registration permits one initial report; subsequent reports require awake.
                    if initial or enabled():
                        async with asyncio.timeout(2):
                            response = await client.post(
                                "/api/v1/gateways/system-info", json=report.model_dump(mode="json")
                            )
                            response.raise_for_status()
                except Exception:
                    # No exception strings and no recursive telemetry logging.
                    pass
            previously_enabled = active
            await asyncio.sleep(5)
