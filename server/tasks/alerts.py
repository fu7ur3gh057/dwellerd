"""Alert dispatch task — broadcasts an Alert to every configured notifier
and persists it to `alerts` for the timeline view."""

import logging
import time

from sqlmodel.ext.asyncio.session import AsyncSession
from taskiq import TaskiqDepends

from core.notifiers import Alert
from db.deps import get_session
from db.models import AlertEvent
from services.taskiq.broker import broker
from services.taskiq.context import AppContext
from services.taskiq.deps import get_app_context

log = logging.getLogger(__name__)


@broker.task
async def send_alert(
    alert: Alert,
    ctx: AppContext = TaskiqDepends(get_app_context),
    session: AsyncSession = TaskiqDepends(get_session),
) -> None:
    now = time.time()
    session.add(AlertEvent(
        ts=now,
        name=alert.check,
        level=alert.level,
        kind=alert.kind or None,
        detail=alert.detail,
        metrics=alert.metrics or None,
    ))
    await session.commit()

    # Skip notifiers that are toggled off in Settings. The persisted Alert
    # row still lands in DB above so the /alerts timeline is complete —
    # only the outbound push is suppressed.
    disabled_types = {
        cfg.type for cfg in ctx.config.notifiers
        if not getattr(cfg, "enabled", True)
    }
    for n_type, n in ctx.notifiers_by_type.items():
        if n_type in disabled_types:
            continue
        try:
            await n.send(alert)
        except Exception:
            log.exception("notifier %s failed", type(n).__name__)

    # Push to /alerts namespace subscribers; no-op if web isn't running.
    from web.sockets import emit
    await emit("/alerts", "alert:fired", {
        "ts": now,
        "name": alert.check,
        "level": alert.level,
        "kind": alert.kind,
        "detail": alert.detail,
        "metrics": alert.metrics,
    })
