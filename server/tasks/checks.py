"""Periodic check execution as TaskIQ tasks.

Persists every result to `check_results`, advances `check_state`, and on
a level transition fires `send_alert.kiq`. Both worker and web see the
same DB through server/db.
"""

import logging
import time

from sqlmodel.ext.asyncio.session import AsyncSession
from taskiq import TaskiqDepends

from core.checks import Result
from core.notifiers import Alert
from core.state import decide_transition
from db.deps import get_session
from db.models import CheckResult, CheckStateEntry
from services.taskiq.broker import broker
from services.taskiq.context import AppContext
from services.taskiq.deps import get_app_context
from tasks.alerts import send_alert

log = logging.getLogger(__name__)


@broker.task
async def run_check(
    name: str,
    ctx: AppContext = TaskiqDepends(get_app_context),
    session: AsyncSession = TaskiqDepends(get_session),
) -> None:
    handler = ctx.checks_by_name.get(name)
    if handler is None:
        log.warning("run_check: unknown check %r", name)
        return

    # Per-check soft-disable from Settings UI. The scheduler still kicks
    # us at the configured interval; we skip silently here so a disabled
    # check leaves no rows in check_results / alerts and emits no state
    # transitions. Re-enable picks back up on the next scheduled tick.
    cfg = next((c for c in ctx.config.checks if c.name == name), None)
    if cfg is not None and not getattr(cfg, "enabled", True):
        return

    try:
        result = await handler.run()
    except Exception as e:
        log.exception("check %s crashed", name)
        result = Result(level="crit", detail=f"crashed: {e}")

    now = time.time()
    session.add(CheckResult(
        ts=now, name=name, kind=result.kind, level=result.level,
        detail=result.detail, metrics=result.metrics or None,
    ))

    prev_entry = await session.get(CheckStateEntry, name)
    new_level = decide_transition(prev_entry.level if prev_entry else None, result.level)

    if prev_entry is None:
        session.add(CheckStateEntry(name=name, level=result.level, updated_at=now))
    else:
        prev_entry.level = result.level
        prev_entry.updated_at = now
        session.add(prev_entry)

    await session.commit()

    # Live update for the dashboard. Sent every tick (not only on
    # transitions) so graphs stay smooth. Frontend that cares about a
    # single check filters by `name` client-side; rooms exist on the
    # namespace for future per-check broadcasts that the dashboard
    # shouldn't see. No-op when web isn't running (Phase 3 stub).
    from web.sockets import emit
    await emit("/checks", "check:result", {
        "ts": now,
        "name": name,
        "level": result.level,
        "kind": result.kind,
        "detail": result.detail,
        "metrics": result.metrics,
    })

    if new_level is None:
        return

    alert = Alert(
        check=name,
        level=new_level,
        detail=result.detail,
        kind=result.kind,
        metrics=result.metrics,
    )
    await send_alert.kiq(alert)
