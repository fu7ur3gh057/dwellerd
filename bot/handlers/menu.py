"""/menu — inline keyboard with the most-used actions.

Each button fires a callback that re-uses the same handler functions the
text commands do (no logic duplication).
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from ..keyboards.main_menu import main_menu
from . import alerts, checks, docker, logs, report, subs, system
from .auth import cmd_me


router = Router(name=__name__)
log = logging.getLogger(__name__)


def _is_admin(current_user) -> bool:
    return current_user is not None and getattr(current_user, "role", "") == "admin"


@router.message(Command("menu"))
async def cmd_menu(message: Message, current_user=None) -> None:
    await message.answer(
        "Что показать?",
        reply_markup=main_menu(is_admin=_is_admin(current_user)),
    )


# Each callback_data starts with `menu:`. Aiogram's `F.data` filter matches
# exact string; we use startswith below to keep one handler per branch.

@router.callback_query(F.data == "menu:status")
async def cb_status(cb: CallbackQuery) -> None:
    await cb.answer()
    if cb.message:
        await system.cmd_status(cb.message)


@router.callback_query(F.data == "menu:checks")
async def cb_checks(cb: CallbackQuery) -> None:
    await cb.answer()
    if cb.message:
        await checks.cmd_checks(cb.message)


@router.callback_query(F.data == "menu:alerts")
async def cb_alerts(cb: CallbackQuery) -> None:
    await cb.answer()
    # Reuse the cmd by faking a CommandObject with no args — easier to call
    # query helpers directly to avoid that dependency.
    from ..queries import recent_alerts
    from ..format import esc, level_emoji, short_dt
    rows = recent_alerts(limit=10)
    if cb.message is None:
        return
    if not rows:
        await cb.message.answer("Нет недавних alert'ов.")
        return
    lines = [f"<b>Alerts</b> · последние {len(rows)}"]
    for r in rows:
        lines.append(
            f"{level_emoji(r.level)} <code>{short_dt(r.ts)}</code>  "
            f"<b>{esc(r.name)}</b>  <i>{esc(r.detail or '—')}</i>"
        )
    await cb.message.answer("\n".join(lines))


@router.callback_query(F.data == "menu:logs")
async def cb_logs(cb: CallbackQuery) -> None:
    await cb.answer()
    from ..queries import recent_logs
    from ..format import esc, short_dt
    if cb.message is None:
        return
    rows = recent_logs(limit=10)
    if not rows:
        await cb.message.answer("Лог-событий пока нет.")
        return
    lines = ["<b>Logs</b> · последние 10"]
    for r in rows:
        marker = "🆕" if r.first else "·"
        line = r.line.strip()
        if len(line) > 120:
            line = line[:119] + "…"
        lines.append(f"{marker} <code>{short_dt(r.ts)}</code> "
                     f"<b>{esc(r.source)}</b>: <i>{esc(line)}</i>")
    await cb.message.answer("\n".join(lines))


@router.callback_query(F.data == "menu:report")
async def cb_report(cb: CallbackQuery) -> None:
    await cb.answer()
    if cb.message:
        await report.cmd_report(cb.message)


@router.callback_query(F.data == "menu:docker")
async def cb_docker(cb: CallbackQuery) -> None:
    await cb.answer()
    if cb.message:
        await docker.cmd_docker(cb.message)


@router.callback_query(F.data == "menu:subs")
async def cb_subs(cb: CallbackQuery) -> None:
    await cb.answer()
    if cb.message:
        await subs.cmd_subscriptions(cb.message)


@router.callback_query(F.data == "menu:me")
async def cb_me(cb: CallbackQuery, current_user=None) -> None:
    await cb.answer()
    if cb.message and current_user is not None:
        await cmd_me(cb.message, current_user=current_user)


@router.callback_query(F.data == "menu:admin_hint")
async def cb_admin_hint(cb: CallbackQuery) -> None:
    await cb.answer()
    if cb.message:
        await cb.message.answer(
            "Admin-команды (текстовые):\n"
            "<code>/run &lt;check&gt;</code>\n"
            "<code>/restart &lt;project&gt; [service]</code>\n"
            "<code>/start &lt;project&gt;</code>  ·  <code>/stop &lt;project&gt;</code>\n"
            "<code>/notify_test [type]</code>"
        )
