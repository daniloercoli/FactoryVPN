"""SMS wakeup or Wi-Fi polling, with authenticated leases and process cleanup."""

import asyncio
from importlib import import_module
import logging
import os
import signal
import sys
import httpx
import time
from contextlib import suppress
from factoryvpn_common.telemetry import telemetry_loop
from factoryvpn_common.technical_logs import LogQueue
from factoryvpn_common.system_info import SystemCollector, system_info_loop

from factoryvpn_common.network import SessionNetwork
from factoryvpn_common.api_errors import temporary_api_error
from factoryvpn_common.protocol import verify_activation

SMSHandler = import_module("gateway-agent.sms_handler").SMSHandler
OpenVPNClient = import_module("gateway-agent.vpn_client").OpenVPNClient
NetworkManager = import_module("gateway-agent.network").NetworkManager
GatewayRegistrar = import_module("gateway-agent.registration").GatewayRegistrar
HeartbeatService = import_module("gateway-agent.heartbeat").HeartbeatService
CertificateRenewer = import_module("gateway-agent.certificate_renewal").CertificateRenewer
enrollment = import_module("gateway-agent.enrollment")
settings = import_module("gateway-agent.config").settings
logger = logging.getLogger(__name__)


class GatewayAgent:
    def __init__(self, modem, vpn, network, heartbeat, config=settings):
        self.modem, self.vpn, self.network, self.heartbeat = modem, vpn, network, heartbeat
        self.config = config
        self.spec = None
        self.activation_token = None
        self.activation_sms_id = None
        self.diagnostic_session_id = None
        self.diagnostic_component = "tunnel"
        self.diagnostic_code = "tunnel_failed"
        self.last_cycle = 0
        self.cycle_state = "unknown"
        self.network_state = self.tunnel_state = "unknown"
        self.logs = LogQueue("agent")

    def telemetry_services(self):
        recent = self.last_cycle and time.monotonic() - self.last_cycle <= max(
            30, self.interval * 2
        )
        control = self.cycle_state if recent else "unknown"
        return {
            "agent": "ok",
            "control": control,
            "openvpn": (
                (
                    "error"
                    if self.tunnel_state == "ok" and not self.vpn.is_running()
                    else self.tunnel_state
                )
                if recent
                else "unknown"
            ),
            "network": self.network_state if recent else "unknown",
        }

    async def report(self, session_id, component, state, code="none"):
        if not session_id:
            return
        try:
            async with asyncio.timeout(2):
                await self.heartbeat.report(session_id, component, state, code)
        except Exception:
            # Telemetry is best effort; never turn reporting failure into authorization.
            logger.warning("Gateway diagnostic report unavailable")

    @property
    def awake(self):
        return self.config.ACTIVATION_MODE == "wifi" or self.activation_token is not None

    @property
    def interval(self):
        if self.config.ACTIVATION_MODE == "wifi" and self.spec is None:
            return self.config.WIFI_CHECK_INTERVAL
        return self.config.POLL_INTERVAL

    async def stop_session(self):
        self.network_state = "unknown"
        try:
            try:
                await asyncio.to_thread(
                    self.network.disable,
                    self.config.VPN_INTERFACE,
                    self.spec,
                )
            except Exception:
                self.network_state = "error"
                if self.diagnostic_session_id:
                    self.logs.emit("cleanup_failed", self.diagnostic_session_id)
                raise
            else:
                self.spec = None
                self.network_state = "idle"
        finally:
            self.tunnel_state = "unknown"
            try:
                await self.vpn.stop()
            except Exception:
                self.tunnel_state = "error"
                if self.diagnostic_session_id:
                    self.logs.emit("cleanup_failed", self.diagnostic_session_id)
                raise
            else:
                self.tunnel_state = "idle"
        self.diagnostic_session_id = None

    async def discard_activation(self):
        if self.activation_sms_id is not None:
            await asyncio.to_thread(self.modem.delete_sms, self.activation_sms_id)
        self.activation_token = None
        self.activation_sms_id = None

    async def handle_failure(self, exc):
        identifier = self.diagnostic_session_id
        component, code = self.diagnostic_component, self.diagnostic_code
        if component == "tunnel" and isinstance(exc, TimeoutError):
            code = "tunnel_timeout"
        if isinstance(exc, httpx.HTTPError):
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            self.logs.emit(
                "api_unavailable", identifier, **({"http_status": status} if status else {})
            )
        elif identifier:
            self.logs.emit(code, identifier)
        elif self.config.ACTIVATION_MODE == "sms":
            self.logs.emit("modem_failed")
        if temporary_api_error(exc) and self.spec is not None and self.vpn.is_running():
            # Conserviamo solo la rete già autorizzata: nessuna riscrittura delle
            # regole. Se l'API non torna, il lease scade autonomamente nel kernel.
            logger.warning("Temporary gateway API failure; existing lease left unchanged")
            return
        try:
            await self.stop_session()
        finally:
            if not isinstance(exc, httpx.HTTPStatusError):
                await self.report(identifier, component, "error", code)
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (
            401,
            403,
            404,
            409,
        ):
            await self.discard_activation()

    async def collect_wakeups(self):
        if self.config.ACTIVATION_MODE != "sms":
            return
        if self.activation_token and not verify_activation(
            self.activation_token, self.config.GATEWAY_SMS_KEY
        ):
            await self.stop_session()
            await self.discard_activation()
        for sms in await asyncio.to_thread(self.modem.list_sms):
            identifier = verify_activation(sms.body, self.config.GATEWAY_SMS_KEY)
            if not identifier:
                await asyncio.to_thread(self.modem.delete_sms, sms.id)
            elif not self.activation_token:
                self.activation_token = sms.body
                self.activation_sms_id = sms.id
                logger.info("Authenticated SMS wakeup for session %s", identifier)
            elif (
                identifier == verify_activation(self.activation_token, self.config.GATEWAY_SMS_KEY)
                and sms.id != self.activation_sms_id
            ):
                await asyncio.to_thread(self.modem.delete_sms, sms.id)
            # Keep valid commands on the modem until the handshake succeeds.
            # An HTTPS failure or a process restart can retry the same command.

    async def step(self):
        await self.collect_wakeups()
        if not self.awake:
            return
        running = self.vpn.is_running()
        if self.spec and self.tunnel_state == "ok" and not running:
            self.logs.emit("tunnel_exited", self.spec.session_id)
        ip = await asyncio.to_thread(self.vpn.get_tunnel_ip) if running else None
        state = await self.heartbeat.send(
            ip, self.spec.session_id if self.spec and ip else None, self.activation_token
        )
        if not state["connect"]:
            await self.stop_session()
            await self.discard_activation()
            return
        spec = SessionNetwork(**state["network"])
        if self.config.ACTIVATION_MODE == "sms" and spec.session_id != verify_activation(
            self.activation_token, self.config.GATEWAY_SMS_KEY
        ):
            await self.stop_session()
            await self.discard_activation()
            raise RuntimeError("Server session differs from signed SMS activation")
        if self.spec and self.spec != spec:
            await self.stop_session()
        self.diagnostic_session_id = spec.session_id
        self.diagnostic_component, self.diagnostic_code = "tunnel", "tunnel_failed"
        self.tunnel_state = "unknown"
        try:
            await self.vpn.start()
            ip = await asyncio.to_thread(self.vpn.get_tunnel_ip)
            if ip != spec.gateway_ip:
                self.diagnostic_code = "address_mismatch"
                raise RuntimeError("OpenVPN did not assign the expected gateway address")
        except Exception:
            self.tunnel_state = "error"
            raise
        self.tunnel_state = "ok"
        await self.report(spec.session_id, "tunnel", "ready")
        requested_at = time.monotonic()
        state = await self.heartbeat.send(
            ip, spec.session_id if self.spec == spec else None, self.activation_token
        )
        if not state["connect"] or SessionNetwork(**state["network"]) != spec:
            await self.stop_session()
            await self.discard_activation()
            return
        already_configured = self.spec == spec
        self.spec = spec
        self.diagnostic_component, self.diagnostic_code = "network", "network_failed"
        self.network_state = "unknown"
        try:
            # Anche una risposta lenta consuma il tempo autorizzato: non aggiunge
            # secondi al lease, come già avviene nel worker del server.
            remaining = int(requested_at + state["lease_seconds"] - time.monotonic())
            if remaining <= 0:
                raise RuntimeError("Authorization lease expired before renewal")
            await asyncio.to_thread(
                self.network.configure_session,
                spec,
                self.config.VPN_INTERFACE,
                remaining,
            )
        except Exception:
            self.network_state = "error"
            raise
        self.network_state = "ok"
        await self.report(spec.session_id, "network", "ready")
        # Nei rinnovi l'heartbeat precedente ha già comunicato la sessione pronta.
        # La conferma dopo la configurazione serve solo alla prima installazione.
        if not already_configured:
            state = await self.heartbeat.send(ip, spec.session_id, self.activation_token)
            if not state["connect"] or SessionNetwork(**state["network"]) != spec:
                await self.stop_session()
                await self.discard_activation()
                return
        if self.activation_sms_id is not None:
            await asyncio.to_thread(self.modem.delete_sms, self.activation_sms_id)
            self.activation_sms_id = None


