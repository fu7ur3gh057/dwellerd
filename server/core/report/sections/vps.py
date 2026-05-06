import asyncio
import os
import shutil
import time

import psutil

from .base import SectionResult

_GB = 1024 ** 3

_LABELS = {
    "en": {
        "title": "📊 VPS (host)",
        "cpu": "CPU", "ram": "RAM", "swap": "SWAP",
        "disk": "Disk", "net": "Net", "load": "Load avg", "uptime": "Uptime",
    },
    "ru": {
        "title": "📊 VPS (хост)",
        "cpu": "CPU", "ram": "RAM", "swap": "SWAP",
        "disk": "Диск", "net": "Сеть", "load": "Load avg", "uptime": "Uptime",
    },
}


def _bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(width, int(round(pct / 100 * width))))
    return "█" * filled + "░" * (width - filled)


def _uptime() -> str:
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
    except OSError:
        return ""
    days, rem = divmod(int(secs), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


class VpsSection:
    def __init__(
        self,
        lang: str = "en",
        disks: list[str] | None = None,
        interfaces: list[str] | None = None,
        warn_pct: float = 80.0,
    ) -> None:
        self.lang = lang
        self.disks = disks or ["/"]
        self.interfaces = interfaces
        self.warn_pct = float(warn_pct)
        self._prev_net: tuple[float, int, int] | None = None

    def _net_counters(self) -> tuple[int, int]:
        if not self.interfaces:
            c = psutil.net_io_counters()
            return c.bytes_sent, c.bytes_recv
        per = psutil.net_io_counters(pernic=True)
        tx = sum(per[i].bytes_sent for i in self.interfaces if i in per)
        rx = sum(per[i].bytes_recv for i in self.interfaces if i in per)
        return tx, rx

    async def render(self) -> SectionResult:
        L = _LABELS.get(self.lang, _LABELS["en"])
        warnings: list[str] = []

        cpu_pct = await asyncio.to_thread(psutil.cpu_percent, None)
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()

        sent, recv = self._net_counters()
        now = time.monotonic()
        rx_mbps = tx_mbps = None
        if self._prev_net is not None:
            dt = now - self._prev_net[0]
            if dt > 0:
                tx_mbps = (sent - self._prev_net[1]) * 8 / dt / 1_000_000
                rx_mbps = (recv - self._prev_net[2]) * 8 / dt / 1_000_000
        self._prev_net = (now, sent, recv)

        load1, load5, load15 = os.getloadavg()
        up = _uptime()

        lines = [L["title"]]
        lines.append(f"• {L['cpu']}: {cpu_pct:.0f}% {_bar(cpu_pct)}")
        used_gb = (mem.total - mem.available) / _GB
        lines.append(
            f"• {L['ram']}: {used_gb:.1f}/{mem.total/_GB:.1f} GB ({mem.percent:.0f}%)"
        )
        if swap.total > 0:
            lines.append(
                f"• {L['swap']}: {swap.used/_GB:.1f}/{swap.total/_GB:.1f} GB "
                f"({swap.percent:.0f}%)"
            )
            if swap.percent >= 50:
                warnings.append(f"swap {swap.percent:.0f}%")

        for path in self.disks:
            try:
                u = shutil.disk_usage(path)
            except OSError:
                continue
            pct = u.used / u.total * 100
            lines.append(
                f"• {L['disk']} {path}: {u.used/_GB:.0f}/{u.total/_GB:.0f} GB "
                f"({pct:.0f}%)"
            )
            if pct >= self.warn_pct:
                warnings.append(f"disk {path} {pct:.0f}%")

        if rx_mbps is not None:
            lines.append(f"• {L['net']}: ↓{rx_mbps:.1f} Mbps  ↑{tx_mbps:.1f} Mbps")

        lines.append(f"• {L['load']}: {load1:.2f}, {load5:.2f}, {load15:.2f}")
        if up:
            lines.append(f"• {L['uptime']}: {up}")

        if mem.percent >= self.warn_pct:
            warnings.append(f"RAM {mem.percent:.0f}%")
        if cpu_pct >= self.warn_pct:
            warnings.append(f"CPU {cpu_pct:.0f}%")

        return SectionResult(text="\n".join(lines), warnings=warnings)
