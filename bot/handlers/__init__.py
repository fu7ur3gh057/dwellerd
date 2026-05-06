"""Handler routers.

Each file in this package owns a `router = Router(name="...")` and a set
of decorated handlers. `register_routers()` includes them all into the
main Dispatcher in a stable order — earlier routers win on overlapping
filters, so put more specific ones first.
"""
from __future__ import annotations

from aiogram import Dispatcher

from . import auth, help, start


def register_routers(dp: Dispatcher) -> None:
    # Auth router goes first so /login + /me + /logout win over any future
    # router that might use the same command names.
    dp.include_router(auth.router)
    dp.include_router(start.router)
    dp.include_router(help.router)
