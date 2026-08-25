"""Report sections — each renders one block of the periodic digest and
reports any warnings it noticed, which the builder turns into the alert
summary and the recommendations at the bottom.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass, field

import psutil

from ..i18n import fmt_duration, normalize_lang, uptime

_GB = 1024 ** 3


@dataclass
class SectionResult:
    text: str
    warnings: list[str] = field(default_factory=list)


_LABELS = {
    "en": {
        "host": "📊 Host",
        "cpu": "CPU", "ram": "RAM", "swap": "SWAP", "disk": "Disk",
        "net": "Net", "load": "Load", "uptime": "Uptime",
        "docker": "🐳 Containers", "total": "total",
        "logs": "📜 Errors", "log_lines": "lines", "log_kinds": "distinct",
        "none": "none",
        "checks": "🩺 Checks", "all_ok": "all ok",
    },
    "ru": {
        "host": "📊 Хост",
        "cpu": "CPU", "ram": "RAM", "swap": "SWAP", "disk": "Диск",
        "net": "Сеть", "load": "Load", "uptime": "Аптайм",
        "docker": "🐳 Контейнеры", "total": "всего",
        "logs": "📜 Ошибки", "log_lines": "строк", "log_kinds": "видов",
        "none": "нет",
        "checks": "🩺 Проверки", "all_ok": "всё в норме",
    },
}


def labels(lang: str) -> dict:
    return _LABELS.get(normalize_lang(lang), _LABELS["en"])


def bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(width, int(round(pct / 100 * width))))
    return "█" * filled + "░" * (width - filled)


# ── host ─────────────────────────────────────────────────────────────────


class HostSection:
    """CPU / RAM / swap / disks / network / load / uptime."""

    def __init__(
        self, lang: str = "en", disks: list[str] | None = None,
        interfaces: list[str] | None = None, warn_pct: float = 80.0,
    ) -> None:
        self.lang = normalize_lang(lang)
        self.disks = disks or ["/"]
        self.interfaces = interfaces or []
        self.warn_pct = float(warn_pct)
        # Network is a counter, not a gauge: we can only report throughput
        # as the delta between two reports.
        self._prev_net: tuple[float, int, int] | None = None

    def _net_counters(self) -> tuple[int, int]:
        if not self.interfaces:
            counters = psutil.net_io_counters()
            return counters.bytes_sent, counters.bytes_recv
        per_nic = psutil.net_io_counters(pernic=True)
        sent = sum(per_nic[i].bytes_sent for i in self.interfaces if i in per_nic)
        recv = sum(per_nic[i].bytes_recv for i in self.interfaces if i in per_nic)
        return sent, recv

    async def render(self) -> SectionResult:
        L = labels(self.lang)
        warnings: list[str] = []

        cpu_pct = await asyncio.to_thread(psutil.cpu_percent, None)
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()

        lines = [L["host"]]
        lines.append(f"• {L['cpu']}: {cpu_pct:.0f}% {bar(cpu_pct)}")
        used_gb = (mem.total - mem.available) / _GB
        lines.append(
            f"• {L['ram']}: {used_gb:.1f}/{mem.total / _GB:.1f} GB "
            f"({mem.percent:.0f}%) {bar(mem.percent)}"
        )
        if swap.total > 0:
            lines.append(
                f"• {L['swap']}: {swap.used / _GB:.1f}/{swap.total / _GB:.1f} GB "
                f"({swap.percent:.0f}%)"
            )
            if swap.percent >= 50:
                warnings.append(f"swap {swap.percent:.0f}%")

        for path in self.disks:
            try:
                usage = shutil.disk_usage(path)
            except OSError:
                continue
            pct = usage.used / usage.total * 100
            lines.append(
                f"• {L['disk']} {path}: {usage.used / _GB:.0f}/{usage.total / _GB:.0f} GB "
                f"({pct:.0f}%) {bar(pct)}"
            )
            if pct >= self.warn_pct:
                warnings.append(f"disk {path} {pct:.0f}%")

        sent, recv = self._net_counters()
        now = time.monotonic()
        if self._prev_net is not None:
            elapsed = now - self._prev_net[0]
            if elapsed > 0:
                tx = (sent - self._prev_net[1]) * 8 / elapsed / 1_000_000
                rx = (recv - self._prev_net[2]) * 8 / elapsed / 1_000_000
                lines.append(f"• {L['net']}: ↓{rx:.1f} Mbps  ↑{tx:.1f} Mbps")
        self._prev_net = (now, sent, recv)

        try:
            load1, load5, load15 = os.getloadavg()
            cores = os.cpu_count() or 1
            lines.append(
                f"• {L['load']}: {load1:.2f}, {load5:.2f}, {load15:.2f} "
                f"({load1 / cores:.2f}/core)"
            )
        except OSError:
            pass

        up = uptime()
        if up:
            lines.append(f"• {L['uptime']}: {fmt_duration(up, self.lang)}")

        if mem.percent >= self.warn_pct:
            warnings.append(f"RAM {mem.percent:.0f}%")
        if cpu_pct >= self.warn_pct:
            warnings.append(f"CPU {cpu_pct:.0f}%")

        return SectionResult(text="\n".join(lines), warnings=warnings)


# ── docker ───────────────────────────────────────────────────────────────


_STATE_ICON = {"running": "✅", "restarting": "🟡", "paused": "🟡",
               "created": "🟡", "starting": "🟡"}


class DockerSection:
    """One line per container with CPU and memory, when docker is enabled."""

    def __init__(self, lang: str = "en", containers: list[str] | None = None) -> None:
        self.lang = normalize_lang(lang)
        self.containers = list(containers or [])

    async def render(self) -> SectionResult:
        from ..checks.docker import _classify, _parse_json_lines, _run

        L = labels(self.lang)
        warnings: list[str] = []

        code, out, err = await _run("docker", "ps", "-a", "--format", "{{json .}}")
        if code != 0:
            return SectionResult(
                text=f"{L['docker']}\n{err[:120] or 'docker unavailable'}",
                warnings=[f"docker: {err[:80] or 'unavailable'}"],
            )

        rows = []
        for item in _parse_json_lines(out):
            name = (item.get("Names") or item.get("Name") or "").split(",")[0].strip()
            if not name:
                continue
            if self.containers and name not in self.containers:
                continue
            level, state = _classify(item.get("State", ""), item.get("Status", ""))
            rows.append((name, level, state))

        missing = [c for c in self.containers if c not in {r[0] for r in rows}]

        if not rows and not missing:
            return SectionResult(text=f"{L['docker']}\n{L['none']}")

        stats = await self._stats([r[0] for r in rows])

        lines = [f"{L['docker']} ({L['total']}: {len(rows) + len(missing)})"]
        for name, level, state in sorted(rows):
            icon = _STATE_ICON.get(state, "❌")
            parts = [f"{icon} {name} | {state}"]
            stat = stats.get(name, {})
            if stat.get("cpu"):
                parts.append(f"cpu {stat['cpu']}%")
            if stat.get("mem"):
                parts.append(f"mem {stat['mem']}")
            lines.append(" | ".join(parts))
            if level != "ok":
                warnings.append(f"container {name} {state}")
        for name in sorted(missing):
            lines.append(f"❌ {name} | not running")
            warnings.append(f"container {name} not running")

        return SectionResult(text="\n".join(lines), warnings=warnings)

    @staticmethod
    async def _stats(names: list[str]) -> dict[str, dict]:
        """Per-container CPU/mem. Best effort: `docker stats` is slow and
        can hang on a sick daemon, so a failure just drops the columns."""
        from ..checks.docker import _run

        if not names:
            return {}
        code, out, _ = await _run(
            "docker", "stats", "--no-stream", "--format", "{{json .}}", *names,
            timeout=15,
        )
        if code != 0:
            return {}
        stats: dict[str, dict] = {}
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            stats[item.get("Name", "")] = {
                "cpu": item.get("CPUPerc", "").rstrip("%"),
                "mem": item.get("MemUsage", "").split("/")[0].strip(),
            }
        return stats


# ── logs ─────────────────────────────────────────────────────────────────


class LogsSection:
    """How many error lines, of how many distinct kinds, since the last
    report — plus the three loudest."""

    def __init__(self, storage, since: float, lang: str = "en") -> None:
        self.storage = storage
        self.since = since
        self.lang = normalize_lang(lang)

    async def render(self) -> SectionResult:
        L = labels(self.lang)
        total = await asyncio.to_thread(self.storage.log_count_since, self.since)
        if not total:
            return SectionResult(text=f"{L['logs']}\n{L['none']}")
        top = await asyncio.to_thread(self.storage.log_summary_since, self.since, 3)
        lines = [
            f"{L['logs']}: {total} {L['log_lines']}, {len(top)}+ {L['log_kinds']}"
        ]
        for item in top:
            sample = item["sample"].strip().replace("\n", " ")
            if len(sample) > 90:
                sample = sample[:89] + "…"
            lines.append(f"• {item['source']} ×{item['count']}: {sample}")
        return SectionResult(text="\n".join(lines))


# ── checks ───────────────────────────────────────────────────────────────


class ChecksSection:
    """Anything currently not ok. A clean board prints one line."""

    def __init__(self, storage, lang: str = "en") -> None:
        self.storage = storage
        self.lang = normalize_lang(lang)

    async def render(self) -> SectionResult:
        L = labels(self.lang)
        states = await asyncio.to_thread(self.storage.all_states)
        bad = [s for s in states if s["level"] != "ok"]
        if not bad:
            return SectionResult(text=f"{L['checks']}: {L['all_ok']} ({len(states)})")
        now = time.time()
        lines = [f"{L['checks']}: {len(bad)}/{len(states)}"]
        warnings = []
        for state in bad:
            icon = "🔴" if state["level"] == "crit" else "🟡"
            since = fmt_duration(now - state["since"], self.lang)
            lines.append(f"{icon} {state['name']} — {state['last_detail']} ({since})")
            warnings.append(f"{state['name']} {state['level']}")
        return SectionResult(text="\n".join(lines), warnings=warnings)
