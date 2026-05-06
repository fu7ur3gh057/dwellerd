"""All Socket.IO namespaces in one file.

Each namespace gets a JWT-checked connect handler from `AuthedNamespace`.
Two of them (`/checks`, `/logs`) share an identical sub/unsub pattern —
client emits {key: value}, server enters/leaves room `<prefix>:<value>` —
which lives in `_RoomNamespace`. Adding a new namespace means appending
2-3 lines here, not creating a new module.

Server-emitted events:
  /alerts → alert:fired
  /checks → check:result, checks:tick
  /logs   → log:line, log:digest
  /system → system:tick
"""

import logging

from jwt import InvalidTokenError
from socketio import AsyncNamespace

from services.taskiq.broker import broker
from web.auth.sessions import lookup_session_status
from web.auth.tokens import decode_access_token

log = logging.getLogger(__name__)


class AuthedNamespace(AsyncNamespace):
    """JWT gate. Token comes from `auth.token` (Socket.IO standard) first,
    then from the `dw_access` cookie set by /api/auth/login.

    `ALLOWED_ROLES = None` (default) → any authenticated user passes.
    Set to a frozenset of role strings to restrict (e.g. {"admin"}).
    """

    ALLOWED_ROLES: frozenset[str] | None = None

    async def on_connect(
        self,
        sid: str,
        environ: dict,
        auth: dict | None = None,
    ) -> bool | None:
        secret = broker.state.data.get("web_jwt_secret")
        if not secret:
            log.warning("sio: refusing %s — auth not configured", self.namespace)
            return False

        token = self._extract_token(auth, environ)
        if not token:
            log.info("sio: refusing %s — no token (sid=%s)", self.namespace, sid)
            return False

        try:
            claims = decode_access_token(token, secret)
        except InvalidTokenError as e:
            log.info("sio: refusing %s — bad token (sid=%s): %s",
                     self.namespace, sid, e)
            return False

        # Validate the session id is still active server-side. Without this,
        # a logged-out / disabled user keeps streaming events for up to 30
        # min (the access token lifetime). The lookup is cached for 30s.
        try:
            session_id = int(claims.get("sid"))
        except (TypeError, ValueError):
            log.info("sio: refusing %s — malformed sid (ws sid=%s)", self.namespace, sid)
            return False

        sm = broker.state.data.get("db_session_maker")
        if sm is None:
            return False
        async with sm() as db:
            entry = await lookup_session_status(db, session_id)
        if entry is None or not entry.valid:
            log.info("sio: refusing %s — session revoked or user disabled (sid=%s)",
                     self.namespace, sid)
            return False

        # Sub-classes can opt into role-gating by overriding
        # ALLOWED_ROLES (None = any logged-in user).
        if self.ALLOWED_ROLES is not None:
            if entry.role not in self.ALLOWED_ROLES:
                log.info(
                    "sio: refusing %s — role %r not in %r (sid=%s)",
                    self.namespace, entry.role, self.ALLOWED_ROLES, sid,
                )
                return False

        await self.save_session(sid, {
            "username": entry.username,
            "role": entry.role,
            "user_id": entry.user_id,
            "session_id": session_id,
        })
        log.info("sio: %s connected to %s as %s (role=%s)",
                 sid, self.namespace, entry.username, entry.role)
        return None  # accept

    async def on_disconnect(self, sid: str) -> None:
        log.info("sio: %s disconnected from %s", sid, self.namespace)

    @staticmethod
    def _extract_token(auth: dict | None, environ: dict) -> str | None:
        if isinstance(auth, dict) and auth.get("token"):
            return str(auth["token"])
        cookies = environ.get("HTTP_COOKIE", "")
        for piece in cookies.split(";"):
            piece = piece.strip()
            if piece.startswith("dw_access="):
                return piece[len("dw_access="):]
        return None


class _RoomNamespace(AuthedNamespace):
    """Sub/unsub pattern: client emits `{KEY: value}` → server moves the
    socket into room `<PREFIX>:<value>`. Subclasses set KEY and PREFIX."""

    KEY: str = "name"
    PREFIX: str = ""

    async def on_subscribe(self, sid: str, data: dict) -> dict:
        v = (data or {}).get(self.KEY) if isinstance(data, dict) else None
        if not v:
            return {"ok": False, "error": f"missing {self.KEY}"}
        room = f"{self.PREFIX}:{v}"
        await self.enter_room(sid, room)
        return {"ok": True, "room": room}

    async def on_unsubscribe(self, sid: str, data: dict) -> dict:
        v = (data or {}).get(self.KEY) if isinstance(data, dict) else None
        if v:
            await self.leave_room(sid, f"{self.PREFIX}:{v}")
        return {"ok": True}


# ── concrete namespaces ──────────────────────────────────────────────


class AlertsNamespace(AuthedNamespace):
    """Read-only feed of `alert:fired`."""


class SystemNamespace(AuthedNamespace):
    """Periodic `system:tick` snapshot pushed from the ticker."""


class DockerNamespace(AuthedNamespace):
    """Periodic `docker:tick` snapshot + live `docker:event` stream."""

    ALLOWED_ROLES = frozenset({"admin"})


class ChecksNamespace(_RoomNamespace):
    """Live `check:result` per run + periodic `checks:tick` snapshot."""

    KEY, PREFIX = "name", "check"


class LogsNamespace(_RoomNamespace):
    """Stream of `log:line` events, optionally filtered to a per-source room."""

    KEY, PREFIX = "source", "src"


__all__ = [
    "AuthedNamespace",
    "AlertsNamespace",
    "ChecksNamespace",
    "DockerNamespace",
    "LogsNamespace",
    "SystemNamespace",
]