async def create_modem(config):
    if config.ACTIVATION_MODE == "wifi":
        return None
    return await asyncio.to_thread(SMSHandler, modem_path=config.MODEM_PATH)


async def main():
    # Senza identità di fabbrica il provisioning manuale segue il percorso esistente.
    if await enrollment.bootstrap():
        settings.reload()
    settings.validate()
    modem = await create_modem(settings)
    agent = GatewayAgent(
        modem,
        OpenVPNClient(
            settings.OPENVPN_CONFIG,
            settings.VPN_INTERFACE,
            connect_timeout=settings.CONNECT_TIMEOUT,
        ),
        NetworkManager(),
        HeartbeatService(settings.SERVER_URL, settings.GATEWAY_API_KEY, settings.API_TIMEOUT),
    )
    registrar = GatewayRegistrar(settings.SERVER_URL, settings.GATEWAY_API_KEY)
    renewer = CertificateRenewer(settings)
    task = asyncio.current_task()
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, task.cancel)

    async def register():
        result = await registrar.register(
            settings.GATEWAY_ID,
            settings.MAC_ADDRESS,
            settings.ACTIVATION_MODE,
            settings.GATEWAY_PHONE_NUMBER or None,
        )
        if (
            not result
            or result["id"] != settings.GATEWAY_ID
            or result["activation_mode"] != settings.ACTIVATION_MODE
        ):
            agent.logs.emit("registration_failed")
            raise RuntimeError("Gateway registration failed or returned a different identity/mode")

    registered = False
    monitor = None
    observers = []
    try:
        await agent.stop_session()
        # One startup registration is necessary to make the gateway selectable.
        await register()
        registered = True
        agent.logs.emit("agent_started")
        headers = {"X-Gateway-Key": settings.GATEWAY_API_KEY}
        collector = SystemCollector(
            wifi_interface=os.getenv("GATEWAY_WIFI_INTERFACE", ""),
            hardware_model=os.getenv("GATEWAY_HARDWARE_MODEL", ""),
            hardware_revision=os.getenv("GATEWAY_HARDWARE_REVISION", ""),
            modem=modem,
            can_probe_modem=lambda: not agent.spec and not agent.activation_token,
        )
        observers = [
            asyncio.create_task(
                system_info_loop(
                    settings.SERVER_URL, headers, collector, agent.logs, lambda: agent.awake
                )
            ),
            asyncio.create_task(
                agent.logs.run(
                    settings.SERVER_URL,
                    headers,
                    "/api/v1/gateways/technical-logs",
                    lambda: agent.awake,
                )
            ),
        ]
        monitor = asyncio.create_task(
            telemetry_loop(
                settings.SERVER_URL,
                {"X-Gateway-Key": settings.GATEWAY_API_KEY},
                "/api/v1/gateways/telemetry",
                agent.telemetry_services,
                enabled=lambda: agent.awake,
            )
        )
        while True:
            try:
                await agent.collect_wakeups()
                try:
                    renewing = await renewer.tick(
                        idle=agent.spec is None and not agent.vpn.is_running(), awake=agent.awake
                    )
                except (OSError, ValueError, KeyError):
                    logger.warning("Certificate renewal requires attention; durable state retained")
                    renewing = renewer.activation_pending()
                if renewing and agent.spec is None:
                    await asyncio.sleep(agent.interval)
                    continue
                if agent.awake:
                    if not registered:
                        await register()
                        registered = True
                    await agent.step()
                    agent.last_cycle, agent.cycle_state = time.monotonic(), "ok"
            except Exception as exc:
                agent.last_cycle, agent.cycle_state = time.monotonic(), "error"
                logger.exception("Gateway reconciliation failed")
                await agent.handle_failure(exc)
                # Un guasto temporaneo con rete conservata non richiede una nuova
                # registrazione e mantiene il normale intervallo dei tentativi.
                if agent.spec is None:
                    registered = False
            await asyncio.sleep(agent.interval)
    finally:
        for observer in observers:
            observer.cancel()
        await asyncio.gather(*observers, return_exceptions=True)
        if monitor:
            monitor.cancel()
        try:
            await agent.stop_session()
        finally:
            if modem is not None:
                await asyncio.to_thread(modem.close)
            if monitor:
                with suppress(asyncio.CancelledError):
                    await monitor


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except enrollment.EnrollmentBlocked as exc:
        logger.error("Enrollment stopped: %s", exc)
        sys.exit(78)
