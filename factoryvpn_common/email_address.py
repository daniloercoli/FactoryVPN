"""Email address validation without application configuration or SMTP imports."""

import re


def email_address(value):
    value = (value or "").strip().lower()
    if len(value) > 255 or not re.fullmatch(
        r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value
    ):
        raise ValueError("Invalid email address")
    local, domain = value.split("@")
    if (
        local.startswith(".")
        or local.endswith(".")
        or ".." in value
        or any(label.startswith("-") or label.endswith("-") for label in domain.split("."))
    ):
        raise ValueError("Invalid email address")
    return value
