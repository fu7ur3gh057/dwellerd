"""Server-to-client event emit helper.

Tasks call `await emit("/alerts", "alert:fired", payload)` without caring
whether Socket.IO is actually mounted — when running worker-only (no
--web flag), this is a no-op because broker.state.sio_server is unset.
"""

import logging

from services.taskiq.broker import broker

log = logging.getLogger(__name__)


async def emit(
    namespace: str,
    event: str,
    data: dict,
    room: str | None = None,
) -> None:
    sio = broker.state.data.get("sio_server")
    if sio is None:
        return
    try:
        if room:
            await sio.emit(event, data, namespace=namespace, room=room)
        else:
            await sio.emit(event, data, namespace=namespace)
    except Exception:
        log.exception("sio emit failed: %s on %s", event, namespace)
