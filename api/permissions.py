"""Human capabilities; resource membership is checked separately on every request."""

from enum import StrEnum


class Permission(StrEnum):
    CONNECT = "connect"
    USERS_MANAGE = "users.manage"
    GROUPS_MANAGE = "groups.manage"
    GATEWAYS_MANAGE = "gateways.manage"
    GATEWAYS_PROVISION = "gateways.provision"
    SESSIONS_MANAGE = "sessions.manage"
    AUDIT_READ = "audit.read"
    TELEMETRY_READ = "telemetry.read"
    LOGS_READ = "logs.read"


ALL_PERMISSIONS = [permission.value for permission in Permission]
