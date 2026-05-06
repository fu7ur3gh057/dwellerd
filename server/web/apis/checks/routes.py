"""Checks API — list configured checks with current state, history per
check, and a manual-run endpoint that kicks the broker task."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import desc, select
from sqlmodel.ext.asyncio.session import AsyncSession

from db.deps import get_session
from db.models import CheckResult, CheckStateEntry
from services.taskiq.broker import broker
from services.taskiq.context import AppContext
from services.taskiq.deps import get_app_context
from web.apis.checks.schemas import CheckSummary, RunResponse

router = APIRouter(tags=["checks"])


@router.get("", response_model=list[CheckSummary])
async def list_checks(
    ctx: AppContext = Depends(lambda: broker.state.app_ctx),
    session: AsyncSession = Depends(get_session),
) -> list[CheckSummary]:
    state_rows = (await session.exec(select(CheckStateEntry))).all()
    state_by_name = {r.name: r for r in state_rows}

    summaries: list[CheckSummary] = []
    for cfg in ctx.config.checks:
        handler = ctx.checks_by_name.get(cfg.name)
        st = state_by_name.get(cfg.name)
        last_value = None
        last_detail = None
        if st is not None:
            last_result = (await session.exec(
                select(CheckResult)
                .where(CheckResult.name == cfg.name)
                .order_by(desc(CheckResult.ts))
                .limit(1),
            )).first()
            if last_result is not None:
                last_value = (last_result.metrics or {}).get("value")
                last_detail = last_result.detail
        summaries.append(CheckSummary(
            name=cfg.name,
            type=cfg.type,
            interval=getattr(handler, "interval", cfg.interval),
            level=st.level if st else None,
            last_run_ts=st.updated_at if st else None,
            last_value=last_value,
            last_detail=last_detail,
        ))
    return summaries


@router.get("/{name}/history", response_model=list[CheckResult])
async def history(
    name: str,
    since: float | None = Query(default=None, description="lower bound on ts (unix seconds, exclusive)"),
    before: float | None = Query(default=None, description="upper bound on ts (unix seconds, exclusive)"),
    limit: int = Query(default=200, ge=1, le=2000),
    session: AsyncSession = Depends(get_session),
) -> list[CheckResult]:
    q = select(CheckResult).where(CheckResult.name == name).order_by(desc(CheckResult.ts)).limit(limit)
    if since is not None:
        q = q.where(CheckResult.ts > since)
    if before is not None:
        q = q.where(CheckResult.ts < before)
    rows = (await session.exec(q)).all()
    # newest-first → return chronological for plotting
    return list(reversed(rows))


@router.post("/{name}/run", response_model=RunResponse)
async def run_now(
    name: str,
    ctx: AppContext = Depends(lambda: broker.state.app_ctx),
) -> RunResponse:
    if name not in ctx.checks_by_name:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown check")
    from tasks.checks import run_check
    asyncio.create_task(run_check.kiq(name))
    return RunResponse(name=name, queued=True)
