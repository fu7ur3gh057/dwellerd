"""FastAPI factory for the Dwellerd web client.

The path prefix (default `/dwellerd`) makes the API self-contained behind
any reverse proxy or direct port access — same URL shape works in both
cases. Override via env DWELLERD_WEB_PREFIX before instantiating.

Routing under <prefix>:
    /api/*    → FastAPI handlers
    /ws/*     → Socket.IO ASGI app
    /health   → cheap healthcheck (json)
    /api/docs · /api/redoc · /api/openapi.json
    /*        → Next.js static export from client/out (if built)
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles

from web.apis import api_router
from web.lifetime import lifespan

log = logging.getLogger(__name__)

# Default to Next.js dev server (port 8677 — out of the way of the usual
# 3000/8080 collisions). In prod the bundle is served by FastAPI itself,
# so it's same-origin and CORS doesn't gate it.
_DEFAULT_CORS = ["http://localhost:8677", "http://127.0.0.1:8677"]

DEFAULT_PREFIX = "/dwellerd"

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CLIENT_OUT = _PROJECT_ROOT / "client" / "out"


def get_app(prefix: str | None = None) -> FastAPI:
    if prefix is None:
        prefix = os.environ.get("DWELLERD_WEB_PREFIX", DEFAULT_PREFIX)
    prefix = prefix.rstrip("/")  # "/dwellerd" or "" for no-prefix

    application = FastAPI(
        title="Dwellerd",
        version="0.1.0",
        docs_url=f"{prefix}/api/docs",
        redoc_url=f"{prefix}/api/redoc",
        openapi_url=f"{prefix}/api/openapi.json",
        lifespan=lifespan,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=_DEFAULT_CORS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.include_router(api_router, prefix=f"{prefix}/api")

    @application.get(f"{prefix}/health", include_in_schema=False)
    async def _health() -> dict[str, str]:
        return {"status": "ok"}

    # Socket.IO — mounted under <prefix>/ws/socket.io. Same JWT secret as
    # the REST API guards every namespace via AuthedNamespace.on_connect.
    from web.sockets.lifetime import init_socketio
    init_socketio(application, prefix=prefix, cors_origins=_DEFAULT_CORS)

    # Static SPA — registered LAST so /api/* and /ws/* keep priority.
    _mount_client(application, prefix)

    return application


def _mount_client(application: FastAPI, prefix: str) -> None:
    """Serve the Next.js static export under `prefix`. If the bundle
    isn't built yet, register a tiny placeholder so visitors get a clear
    instruction instead of a bare 404."""
    mount_path = prefix or "/"
    if _CLIENT_OUT.exists() and (_CLIENT_OUT / "index.html").exists():
        application.mount(
            mount_path,
            StaticFiles(directory=_CLIENT_OUT, html=True),
            name="client",
        )
        log.info("web client mounted at %s from %s", mount_path, _CLIENT_OUT)
        return

    log.warning(
        "web client not built — %s missing. "
        "Run `make client-build` to populate it.",
        _CLIENT_OUT,
    )

    @application.get(prefix + "/", include_in_schema=False)
    async def _client_placeholder() -> HTMLResponse:
        return HTMLResponse(
            f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Dwellerd · client not built</title>
<style>body{{font-family:system-ui;background:#0E0F12;color:#F4F4F5;
padding:48px;max-width:640px;margin:auto}}code{{background:#16181D;
padding:2px 6px;border-radius:4px;color:#FDBA74}}h1{{color:#F97316}}</style>
</head><body>
<h1>Dwellerd · client not built</h1>
<p>The Next.js bundle is missing at <code>{_CLIENT_OUT}</code>.</p>
<p>Build it with:</p>
<pre><code>cd client &amp;&amp; pnpm install &amp;&amp; pnpm build</code></pre>
<p>Or via Makefile:</p>
<pre><code>make client-install &amp;&amp; make client-build</code></pre>
<p>API is live at <a href="{prefix}/api/docs" style="color:#F97316">{prefix}/api/docs</a>.</p>
</body></html>""",
        )
