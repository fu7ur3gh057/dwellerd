"""Socket.IO setup — register namespaces, mount the ASGI app on the
FastAPI instance under `<prefix>/ws`, attach the server to broker.state
so tasks can `emit()` without passing it around.

Path arithmetic:
    init_socketio(app, prefix="/dwellerd") → /dwellerd/ws/socket.io/...
    init_socketio(app, prefix="")          → /ws/socket.io/...

Note: Starlette's Mount sets `scope.root_path` but doesn't strip the
prefix from `scope.path` for sub-apps. python-socketio's ASGIApp matches
against the raw path, so we configure its `socketio_path` to include the
mount prefix; the Mount itself is what makes Starlette route requests to
us in the first place.
"""

import logging

from fastapi import FastAPI
from socketio import ASGIApp, AsyncServer

from services.taskiq.broker import broker
from web.sockets.namespaces import (
    AlertsNamespace,
    ChecksNamespace,
    DockerNamespace,
    LogsNamespace,
    SystemNamespace,
)
from web.sockets.terminal import TerminalNamespace

log = logging.getLogger(__name__)


def init_socketio(
    app: FastAPI,
    *,
    prefix: str = "",
    cors_origins: list[str] | None = None,
) -> AsyncServer:
    # Socket.IO does its own Origin check before the WebSocket upgrade and
    # uses an exact-match list — that means same-origin requests from the
    # served bundle (e.g. localhost:8765 hitting localhost:8765) get
    # rejected if the explicit list doesn't include them. We can't
    # enumerate every host the daemon might be reached on, so accept any
    # Origin here and lean on JWT auth in AuthedNamespace.on_connect for
    # actual access control.
    server = AsyncServer(
        async_mode="asgi",
        cors_allowed_origins="*",
    )
    server.register_namespace(AlertsNamespace("/alerts"))
    server.register_namespace(ChecksNamespace("/checks"))
    server.register_namespace(DockerNamespace("/docker"))
    server.register_namespace(LogsNamespace("/logs"))
    server.register_namespace(SystemNamespace("/system"))

    # /terminal namespace registration is gated by BOTH the env-level
    # kill-switch (DWELLERD_TERMINAL_DISABLED — set in init_auth as
    # `terminal_killed`) and `web.terminal.enabled`. The kill-switch wins.
    # When killed, the namespace is NOT registered at all so a determined
    # client can't even speak the protocol against it; socket.io will
    # respond "Invalid namespace" which we accept as the desired UX here.
    if broker.state.data.get("terminal_killed"):
        log.info("terminal: namespace NOT mounted (DWELLERD_TERMINAL_DISABLED)")
        ns_list = "/alerts /checks /docker /logs /system"
    else:
        ctx = broker.state.data.get("app_ctx")
        cfg = ctx.config if ctx is not None else None
        terminal_cfg = ((getattr(cfg, "web", None) or {}).get("terminal") or {})
        server.register_namespace(TerminalNamespace("/terminal"))
        if terminal_cfg.get("enabled"):
            log.info("terminal: enabled, namespace /terminal mounted")
        else:
            log.info("terminal: disabled (web.terminal.enabled != true)")
        ns_list = "/alerts /checks /docker /logs /system /terminal"

    mount_path = f"{prefix}/ws" if prefix else "/ws"
    # ASGIApp checks `scope.path.startswith(f"/{socketio_path}/")` — so we
    # bake the full URL prefix in here.
    sio_path = f"{mount_path.lstrip('/')}/socket.io"
    sio_app = ASGIApp(server, socketio_path=sio_path)
    app.mount(mount_path, sio_app)

    broker.state.sio_server = server
    log.info(
        "socket.io mounted at /%s — namespaces: %s",
        sio_path,
        ns_list,
    )
    return server


async def shutdown_socketio() -> None:
    """AsyncServer doesn't need explicit shutdown; the helper is here for
    symmetry with init_*. Drop sio_server from state so a stale handle
    doesn't end up serving emits after shutdown."""
    broker.state.data.pop("sio_server", None)
