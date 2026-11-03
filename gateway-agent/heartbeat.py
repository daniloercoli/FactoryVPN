"""Authenticated desired-state heartbeat; failures propagate to the agent."""

import httpx
from datetime import datetime, timezone


class HeartbeatService:
    def __init__(self, server_url, api_key):
        if not api_key:
            raise ValueError("GATEWAY_API_KEY must be configured")
        self.url = server_url.rstrip("/") + "/api/v1/gateways/heartbeat"
        self.headers = {"X-Gateway-Key": api_key}

    async def send(self, vpn_ip=None, ready_session_id=None, activation_token=None):
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                self.url,
                headers=self.headers,
                json={
                    "vpn_ip": vpn_ip,
                    "ready_session_id": ready_session_id,
                    "activation_token": activation_token,
                },
            )
            response.raise_for_status()
            return response.json()

    async def report(self, session_id, component, state, code="none"):
        url = self.url.removesuffix("/heartbeat") + f"/sessions/{session_id}/diagnostics"
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.post(
                url,
                headers=self.headers,
                json={
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "component": component,
                    "state": state,
                    "code": code,
                },
            )
            response.raise_for_status()
