"""Detect mounted disks via psutil and offer the operator a multi-select.

Skips loop/pseudo filesystems and snap-related mountpoints — those don't
need monitoring (and would clutter the picker)."""
from __future__ import annotations

import shutil

import psutil
import questionary
from rich.prompt import Prompt

from .i18n import t
from .ui import console, step


_FS_SKIP = {"squashfs", "iso9660", "tmpfs", "devtmpfs", "overlay"}
_MOUNT_SKIP_PREFIXES = ("/snap/", "/boot/efi", "/var/snap/", "/run/")


def detect_disks() -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for p in psutil.disk_partitions(all=False):
        if p.fstype in _FS_SKIP or not p.fstype:
            continue
        if any(p.mountpoint.startswith(pref) for pref in _MOUNT_SKIP_PREFIXES):
            continue
        if p.mountpoint in seen:
            continue
        try:
            usage = shutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        seen.add(p.mountpoint)
        out.append({
            "mountpoint": p.mountpoint,
            "fstype": p.fstype,
            "total_gb": usage.total / 1024 ** 3,
            "used_pct": (usage.used / usage.total * 100) if usage.total else 0.0,
        })
    return out


def configure_disks() -> list[str]:
    detected = step(t("step_detect_disks"), detect_disks, delay=0.0)

    if not detected:
        # No detection succeeded — fall back to manual entry, keep `/`.
        disks_input = Prompt.ask(t("ask_disk_paths"), default="/")
        return [d.strip() for d in disks_input.split(",") if d.strip()] or ["/"]

    choices = [
        questionary.Choice(
            title=f"{d['mountpoint']:<20s} {d['total_gb']:>6.1f} GB  "
                  f"{d['used_pct']:>5.1f}% used  ({d['fstype']})",
            value=d["mountpoint"],
            checked=(d["mountpoint"] == "/"),
        )
        for d in detected
    ]
    choices.append(questionary.Choice(title=t("disks_custom"), value=None))

    console.print(f"  [dim]{t('docker_hint')}[/dim]")
    selected = questionary.checkbox(t("disks_pick"), choices=choices).ask() or []

    paths = [s for s in selected if s is not None]
    if None in selected:
        custom = Prompt.ask(t("disks_custom_input"), default="")
        for d in custom.split(","):
            d = d.strip()
            if d:
                paths.append(d)
    return paths or ["/"]
