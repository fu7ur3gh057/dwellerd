"""Public status endpoint — no auth required.

Used by health checks and the SPA's "do I need to log in?" probe.
"""
from fastapi import APIRouter

router = APIRouter(tags=["status"])


@router.get("/status")
async def get_status() -> dict:
    return {
        "service": "dwellerd",
        "version": "0.1.0",
    }
