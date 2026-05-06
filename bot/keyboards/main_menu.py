"""Main inline menu builder."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_menu(*, is_admin: bool = False) -> InlineKeyboardMarkup:
    """Two-column grid. Admin extras only when role=admin."""
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📊 Status", callback_data="menu:status"),
        InlineKeyboardButton(text="✅ Checks", callback_data="menu:checks"),
    )
    kb.row(
        InlineKeyboardButton(text="🔔 Alerts", callback_data="menu:alerts"),
        InlineKeyboardButton(text="📝 Logs", callback_data="menu:logs"),
    )
    kb.row(
        InlineKeyboardButton(text="📋 Report", callback_data="menu:report"),
        InlineKeyboardButton(text="🐳 Docker", callback_data="menu:docker"),
    )
    kb.row(
        InlineKeyboardButton(text="🔕 Subs", callback_data="menu:subs"),
        InlineKeyboardButton(text="ℹ️ Me", callback_data="menu:me"),
    )
    if is_admin:
        kb.row(
            InlineKeyboardButton(text="🔁 Restart prompt", callback_data="menu:admin_hint"),
        )
    return kb.as_markup()
