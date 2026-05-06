"""Probe systemctl for running .service units and offer them as alert
candidates. The wizard turns each pick into a `systemd_unit` check.
"""
from __future__ import annotations

import subprocess

import questionary

from .i18n import t
from .ui import console, step, warn_line


# Generic noise / always-on bits the operator usually wouldn't want to
# alert on (their failure has its own dedicated handling). ssh.service /
# sshd.service stay — losing them on a remote box matters.
_SYSTEMD_SKIP_PREFIXES = ("systemd-", "user@", "session-")
_SYSTEMD_SKIP_EXACT = {
    "dbus.service", "dbus-broker.service", "polkit.service",
    "wpa_supplicant.service", "ModemManager.service", "NetworkManager.service",
    "rsyslog.service", "cron.service", "snapd.service", "snap.service",
    "accounts-daemon.service", "udisks2.service", "upower.service",
    "avahi-daemon.service", "thermald.service", "irqbalance.service",
    "cups.service", "cups-browsed.service", "bluetooth.service",
}


def detect_systemd_units() -> list[dict]:
    try:
        proc = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--state=running",
             "--no-legend", "--no-pager", "--plain"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        name = parts[0]
        if not name.endswith(".service"):
            continue
        if name.startswith(_SYSTEMD_SKIP_PREFIXES) or name in _SYSTEMD_SKIP_EXACT:
            continue
        desc = parts[4] if len(parts) > 4 else ""
        out.append({"name": name, "desc": desc})
    return out


def configure_systemd() -> list[str]:
    """Returns the picked unit names ([] if operator chose nothing)."""
    detected = step(t("step_detect_systemd"), detect_systemd_units, delay=0.0)
    if not detected:
        warn_line(t("systemd_none"))
        return []
    choices = [
        questionary.Choice(
            title=f"{u['name']:<30s}  {u['desc']}",
            value=u["name"],
            checked=False,
        )
        for u in detected
    ]
    console.print(f"  [dim]{t('docker_hint')}[/dim]")
    return questionary.checkbox(t("systemd_pick"), choices=choices).ask() or []
