"""Config — read-only view of the loaded config.yaml with secrets masked."""

from dataclasses import asdict

from fastapi import APIRouter

from services.taskiq.broker import broker
from services.taskiq.context import AppContext

router = APIRouter(tags=["config"])

_MASK = "***"
_MASK_KEYS = {
    "bot_token", "chat_id", "password", "password_hash", "secret",
    "token", "dsn",
}


def _mask(obj):
    if isinstance(obj, dict):
        return {k: (_MASK if k in _MASK_KEYS and v else _mask(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask(v) for v in obj]
    return obj


@router.get("")
async def view_config() -> dict:
    ctx: AppContext = broker.state.app_ctx
    cfg = ctx.config
    raw = {
        "checks": [{"type": c.type, "name": c.name, "interval": c.interval, **c.options}
                   for c in cfg.checks],
        "notifiers": [{"type": n.type, **n.options} for n in cfg.notifiers],
        "report": cfg.report,
        "logs": cfg.logs,
        "web": cfg.web,
        "db": cfg.db,
    }
    return _mask(raw)
