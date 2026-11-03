"""Small, non-secret runtime identity from the process that actually runs it."""

from functools import lru_cache
import os
import platform
import re
from pydantic import BaseModel, ConfigDict
from factoryvpn_common.observability import Label
from factoryvpn_common.system_info import project_version


class RuntimeInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Label | None = None
    build: Label | None = None
    python: Label | None = None
    openvpn: Label | None = None


def safe_label(value):
    return value if value and re.fullmatch(r"[\w .()+,:@-]{1,128}", value) else None


@lru_cache(maxsize=1)
def local_runtime():
    return RuntimeInfo(
        version=safe_label(project_version()),
        build=safe_label(os.getenv("FACTORYVPN_BUILD_ID")),
        python=platform.python_version(),
    ).model_dump()
