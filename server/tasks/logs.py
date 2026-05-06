"""Log notification tasks — dispatch first-seen and digest events through
the configured notifiers. Source filtering (which notifier to target) is
read from `config.logs.notifier` via AppContext."""

import logging

from services.taskiq.broker import broker
from services.taskiq.context import AppContext
from services.taskiq.deps import get_app_context
from taskiq import TaskiqDepends

log = logging.getLogger(__name__)


def _resolve_targets(ctx: AppContext) -> list:
    """Pick the notifier(s) the logs section should hit. Returns an empty
    list when `logs.notify` is explicitly false — that's the operator-side
    kill-switch for Telegram blasting on every captured error line. Logs
    still get stored in `log_events` and surface in /logs; just no push.

    Default is True (preserves the original send-everywhere behaviour).
    """
    cfg = ctx.config.logs or {}
    if cfg.get("notify") is False:
        return []
    sel = cfg.get("notifier")
    if sel and sel in ctx.notifiers_by_type:
        return [ctx.notifiers_by_type[sel]]
    return ctx.notifiers


@broker.task
async def notify_log_first(
    source: str,
    sample: str,
    ctx: AppContext = TaskiqDepends(get_app_context),
) -> None:
    targets = _resolve_targets(ctx)
    if not targets:
        return  # notifications disabled — log was already stored upstream
    for n in targets:
        try:
            await n.send_log_first(source, sample)
        except Exception:
            log.exception("notify_log_first: %s failed", type(n).__name__)


@broker.task
async def notify_log_digest(
    items: list[dict],
    period_label: str = "",
    ctx: AppContext = TaskiqDepends(get_app_context),
) -> None:
    targets = _resolve_targets(ctx)
    if not targets:
        return
    for n in targets:
        try:
            await n.send_log_digest(items, period_label=period_label)
        except Exception:
            log.exception("notify_log_digest: %s failed", type(n).__name__)


@broker.task
async def prune_log_events() -> None:
    """Bound the `log_events` table by retention/cap. Scheduled hourly."""
    store = broker.state.data.get("log_store")
    if store is None:
        return
    try:
        await store.prune()
    except Exception:
        log.exception("prune_log_events failed")
