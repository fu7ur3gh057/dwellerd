"""Alerts timeline — newest-first feed of every Alert that left a notifier."""

from fastapi import APIRouter, Depends, Query
from sqlmodel import desc, select
from sqlmodel.ext.asyncio.session import AsyncSession

from db.deps import get_session
from db.models import AlertEvent

router = APIRouter(tags=["alerts"])


@router.get("", response_model=list[AlertEvent])
async def list_alerts(
    name: str | None = Query(default=None, description="filter by check name"),
    level: str | None = Query(default=None, description="warn | crit | ok"),
    before: float | None = Query(default=None, description="ts cursor — return alerts older than this"),
    limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[AlertEvent]:
    q = select(AlertEvent).order_by(desc(AlertEvent.ts)).limit(limit)
    if name:
        q = q.where(AlertEvent.name == name)
    if level:
        q = q.where(AlertEvent.level == level)
    if before is not None:
        q = q.where(AlertEvent.ts < before)
    return list((await session.exec(q)).all())
