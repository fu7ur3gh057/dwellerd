"""Periodic task scheduler.

For each configured check we spawn an asyncio task that fires
`run_check.kiq(name)` every `interval` seconds. Same pattern for the report
digest. Kicks are wrapped in `create_task` so a slow check doesn't delay
the next tick — late results are fine for monitoring, missed ticks aren't.

When we eventually swap InMemoryBroker for Redis, this scheduler can stay
exactly as-is — only the in-process Receiver setup changes.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.taskiq.context import AppContext

log = logging.getLogger(__name__)


async def run_scheduler(ctx: "AppContext") -> None:
    from tasks.checks import run_check
    from tasks.db_prune import prune_alerts, prune_check_results, prune_terminal_audit
    from tasks.logs import prune_log_events
    from tasks.report import build_and_send_report

    coros = []
    for name, check in ctx.checks_by_name.items():
        coros.append(_periodic(name, check.interval, lambda n=name: run_check.kiq(n)))

    if ctx.report_sections and ctx.report_targets:
        report_interval = float((ctx.config.report or {}).get("interval", 300))
        coros.append(_periodic("report", report_interval, lambda: build_and_send_report.kiq()))

    if ctx.logs_enabled:
        coros.append(_periodic("logs-prune", 3600.0, lambda: prune_log_events.kiq()))

    # Always-on retention for tables that grow per-event and have no other
    # bound. Hourly is plenty: at typical write rates none of these tables
    # crosses the threshold in under a day.
    coros.append(_periodic("prune-check-results", 3600.0, lambda: prune_check_results.kiq()))
    coros.append(_periodic("prune-alerts",        3600.0, lambda: prune_alerts.kiq()))
    coros.append(_periodic("prune-terminal-audit", 3600.0, lambda: prune_terminal_audit.kiq()))

    if not coros:
        log.warning("scheduler: nothing to schedule")
        return

    log.info("scheduler: %d periodic kickers", len(coros))
    await asyncio.gather(*coros)


async def _periodic(name: str, interval: float, kick) -> None:
    while True:
        try:
            asyncio.create_task(kick())
        except Exception:
            log.exception("scheduler: failed to kick %s", name)
        await asyncio.sleep(interval)
