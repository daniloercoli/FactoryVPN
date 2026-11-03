"""OpenVPN child process ownership, readiness and bounded shutdown."""

import asyncio
import json
import logging
import re
import subprocess

logger = logging.getLogger(__name__)


class OpenVPNClient:
    def __init__(
        self, config_path="/etc/openvpn/client.conf", interface="tun0", connect_timeout=30
    ):
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", interface):
            raise ValueError("Invalid VPN interface")
        self.config_path = config_path
        self.interface = interface
        self.connect_timeout = connect_timeout
        self.process = None
        self._readers = []
        self._ready = asyncio.Event()
        self._lock = asyncio.Lock()

    async def _read_output(self, stream):
        while line := await stream.readline():
            text = line.decode(errors="replace").rstrip()
            if "Initialization Sequence Completed" in text:
                self._ready.set()
            elif "process restarting" in text:
                # OpenVPN can reconnect without exiting, retaining tun0 and
                # its address. A previous handshake no longer proves readiness.
                self._ready.clear()
            logger.debug("OpenVPN: %s", text)

    async def start(self):
        async with self._lock:
            if self.is_running() and self._ready.is_set():
                return
            await self._stop()
            self._ready.clear()
            self.process = await asyncio.create_subprocess_exec(
                "openvpn",
                "--config",
                self.config_path,
                "--dev",
                self.interface,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._readers = [
                asyncio.create_task(self._read_output(stream))
                for stream in (self.process.stdout, self.process.stderr)
            ]
            ready = asyncio.create_task(self._ready.wait())
            exited = asyncio.create_task(self.process.wait())
            try:
                done, _ = await asyncio.wait(
                    [ready, exited],
                    timeout=self.connect_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if exited in done:
                    raise RuntimeError("OpenVPN exited before the tunnel was ready")
                if ready not in done:
                    raise TimeoutError("OpenVPN connection timed out")
            except BaseException:
                await self._stop()
                raise
            finally:
                for task in (ready, exited):
                    task.cancel()
                await asyncio.gather(ready, exited, return_exceptions=True)

    async def _stop(self):
        process = self.process
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await asyncio.wait_for(process.wait(), timeout=5)
        for task in self._readers:
            task.cancel()
        await asyncio.gather(*self._readers, return_exceptions=True)
        self._readers.clear()
        self.process = None
        self._ready.clear()

    async def stop(self):
        async with self._lock:
            await self._stop()

    def is_running(self):
        return self.process is not None and self.process.returncode is None

    def get_tunnel_ip(self):
        result = subprocess.run(
            ["ip", "-j", "addr", "show", "dev", self.interface],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode:
            return None
        for interface in json.loads(result.stdout):
            for address in interface.get("addr_info", []):
                if address.get("family") == "inet":
                    return address["local"]
        return None
