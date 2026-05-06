"""/alerts [N] — recent fired alert events."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.types import Message

from ..format import esc, level_emoji, short_dt
from ..queries import recent_alerts


router = Router(name=__name__)


def _parse_n(args: str | None, *, default: int = 10, maximum: int = 50) -> int:
    if not args:
        return default
    try:
        n = int(args.strip())
    except ValueError:
        return default
    return max(1, min(maximum, n))


@router.message(Command("alerts"))
async def cmd_alerts(message: Message, command: CommandObject) -> None:
    n = _parse_n(command.args)
    rows = recent_alerts(limit=n)
    if not rows:
        await message.answer("За последнее время ни одного alert'а — всё спокойно.")
        return

    lines = [f"<b>Alerts</b> · последние {len(rows)}"]
    for r in rows:
        lines.append(
            f"{level_emoji(r.level)} <code>{short_dt(r.ts)}</code>  "
            f"<b>{esc(r.name)}</b>  <i>{esc(r.detail or '—')}</i>"
        )
    await message.answer("\n".join(lines))
