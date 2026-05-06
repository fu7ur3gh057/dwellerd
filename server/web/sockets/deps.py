"""FastAPI deps for the Socket.IO server. Same pattern as services/db/deps:
the AsyncServer lives on broker.state so handlers can emit through the
same instance the namespaces are bound to."""

from socketio import AsyncServer

from services.taskiq.broker import broker


def get_sio_server() -> AsyncServer:
    """Returns the running AsyncServer or raises if Socket.IO isn't
    initialized (which happens in worker-only mode without --web)."""
    sio = broker.state.data.get("sio_server")
    if sio is None:
        raise RuntimeError("socket.io not initialized")
    return sio
