"""Handler routers.

Each file in this package owns a `router = Router(name="...")` and a set
of decorated handlers. `register_routers()` includes them all into the
main Dispatcher in a stable order — earlier routers win on overlapping
filters, so put more specific ones first.
"""
from __future__ import annotations

from aiogram import Dispatcher

from . import (
    admin,
    alerts,
    auth,
    checks,
    docker,
    help,
    logs,
    menu,
    report,
    start,
    subs,
    system,
)


def register_routers(dp: Dispatcher) -> None:
    # Auth router goes first so /login + /me + /logout win over any future
    # router that might use the same command names.
    dp.include_router(auth.router)
    dp.include_router(start.router)
    dp.include_router(help.router)

    # Phase 1 — read-only monitoring.
    dp.include_router(system.router)   # /status, /uptime
    dp.include_router(checks.router)   # /checks, /check
    dp.include_router(alerts.router)   # /alerts
    dp.include_router(logs.router)     # /logs, /signatures
    dp.include_router(report.router)   # /report
    dp.include_router(docker.router)   # /docker

    # Phase 2 — admin actions (proxy to daemon REST).
    dp.include_router(admin.router)    # /run, /restart, /start, /stop, /notify_test

    # Phase 3 — push subscriptions.
    dp.include_router(subs.router)     # /subscribe, /unsubscribe, /subscriptions

    # Phase 4 — inline menu.
    dp.include_router(menu.router)     # /menu + callback buttons
