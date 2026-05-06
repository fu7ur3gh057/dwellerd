"""Top-level wizard flow: chains all the section helpers together, writes
the boot YAML, persists the runtime settings + admin user to the DB, and
optionally builds + restarts the web client.
"""
from __future__ import annotations

import shutil
import socket
from pathlib import Path

from rich.prompt import Confirm, Prompt

from .bot import configure_bot
from .config_io import write_yaml
from .disks import configure_disks
from .docker import configure_docker
from .i18n import LANG, t
from .logs import configure_logs
from .network import configure_network
from .paths import DEV_CONFIG
from .systemd_units import configure_systemd
from .telegram import gather_telegram
from .ui import ask_seconds, console, section, step
from .web import (
    build_client_bundle,
    configure_web,
    print_web_summary,
    restart_service_if_installed,
)
from .yaml_writer import (
    build_boot_yaml,
    build_runtime_settings,
    db_path_for,
    persist_runtime_to_db,
)


def run_wizard(target: Path | None = None) -> Path:
    """Walk every section, write the YAML, persist DB, return the path
    that was written.
    """
    target = target or DEV_CONFIG

    # ── Telegram (notifier) ──────────────────────────────────────────────
    section(t("section_tg"))
    tg = gather_telegram()
    bot_token, chat_id, proxy_url = (tg if tg else (None, None, ""))

    # ── Bot (interactive companion) ─────────────────────────────────────
    section(t("section_bot"))
    bot_cfg = configure_bot(notifier_token=bot_token)

    # ── Host ────────────────────────────────────────────────────────────
    section(t("section_host"))
    hostname = Prompt.ask(f"  {t('ask_hostname')}", default=socket.gethostname())
    check_int = ask_seconds("ask_check_int", default=60, minimum=30)
    report_int = ask_seconds("ask_report_int", default=2700, minimum=30)
    warn_pct = int(Prompt.ask(f"  {t('ask_warn')}", default="80") or "80")
    crit_pct = int(Prompt.ask(f"  {t('ask_crit')}", default="90") or "90")

    # ── Disks ───────────────────────────────────────────────────────────
    section(t("section_disks"))
    disks = configure_disks()

    # ── Network ─────────────────────────────────────────────────────────
    section(t("section_net"))
    net_cfg = configure_network()

    # ── Docker compose ──────────────────────────────────────────────────
    section(t("section_docker"))
    docker_blocks: list[dict] = []
    if Confirm.ask(f"  {t('ask_docker_yn')}", default=False):
        docker_blocks = configure_docker()

    # ── Systemd units ───────────────────────────────────────────────────
    section(t("section_systemd"))
    systemd_units: list[str] = []
    if Confirm.ask(f"  {t('ask_systemd_yn')}", default=False):
        systemd_units = configure_systemd()

    # ── Logs ────────────────────────────────────────────────────────────
    section(t("section_logs"))
    logs_cfg = configure_logs(
        systemd_units=systemd_units, docker_blocks=docker_blocks,
    )

    # ── Web client ──────────────────────────────────────────────────────
    section(t("section_web"))
    web_cfg = configure_web()

    # ── Render + write ──────────────────────────────────────────────────
    yaml_text = build_boot_yaml(target=target, web_cfg=web_cfg, bot_cfg=bot_cfg)
    runtime_settings = build_runtime_settings(
        bot_token=bot_token, chat_id=chat_id, proxy=proxy_url,
        hostname=hostname,
        check_interval=check_int, report_interval=report_int,
        warn_pct=warn_pct, crit_pct=crit_pct,
        disks=disks, docker_blocks=docker_blocks,
        net_cfg=net_cfg, systemd_units=systemd_units,
        logs_cfg=logs_cfg, web_cfg=web_cfg,
        notifier_lang=LANG,
    )

    section(t("section_writing"))
    if target.exists():
        backup = target.with_suffix(".yaml.bak")
        step(t("step_backup", name=backup.name), lambda: shutil.copy(target, backup))
    step(t("step_write"), lambda: write_yaml(target, yaml_text))

    user_row: dict | None = None
    if web_cfg and (u := web_cfg.get("user")):
        if u.get("username") and u.get("password_hash"):
            user_row = {
                "username": u["username"],
                "password_hash": u["password_hash"],
            }

    db_path = db_path_for(target)
    step(
        t("step_db_persist"),
        lambda: persist_runtime_to_db(db_path, runtime_settings, user_row),
    )

    # ── post-wizard convenience ────────────────────────────────────────
    build_client_bundle(web_cfg)
    if web_cfg and web_cfg.get("enabled"):
        restart_service_if_installed()

    print_web_summary(web_cfg)

    if bot_cfg and bot_cfg.get("enabled"):
        console.print()
        console.print(
            f"[bold green]✓[/bold green] [bold]bot[/bold]: "
            f"admins={bot_cfg.get('admins') or []} · run with [cyan]make run-bot[/cyan]"
        )

    return target
