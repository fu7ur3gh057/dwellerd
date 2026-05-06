"""Notifiers — list configured channels and fire a test alert."""

from fastapi import APIRouter, Depends, HTTPException, status

from core.notifiers import Alert
from services.taskiq.broker import broker
from services.taskiq.context import AppContext
from web.apis.notifiers.schemas import NotifierInfo, TestRequest

router = APIRouter(tags=["notifiers"])


@router.get("", response_model=list[NotifierInfo])
async def list_notifiers() -> list[NotifierInfo]:
    ctx: AppContext = broker.state.app_ctx
    return [
        NotifierInfo(type=n_type, lang=getattr(n, "lang", None))
        for n_type, n in ctx.notifiers_by_type.items()
    ]


@router.post("/{type}/test")
async def test_notifier(type: str, body: TestRequest) -> dict:
    ctx: AppContext = broker.state.app_ctx
    n = ctx.notifiers_by_type.get(type)
    if n is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown notifier type")
    if body.level not in ("ok", "warn", "crit"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="level must be ok|warn|crit")

    await n.send(Alert(
        check="manual-test",
        level=body.level,
        detail=body.message,
        kind="",
    ))
    return {"ok": True, "type": type}
