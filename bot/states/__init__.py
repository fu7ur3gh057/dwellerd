"""FSM state groups.

Subclass `aiogram.fsm.state.StatesGroup` per dialog flow:

    from aiogram.fsm.state import State, StatesGroup
    class AddAlert(StatesGroup):
        choosing_check = State()
        entering_threshold = State()
"""
