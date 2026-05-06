"""/help — list of available commands."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message


router = Router(name=__name__)


@router.message(Command("help"))
async def cmd_help(message: Message, current_user=None) -> None:
    public = (
        "<b>Команды (без логина)</b>\n"
        "/start — приветствие\n"
        "/login — войти (логин + пароль из <code>users</code>)\n"
        "/help — этот список"
    )
    if current_user is None:
        await message.answer(public)
        return
    await message.answer(
        f"{public}\n\n"
        "<b>После логина</b>\n"
        "/me — текущий пользователь\n"
        "/logout — выйти"
    )
