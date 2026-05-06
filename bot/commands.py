"""BotCommand lists + Telegram setMyCommands wiring.

Telegram clients show a `/`-typed dropdown built from whatever the bot
registered via `setMyCommands`. We register three scopes:

  GUEST  — applied to BotCommandScopeDefault at startup; visible to anyone
           who hasn't logged in. Tiny list: just /start /login /help.
  USER   — applied per-chat (BotCommandScopeChat) on /login success.
  ADMIN  — same trigger but only when the linked Dwellerd User has
           role=admin. Adds /run /restart /up /down /notify_test.

On /logout we delete the per-chat scope so the chat falls back to GUEST.

Per-chat scope only takes effect in *private* chats with the bot — that's
all we use, so no extra handling for groups.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)


log = logging.getLogger(__name__)


GUEST: list[BotCommand] = [
    BotCommand(command="start",  description="Приветствие"),
    BotCommand(command="login",  description="Войти"),
    BotCommand(command="help",   description="Список команд"),
]


USER: list[BotCommand] = [
    BotCommand(command="menu",          description="🦆 Меню"),
    BotCommand(command="status",        description="📊 Снапшот хоста"),
    BotCommand(command="checks",        description="✅ Все проверки"),
    BotCommand(command="check",         description="🔍 Детали проверки"),
    BotCommand(command="alerts",        description="🔔 Последние алерты"),
    BotCommand(command="logs",          description="📝 Последние логи"),
    BotCommand(command="signatures",    description="🆔 Топ сигнатур"),
    BotCommand(command="report",        description="📋 Полный отчёт"),
    BotCommand(command="docker",        description="🐳 Docker compose"),
    BotCommand(command="uptime",        description="⏱ Uptime"),
    BotCommand(command="subscribe",     description="🔔 Подписаться на push"),
    BotCommand(command="unsubscribe",   description="🔕 Отписаться"),
    BotCommand(command="subscriptions", description="📑 Мои подписки"),
    BotCommand(command="me",            description="👤 Я"),
    BotCommand(command="logout",        description="🚪 Выйти"),
    BotCommand(command="help",          description="❓ Помощь"),
]


ADMIN: list[BotCommand] = USER + [
    BotCommand(command="run",         description="⚡ Триггернуть проверку"),
    BotCommand(command="restart",     description="🔁 Перезапустить compose"),
    BotCommand(command="up",          description="▶️ Поднять compose"),
    BotCommand(command="down",        description="⏹ Остановить compose"),
    BotCommand(command="notify_test", description="📨 Тест нотифаера"),
]


# ── apply helpers ──────────────────────────────────────────────────────────


async def set_default_commands(bot: Bot) -> None:
    """Apply the GUEST list as the bot-wide default. Idempotent — Telegram
    upserts on each call."""
    try:
        await bot.set_my_commands(GUEST, scope=BotCommandScopeDefault())
        log.info("registered %d default commands", len(GUEST))
    except TelegramBadRequest as e:
        log.warning("setMyCommands(default) failed: %s", e)


async def set_user_commands(
    bot: Bot, chat_id: int, *, is_admin: bool,
) -> None:
    """Apply USER (or ADMIN) commands to a single private-chat scope.
    Called from /login on success."""
    cmds = ADMIN if is_admin else USER
    try:
        await bot.set_my_commands(
            cmds, scope=BotCommandScopeChat(chat_id=chat_id),
        )
    except TelegramBadRequest as e:
        log.warning("setMyCommands(chat=%s) failed: %s", chat_id, e)


async def reset_commands(bot: Bot, chat_id: int) -> None:
    """Drop the per-chat override so the chat falls back to GUEST.
    Called from /logout."""
    try:
        await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=chat_id))
    except TelegramBadRequest as e:
        log.warning("deleteMyCommands(chat=%s) failed: %s", chat_id, e)
