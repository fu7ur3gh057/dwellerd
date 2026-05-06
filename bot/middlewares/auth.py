"""Auth gate.

For every incoming Message:
  1. Look up `current_user` from bot_sessions, inject into handler data
  2. If unauthenticated and the message isn't a public command (and the
     user isn't already mid-/login flow), drop with a 'log in first' nudge.

Public commands are intentionally kept tiny — anything that does real
work should require a session.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, TelegramObject

from ..auth import current_user


# Commands that work without a login. /cancel is here so a half-completed
# /login flow can always be aborted.
PUBLIC_COMMANDS = {"/start", "/login", "/help", "/cancel"}


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Only Messages are gated — callbacks/inline-queries get through
        # unconditionally for now (we'll layer them on as we add features).
        if not isinstance(event, Message) or event.from_user is None:
            return await handler(event, data)

        tg_id = event.from_user.id
        user = current_user(tg_id)
        data["current_user"] = user

        if user is not None:
            return await handler(event, data)

        # Strip @botname suffix and any args so the comparison is robust.
        text = event.text or event.caption or ""
        cmd = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        if cmd in PUBLIC_COMMANDS:
            return await handler(event, data)

        # Allow free-text messages while the user is already in the FSM
        # /login flow — those are the username/password being entered.
        state: FSMContext | None = data.get("state")
        if state is not None and (await state.get_state()) is not None:
            return await handler(event, data)

        await event.answer(
            "🔒 Чтобы пользоваться командами, сначала войди: /login"
        )
        return None
