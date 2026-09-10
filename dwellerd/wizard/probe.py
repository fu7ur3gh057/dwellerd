"""Host discovery used to pre-fill the wizard's answers.

Everything here is best-effort and never raises: on a box without docker,
without systemd, or with an unreadable /proc the wizard must still finish.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import psutil

# Pseudo-filesystems that are always "100% full" or always empty, and would
# only ever produce noise if offered as disks to watch.
_SKIP_FS = {
    "tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs", "devfs",
    "autofs", "cgroup", "cgroup2", "ramfs", "fuse.snapfuse", "iso9660",
}
_SKIP_PREFIX = (
    "/snap", "/sys", "/proc", "/dev", "/run", "/var/lib/docker",
    # macOS system volumes, so a dev run on a Mac offers `/` and not
    # nine read-only firmware partitions.
    "/System", "/Library/Developer",
)


def _cmd(argv: list[str], timeout: int = 10) -> str:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def mount_points() -> list[str]:
    """Real, writable mount points worth watching, root first."""
    points: list[str] = []
    try:
        partitions = psutil.disk_partitions(all=False)
    except Exception:
        return ["/"]
    for part in partitions:
        if part.fstype in _SKIP_FS:
            continue
        if any(part.mountpoint.startswith(p) for p in _SKIP_PREFIX):
            continue
        if part.mountpoint not in points:
            points.append(part.mountpoint)
    if "/" not in points:
        points.insert(0, "/")
    points.sort(key=lambda p: (p != "/", p))
    return points


def disk_space(path: str) -> dict[str, float] | None:
    """Capacity figures for a mount point, in GiB. Best effort."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    gib = 1024 ** 3
    percent = usage.used / usage.total * 100 if usage.total else 0.0
    return {
        "total": usage.total / gib,
        "used": usage.used / gib,
        "free": usage.free / gib,
        "percent": percent,
    }


def has_docker() -> bool:
    return shutil.which("docker") is not None


def containers() -> list[str]:
    """Names of every container docker knows about, running or not."""
    out = _cmd(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=15)
    names: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = (item.get("Names") or item.get("Name") or "").split(",")[0].strip()
        if name and name not in names:
            names.append(name)
    return sorted(names)


def has_systemd() -> bool:
    return shutil.which("systemctl") is not None


def systemd_units(limit: int = 40) -> list[str]:
    """Enabled, currently-running units, minus the ones nobody wants an
    alert about (user sessions, timers, mounts, the box's own scaffolding)."""
    out = _cmd([
        "systemctl", "list-units", "--type=service", "--state=running",
        "--no-legend", "--no-pager", "--plain",
    ], timeout=15)
    skip = ("systemd-", "user@", "session-", "dbus", "getty", "polkit",
            "snapd", "cloud-init", "unattended", "dwellerd")
    units: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        unit = parts[0]
        if not unit.endswith(".service"):
            continue
        if any(unit.startswith(prefix) for prefix in skip):
            continue
        units.append(unit)
    return sorted(units)[:limit]


def log_files() -> list[str]:
    """Common log files that actually exist and are readable."""
    candidates = [
        "/var/log/syslog", "/var/log/messages",
        "/var/log/nginx/error.log", "/var/log/apache2/error.log",
        "/var/log/auth.log", "/var/log/secure",
        "/var/log/postgresql/postgresql.log",
    ]
    found = []
    for path in candidates:
        try:
            with open(path, "rb"):
                found.append(path)
        except OSError:
            continue
    return found
