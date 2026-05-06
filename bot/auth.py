"""Bot auth — credential verification + persistent TG-user → Dwellerd-User
linkage via the `bot_sessions` table.

Reuses the daemon's password helpers so timing characteristics, bcrypt
cost factor, and the >128-char rejection match exactly. Don't re-implement
password compare here; that's a footgun.
"""
from __future__ import annotations

import time

from sqlmodel import select

from db.models import BotSession, User  # type: ignore
from web.auth.passwords import verify_password_constant_time  # type: ignore

from .db import db_session


def verify_credentials(username: str, password: str) -> User | None:
    """Lookup user by username, run constant-time bcrypt verify, return
    the row on success. None when user doesn't exist, is disabled, or
    password is wrong — all three paths take the same wall time so a TG
    attacker can't enumerate usernames by latency.

    Bumps `last_login_ts` on success.
    """
    username = (username or "").strip()
    with db_session() as s:
        row = s.exec(select(User).where(User.username == username)).first()
        if row is None or not row.is_active:
            # Run a real bcrypt compare against a dummy hash so timing is
            # identical to "user exists, wrong password". The helper handles
            # the None case internally.
            verify_password_constant_time(password, None)
            return None
        if not verify_password_constant_time(password, row.password_hash):
            return None

        row.last_login_ts = time.time()
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def start_session(tg_user_id: int, user: User) -> None:
    """Upsert the bot_sessions row binding `tg_user_id` to `user.id`.
    A subsequent /login from the same TG user replaces the row in place."""
    now = time.time()
    with db_session() as s:
        existing = s.get(BotSession, tg_user_id)
        if existing is not None:
            existing.user_id = user.id
            existing.started_at = now
            existing.last_seen_at = now
            s.add(existing)
        else:
            s.add(BotSession(
                tg_user_id=tg_user_id,
                user_id=user.id,
                started_at=now,
                last_seen_at=now,
            ))
        s.commit()


def revoke_session(tg_user_id: int) -> bool:
    """Delete the bot_sessions row. Returns True if a row was actually
    removed (user was logged in), False otherwise."""
    with db_session() as s:
        row = s.get(BotSession, tg_user_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def current_user(tg_user_id: int) -> User | None:
    """Return the User this TG id is logged in as, or None. Touches
    `last_seen_at` so an idle-timeout cron can prune stale sessions
    later if we ever want one."""
    now = time.time()
    with db_session() as s:
        sess = s.get(BotSession, tg_user_id)
        if sess is None:
            return None
        u = s.get(User, sess.user_id)
        if u is None or not u.is_active:
            # User was disabled/deleted server-side — kill the orphan row
            # so we don't keep hitting this path.
            s.delete(sess)
            s.commit()
            return None
        sess.last_seen_at = now
        s.add(sess)
        s.commit()
        s.refresh(u)
        return u
