"""Dwellerd setup wizard — split into one-purpose-per-file modules.

Public surface (everything `setup.py` imports):

    run_wizard          — full interactive flow that writes the YAML + DB
    install_systemd     — write systemd unit + enable + start
    uninstall_systemd   — disable + remove unit (--purge also drops user/dirs)
    run_dev             — foreground daemon (`make run` from a shell)
    main_menu           — top-level menu
    pick_language       — RU/EN selector
    set_lang / detect_lang
    banner / banner_minimal / console — UI primitives reused by setup.py

Internal modules — read top-down to understand the wizard:

    paths.py            — constants (PROJECT_ROOT, paths, service name)
    i18n.py             — RU/EN translation table + LANG state
    ui.py               — Rich console, banner, section, step spinner
    menu.py             — main_menu (returns 'dev'|'service'|'uninstall'|'edit'|'exit')
    config_io.py        — read existing config.yaml / write fresh YAML
    telegram.py         — bot token + chat id + SOCKS5 proxy
    disks.py            — psutil-based disk detection + multi-select
    network.py          — psutil-based network interface detection + multi-select
    systemd_units.py    — systemctl list + multi-select
    docker.py           — docker compose ls + per-project container picker
    logs.py             — journal/file/docker source candidates + journal-group bootstrap
    web.py              — port + admin user (bcrypt) + terminal + client bundle build
    yaml_writer.py      — render boot YAML + persist runtime settings/admin to SQLite
    service.py          — systemd install/uninstall + dev mode runner
    wizard.py           — chains everything together → run_wizard()
"""
from __future__ import annotations

from .i18n import detect_lang, pick_language, set_lang
from .menu import main_menu
from .service import install_systemd, run_dev, uninstall_systemd
from .ui import banner, banner_minimal, console
from .wizard import run_wizard

__all__ = [
    "banner",
    "banner_minimal",
    "console",
    "detect_lang",
    "install_systemd",
    "main_menu",
    "pick_language",
    "run_dev",
    "run_wizard",
    "set_lang",
    "uninstall_systemd",
]
