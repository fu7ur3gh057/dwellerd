"""The setup wizard — the only interactive surface this project has.

It asks a dozen questions, pre-filling every answer from what it can see on
the host (mount points, running units, docker containers, readable log
files), verifies the Telegram credentials by actually sending a message,
and writes one annotated YAML file.
"""
from __future__ import annotations

import asyncio
import socket
from pathlib import Path

from ..notify import TelegramNotifier
from . import probe, writer
from .i18n import lang, set_lang, t
from .ui import (
    ask, ask_float, ask_int, banner, choose_many, confirm, console, fail,
    info, ok, section, step, warn,
)

__all__ = ["run_wizard"]

_LOG_LEVELS = {"1": "error", "2": "warn", "3": "info", "4": "all"}


def _choose_many(prompt: str, options: list[str], **kwargs) -> list[str]:
    """Localized checkbox picker used by every wizard section."""
    return choose_many(prompt, options, hint=t("multi_hint"), **kwargs)


def run_wizard(target: Path) -> Path | None:
    """Walk every section and write the config. Returns the path written,
    or None if the operator backed out."""
    _pick_language()
    banner(t("subtitle"))

    if target.exists() and not confirm(t("ask_overwrite", path=target), default=True):
        console.print(f"\n  [yellow]{t('aborted')}[/yellow]\n")
        return None

    answers: dict = {"lang": lang()}
    answers.update(_telegram())
    answers.update(_host())
    answers.update(_disks())
    answers.update(_services())
    answers.update(_docker())
    answers.update(_logs(answers))
    answers.update(_report())
    answers.pop("_containers", None)   # inter-section scratch, not config

    section(t("sec_write"))
    backup = writer.write(target, writer.render(answers))
    if backup is not None:
        info(f"{t('backed_up')} {backup}")
    ok(f"{t('written')} {target}")

    section(t("next_steps"))
    info(t("next_dev"))
    info(t("next_install"))
    console.print(f"\n  [bold green]{t('done')}[/bold green]\n")
    return target


# ── sections ─────────────────────────────────────────────────────────────


def _pick_language() -> None:
    banner()
    choice = ask(t("ask_lang"), default="1")
    set_lang("ru" if choice.strip() in ("2", "ru", "рус") else "en")


def _telegram() -> dict:
    section(t("sec_tg"))
    info(t("tg_hint"))
    while True:
        token = ask(t("ask_token"))
        chat_id = ask(t("ask_chat"))
        proxy = ask(t("ask_proxy"), default="")
        if not (token and chat_id):
            warn(t("tg_skip"))
            return {"bot_token": "", "chat_id": "", "proxy": ""}
        if _test_telegram(token, chat_id, proxy):
            return {"bot_token": token, "chat_id": chat_id, "proxy": proxy}
        if not confirm(t("tg_retry"), default=True):
            warn(t("tg_skip"))
            return {"bot_token": token, "chat_id": chat_id, "proxy": proxy}


def _test_telegram(token: str, chat_id: str, proxy: str) -> bool:
    """Send a real message. Catching a bad token here saves the operator
    from discovering it at 4am when the first alert silently fails."""
    notifier = TelegramNotifier(token, chat_id, lang=lang(), proxy=proxy)

    async def send() -> bool:
        import httpx
        payload = {
            "chat_id": chat_id,
            "text": "🟢 <b>Dwellerd</b>\n\nSetup test message.",
            "parse_mode": "HTML",
        }
        kwargs: dict = {"timeout": 15}
        if proxy:
            kwargs["proxy"] = proxy
        try:
            async with httpx.AsyncClient(**kwargs) as client:
                response = await client.post(notifier.url, json=payload)
        except Exception as e:
            fail(f"{t('tg_fail')}: {type(e).__name__}: {e}")
            return False
        if response.status_code == 200:
            return True
        detail = ""
        try:
            detail = response.json().get("description", "")
        except Exception:
            detail = response.text[:200]
        fail(f"{t('tg_fail')}: {response.status_code} {detail}")
        return False

    try:
        delivered = step(t("tg_testing"), lambda: asyncio.run(send()))
    except Exception:
        return False
    if delivered:
        ok(t("tg_ok"))
    return bool(delivered)


