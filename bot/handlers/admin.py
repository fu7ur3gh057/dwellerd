"""Admin actions — phase 2.

All commands here proxy to the daemon's REST API using tokens cached in
`bot_sessions` at /login time. Requires `web.enabled: true` in config.yaml.

Each handler is gated by IsAdmin() so non-admin users hitting them get
no answer (default Aiogram behaviour: filter mismatch = no match, falls
through to the next handler or nothing).
"""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.types import Message

from ..api import ApiError, WebDisabled, docker_action, docker_service_action, notifier_test, run_check
from ..middlewares.admin import IsAdmin


router = Router(name=__name__)
log = logging.getLogger(__name__)


_ALLOWED_DOCKER_ACTIONS = {"up", "down", "restart", "stop", "start", "pull"}


def _format_error(e: Exception) -> str:
    if isinstance(e, WebDisabled):
        return (
            "❌ Web API не включён в config.yaml.\n"
            "Запусти демон с <code>--web</code> или поправь конфиг и перезагрузи."
        )
    if isinstance(e, ApiError):
        return f"❌ <b>{e.status}</b>: <i>{e.body[:300]}</i>"
    return f"❌ Ошибка: <i>{e}</i>"


# ── /run <check> ──────────────────────────────────────────────────────────


@router.message(IsAdmin(), Command("run"))
async def cmd_run(message: Message, command: CommandObject) -> None:
    name = (command.args or "").strip()
    if not name:
        await message.answer(
            "Использование: <code>/run &lt;check_name&gt;</code>\n"
            "Список: /checks"
        )
        return
    try:
        res = await run_check(message.from_user.id, name)
    except Exception as e:
        await message.answer(_format_error(e))
        return
    queued = res.get("queued") if isinstance(res, dict) else False
    await message.answer(
        f"⏱ Проверка <b>{name}</b> поставлена в очередь.\n"
        if queued else
        f"⚠️ Поставил, но ответ необычный: <code>{res}</code>"
    )


# ── /restart <project> [service] ──────────────────────────────────────────


@router.message(IsAdmin(), Command("restart", "start", "stop"))
async def cmd_compose(message: Message, command: CommandObject) -> None:
    """Routes /restart, /start, /stop to compose actions.
    Format: `/<action> <project> [service]`
    """
    action = command.command  # "restart" / "start" / "stop"
    if action not in _ALLOWED_DOCKER_ACTIONS:
        return  # shouldn't reach here

    parts = (command.args or "").split()
    if not parts:
        await message.answer(
            f"Использование:\n"
            f"<code>/{action} &lt;project&gt;</code> — действие на проект\n"
            f"<code>/{action} &lt;project&gt; &lt;service&gt;</code> — на конкретный контейнер"
        )
        return

    project = parts[0]
    service = parts[1] if len(parts) > 1 else None

    try:
        if service:
            res = await docker_service_action(message.from_user.id, project, service, action)
        else:
            res = await docker_action(message.from_user.id, project, action)
    except Exception as e:
        await message.answer(_format_error(e))
        return

    ok = res.get("ok", False) if isinstance(res, dict) else False
    detail = res.get("stderr") or res.get("stdout") or "" if isinstance(res, dict) else str(res)
    target = f"{project}/{service}" if service else project
    icon = "✅" if ok else "❌"
    text = f"{icon} <b>{action}</b> <code>{target}</code>"
    if detail:
        snippet = detail[:300]
        text += f"\n<pre>{snippet}</pre>"
    await message.answer(text)


# ── /notify_test ──────────────────────────────────────────────────────────


@router.message(IsAdmin(), Command("notify_test"))
async def cmd_notify_test(message: Message, command: CommandObject) -> None:
    type_ = (command.args or "telegram").strip() or "telegram"
    try:
        res = await notifier_test(message.from_user.id, type_)
    except Exception as e:
        await message.answer(_format_error(e))
        return
    await message.answer(f"✅ Test через <b>{type_}</b> отправлен.\n<code>{res}</code>")


# ── friendly nudge for non-admins hitting these commands ──────────────────
#
# Admin-gated handlers above only fire when current_user.role == admin. For
# everyone else (logged-in but not admin) we still want a polite reply
# rather than silent fallthrough — register a low-priority handler with the
# inverse filter.


@router.message(Command("run", "restart", "start", "stop", "notify_test"))
async def cmd_admin_only(message: Message, current_user=None) -> None:
    if current_user is None:
        return  # AuthMiddleware would have already nudged
    await message.answer(
        f"🔒 Команда требует роль <code>admin</code>. Текущая: "
        f"<code>{getattr(current_user, 'role', 'unknown')}</code>"
    )
