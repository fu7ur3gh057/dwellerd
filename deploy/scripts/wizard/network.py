"""Detect network interfaces via psutil and offer a multi-select.

Returns one of:
  - True   → all interfaces (sum of bytes_sent / bytes_recv)
  - False  → operator deselected everything (skip net section)
  - dict   → {"interfaces": [...]} (limit to picked ones)
"""
from __future__ import annotations

import socket as _socket

import psutil
import questionary

from .i18n import t
from .ui import console, step, warn_line


_NET_SKIP_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "tun", "tap")


def detect_network_interfaces() -> list[dict]:
    out: list[dict] = []
    try:
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
    except Exception:
        return []
    for name, st in stats.items():
        if not st.isup:
            continue
        if name.startswith(_NET_SKIP_PREFIXES):
            continue
        ipv4 = next(
            (a.address for a in addrs.get(name, []) if a.family == _socket.AF_INET),
            "",
        )
        out.append({"name": name, "speed": st.speed, "ipv4": ipv4})
    return out


def configure_network() -> dict | bool:
    detected = step(t("step_detect_net"), detect_network_interfaces, delay=0.0)
    if not detected:
        warn_line(t("net_none"))
        return True

    choices = [
        questionary.Choice(
            title=(f"{d['name']:<10s}  {d['ipv4'] or '(no ipv4)':<16s}  {d['speed']} Mbps"
                   if d["speed"]
                   else f"{d['name']:<10s}  {d['ipv4'] or '(no ipv4)'}"),
            value=d["name"],
            checked=True,
        )
        for d in detected
    ]
    console.print(f"  [dim]{t('docker_hint')}[/dim]")
    selected = questionary.checkbox(t("net_pick"), choices=choices).ask() or []
    if not selected:
        return False
    if len(selected) == len(detected):
        return True
    return {"interfaces": selected}
