"""
Gateway Agent Configuration
"""

import os
from uuid import UUID
from api.schemas.gateway import GatewayRegisterRequest


class Config:
    GATEWAY_ID = os.getenv("GATEWAY_ID", "")
    ACTIVATION_MODE = os.getenv("GATEWAY_ACTIVATION_MODE", "sms").lower()
    WIFI_CHECK_INTERVAL = int(os.getenv("GATEWAY_WIFI_CHECK_INTERVAL", "30"))
    VPN_INTERFACE = os.getenv("VPN_INTERFACE", "tun0")
    POLL_INTERVAL = int(os.getenv("GATEWAY_POLL_INTERVAL", "5"))
    MODEM_PATH = os.getenv("MODEM_PATH", "/dev/ttyUSB0")
    OPENVPN_CONFIG = os.getenv("OPENVPN_CONFIG", "/etc/factoryvpn/gateway.ovpn")
    SERVER_URL = os.getenv("SERVER_URL", "https://vpn.example.com")
    GATEWAY_PHONE_NUMBER = os.getenv("GATEWAY_PHONE_NUMBER", "")
    GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY", "")
    GATEWAY_SMS_KEY = os.getenv("GATEWAY_SMS_KEY", "")
    MAC_ADDRESS = os.getenv("MAC_ADDRESS", "")
    LAN_INTERFACE = os.getenv("LAN_INTERFACE", "eth0")
    LAN_ADDRESS = os.getenv("LAN_ADDRESS", "192.168.1.1")
    LAN_CIDR = os.getenv("LAN_CIDR", "192.168.1.0/24")

    def validate(self):
        GatewayRegisterRequest(
            gateway_id=UUID(self.GATEWAY_ID),
            activation_mode=self.ACTIVATION_MODE,
            phone_number=self.GATEWAY_PHONE_NUMBER or None,
            mac_address=self.MAC_ADDRESS or None,
            lan_interface=self.LAN_INTERFACE,
            lan_address=self.LAN_ADDRESS,
            lan_cidr=self.LAN_CIDR,
        )
        if len(self.GATEWAY_API_KEY) < 32:
            raise ValueError("GATEWAY_API_KEY must contain at least 32 characters")
        if self.ACTIVATION_MODE == "sms" and len(self.GATEWAY_SMS_KEY) < 32:
            raise ValueError("GATEWAY_SMS_KEY must contain at least 32 characters")
        if not 1 <= self.POLL_INTERVAL <= 10:
            raise ValueError("GATEWAY_POLL_INTERVAL must be between 1 and 10 seconds")
        if not 5 <= self.WIFI_CHECK_INTERVAL <= 300:
            raise ValueError("GATEWAY_WIFI_CHECK_INTERVAL must be between 5 and 300 seconds")
        if self.ACTIVATION_MODE == "sms" and not self.MODEM_PATH:
            raise ValueError("SMS activation requires MODEM_PATH")


settings = Config()
