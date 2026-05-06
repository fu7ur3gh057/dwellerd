"""Declarative periodic emitters.

Add a new periodic push by appending one `Tick(...)` to `TICKS`. Each
fetcher returns a JSON-serializable dict; the runner emits it on the
configured namespace at the configured interval.

Tickers run inside the FastAPI lifespan — they only matter when the web
client is up. Without subscribers Socket.IO drops the emit cheaply, so
there's no point gating on subscriber count manually.
"""

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import psutil
from sqlmodel import desc, select

from db.models import CheckResult, CheckStateEntry
from services.taskiq.broker import broker
from web.apis.docker.routes import collect_docker_snapshot

log = logging.getLogger(__name__)

_GB = 1024 ** 3


# ── snapshot fetchers ────────────────────────────────────────────────


def _uptime_seconds() -> int:
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except OSError:
        return int(time.time() - psutil.boot_time())


def _watched_paths() -> list[str]:
    """Disks to surface in the snapshot. Mirrors the REST handler default —
    derive from `report.host.disks`, fall back to "/"."""
    ctx = broker.state.data.get("app_ctx")
    if ctx is None:
        return ["/"]
    host = (ctx.config.report or {}).get("host") or {}
    disks = host.get("disks")
    if isinstance(disks, dict):
        disks = disks.get("paths")
    return list(disks) if disks else ["/"]


async def collect_system_snapshot() -> dict:
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)

    disks: list[dict] = []
    for p in _watched_paths():
        try:
            u = shutil.disk_usage(p)
        except OSError:
            continue
        disks.append({
            "path": p,
            "total_gb": round(u.total / _GB, 2),
            "used_gb":  round(u.used / _GB, 2),
            "free_gb":  round(u.free / _GB, 2),
            "pct":      round(u.used / u.total * 100, 1),
        })

    return {
        "cpu_pct":         psutil.cpu_percent(interval=None),
        "memory_pct":      vm.percent,
        "memory_used_gb":  round(vm.used / _GB, 2),
        "memory_total_gb": round(vm.total / _GB, 2),
        "swap_pct":        swap.percent,
        "load_1m":  load[0],
        "load_5m":  load[1],
        "load_15m": load[2],
        "uptime_seconds": _uptime_seconds(),
        "disks": disks,
        "ts": time.time(),
    }


async def collect_checks_snapshot() -> dict:
    """Mirrors `GET /api/checks` — list of CheckSummary dicts."""
    ctx = broker.state.data.get("app_ctx")
    sm = broker.state.data.get("db_session_maker")
    if ctx is None or sm is None:
        return {"checks": [], "ts": time.time()}

    async with sm() as session:
        states = (await session.exec(select(CheckStateEntry))).all()
        state_by_name = {r.name: r for r in states}

        out = []
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
            out.append({
                "name": cfg.name,
                "type": cfg.type,
                "interval": getattr(handler, "interval", cfg.interval),
                "level": st.level if st else None,
                "last_run_ts": st.updated_at if st else None,
                "last_value": last_value,
                "last_detail": last_detail,
            })

    return {"checks": out, "ts": time.time()}


# ── tick declarations ────────────────────────────────────────────────


@dataclass
class Tick:
    namespace: str                                  # "/system"
    event: str                                      # "system:tick"
    interval: float                                 # seconds (min 5)
    fetch: Callable[[], Awaitable[dict]]


# Keep this list short. Adding a new one = three lines, no new file.
TICKS: list[Tick] = [
    Tick("/system", "system:tick",  5.0,  collect_system_snapshot),
    Tick("/checks", "checks:tick", 15.0, collect_checks_snapshot),
    Tick("/docker", "docker:tick", 10.0, collect_docker_snapshot),
]


# ── runner ───────────────────────────────────────────────────────────


async def run_tickers() -> None:
    """Spawn one loop per Tick. Cancellation propagates out to the
    lifespan that owns this coroutine."""
    if not TICKS:
        return
    log.info(
        "tickers: %d periodic emitters: %s",
        len(TICKS),
        ", ".join(f"{t.event}@{t.interval:.0f}s" for t in TICKS),
    )
    await asyncio.gather(*[_loop(t) for t in TICKS])


async def _loop(tick: Tick) -> None:
    interval = max(5.0, float(tick.interval))
    from web.sockets import emit
    while True:
        try:
            data = await tick.fetch()
            await emit(tick.namespace, tick.event, data)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ticker %s failed", tick.event)
        await asyncio.sleep(interval)
