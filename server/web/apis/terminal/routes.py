"""Terminal lock — second-step PAM auth against system users.

Flow:

  1. Browser is already logged in as the web admin (dw_access JWT).
     Required to reach this endpoint at all.
  2. POST /api/terminal/unlock with {username, password} — verified
     against the host's PAM stack (`python-pam` → libpam). Any system
     account with a valid password works; optionally restricted by
     `web.terminal.allow_users` whitelist.
  3. On success the server returns a short-lived token whose `sub` is
     the unix username. The /terminal WS namespace forks a PTY and
     drops privileges to that user (setuid/setgid) before exec'ing the
     shell — the resulting session is naturally bounded by OS perms.

Failed attempts are written to `terminal_audit` (kind=`auth_failed`) so
brute-force shows up alongside successful sessions.
"""

import logging
import pwd
import time

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from services.taskiq.broker import broker
from web.apis.deps import require_auth
from web.auth.tokens import encode_terminal_token

log = logging.getLogger(__name__)
router = APIRouter(tags=["terminal"])


class UnlockRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UnlockResponse(BaseModel):
    token: str
    expires_in: int
    username: str


@router.get("/status")
async def terminal_status(claims: dict = Depends(require_auth)) -> dict:
    """Whether the in-browser terminal is enabled. Public to any logged-
    in user so the UI knows whether to show the lock screen or the
    "disabled" placeholder. Doesn't leak the allow-list.

    The kill-switch (DWELLERD_TERMINAL_DISABLED env var) wins over config —
    when set, this always reports disabled regardless of `web.terminal.enabled`.
    The UI's left-rail uses this to hide the Terminal nav item entirely."""
    if broker.state.data.get("terminal_killed"):
        return {"enabled": False, "ttl_seconds": 0}
    cfg = _terminal_cfg()
    return {
        "enabled": bool(cfg.get("enabled")),
        "ttl_seconds": int(cfg.get("token_ttl") or 1800),
    }


@router.post("/unlock", response_model=UnlockResponse)
async def unlock(req: UnlockRequest, claims: dict = Depends(require_auth)) -> UnlockResponse:
    """PAM-authenticate the supplied username + password. On success
    returns a short-lived token whose only valid use is connecting to
    the `/terminal` WS namespace as that unix user."""
    cfg = _terminal_cfg()
    secret = broker.state.data.get("web_jwt_secret")
    web_user = (claims or {}).get("sub", "")

    if broker.state.data.get("terminal_killed"):
        await _audit_failed(web_user, req.username, "terminal force-disabled (env)")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="terminal is disabled by operator")

    if not cfg.get("enabled") or not secret:
        await _audit_failed(web_user, req.username, "terminal not enabled")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="terminal is not enabled in config.web.terminal")

    # Whitelist check (optional). Empty/unset = allow any system user.
    allow = cfg.get("allow_users") or []
    if allow and req.username not in allow:
        await _audit_failed(web_user, req.username, "user not in allow_users")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    # PAM check.
    if not _pam_authenticate(req.username, req.password):
        await _audit_failed(web_user, req.username, "PAM authentication failed")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    # Confirm the user actually exists in /etc/passwd (PAM might say yes
    # for service accounts that have no home/shell — we need a real one).
    try:
        pw = pwd.getpwnam(req.username)
    except KeyError:
        await _audit_failed(web_user, req.username, "user not in /etc/passwd")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    ttl = int(cfg.get("token_ttl") or 1800)
    # Terminal tokens use a distinct audience (dwellerd-terminal) so a
    # leaked terminal JWT can't be substituted for the main access JWT
    # (aud=dwellerd-web), and vice versa.
    token = encode_terminal_token(
        unix_user=req.username,
        via_web_user=web_user,
        uid=pw.pw_uid,
        secret=secret,
        expiry_seconds=ttl,
    )
    await _audit_ok(web_user, req.username)
    return UnlockResponse(token=token, expires_in=ttl, username=req.username)


# ── helpers ───────────────────────────────────────────────────────────


def _terminal_cfg() -> dict:
    ctx = broker.state.data.get("app_ctx")
    if ctx is None:
        return {}
    return ((getattr(ctx.config, "web", None) or {}).get("terminal") or {})


def _pam_authenticate(username: str, password: str) -> bool:
    """Wrap libpam through python-pam. Returns True iff the credentials
    pass the system's PAM stack (typically /etc/pam.d/login)."""
    try:
        import pam as _pam
    except ImportError:
        log.exception("terminal: python-pam not installed; cannot authenticate")
        return False
    try:
        p = _pam.pam()
        return bool(p.authenticate(username, password, service="login"))
    except Exception:
        log.exception("terminal: PAM check crashed for username=%r", username)
        return False


async def _audit_failed(via_user: str, term_user: str, detail: str) -> None:
    from db.models import TerminalAuditEntry
    sm = broker.state.data.get("db_session_maker")
    if sm is None:
        return
    try:
        async with sm() as session:
            session.add(TerminalAuditEntry(
                ts=time.time(), sid="-", username=via_user,
                kind="auth_failed",
                data=f"as={term_user!r} reason={detail}",
            ))
            await session.commit()
    except Exception:
        pass


async def _audit_ok(via_user: str, term_user: str) -> None:
    from db.models import TerminalAuditEntry
    sm = broker.state.data.get("db_session_maker")
    if sm is None:
        return
    try:
        async with sm() as session:
            session.add(TerminalAuditEntry(
                ts=time.time(), sid="-", username=via_user,
                kind="auth_ok",
                data=f"unlocked as unix user {term_user!r}",
            ))
            await session.commit()
    except Exception:
        pass
