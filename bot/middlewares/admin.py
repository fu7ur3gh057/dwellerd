"""Admin filter — convenience for handlers that need role=admin.

Used as an aiogram filter (not a middleware): apply per-handler with
`@router.message(IsAdmin(), Command("run"))`. Cleaner than repeating
`if current_user.role != 'admin': ...` in every action handler.
"""
from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import Message


class IsAdmin(BaseFilter):
    async def __call__(self, message: Message, current_user=None) -> bool:
        return current_user is not None and getattr(current_user, "role", "") == "admin"
