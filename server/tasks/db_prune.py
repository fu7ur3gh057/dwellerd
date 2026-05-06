"""Periodic retention for the unbounded append-only tables.

The `log_events` table has its own pruner (see `core.logs.store.LogEventStore.prune`
+ `tasks.logs.prune_log_events`). Everything else that grows per-event lives
here:

- `check_results` — one row per check execution. With ~10 checks at 30s, a
  year is several million rows / multiple GB without retention.
- `alerts` — one row per fired alert. Smaller volume but still unbounded.
- `terminal_audit` — one row per keystroke chunk. Sensitive (records typed
  passwords) so retention is also a security argument, not just disk.

Each task does the same two-pass cleanup the log store uses: drop rows older
than N days, then trim to the newest M rows by id. Whichever is tighter wins.
Hourly schedule lives in `services.taskiq.scheduler.run_scheduler`.
"""

import logging
import time
from typing import Type

from sqlalchemy import delete, func, select as sa_select
from sqlmodel import SQLModel

from services.taskiq.broker import broker

log = logging.getLogger(__name__)


# Retention defaults. Conservative — drop very old data first (age cutoff)
# then enforce a hard row cap as a backstop. Tune via DB-level migration if
# the operator needs more history; making these config-driven adds a knob
# nobody actually changes, so keep them hardcoded until that demand shows up.
_RETENTION_DAYS = {
    "check_results":   30,
    "alerts":          90,
    "terminal_audit":  14,
}
_MAX_ROWS = {
    "check_results":   1_000_000,
    "alerts":          100_000,
    "terminal_audit":  50_000,
}


async def _prune_table(
    model: Type[SQLModel], days: int, max_rows: int, label: str,
) -> int:
    """Two-pass cleanup: by ts (age) then by id (count). Returns rows removed."""
    sm = broker.state.data.get("db_session_maker")
    if sm is None:
        return 0

    cutoff_ts = time.time() - days * 86400
    removed = 0
    try:
        async with sm() as session:
            # by-age — model.ts is a column on all three target tables.
            age_res = await session.execute(
                delete(model).where(model.ts < cutoff_ts),  # type: ignore[attr-defined]
            )
            removed += age_res.rowcount or 0

            # by-count — keep newest `max_rows`. Find the id at the boundary
            # then delete <= it. Two queries, but cheaper than ORDER BY DESC
            # OFFSET + LIMIT on a multi-million row table without an index
            # on id (it's the PK so it has one).
            count_res = await session.execute(
                sa_select(func.count()).select_from(model),
            )
            total = count_res.scalar_one() or 0
            excess = total - max_rows
            if excess > 0:
                boundary_res = await session.execute(
                    sa_select(model.id)  # type: ignore[attr-defined]
                    .order_by(model.id)  # type: ignore[attr-defined]
                    .offset(excess - 1)
                    .limit(1),
                )
                cutoff_id = boundary_res.scalar_one_or_none()
                if cutoff_id is not None:
                    cnt_res = await session.execute(
                        delete(model).where(
                            model.id <= cutoff_id,  # type: ignore[attr-defined]
                        ),
                    )
                    removed += cnt_res.rowcount or 0
            await session.commit()
    except Exception:
        log.exception("prune_%s failed", label)
        return removed

    if removed:
        log.info("prune_%s: removed %d row(s)", label, removed)
    return removed


@broker.task
async def prune_check_results() -> None:
    from db.models import CheckResult
    await _prune_table(
        CheckResult,
        _RETENTION_DAYS["check_results"],
        _MAX_ROWS["check_results"],
        "check_results",
    )


@broker.task
async def prune_alerts() -> None:
    from db.models import AlertEvent
    await _prune_table(
        AlertEvent,
        _RETENTION_DAYS["alerts"],
        _MAX_ROWS["alerts"],
        "alerts",
    )


@broker.task
async def prune_terminal_audit() -> None:
    from db.models import TerminalAuditEntry
    await _prune_table(
        TerminalAuditEntry,
        _RETENTION_DAYS["terminal_audit"],
        _MAX_ROWS["terminal_audit"],
        "terminal_audit",
    )
