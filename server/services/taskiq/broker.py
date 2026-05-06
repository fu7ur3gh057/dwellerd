"""Module-level TaskIQ broker.

Defaults to InMemoryBroker — single-process, no external dependencies. Tasks
kicked via `task.kiq(...)` execute on the same event loop. Wrap in
`asyncio.create_task(...)` for fire-and-forget.

Override via env: DWELLERD_BROKER_URL=redis://host:port/0 (requires
taskiq-redis; not installed by default).
"""

import os

from taskiq import AsyncBroker, InMemoryBroker


def _build() -> AsyncBroker:
    url = os.environ.get("DWELLERD_BROKER_URL", "")
    if url.startswith("redis://") or url.startswith("rediss://"):
        try:
            from taskiq_redis import ListQueueBroker
        except ImportError as e:
            raise RuntimeError(
                "DWELLERD_BROKER_URL points at Redis but taskiq-redis is not "
                "installed. Run: pip install taskiq-redis"
            ) from e
        return ListQueueBroker(url)
    return InMemoryBroker()


broker = _build()
