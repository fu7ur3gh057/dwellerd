"""Check registry — turns a parsed config into a list of live check objects."""
from __future__ import annotations

from ..config import Config
from .base import Check, Result, by_threshold
from .docker import DockerCheck
from .host import CpuCheck, DiskCheck, LoadCheck, MemoryCheck, SwapCheck
from .service import HttpCheck, SystemdCheck

__all__ = [
    "Check", "Result", "by_threshold",
    "CpuCheck", "MemoryCheck", "SwapCheck", "LoadCheck", "DiskCheck",
    "HttpCheck", "SystemdCheck", "DockerCheck",
    "build_checks",
]


def _disk_name(path: str) -> str:
    """`/` -> disk-root, `/var/lib` -> disk-var-lib. Stable across restarts
    because it is derived from the path, and the name is the key the
    transition state is stored under."""
    slug = path.strip("/").replace("/", "-") or "root"
    return f"disk-{slug}"


def build_checks(cfg: Config) -> list:
    """Instantiate every check the config asks for. Order is the order they
    appear in the periodic report."""
    c = cfg.checks
    interval = c.interval
    checks: list = []

    if c.cpu.enabled:
        checks.append(CpuCheck("cpu", interval, c.cpu.warn, c.cpu.crit))
    if c.memory.enabled:
        checks.append(MemoryCheck("memory", interval, c.memory.warn, c.memory.crit))
    if c.swap.enabled:
        checks.append(SwapCheck("swap", interval, c.swap.warn, c.swap.crit))
    if c.load.enabled:
        checks.append(LoadCheck("load", interval, c.load.warn, c.load.crit))

    for disk in c.disks:
        checks.append(
            DiskCheck(_disk_name(disk.path), interval, disk.path, disk.warn, disk.crit)
        )

    for http in c.http:
        checks.append(
            HttpCheck(
                f"http-{http.name}", interval, http.url,
                expect_status=http.expect_status, timeout=http.timeout,
                proxy=cfg.telegram.proxy,
            )
        )

    for unit in c.systemd:
        # Strip the suffix for the display name; systemctl accepts either.
        checks.append(SystemdCheck(f"unit-{unit.removesuffix('.service')}", interval, unit))

    if c.docker.enabled:
        checks.append(
            DockerCheck("docker", interval,
                        containers=c.docker.containers, compose=c.docker.compose)
        )

    return checks
