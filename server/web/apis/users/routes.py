"""User-management API. Admin-only.

Replaces the single-admin model that lived in `web.user.{username,
password_hash}` in config.yaml. The first user is seeded from YAML on
the daemon's first boot (see `db.migrations.import_yaml_into_db`); from
then on, accounts are CRUD'd through this endpoint or directly in the
SQLite `users` table.

Guardrails:
  - admin role required to list / create / delete / change someone
    else's password
  - any user can change their OWN password (`PATCH /me/password`)
  - cannot delete the last active admin (would lock everyone out)
  - cannot deactivate yourself (same risk)
"""

import time

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from db.deps import get_session
from db.models import User
from web.apis.deps import require_auth
from web.auth.passwords import hash_password, verify_password

router = APIRouter(tags=["users"])


# ── schemas ───────────────────────────────────────────────────────────


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool
    created_at: float
    last_login_ts: float | None


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    role: str = "admin"
    is_active: bool = True


class UpdateUserRequest(BaseModel):
    role: str | None = None
    is_active: bool | None = None


class ChangePasswordRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=256)
    # Required when the user is changing their OWN password — re-auth
    # so a stolen cookie can't silently rotate the admin's password.
    current_password: str | None = None


# ── helpers ───────────────────────────────────────────────────────────


def _require_admin(claims: dict) -> None:
    if claims.get("role") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="admin role required")


def _to_out(u: User) -> UserOut:
    return UserOut(
        id=u.id or 0,
        username=u.username,
        role=u.role,
        is_active=u.is_active,
        created_at=u.created_at,
        last_login_ts=u.last_login_ts,
    )


async def _count_active_admins(session: AsyncSession) -> int:
    rows = (await session.exec(
        select(User).where(User.role == "admin", User.is_active == True),  # noqa: E712
    )).all()
    return len(rows)


# ── endpoints ─────────────────────────────────────────────────────────


@router.get("", response_model=list[UserOut])
async def list_users(
    claims: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> list[UserOut]:
    _require_admin(claims)
    rows = (await session.exec(select(User).order_by(User.id))).all()
    return [_to_out(u) for u in rows]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: CreateUserRequest,
    claims: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    _require_admin(claims)
    if body.role not in {"admin", "staff", "viewer"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="role must be admin, staff, or viewer")
    existing = (await session.exec(
        select(User).where(User.username == body.username),
    )).first()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="username already taken")
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
        is_active=body.is_active,
        created_at=time.time(),
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return _to_out(user)


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    body: UpdateUserRequest,
    claims: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    _require_admin(claims)
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user not found")

    me = claims.get("sub", "")
    if body.is_active is False and user.username == me:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail="you can't deactivate yourself")

    if body.role is not None:
        if body.role not in {"admin", "staff", "viewer"}:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="role must be admin, staff, or viewer")
        # Block demoting the last admin.
        if user.role == "admin" and body.role != "admin":
            count = await _count_active_admins(session)
            if count <= 1:
                raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                    detail="cannot demote the last active admin")
        user.role = body.role

    if body.is_active is not None:
        if user.is_active and not body.is_active and user.role == "admin":
            count = await _count_active_admins(session)
            if count <= 1:
                raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                    detail="cannot disable the last active admin")
        user.is_active = body.is_active

    session.add(user)
    await session.commit()
    await session.refresh(user)
    return _to_out(user)


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    claims: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _require_admin(claims)
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user not found")
    if user.username == claims.get("sub", ""):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail="you can't delete yourself")
    if user.role == "admin" and user.is_active:
        count = await _count_active_admins(session)
        if count <= 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                detail="cannot delete the last active admin")
    await session.delete(user)
    await session.commit()
    return {"ok": True, "id": user_id}


@router.patch("/{user_id}/password")
async def admin_change_password(
    user_id: int,
    body: ChangePasswordRequest,
    claims: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Admin changes someone else's password. No `current_password`
    needed since admin authority is what authorises the change."""
    _require_admin(claims)
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user not found")
    user.password_hash = hash_password(body.new_password)
    session.add(user)
    await session.commit()
    return {"ok": True}


@router.patch("/me/password")
async def self_change_password(
    body: ChangePasswordRequest,
    claims: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Self-service password change — requires re-auth via the user's
    current password. Works for admins and viewers alike."""
    me = claims.get("sub", "")
    user = (await session.exec(
        select(User).where(User.username == me),
    )).first()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user not found")
    if not body.current_password or not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="current password incorrect")
    user.password_hash = hash_password(body.new_password)
    session.add(user)
    await session.commit()
    return {"ok": True}
