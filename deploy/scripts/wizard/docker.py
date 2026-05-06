"""Detect running `docker compose` projects and let the operator pick which
ones to monitor. For each picked project we walk the compose file's
containers and offer star-marking for the most-important ones.

If `docker compose ls` returns nothing (CLI missing, no projects up yet),
falls back to manual `compose path / containers / starred` entry.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import questionary
from rich.prompt import Prompt

from .i18n import t
from .ui import console, step, warn_line


def detect_compose_projects() -> list[dict]:
    """Returns [{Name, Status, ConfigFiles}, ...] of running compose projects."""
    try:
        result = subprocess.run(
            ["docker", "compose", "ls", "--format", "json"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout) or []
    except json.JSONDecodeError:
        return []


def list_compose_containers(compose_path: str) -> list[dict]:
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", compose_path,
             "ps", "--format", "json", "--all"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    out = result.stdout.strip()
    if not out:
        return []
    if out.startswith("["):
        return json.loads(out)
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def manual_docker_entry() -> list[dict]:
    """Prompt-driven fallback when compose CLI isn't available or no projects
    are running. Loops asking for a path until the operator hits Enter empty.
    """
    blocks: list[dict] = []
    idx = 1
    while True:
        path = Prompt.ask(
            f"  [{idx}] {t('ask_compose_path')}",
            default="",
            show_default=False,
        )
        if not path:
            break
        if not Path(path).is_file():
            warn_line(t("warn_not_found", path=path))
        containers = Prompt.ask(f"      {t('ask_containers')}", default="")
        starred = Prompt.ask(f"      {t('ask_starred')}", default="")
        blocks.append({
            "compose": path,
            "containers": [c.strip() for c in containers.split(",") if c.strip()],
            "starred": [s.strip() for s in starred.split(",") if s.strip()],
        })
        idx += 1
    return blocks


def configure_docker() -> list[dict]:
    """Returns a list of `{compose, containers, starred}` blocks ready to be
    spliced into `report.docker` and `logs.sources` (docker type)."""
    detected = step(t("step_detecting"), detect_compose_projects, delay=0.0)
    if not detected:
        warn_line(t("docker_none_found"))
        return manual_docker_entry()

    project_choices = [
        questionary.Choice(
            title=f"{p.get('Name','?'):<20s}  {p.get('ConfigFiles','').split(',')[0]}",
            value=p,
        )
        for p in detected
    ]
    project_choices.append(questionary.Choice(title=t("docker_custom_path"), value=None))

    console.print(f"  [dim]{t('docker_hint')}[/dim]")
    selected = questionary.checkbox(
        t("docker_pick_projects"), choices=project_choices,
    ).ask() or []

    blocks: list[dict] = []
    for proj in selected:
        if proj is None:
            blocks.extend(manual_docker_entry())
            continue

        path = proj.get("ConfigFiles", "").split(",")[0].strip()
        if not path:
            continue

        containers = list_compose_containers(path)
        if not containers:
            blocks.append({"compose": path, "containers": [], "starred": []})
            continue

        services = [
            {"name": c.get("Service") or c.get("Name", "?"), "status": c.get("Status", "")}
            for c in containers
        ]

        ctn_choices = [
            questionary.Choice(
                title=f"{s['name']:<25s} {s['status']}",
                value=s["name"],
                checked=True,
            )
            for s in services
        ]
        chosen = questionary.checkbox(
            t("docker_pick_containers", project=proj.get("Name", "?")),
            choices=ctn_choices,
        ).ask() or []

        starred: list[str] = []
        if chosen:
            star_choices = [
                questionary.Choice(title=name, value=name, checked=False)
                for name in chosen
            ]
            starred = questionary.checkbox(
                t("docker_pick_starred"), choices=star_choices,
            ).ask() or []

        blocks.append({"compose": path, "containers": chosen, "starred": starred})

    return blocks
