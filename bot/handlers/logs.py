"""/logs [N] — recent log events · /signatures — top dedup'd error patterns."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.types import Message

from ..format import esc, short_dt, time_ago
from ..queries import recent_logs, top_signatures


router = Router(name=__name__)


def _parse_n(args: str | None, *, default: int = 10, maximum: int = 30) -> int:
    if not args:
        return default
    try:
        return max(1, min(maximum, int(args.strip())))
    except ValueError:
        return default


def _truncate(s: str, *, limit: int = 120) -> str:
    s = s.strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"


@router.message(Command("logs"))
async def cmd_logs(message: Message, command: CommandObject) -> None:
    n = _parse_n(command.args, default=10)
    rows = recent_logs(limit=n)
    if not rows:
        await message.answer("Лог-событий пока нет.")
        return

    lines = [f"<b>Logs</b> · последние {len(rows)}"]
    for r in rows:
        marker = "🆕" if r.first else "·"
        lines.append(
            f"{marker} <code>{short_dt(r.ts)}</code> "
            f"<b>{esc(r.source)}</b>: <i>{esc(_truncate(r.line))}</i>"
        )
    await message.answer("\n".join(lines))


@router.message(Command("signatures"))
async def cmd_signatures(message: Message, command: CommandObject) -> None:
    n = _parse_n(command.args, default=10, maximum=20)
    rows = top_signatures(limit=n)
    if not rows:
        await message.answer("Дедуп-сигнатур пока нет.")
        return

    lines = [f"<b>Top error signatures</b> · топ-{len(rows)}"]
    for r in rows:
        lines.append(
            f"<b>×{r.total}</b>  <code>{esc(r.source)}</code>  "
            f"<i>{time_ago(r.first_seen)}</i>\n"
            f"   <i>{esc(_truncate(r.sample, limit=140))}</i>"
        )
    await message.answer("\n".join(lines))
