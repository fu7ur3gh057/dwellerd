"""Middleware registration.

`register_middlewares` is the single attach point — install new layers
here in the right order. AuthMiddleware is the inner layer (after FSM
storage middleware) so it sees `state` in `data` and can let in-flight
/login flows through.
"""
from __future__ import annotations

from aiogram import Dispatcher

from .auth import AuthMiddleware


def register_middlewares(dp: Dispatcher) -> None:
    dp.message.middleware(AuthMiddleware())
