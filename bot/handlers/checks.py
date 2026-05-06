"""/checks — list all checks · /check <name> — one check + history."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.types import Message

from ..format import code, esc, level_emoji, short_dt, time_ago
from ..queries import check_history, list_checks


router = Router(name=__name__)


@router.message(Command("checks"))
async def cmd_checks(message: Message) -> None:
    rows = list_checks()
    if not rows:
        await message.answer("Проверок ещё нет — настрой через <code>make setup</code>.")
        return

    lines = ["<b>Checks</b>"]
    for r in rows:
        emoji = level_emoji(r["level"])
        val = r.get("last_value")
        val_str = ""
        if isinstance(val, (int, float)):
            val_str = f" <code>{val:.1f}</code>"
        lines.append(
            f"{emoji} <code>{esc(r['name']):<22}</code>{val_str}  "
            f"<i>{time_ago(r['last_ts'])}</i>"
        )
    lines.append("")
    lines.append("<i>детали:</i> /check &lt;name&gt;")
    await message.answer("\n".join(lines))


@router.message(Command("check"))
async def cmd_check(message: Message, command: CommandObject) -> None:
    name = (command.args or "").strip()
    if not name:
        await message.answer(
            "Использование: <code>/check &lt;name&gt;</code>\n"
            "Список: /checks"
        )
        return

    history = check_history(name, limit=10)
    if not history:
        await message.answer(f"Проверка <code>{esc(name)}</code> не найдена или ещё ни разу не запускалась.")
        return

    latest = history[-1]
    lines = [
        f"<b>{esc(name)}</b> {level_emoji(latest.level)} <code>{esc(latest.level)}</code>",
        f"detail: <i>{esc(latest.detail or '—')}</i>",
        "",
        "<b>history</b> (последние 10):",
    ]
    for r in reversed(history):
        val = (r.metrics or {}).get("value")
        val_str = f" {val:.1f}" if isinstance(val, (int, float)) else ""
        lines.append(
            f"{level_emoji(r.level)}  <code>{short_dt(r.ts)}</code>"
            f"  <code>{esc(r.level)}</code>{val_str}"
        )
    await message.answer("\n".join(lines))
