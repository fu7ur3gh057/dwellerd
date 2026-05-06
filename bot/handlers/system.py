"""/status — host snapshot · /uptime — host + daemon uptime."""
from __future__ import annotations

import platform
import socket
import time

import psutil
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..format import code, esc, fmt_pct, level_emoji, pct_bar, size_h, time_ago
from ..queries import daemon_uptime, list_checks, stat_counts


router = Router(name=__name__)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    load1, load5, load15 = psutil.getloadavg()

    states = list_checks()
    crit = sum(1 for s in states if s["level"] == "crit")
    warn = sum(1 for s in states if s["level"] == "warn")
    ok = sum(1 for s in states if s["level"] == "ok")

    counts = stat_counts()

    text = (
        f"📊 <b>{esc(socket.gethostname())}</b>\n"
        f"<code>{pct_bar(cpu)}</code>  CPU {fmt_pct(cpu)}\n"
        f"<code>{pct_bar(mem.percent)}</code>  RAM {fmt_pct(mem.percent)}  "
        f"({size_h(mem.used)} / {size_h(mem.total)})\n"
        f"<code>{pct_bar(disk.percent)}</code>  Disk / {fmt_pct(disk.percent)}  "
        f"({size_h(disk.used)} / {size_h(disk.total)})\n"
        f"\n"
        f"load avg: <code>{load1:.2f} {load5:.2f} {load15:.2f}</code>\n"
        f"checks: 🟢 {ok} · 🟡 {warn} · 🔴 {crit}\n"
        f"за 24ч: {counts['alerts_24h']} alerts · {counts['log_events_24h']} log events"
    )
    await message.answer(text)


@router.message(Command("uptime"))
async def cmd_uptime(message: Message) -> None:
    boot, daemon_start = daemon_uptime()
    now = time.time()

    parts = [f"🦆  <b>Dwellerd</b>"]
    parts.append(f"host: <code>{esc(socket.gethostname())}</code>  ({esc(platform.system())} {esc(platform.release())})")
    if boot:
        delta = now - boot
        days = int(delta // 86400)
        hours = int((delta % 86400) // 3600)
        parts.append(f"host uptime: <b>{days}d {hours}h</b>  (с {time_ago(boot)})")
    if daemon_start:
        delta = now - daemon_start
        if delta < 60:
            human = f"{int(delta)}s"
        elif delta < 3600:
            human = f"{int(delta / 60)}m"
        elif delta < 86400:
            human = f"{int(delta / 3600)}h {int((delta % 3600) / 60)}m"
        else:
            human = f"{int(delta / 86400)}d {int((delta % 86400) / 3600)}h"
        parts.append(f"daemon running: <b>~{human}</b>  ({time_ago(daemon_start)})")
    else:
        parts.append("daemon: <i>no recent check results — is it running?</i>")

    await message.answer("\n".join(parts))
