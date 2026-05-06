"""Auth endpoints — login, refresh, logout, me.

Hardened-vs-Blackbox checklist:

  - Per-IP throttle (kept) AND per-username throttle (new) — defeats the
    "many IPs, one target user" pivot.
  - Constant-time bcrypt verify (new) — `verify_password_constant_time`
    runs a real bcrypt check even when the user lookup returned None,
    so timing can't distinguish "user exists" from "user doesn't".
  - Single generic 401 across all failure modes (kept).
  - Refresh-token rotation (new) — every refresh issues a new pair and
    revokes the old. A leaked refresh becomes useless the moment the
    real user does anything.
  - Server-side session revocation (new) — logout / disable-user flips
    `sessions.revoked_at`, the in-process cache TTL keeps the rejection
    consistent within 30s.
  - Cookie hardening (new) — `Secure` auto-flipped by behind-TLS env,
    `SameSite=Strict` default, refresh cookie scoped to the auth path so
    it isn't sent to every request.
"""
from __future__ import annotations

import logging
import os
import time

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from db.deps import get_session
from db.models import User
from services.taskiq.broker import broker
from web.apis.auth.schemas import LoginRequest, MeResponse, RefreshResponse, TokenResponse
from web.apis.deps import COOKIE_ACCESS, COOKIE_REFRESH, require_auth
from web.auth.passwords import verify_password_constant_time
from web.auth.sessions import (
    create_session,
    find_active_by_refresh,
    revoke_session,
    touch_session,
)
from web.auth.tokens import encode_access_token, generate_refresh_token

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

# Refresh cookie is scoped to the auth path so it isn't sent on every API
# call. Access cookie covers "/" because the SPA uses it for all routes.
_REFRESH_COOKIE_PATH = "/api/auth"


# ── login throttle (in-process) ───────────────────────────────────────────


_FAIL_WINDOW_SEC = 15 * 60
_FAIL_THRESHOLD = 5
_BACKOFF_BASE_SEC = 60
_BACKOFF_MAX_FACTOR = 64
_GC_THRESHOLD = 1024

# Two parallel state dicts: per-IP and per-username. The per-username
# throttle defeats the "many proxies, one target user" pivot the per-IP
# table can't see.
_login_fail_ip: dict[str, dict] = {}
_login_fail_user: dict[str, dict] = {}


def _client_ip(request: Request) -> str:
    """Caller-IP for throttling. Trust X-Forwarded-For only when explicitly
    enabled via env (DWELLERD_TRUST_PROXY=1); otherwise the direct peer
    wins. Without the gate, an attacker spoofing the header would bypass
    the throttle from a single source."""
    if os.environ.get("DWELLERD_TRUST_PROXY", "").lower() in ("1", "true", "yes", "on"):
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            # First entry in XFF chain is the original client.
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_throttle(state: dict[str, dict], key: str, label: str) -> None:
    """Raise 429 if `key` is currently in back-off. Also rotates an
    expired sliding window so old failures don't haunt new attempts."""
    now = time.time()
    s = state.get(key)
    if s is None:
        return
    if now < s["blocked_until"]:
        retry = max(1, int(s["blocked_until"] - now))
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"too many failed logins; retry in {retry}s",
            headers={"Retry-After": str(retry)},
        )
    if now - s["last_failure"] > _FAIL_WINDOW_SEC:
        state.pop(key, None)


def _record_failure(state: dict[str, dict], key: str, label: str) -> None:
    now = time.time()
    s = state.setdefault(
        key, {"failures": 0, "blocked_until": 0.0, "last_failure": 0.0},
    )
    if now - s["last_failure"] > _FAIL_WINDOW_SEC:
        s["failures"] = 0
    s["failures"] += 1
    s["last_failure"] = now
    if s["failures"] >= _FAIL_THRESHOLD:
        over = s["failures"] - _FAIL_THRESHOLD
        factor = min(2 ** over, _BACKOFF_MAX_FACTOR)
        s["blocked_until"] = now + _BACKOFF_BASE_SEC * factor
        log.warning(
            "auth: throttling %s=%s after %d failed logins (next try in %ds)",
            label, key, s["failures"], int(s["blocked_until"] - now),
        )

    # Bound the dict.
    if len(state) > _GC_THRESHOLD:
        cutoff = now - _FAIL_WINDOW_SEC * 2
        stale = [k for k, v in state.items() if v["last_failure"] < cutoff]
        for k in stale:
            state.pop(k, None)


def _record_success(ip: str, username: str) -> None:
    _login_fail_ip.pop(ip, None)
    _login_fail_user.pop(username, None)


# ── cookies ──────────────────────────────────────────────────────────────


def _set_cookies(response: Response, access: str, refresh: str) -> None:
    secure = bool(broker.state.data.get("web_cookie_secure", False))
    samesite = broker.state.data.get("web_cookie_samesite", "strict")
    access_ttl = int(broker.state.data.get("web_jwt_access_ttl", 30 * 60))
    refresh_ttl = int(broker.state.data.get("web_jwt_refresh_ttl", 30 * 24 * 3600))

    response.set_cookie(
        COOKIE_ACCESS, access,
        httponly=True, samesite=samesite, max_age=access_ttl,
        secure=secure, path="/",
    )
    response.set_cookie(
        COOKIE_REFRESH, refresh,
        httponly=True, samesite=samesite, max_age=refresh_ttl,
        secure=secure, path=_REFRESH_COOKIE_PATH,
    )


