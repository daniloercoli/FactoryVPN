"""
Gateway Registration Service
"""

import httpx
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class GatewayRegistrar:
    def __init__(self, server_url: str, api_key: str):
        if not api_key:
            raise ValueError("GATEWAY_API_KEY must be configured")
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key

    async def register(
        self,
        gateway_id: str,
        mac_address: str,
        activation_mode: str = "sms",
        phone_number: str | None = None,
    ) -> Optional[dict]:
        """Register gateway with central server"""

        payload = {
            "gateway_id": gateway_id,
            "activation_mode": activation_mode,
            "phone_number": phone_number,
            "mac_address": mac_address or None,
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.server_url}/api/v1/gateways/register",
                    json=payload,
                    headers={"X-Gateway-Key": self.api_key},
                )

                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"Gateway registered: {data}")
                    return data
                else:
                    logger.error(f"Registration failed: {response.status_code}")
                    return None

        except Exception as e:
            logger.error(f"Registration error: {e}")
            return None