def _host() -> dict:
    section(t("sec_host"))
    hostname = ask(t("ask_hostname"), default=socket.gethostname())
    interval = ask_int(t("ask_interval"), default=60, minimum=10)
    warn_pct = ask_float(t("ask_warn"), default=80.0, minimum=1.0)
    crit_pct = ask_float(t("ask_crit"), default=90.0, minimum=warn_pct)
    return {
        "hostname": hostname,
        "interval": interval,
        "warn": int(warn_pct),
        "crit": int(crit_pct),
        # Swap and load want their own scales: 80% swap is a crisis, and
        # load is a ratio, not a percentage.
        "swap_warn": 50, "swap_crit": 80,
        "load_warn": 2, "load_crit": 4,
    }


def _disks() -> dict:
    section(t("sec_disks"))
    points = probe.mount_points()
    console.print(f"  [dim]{t('disks_found')}[/dim]")
    labels = []
    for path in points:
        space = probe.disk_space(path)
        labels.append(t("disk_item", path=path, **space) if space else path)
    chosen = _choose_many(
        t("ask_disks"), points, default_all=True, display_options=labels,
    )
    return {"disks": chosen or ["/"]}


def _services() -> dict:
    section(t("sec_services"))
    units: list[str] = []
    if probe.has_systemd() and confirm(t("ask_systemd_yn"), default=True):
        available = probe.systemd_units()
        if available:
            units = _choose_many(t("ask_systemd"), available)
        else:
            warn("no running units found")

    probes: list[dict] = []
    if confirm(t("ask_http_yn"), default=False):
        while True:
            url = ask(t("ask_http_url"), default="")
            if not url:
                break
            if not url.startswith(("http://", "https://")):
                url = f"https://{url}"
            probes.append({
                "name": url.split("//", 1)[-1].split("/", 1)[0],
                "url": url,
                "expect_status": ask_int(t("ask_http_status"), default=200, minimum=100),
            })
    return {"systemd": units, "http": probes}


def _docker() -> dict:
    section(t("sec_docker"))
    if not probe.has_docker():
        info(t("docker_missing"))
        return {"docker_enabled": False, "docker_containers": [], "_containers": []}
    found = probe.containers()
    if not confirm(t("ask_docker_yn"), default=bool(found)):
        return {"docker_enabled": False, "docker_containers": [], "_containers": found}
    chosen: list[str] = []
    if found:
        console.print(f"  [dim]{t('containers_found')}[/dim]")
        chosen = _choose_many(t("ask_containers"), found)
    return {"docker_enabled": True, "docker_containers": chosen, "_containers": found}


def _logs(answers: dict) -> dict:
    section(t("sec_logs"))
    if not confirm(t("ask_logs_yn"), default=True):
        return {
            "logs_enabled": False, "log_level": "error", "log_sources": [],
            "digest_interval": 3600, "retention_days": 14,
        }

    level = _LOG_LEVELS.get(ask(t("ask_log_level"), default="1"), "error")
    sources: list[dict] = []

    containers = answers.get("_containers") or []
    if containers:
        chosen = _choose_many(t("ask_log_containers"), containers)
        for name in chosen:
            sources.append({
                "type": "docker_container", "name": name,
                "container": name, "pattern": ".+",
            })

    found_files = probe.log_files()
    if found_files:
        for path in _choose_many(t("ask_log_files"), found_files):
            sources.append({
                "type": "file",
                "name": Path(path).stem or path,
                "path": path, "pattern": ".+",
            })

    while True:
        path = ask(t("ask_log_files"), default="")
        if not path:
            break
        pattern = ask(t("ask_log_pattern"), default=".+") or ".+"
        sources.append({
            "type": "file", "name": Path(path).stem or path,
            "path": path, "pattern": pattern,
        })

    return {
        "logs_enabled": True,
        "log_level": level,
        "log_sources": sources,
        "digest_interval": ask_int(t("ask_digest"), default=3600, minimum=60),
        "retention_days": ask_int(t("ask_retention"), default=14, minimum=1),
    }


def _report() -> dict:
    section(t("sec_report"))
    if not confirm(t("ask_report_yn"), default=True):
        return {"report_enabled": False, "report_interval": 10800}
    return {
        "report_enabled": True,
        "report_interval": ask_int(t("ask_report_int"), default=10800, minimum=300),
    }
