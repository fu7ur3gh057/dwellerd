"""Systemd unit install / uninstall.

Default: `User=dwellerd` (the dedicated system user created in
`_bootstrap.sh`). `--as-root` falls back to running as root, which is
required on control-panel hosts (FastPanel/ISPmanager) where per-site
ACLs under `/var/www/<panel-user>/` block any non-owner.

Hardening flags ProtectSystem/PrivateTmp/ProtectHome are deliberately
omitted: they create a private mount namespace which would hide
bind-mounted `/var/www/<user>/` paths the daemon may need to reach.
`ReadWritePaths=/var/lib/dwellerd` + `NoNewPrivileges=yes` cover the
practical attack surface without breaking control-panel deployments.
"""
from __future__ import annotations

import getpass
import grp
import os
import pwd
import shutil
import subprocess
import time
from pathlib import Path

from rich.prompt import Confirm

from .i18n import t
from .paths import (
    DWELLERD_ETC, DWELLERD_HOME, DWELLERD_USER, DEV_CONFIG,
    EXAMPLE_CONFIG, PROD_CONFIG, PROJECT_ROOT, SERVICE_NAME, UNIT_PATH,
)
from .ui import console, fail, step, warn_line


# ── small helpers ──────────────────────────────────────────────────────────


def _ensure_sudo() -> bool:
    """Prime sudo creds before any spinner-y output. Avoids a sudo password
    prompt landing under a Live spinner where it can't be read."""
    if subprocess.run(
        ["sudo", "-n", "true"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0:
        return True
    console.print(f"  [dim italic]{t('sudo_hint')}[/dim italic]")
    return subprocess.run(["sudo", "-v"]).returncode == 0


def _user_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def _resolve_invoking_user() -> tuple[str, str]:
    """For dev-mode chown after `make run` switches back from systemd —
    SUDO_USER wins over getpass so `sudo make ...` flows still chown to
    the human, not to root."""
    user = os.environ.get("SUDO_USER") or getpass.getuser()
    try:
        gid = pwd.getpwnam(user).pw_gid
        group = grp.getgrgid(gid).gr_name
    except KeyError:
        group = user
    return user, group


# ── systemd unit body ──────────────────────────────────────────────────────


def build_unit(*, venv_py: Path, as_root: bool) -> str:
    """Render the unit file. Default: User=dwellerd + ReadWritePaths +
    NoNewPrivileges. --as-root keeps the unit as root (no User=, no
    ReadWritePaths since root needs neither)."""
    if as_root:
        user_lines = ""
        rwpaths = ""
    else:
        user_lines = f"User={DWELLERD_USER}\nGroup={DWELLERD_USER}\n"
        rwpaths = f"ReadWritePaths={DWELLERD_HOME}\nNoNewPrivileges=yes\n"

    return f"""[Unit]
Description=Dwellerd monitoring daemon
Documentation=https://github.com/fu7ur3gh057/Dwellerd
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
{user_lines}WorkingDirectory={PROJECT_ROOT}
Environment=PYTHONPATH={PROJECT_ROOT}/server
ExecStart={venv_py} -m main {PROD_CONFIG}
Restart=on-failure
RestartSec=5s
{rwpaths}
[Install]
WantedBy=multi-user.target
"""


# ── install ────────────────────────────────────────────────────────────────


def install_systemd(*, as_root: bool = False) -> int:
    venv_py = PROJECT_ROOT / ".venv" / "bin" / "python"
    if not venv_py.exists():
        fail(t("venv_missing"))
        return 1

    from .ui import section
    section(t("section_service"))

    if as_root:
        console.print(f"  {t('unit_user_root')}")
    else:
        if not _user_exists(DWELLERD_USER):
            fail(t("user_missing", user=DWELLERD_USER))
            return 1
        console.print(f"  {t('unit_user_normal')}")
        console.print(f"  [dim]{t('user_lines')}[/dim]")
    console.print(f"  [dim]project:[/dim] [cyan]{PROJECT_ROOT}[/cyan]")
    console.print(f"  [dim]unit:[/dim]    [cyan]{UNIT_PATH}[/cyan]")
    console.print(f"  [dim]config:[/dim]  [cyan]{PROD_CONFIG}[/cyan]")
    console.print(f"  [dim italic]{t('control_panel_warn')}[/dim italic]")

    if not Confirm.ask(f"\n[bold]{t('confirm_install', unit=UNIT_PATH)}[/bold]", default=True):
        return 0

    if not _ensure_sudo():
        fail(t("sudo_failed"))
        return 1

    # 1) Copy ./config.yaml → /etc/dwellerd/config.yaml as root:dwellerd 640.
    def write_prod_config():
        if PROD_CONFIG.exists():
            return  # don't clobber an operator's edits
        src = DEV_CONFIG if DEV_CONFIG.exists() else EXAMPLE_CONFIG
        if not src.exists():
            raise FileNotFoundError(
                f"no source config (looked for {DEV_CONFIG} and {EXAMPLE_CONFIG})"
            )
        # `install` does mkdir -p + chown + chmod in one shot — group
        # `dwellerd` so the daemon can read; mode 640 keeps tokens off
        # world-readable.
        subprocess.run(
            ["sudo", "install", "-D",
             "-o", "root",
             "-g", DWELLERD_USER if not as_root else "root",
             "-m", "640",
             str(src), str(PROD_CONFIG)],
            check=True,
        )

    step(t("step_config", dst=str(PROD_CONFIG)), write_prod_config)

    # 2) Make sure /var/lib/dwellerd/{data,logs} exist and are owned by the
    #    dwellerd user so the daemon's first start can write to them. The
    #    bootstrap script set up /var/lib/dwellerd itself; data/logs may
    #    not exist yet on a fresh install. (Skipped in --as-root mode —
    #    root can write anywhere.)
    if not as_root:
        def ensure_state_dirs():
            for sub in ("data", "logs"):
                p = DWELLERD_HOME / sub
                subprocess.run(
                    ["sudo", "install", "-d", "-o", DWELLERD_USER, "-g", DWELLERD_USER,
                     "-m", "750", str(p)],
                    check=True,
                )
        step("preparing /var/lib/dwellerd/{data,logs}", ensure_state_dirs)

    # 3) Write the unit file via `sudo tee`.
    unit = build_unit(venv_py=venv_py, as_root=as_root)
    def write_unit():
        subprocess.run(
            ["sudo", "tee", str(UNIT_PATH)],
            input=unit, text=True, check=True, stdout=subprocess.DEVNULL,
        )
    step(t("step_unit"), write_unit)

    # 4) reload + enable + a quick liveness probe.
    step(t("step_reload"), lambda: subprocess.run(
        ["sudo", "systemctl", "daemon-reload"], check=True,
    ))
    step(t("step_enable"), lambda: subprocess.run(
        ["sudo", "systemctl", "enable", "--now", SERVICE_NAME], check=True,
    ))

    time.sleep(1)
    rc = subprocess.run(
        ["systemctl", "is-active", "--quiet", SERVICE_NAME],
    ).returncode
    if rc != 0:
        warn_line(
            f"unit started but is-active != 0; check `journalctl -u {SERVICE_NAME} -n 50`"
        )

    console.print(f"\n[bold green]✓[/bold green] [bold]{t('service_done')}[/bold]")
    console.print(f"  [dim italic]{t('logs_hint')}[/dim italic]")
    console.print(f"  [dim italic]{t('status_hint')}[/dim italic]")
    return 0


# ── uninstall ──────────────────────────────────────────────────────────────


def uninstall_systemd(*, purge: bool = False) -> int:
    from .ui import section
    section(t("section_uninstall"))

    if not UNIT_PATH.exists() and not purge:
        warn_line(t("no_service", path=str(UNIT_PATH)))
        return 0

    if UNIT_PATH.exists():
        if not Confirm.ask(f"[bold]{t('confirm_uninstall')}[/bold]", default=True):
            return 0

    if not _ensure_sudo():
        fail(t("sudo_failed"))
        return 1

    if UNIT_PATH.exists():
        step(t("step_disable"), lambda: subprocess.run(
            ["sudo", "systemctl", "disable", "--now", SERVICE_NAME],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
        step(t("step_remove_unit"), lambda: subprocess.run(
            ["sudo", "rm", "-f", str(UNIT_PATH)], check=True,
        ))
        step(t("step_reload"), lambda: subprocess.run(
            ["sudo", "systemctl", "daemon-reload"], check=True,
        ))

    if purge:
        if not Confirm.ask(
            f"[bold red]{t('confirm_purge', user=DWELLERD_USER, home=DWELLERD_HOME, etc=DWELLERD_ETC)}[/bold red]",
            default=False,
        ):
            console.print(f"  [dim]{t('purge_skipped')}[/dim]")
        else:
            if _user_exists(DWELLERD_USER):
                step(t("step_purge_user"), lambda: subprocess.run(
                    ["sudo", "userdel", DWELLERD_USER],
                    check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                ))
            for p in (DWELLERD_HOME, DWELLERD_ETC):
                if p.exists():
                    step(t("step_purge_home", path=str(p)), lambda p=p: subprocess.run(
                        ["sudo", "rm", "-rf", str(p)], check=True,
                    ))

    console.print(f"\n[bold green]✓[/bold green] [bold]{t('service_removed')}[/bold]")
    return 0


# ── dev mode (foreground) ──────────────────────────────────────────────────


def run_dev() -> int:
    """`make run` from a normal shell — runs the daemon in foreground using
    the project's venv."""
    venv_py = PROJECT_ROOT / ".venv" / "bin" / "python"
    if not venv_py.exists():
        fail(t("venv_missing"))
        return 1
    if not DEV_CONFIG.exists():
        fail(t("no_config"))
        return 1
    console.print(f"\n[bold green]→[/bold green] [bold]{t('starting_dev')}[/bold]\n")
    os.chdir(PROJECT_ROOT)
    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "server")}
    return subprocess.call([str(venv_py), "-m", "main", str(DEV_CONFIG)], env=env)
