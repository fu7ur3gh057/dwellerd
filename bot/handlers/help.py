"""/help — list of available commands grouped by access level."""
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

    user_block = (
        "\n\n"
        "<b>Меню</b>\n"
        "/menu — кнопки самых частых действий\n"
        "\n"
        "<b>Мониторинг</b>\n"
        "/status — снапшот хоста (CPU/mem/disk + active alerts)\n"
        "/checks — все проверки + последние результаты\n"
        "/check &lt;name&gt; — детали + история одной проверки\n"
        "/alerts [N] — последние сработавшие алерты\n"
        "/logs [N] — последние log-события\n"
        "/signatures — топ-N дедуп'нутых ошибок\n"
        "/report — полный системный отчёт\n"
        "/docker — статус docker compose проектов\n"
        "/uptime — uptime хоста и демона\n"
        "\n"
        "<b>Подписки (push в DM)</b>\n"
        "/subscribe alerts [check] — все/одной проверки\n"
        "/subscribe logs [source] — первое появление каждой error-сигнатуры\n"
        "/subscribe checks &lt;name&gt; — каждый результат конкретной проверки\n"
        "/unsubscribe [topic] — отписаться\n"
        "/subscriptions — список текущих подписок\n"
        "\n"
        "<b>Аккаунт</b>\n"
        "/me — текущий пользователь\n"
        "/logout — выйти"
    )

    is_admin = getattr(current_user, "role", "") == "admin"
    admin_block = ""
    if is_admin:
        admin_block = (
            "\n\n"
            "<b>Админ</b>\n"
            "/run &lt;name&gt; — триггернуть проверку прямо сейчас\n"
            "/restart &lt;project&gt; [service] — перезапустить compose\n"
            "/start &lt;project&gt;  ·  /stop &lt;project&gt;\n"
            "/notify_test [type] — пробное уведомление\n"
        )

    await message.answer(public + user_block + admin_block)
