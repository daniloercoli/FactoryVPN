"""Set the fixed session sysctls inside ip-netns-exec's private mount namespace.

Docker mounts /proc/sys read-only even with NET_ADMIN/SYS_ADMIN. Remounting it
here affects only this short-lived mount namespace, never the worker or host.
"""

import os
import subprocess
import sys

ALLOWED = {
    "net.ipv4.ip_forward": {"0", "1"},
    "net.ipv4.conf.all.rp_filter": {"0"},
    "net.ipv4.conf.default.rp_filter": {"0"},
}


def namespace_id(process, kind):
    return os.stat(f"/proc/{process}/ns/{kind}").st_ino


def configure(assignments):
    if not assignments or any(
        value not in ALLOWED.get(key, set())
        for key, _, value in (item.partition("=") for item in assignments)
    ):
        raise ValueError("Only the fixed FactoryVPN session network sysctls are allowed")
    for kind in ("mnt", "net"):
        if namespace_id("self", kind) == namespace_id("1", kind):
            raise RuntimeError("Requires private mount and network namespaces from ip netns exec")
    if os.statvfs("/proc/sys").f_flag & os.ST_RDONLY:
        subprocess.run(["mount", "-o", "remount,rw", "/proc/sys"], check=True, timeout=5)
    subprocess.run(["sysctl", "-w", *assignments], check=True, timeout=5)


if __name__ == "__main__":
    configure(sys.argv[1:])
