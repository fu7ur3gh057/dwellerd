"""/docker — list compose projects + container statuses (read-only)."""
from __future__ import annotations

import json
import subprocess

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..format import esc, time_ago


router = Router(name=__name__)


def _detect() -> list[dict]:
    """Re-implements wizard.docker.detect_compose_projects but for the bot
    runtime. Cheap shell-out — cached implicitly by docker daemon's
    project ledger.
    """
    try:
        r = subprocess.run(
            ["docker", "compose", "ls", "--format", "json"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout) or []
    except json.JSONDecodeError:
        return []


def _ps(compose_path: str) -> list[dict]:
    try:
        r = subprocess.run(
            ["docker", "compose", "-f", compose_path,
             "ps", "--format", "json", "--all"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    out = r.stdout.strip()
    if not out:
        return []
    if out.startswith("["):
        return json.loads(out)
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _status_emoji(state: str) -> str:
    s = state.lower()
    if "running" in s or "healthy" in s:
        return "🟢"
    if "exit" in s or "dead" in s or "fail" in s:
        return "🔴"
    if "restart" in s:
        return "🟡"
    return "⚪"


@router.message(Command("docker"))
async def cmd_docker(message: Message) -> None:
    projects = _detect()
    if not projects:
        await message.answer(
            "Нет запущенных <code>docker compose</code> проектов.\n"
            "<i>(если они есть, проверь что demon работает от юзера в группе docker)</i>"
        )
        return

    chunks = [f"<b>Docker compose</b> · {len(projects)} проект(а)"]
    for p in projects:
        name = p.get("Name", "?")
        path = p.get("ConfigFiles", "").split(",")[0]
        chunks.append(f"\n📦 <b>{esc(name)}</b>  <code>{esc(path)}</code>")
        for c in _ps(path):
            cname = c.get("Service") or c.get("Name", "?")
            state = c.get("State", "")
            status = c.get("Status", "")
            chunks.append(
                f"  {_status_emoji(state)} <code>{esc(cname):<20}</code> "
                f"<i>{esc(status or state)}</i>"
            )

    text = "\n".join(chunks)
    if len(text) > 4000:
        text = text[:3990] + "\n…⟨обрезано⟩"
    await message.answer(text)