def _clear_cookies(response: Response) -> None:
    samesite = broker.state.data.get("web_cookie_samesite", "strict")
    secure = bool(broker.state.data.get("web_cookie_secure", False))
    response.delete_cookie(COOKIE_ACCESS, path="/", samesite=samesite, secure=secure, httponly=True)
    response.delete_cookie(COOKIE_REFRESH, path=_REFRESH_COOKIE_PATH, samesite=samesite, secure=secure, httponly=True)


# ── routes ───────────────────────────────────────────────────────────────


@router.post("/login", response_model=TokenResponse)
async def login(
    creds: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> TokenResponse:
    secret = broker.state.data.get("web_jwt_secret")
    if not secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="auth not configured")

    ip = _client_ip(request)
    _check_throttle(_login_fail_ip, ip, "ip")
    _check_throttle(_login_fail_user, creds.username, "user")

    # Look up by username, run constant-time bcrypt verify regardless of
    # whether the row exists. One generic 401 for missing / inactive / bad
    # password — never leak which.
    row = (await db.exec(select(User).where(User.username == creds.username))).first()
    stored_hash = row.password_hash if (row is not None and row.is_active) else None
    ok = verify_password_constant_time(creds.password, stored_hash)
    if not ok or row is None or not row.is_active:
        _record_failure(_login_fail_ip, ip, "ip")
        _record_failure(_login_fail_user, creds.username, "user")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    # Stamp the login so admins can see who's actually using the UI.
    row.last_login_ts = time.time()
    db.add(row)
    await db.commit()

    _record_success(ip, creds.username)

    # Issue refresh first (DB row), then access JWT carrying its sid.
    access_ttl = int(broker.state.data["web_jwt_access_ttl"])
    refresh_ttl = int(broker.state.data["web_jwt_refresh_ttl"])
    refresh_plain = generate_refresh_token()
    session_row = await create_session(
        db,
        user_id=row.id,  # type: ignore[arg-type]
        refresh_plain=refresh_plain,
        refresh_lifetime_seconds=refresh_ttl,
        ip=ip,
        user_agent=request.headers.get("user-agent", "")[:255] or None,
    )
    access_token, _jti = encode_access_token(
        sub=row.username,
        role=row.role,
        sid=str(session_row.id),
        secret=secret,
        expiry_seconds=access_ttl,
    )

    _set_cookies(response, access_token, refresh_plain)
    return TokenResponse(
        access_token=access_token,
        expires_in=access_ttl,
        username=row.username,
        role=row.role,
    )


@router.post("/refresh", response_model=RefreshResponse)
async def refresh(
    request: Request,
    response: Response,
    dw_refresh: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_session),
) -> RefreshResponse:
    secret = broker.state.data.get("web_jwt_secret")
    if not secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="auth not configured")

    if not dw_refresh:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="missing refresh")

    old_session = await find_active_by_refresh(db, dw_refresh)
    if old_session is None:
        # Either never existed, or revoked, or expired. Treat all the same.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid refresh")

    user = await db.get(User, old_session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="user disabled")

    # Rotate: revoke old, issue new pair. Race-safe enough for SQLite —
    # if two refresh requests arrive at the same time, one wins, the
    # other gets "invalid refresh" because the row is now revoked.
    await revoke_session(db, old_session.id)  # type: ignore[arg-type]

    access_ttl = int(broker.state.data["web_jwt_access_ttl"])
    refresh_ttl = int(broker.state.data["web_jwt_refresh_ttl"])
    new_refresh_plain = generate_refresh_token()
    new_session = await create_session(
        db,
        user_id=user.id,  # type: ignore[arg-type]
        refresh_plain=new_refresh_plain,
        refresh_lifetime_seconds=refresh_ttl,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent", "")[:255] or None,
    )
    await touch_session(db, new_session.id)  # type: ignore[arg-type]

    new_access, _jti = encode_access_token(
        sub=user.username,
        role=user.role,
        sid=str(new_session.id),
        secret=secret,
        expiry_seconds=access_ttl,
    )

    _set_cookies(response, new_access, new_refresh_plain)
    return RefreshResponse(access_token=new_access, expires_in=access_ttl)


@router.post("/logout")
async def logout(
    response: Response,
    claims: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
) -> dict:
    # Revoke the server-side session row regardless of whether the cookie
    # makes it to the response (e.g. SPA already cleared its in-memory
    # access JWT). The next request with a leaked cookie will fail the
    # sessions lookup.
    await revoke_session(db, int(claims["sid"]))
    _clear_cookies(response)
    return {"ok": True}


@router.get("/me", response_model=MeResponse)
async def me(claims: dict = Depends(require_auth)) -> MeResponse:
    return MeResponse(
        username=claims["sub"],
        role=claims.get("role", "admin"),
        user_id=int(claims["user_id"]),
        expires_at=int(claims.get("exp", 0)),
        sid=int(claims["sid"]),
    )
