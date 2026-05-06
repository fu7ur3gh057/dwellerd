"""/start — first-touch greeting. Nudges to /login when not authenticated."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message


router = Router(name=__name__)


@router.message(CommandStart())
async def cmd_start(message: Message, current_user=None) -> None:
    if current_user is not None:
        await message.answer(
            f"🦆  <b>Dwellerd</b>\n"
            f"Залогинен как <b>{current_user.username}</b>. /help для списка команд."
        )
        return
    await message.answer(
        "🦆  <b>Dwellerd</b> bot\n"
        "Чтобы пользоваться командами — войди:  /login"
    )
