"""FSM states for the /login flow."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class Login(StatesGroup):
    username = State()
    password = State()
