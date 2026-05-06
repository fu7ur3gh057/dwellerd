"""Common API dependencies — JWT auth gate.

Layers of validation `require_auth` runs (in order, fail-fast):

  1. Extract the access token from `Authorization: Bearer <jwt>` header
     OR the `dw_access` httpOnly cookie set by /api/auth/login.
  2. Decode + verify the JWT — signature, iss, aud, jti/nbf/exp/sub claims.
  3. Look up the session row by the `sid` claim. Reject if revoked,
     expired, or the linked user is disabled. (Cached in-process for
     30s — see web.auth.sessions.lookup_session_status.)

Returns a flat dict of the things downstream handlers care about so they
don't need to reach into broker.state again.
"""
from __future__ import annotations

from fastapi import Cookie, Depends, Header, HTTPException, status
from jwt import (
    ExpiredSignatureError,
    ImmatureSignatureError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidSignatureError,
    InvalidTokenError,
    MissingRequiredClaimError,
)
from sqlmodel.ext.asyncio.session import AsyncSession

from db.deps import get_session
from services.taskiq.broker import broker
from web.auth.sessions import lookup_session_status
from web.auth.tokens import decode_access_token


COOKIE_ACCESS = "dw_access"
COOKIE_REFRESH = "dw_refresh"


def _extract_token(authorization: str | None, cookie: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        if token:
            return token
    if cookie:
        return cookie
    return None


async def require_auth(
    authorization: str | None = Header(default=None),
    dw_access: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_session),
) -> dict:
    secret: str | None = broker.state.data.get("web_jwt_secret")
    if not secret:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="auth not configured",
        )

    raw = _extract_token(authorization, dw_access)
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="missing token")

    # Catch the specific subclasses first for clearer 401 reasons; fall
    # back to the parent for everything else (malformed, bad signature,
    # algorithm confusion).
    try:
        claims = decode_access_token(raw, secret)
    except ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="token expired")
    except ImmatureSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="token not yet valid")
    except InvalidIssuerError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid issuer")
    except InvalidAudienceError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid audience")
    except MissingRequiredClaimError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="malformed token")
    except InvalidSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid signature")
    except InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid token")

    # `sid` ties the JWT to a server-side session row that can be revoked
    # mid-life. Without this lookup, logout / disable-user wouldn't take
    # effect until the JWT's natural expiry.
    sid_raw = claims.get("sid")
    try:
        sid = int(sid_raw)
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="malformed token (sid)")

    status_entry = await lookup_session_status(db, sid)
    if status_entry is None or not status_entry.valid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="session revoked or user disabled")

    # Hand handlers a single flat dict — no broker.state digging downstream.
    return {
        "sub": status_entry.username,
        "role": status_entry.role,
        "sid": sid,
        "user_id": status_entry.user_id,
        "exp": int(claims.get("exp", 0)),
        "jti": claims.get("jti"),
    }


async def require_admin(claims: dict = Depends(require_auth)) -> dict:
    """Stricter gate — only `role: admin` claims pass. Use on routers that
    touch privileged surface (terminal shell, docker actions, user
    management, runtime settings)."""
    if claims.get("role") != "admin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="admin role required",
        )
    return claims
