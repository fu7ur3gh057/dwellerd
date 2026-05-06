"""FastAPI lifespan: place to attach process-wide resources.

Owns the WS ticker loops (periodic snapshot pushes — see
`web.sockets.tickers.TICKS`) and the `docker events` consumer (real-time
container start/die/restart pushes — see `web.apis.docker.events`).
Both are background tasks that get cancelled on shutdown.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from web.apis.docker.events import run_docker_events
from web.sockets.tickers import run_tickers

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    bg_tasks = [
        asyncio.create_task(run_tickers(),       name="ws-tickers"),
        asyncio.create_task(run_docker_events(), name="docker-events"),
    ]
    try:
        yield
    finally:
        for t in bg_tasks:
            t.cancel()
        await asyncio.gather(*bg_tasks, return_exceptions=True)
