"""Refresh-token / session management.

The auth flow:

    POST /api/auth/login
      → User.password verified
      → INSERT Session(refresh_hash=sha256(plain), expires_at=now+30d)
      → encode access JWT with sid=Session.id, exp=now+30min
      → return {access_token, refresh_token (cookie)}

    POST /api/auth/refresh
      → look up Session by sha256(cookie); reject if not found / revoked / expired
      → mark Session.revoked_at = now
      → INSERT new Session, encode new access JWT
      → return new pair (rotation)

    POST /api/auth/logout
      → mark Session.revoked_at = now (the one whose sid is in the access JWT)

    Any protected request
      → decode access JWT (validates iss/aud/jti/nbf/exp)
      → look up sid in Session table; reject if revoked
      → look up User by Session.user_id; reject if !is_active

The two table lookups happen on every request, which would be expensive
on a busy API. We cache the (sid → ok?) decision for 30 seconds in-process.
30s is short enough that revocation kicks in promptly, long enough that a
sustained dashboard refresh doesn't hammer SQLite.

State is in-process — fine for the typical single-binary deploy.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from db.models import Session as DbSession
from db.models import User
from web.auth.tokens import hash_refresh_token

log = logging.getLogger(__name__)


# ── cached "is this sid still valid?" lookup ─────────────────────────────


_AUTH_CACHE_TTL = 30.0  # seconds


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    user_id: int
    username: str
    role: str
    # `valid=False` means the lookup found a revoked / expired / inactive
    # combination. We cache negative results too so a revoked token can't
    # spam the DB.
    valid: bool


_status_cache: dict[int, _CacheEntry] = {}


def invalidate_sid(sid: int) -> None:
    """Drop a sid from the in-process cache. Call after revocation so the
    next request hits DB and re-confirms the revoked state."""
    _status_cache.pop(sid, None)


async def lookup_session_status(
    db: AsyncSession, sid: int,
) -> Optional[_CacheEntry]:
    """Return the cached status for a session id. None if the sid doesn't
    exist; .valid=False if revoked / expired / user inactive."""
    now = time.time()
    cached = _status_cache.get(sid)
    if cached is not None and cached.expires_at > now:
        return cached

    row = await db.get(DbSession, sid)
    if row is None:
        return None

    user = await db.get(User, row.user_id)
    if user is None:
        return None

    valid = (
        row.revoked_at is None
        and row.expires_at > now
        and user.is_active
    )
    entry = _CacheEntry(
        expires_at=now + _AUTH_CACHE_TTL,
        user_id=row.user_id,
        username=user.username,
        role=user.role,
        valid=valid,
    )
    _status_cache[sid] = entry
    return entry


# ── session lifecycle ─────────────────────────────────────────────────────


async def create_session(
    db: AsyncSession,
    *,
    user_id: int,
    refresh_plain: str,
    refresh_lifetime_seconds: int,
    ip: str | None,
    user_agent: str | None,
) -> DbSession:
    """Persist a new session row for a freshly issued refresh token. Returns
    the row so the caller can use `row.id` as the access JWT's sid claim."""
    now = time.time()
    row = DbSession(
        user_id=user_id,
        refresh_token_hash=hash_refresh_token(refresh_plain),
        issued_at=now,
        last_used_at=now,
        expires_at=now + refresh_lifetime_seconds,
        ip=ip,
        user_agent=user_agent,
        revoked_at=None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def find_active_by_refresh(
    db: AsyncSession, refresh_plain: str,
) -> DbSession | None:
    """Look up an active (non-revoked, non-expired) session by the plaintext
    refresh token. Returns None if not found / revoked / expired so the
    caller can treat all three the same way (401)."""
    h = hash_refresh_token(refresh_plain)
    row = (await db.exec(
        select(DbSession).where(DbSession.refresh_token_hash == h),
    )).first()
    if row is None:
        return None
    if row.revoked_at is not None or row.expires_at <= time.time():
        return None
    return row


async def revoke_session(db: AsyncSession, sid: int) -> bool:
    """Mark a session revoked. Returns True if it existed and was active.
    Idempotent — revoking an already-revoked session is a no-op."""
    row = await db.get(DbSession, sid)
    if row is None:
        return False
    if row.revoked_at is not None:
        invalidate_sid(sid)
        return False
    row.revoked_at = time.time()
    db.add(row)
    await db.commit()
    invalidate_sid(sid)
    return True


async def revoke_all_for_user(db: AsyncSession, user_id: int) -> int:
    """Bulk-revoke every active session for a user. Used on password change
    or admin-disable. Returns count revoked."""
    rows = (await db.exec(
        select(DbSession).where(
            DbSession.user_id == user_id,
            DbSession.revoked_at.is_(None),  # type: ignore[union-attr]
        ),
    )).all()
    now = time.time()
    for r in rows:
        r.revoked_at = now
        db.add(r)
        invalidate_sid(r.id)  # type: ignore[arg-type]
    if rows:
        await db.commit()
    return len(rows)


async def touch_session(db: AsyncSession, sid: int) -> None:
    """Update last_used_at on the active session. Cheap — used by the
    refresh endpoint to track liveness for the admin UI without rewriting
    the row's other fields."""
    row = await db.get(DbSession, sid)
    if row is None:
        return
    row.last_used_at = time.time()
    db.add(row)
    await db.commit()
