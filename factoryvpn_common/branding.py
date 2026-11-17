"""The public display label is independent of stable technical identifiers."""

import re

DEFAULT_APP_NAME = "FactoryVPN"


def normalize_app_name(value: str) -> str:
    if re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", value):
        raise ValueError("APP_NAME must be a single line without control characters")
    value = value.strip()
    if not 1 <= len(value) <= 80:
        raise ValueError("APP_NAME must contain 1 to 80 characters")
    return value
