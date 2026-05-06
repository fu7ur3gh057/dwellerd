"""/login, /logout, /me, /cancel — credential auth flow.

Login is two-step (FSM):
    1. /login → ask for username
    2. text   → ask for password
    3. text   → verify; on success start a bot_session row.

The password message is deleted from the chat as soon as we read it so
it doesn't sit in scrollback. (Telegram has no "secret input" field; this
is the best we can do client-side.)
"""
from __future__ import annotations

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from ..auth import revoke_session, start_session, verify_credentials
from ..commands import reset_commands, set_user_commands
from ..states.auth import Login


router = Router(name=__name__)


@router.message(Command("login"))
async def cmd_login(message: Message, state: FSMContext) -> None:
    # If already logged in, no need to re-auth.
    await state.clear()
    await state.set_state(Login.username)
    await message.answer(
        "Логин:\n"
        "<i>(или /cancel чтобы отменить)</i>"
    )


@router.message(Login.username)
async def login_username(message: Message, state: FSMContext) -> None:
    username = (message.text or "").strip()
    if not username:
        await message.answer("Логин пустой — попробуй ещё или /cancel")
        return
    await state.update_data(username=username)
    await state.set_state(Login.password)
    await message.answer(
        "Пароль:\n"
        "<i>(сообщение с паролем удалю сразу же)</i>"
    )


@router.message(Login.password)
async def login_password(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    username = data.get("username", "")
    password = message.text or ""

    # Try to wipe the password message immediately. Bot needs to be admin
    # of the chat for delete in groups, but in private chats this works
    # for messages newer than 48 hours.
    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    user = verify_credentials(username, password)
    await state.clear()

    if user is None:
        await message.answer(
            "❌ Неверный логин или пароль.\n"
            "Попробуй ещё раз: /login"
        )
        return

    # Also obtain REST tokens — required for admin actions (/run, /restart).
    # Silent failure when web is disabled: bot still works, just without
    # the action commands.
    rest = None
    try:
        from ..api import rest_login
        rest = await rest_login(username, password)
    except Exception:
        rest = None

    if rest:
        start_session(
            message.from_user.id, user,
            access_token=rest.get("access_token"),
            refresh_token=rest.get("refresh_token"),
            expires_in=rest.get("expires_in"),
        )
    else:
        start_session(message.from_user.id, user)

    # Refresh the per-chat command list so `/` autocomplete reflects the
    # new user's role. Errors are non-fatal — the chat just keeps showing
    # the GUEST defaults.
    await set_user_commands(
        message.bot, message.chat.id,
        is_admin=(user.role == "admin"),
    )

    rest_note = "" if rest else "\n<i>(web API не запущен — команды действий недоступны)</i>"
    await message.answer(
        f"✅ Привет, <b>{user.username}</b>!\n"
        f"Роль: <code>{user.role}</code>\n"
        f"Команды: /help{rest_note}"
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if (await state.get_state()) is None:
        await message.answer("Нечего отменять.")
        return
    await state.clear()
    await message.answer("Отменено.")


@router.message(Command("logout"))
async def cmd_logout(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    await state.clear()
    revoked = revoke_session(message.from_user.id)
    # Drop the per-chat command override so `/` falls back to GUEST.
    await reset_commands(message.bot, message.chat.id)
    if revoked:
        await message.answer("Вышел из аккаунта. /login чтобы войти снова.")
    else:
        await message.answer("Ты не залогинен.")


@router.message(Command("me"))
async def cmd_me(message: Message, current_user) -> None:
    # AuthMiddleware injects `current_user`; if we got here it's not None
    # (middleware would have nudged to /login otherwise).
    await message.answer(
        f"<b>{current_user.username}</b>\n"
        f"role: <code>{current_user.role}</code>\n"
        f"id: <code>{current_user.id}</code>"
    )
