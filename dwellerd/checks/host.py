"""Host resource checks: cpu, memory, swap, load average, disk."""
from __future__ import annotations

import asyncio
import os
import shutil

import psutil

from .base import Result, by_threshold


class CpuCheck:
    kind = "cpu"

    def __init__(self, name: str, interval: float, warn: float, crit: float) -> None:
        self.name, self.interval = name, interval
        self.warn, self.crit = float(warn), float(crit)

    async def run(self) -> Result:
        # psutil.cpu_percent(None) is non-blocking and reports usage since
        # the previous call — which is exactly one check interval.
        pct = await asyncio.to_thread(psutil.cpu_percent, None)
        return by_threshold(
            pct=pct, warn=self.warn, crit=self.crit, kind="cpu", label="CPU",
        )


class MemoryCheck:
    kind = "memory"

    def __init__(self, name: str, interval: float, warn: float, crit: float) -> None:
        self.name, self.interval = name, interval
        self.warn, self.crit = float(warn), float(crit)

    async def run(self) -> Result:
        mem = psutil.virtual_memory()
        return by_threshold(
            pct=mem.percent, warn=self.warn, crit=self.crit,
            kind="memory", label="RAM",
            extra={"used_gb": (mem.total - mem.available) / 1024 ** 3,
                   "total_gb": mem.total / 1024 ** 3},
        )


class SwapCheck:
    kind = "swap"

    def __init__(self, name: str, interval: float, warn: float, crit: float) -> None:
        self.name, self.interval = name, interval
        self.warn, self.crit = float(warn), float(crit)

    async def run(self) -> Result:
        swap = psutil.swap_memory()
        if swap.total == 0:
            # No swap configured is a normal, deliberate state on many VPS
            # images — reporting 0% forever would be noise, not signal.
            return Result(level="ok", kind="swap",
                          metrics={"value": 0.0, "threshold": self.warn},
                          detail="no swap configured")
        return by_threshold(
            pct=swap.percent, warn=self.warn, crit=self.crit,
            kind="swap", label="SWAP",
            extra={"used_gb": swap.used / 1024 ** 3,
                   "total_gb": swap.total / 1024 ** 3},
        )


class LoadCheck:
    """Load average, normalised per core so the thresholds mean the same
    thing on a 1-core VPS and a 32-core box."""
    kind = "load"

    def __init__(self, name: str, interval: float, warn: float, crit: float) -> None:
        self.name, self.interval = name, interval
        self.warn, self.crit = float(warn), float(crit)
        self.cores = os.cpu_count() or 1

    async def run(self) -> Result:
        load1, load5, load15 = os.getloadavg()
        per_core = load1 / self.cores
        extra = {"load1": load1, "load5": load5, "load15": load15,
                 "cores": self.cores, "per_core": per_core}
        if per_core >= self.crit:
            level, threshold = "crit", self.crit
        elif per_core >= self.warn:
            level, threshold = "warn", self.warn
        else:
            level, threshold = "ok", self.warn
        return Result(
            level=level, kind="load",
            metrics={**extra, "value": per_core, "threshold": threshold},
            detail=f"load {load1:.2f} ({per_core:.2f}/core, {self.cores} cores)",
        )


class DiskCheck:
    kind = "disk"

    def __init__(
        self, name: str, interval: float, path: str, warn: float, crit: float,
    ) -> None:
        self.name, self.interval = name, interval
        self.path = path
        self.warn, self.crit = float(warn), float(crit)

    async def run(self) -> Result:
        try:
            usage = shutil.disk_usage(self.path)
        except OSError as e:
            return Result(
                level="crit", kind="disk",
                metrics={"path": self.path, "value": 0.0,
                         "threshold": self.warn, "free_gb": 0.0},
                detail=f"disk {self.path}: {e}",
            )
        pct = usage.used / usage.total * 100
        return by_threshold(
            pct=pct, warn=self.warn, crit=self.crit,
            kind="disk", label=f"disk {self.path}",
            extra={"path": self.path,
                   "free_gb": usage.free / 1024 ** 3,
                   "total_gb": usage.total / 1024 ** 3},
        )
