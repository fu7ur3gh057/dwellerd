"""In-browser shell: PTY pump over a Socket.IO namespace.

Each client connection forks a PTY and execs the configured shell — as
the unix user that PAM-authenticated through /api/terminal/unlock. The
daemon must run as root to drop privileges via setuid/setgid; if it
doesn't, the namespace refuses any login that isn't the same user the
process is already running as.

Bytes coming from the browser (`terminal:input`) write straight into the
pty master; bytes coming out of the pty are emitted back as
`terminal:output`. Window resize comes in as `terminal:resize` and is
forwarded via TIOCSWINSZ.

Three guardrails baked in:

  - **two-step auth**: dw_access JWT cookie + PAM-validated
    unix user/password via the terminal_token in the WS auth payload.
  - **single session per user**: a second connect with the same username
    is refused — keeps fork bombs / stuck handles bounded.
  - **audit**: every input chunk is persisted to `terminal_audit`. The
    open / close / kill events are also written, plus an alert is fired
    so the operator sees session activity in `RecentAlertsFeed`.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import pwd
import signal
import struct
import subprocess
import termios
import time

from socketio import AsyncNamespace

from services.taskiq.broker import broker
from web.auth.sessions import lookup_session_status
from web.auth.tokens import decode_access_token, decode_terminal_token

log = logging.getLogger(__name__)


class _Session:
    """One PTY + child process pair. Owned by a single sid."""

    __slots__ = ("master_fd", "proc", "username", "started_at", "_loop")

    def __init__(self, master_fd: int, proc: subprocess.Popen, username: str) -> None:
        self.master_fd = master_fd
        self.proc = proc
        self.username = username
        self.started_at = time.time()
        self._loop: asyncio.AbstractEventLoop | None = None

    def write(self, data: bytes) -> None:
        try:
            os.write(self.master_fd, data)
        except OSError:
            pass

    def resize(self, cols: int, rows: int) -> None:
        try:
            fcntl.ioctl(
                self.master_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
        except OSError:
            pass

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                # Send SIGHUP to the whole session so backgrounded jobs go too.
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGHUP)
                except (OSError, ProcessLookupError):
                    pass
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass


class TerminalNamespace(AsyncNamespace):
    """Single-session-per-user PTY bridge.

    JWT auth is the same as `AuthedNamespace` but we duplicate the body
    here because we also need to remember the username on the session
    object (for audit + single-session enforcement).
    """

    def __init__(self, namespace: str = "/terminal") -> None:
        super().__init__(namespace)
        self._sessions: dict[str, _Session] = {}        # sid → session
        self._user_to_sid: dict[str, str] = {}          # username → sid (single-session lock)

    # ── connect ──────────────────────────────────────────────────────

    async def on_connect(self, sid: str, environ: dict, auth: dict | None = None) -> bool | None:
        secret = broker.state.data.get("web_jwt_secret")
        if not secret:
            log.warning("terminal: refusing %s — auth not configured", sid)
            return False

        # Step 1: dw_access cookie — same JWT every other namespace uses.
        # Confirms the browser is logged in as the web admin AND the
        # server-side session row hasn't been revoked.
        session_token = _extract_token(auth, environ)
        if not session_token:
            log.info("terminal: refusing %s — no session token", sid)
            return False
        from jwt import InvalidTokenError
        try:
            session_claims = decode_access_token(session_token, secret)
        except InvalidTokenError as e:
            log.info("terminal: refusing %s — bad session token: %s", sid, e)
            return False

        try:
            session_id = int(session_claims.get("sid"))
        except (TypeError, ValueError):
            log.info("terminal: refusing %s — malformed sid claim", sid)
            return False
        sm = broker.state.data.get("db_session_maker")
        if sm is None:
            return False
        async with sm() as db:
            session_status = await lookup_session_status(db, session_id)
        if session_status is None or not session_status.valid:
            log.info("terminal: refusing %s — session revoked", sid)
            return False
        # Terminal is admin-only — even if dw_access is valid, staff/viewer
        # can't open a shell.
        if session_status.role != "admin":
            log.info(
                "terminal: refusing %s — role %r is not admin (user=%s)",
                sid, session_status.role, session_status.username,
            )
            return False
        web_user = session_status.username

        # Step 2: terminal token — issued by POST /api/terminal/unlock
        # after PAM-authenticating a unix user/password. Has aud=dwellerd-
        # terminal so it can't double as an access token (and vice versa).
        if not broker.state.data.get("terminal_enabled"):
            log.info("terminal: refusing %s — terminal disabled in config", sid)
            return False

        term_token = (auth or {}).get("terminal_token") if isinstance(auth, dict) else None
        if not term_token:
            log.info("terminal: refusing %s — no terminal token", sid)
            return False
        try:
            term_claims = decode_terminal_token(str(term_token), secret)
        except InvalidTokenError as e:
            log.info("terminal: refusing %s — bad terminal token: %s", sid, e)
            return False

        # The terminal token's `sub` is the unix user we'll fork as.
        # Use it for the single-session lock and audit attribution; the
        # web admin user goes into the `via` claim for forensics.
        username = term_claims.get("sub", "")
        if not username:
            return False

        # Single-session lock — refuse if user already has a session.
        existing = self._user_to_sid.get(username)
        if existing and existing in self._sessions:
            log.info("terminal: refusing %s — %s already has session %s", sid, username, existing)
            await _audit("denied", sid, username, "another session active")
            return False

        # Resolve the unix user so we can fork as them.
        try:
            pw = pwd.getpwnam(username)
        except KeyError:
            log.warning("terminal: refusing %s — unix user %r not in /etc/passwd", sid, username)
            return False

        # Daemon must be root to setuid to anyone else. If it's not,
        # only the same-user case is permitted (so a dev running dwellerd
        # under their own account can still test the page).
        running_uid = os.getuid()
        if running_uid != 0 and running_uid != pw.pw_uid:
            log.warning(
                "terminal: refusing %s — daemon is not root (uid=%d) so cannot setuid to %s (uid=%d)",
                sid, running_uid, username, pw.pw_uid,
            )
            return False

        cfg = _terminal_cfg()
        shell_cfg = cfg.get("shell")
        # If the operator left shell empty/auto, fall back to the user's
        # /etc/passwd entry, then bash, then sh. Anything explicit in
        # config wins — handy if you want every session to use zsh
        # regardless of the user's pw_shell.
        shell = (
            shell_cfg
            or (pw.pw_shell if pw.pw_shell and pw.pw_shell != "/usr/sbin/nologin" else None)
            or "/bin/bash"
        )
        cwd_cfg = cfg.get("cwd")
        cwd = cwd_cfg if (cwd_cfg and os.path.isdir(cwd_cfg)) else (
            pw.pw_dir if os.path.isdir(pw.pw_dir or "") else "/tmp"
        )

        # Build a clean env that looks like a fresh login shell.
        env = {
            "HOME":        pw.pw_dir or "/tmp",
            "USER":        username,
            "LOGNAME":     username,
            "SHELL":       shell,
            "PATH":        os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
            "TERM":        "xterm-256color",
            "COLORTERM":   "truecolor",
            "LANG":        os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL":      os.environ.get("LC_ALL", os.environ.get("LANG", "C.UTF-8")),
        }

        try:
            master_fd, slave_fd = pty.openpty()
            # Sane initial size — client will TIOCSWINSZ on its first frame.
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

            def _child_setup(slave_fd: int = slave_fd) -> None:
                # Order matters: subprocess will (after this returns)
                # dup2 the slave fd onto 0/1/2 and then close_fds. We
                # must (a) make this a new session and (b) attach the
                # slave PTY as the controlling TTY *here*, otherwise
                # `bash -i` notices no ctty and exits immediately.
                os.setsid()
                try:
                    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
                except OSError:
                    # Some kernels need the slave to be opened *after*
                    # setsid to grab ctty. Re-open it as the steal arg.
                    pass

                # Drop privileges. Must run before exec — sets the child
                # up as the target uid for the rest of its lifetime.
                try:
                    os.initgroups(username, pw.pw_gid)
                except PermissionError:
                    pass  # not root → already same-user; skip
                try:
                    os.setgid(pw.pw_gid)
                    os.setuid(pw.pw_uid)
                except PermissionError:
                    pass

            # Just `-i` — `-l` adds login-shell profile sourcing
            # (/etc/profile, ~/.profile, …) which sometimes exits early
            # for non-tty heuristics or PAM-driven `if shopt -q login`
            # checks. Interactive without login is enough for a web
            # shell — the user can `bash -l` themselves if they care.
            proc = subprocess.Popen(
                [shell, "-i"],
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                preexec_fn=_child_setup,
                cwd=cwd,
                env=env,
                close_fds=True,
            )
            os.close(slave_fd)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, os.O_NONBLOCK)
        except Exception:
            log.exception("terminal: failed to spawn shell")
            return False

        sess = _Session(master_fd=master_fd, proc=proc, username=username)
        self._sessions[sid] = sess
        self._user_to_sid[username] = sid
        await self.save_session(sid, {"username": username})

        loop = asyncio.get_running_loop()
        sess._loop = loop
        loop.add_reader(master_fd, lambda: asyncio.create_task(self._on_pty_readable(sid)))

        log.info("terminal: %s opened for %s (pid=%s, shell=%s)", sid, username, proc.pid, shell)
        await _audit("open", sid, username, f"shell={shell} cwd={cwd}")
        await _alert("terminal_open", username,
                     f"web terminal session opened ({shell})")
        return None

    # ── disconnect ───────────────────────────────────────────────────

    async def on_disconnect(self, sid: str) -> None:
        sess = self._sessions.pop(sid, None)
        if not sess:
            return
        # Drop the user-lock if it pointed at this sid.
        if self._user_to_sid.get(sess.username) == sid:
            self._user_to_sid.pop(sess.username, None)

        loop = sess._loop
        if loop is not None:
            try:
                loop.remove_reader(sess.master_fd)
            except (ValueError, OSError):
                pass

        sess.close()
        duration = int(time.time() - sess.started_at)
        log.info("terminal: %s closed for %s (duration=%ds)", sid, sess.username, duration)
        await _audit("close", sid, sess.username, f"duration={duration}s")
        await _alert("terminal_close", sess.username,
                     f"web terminal session ended ({duration}s)")

    # ── client → server ──────────────────────────────────────────────

    async def on_input(self, sid: str, data: dict) -> None:
        sess = self._sessions.get(sid)
        if not sess:
            return
        chunk = (data or {}).get("data") if isinstance(data, dict) else None
        if not isinstance(chunk, str):
            return
        sess.write(chunk.encode("utf-8", errors="replace"))
        # Audit the raw chunk — includes whatever was typed, no scrubbing.
        await _audit("input", sid, sess.username, chunk)

    async def on_resize(self, sid: str, data: dict) -> None:
        sess = self._sessions.get(sid)
        if not sess:
            return
        cols = int((data or {}).get("cols") or 80)
        rows = int((data or {}).get("rows") or 24)
        cols = max(2, min(500, cols))
        rows = max(2, min(200, rows))
        sess.resize(cols=cols, rows=rows)

    # ── pty → client ─────────────────────────────────────────────────

    async def _on_pty_readable(self, sid: str) -> None:
        sess = self._sessions.get(sid)
        if not sess:
            return
        try:
            data = os.read(sess.master_fd, 4096)
        except BlockingIOError:
            # Spurious wakeup on non-blocking master — no data yet.
            # Without this catch we'd treat EAGAIN as EOF and tear down
            # the session before the shell even finished printing PS1.
            return
        except (OSError, ValueError):
            data = b""
        if not data:
            # EOF — child closed its end. Reap so we know the exit code
            # and surface it in the audit row + the `terminal:exit` payload.
            rc = sess.proc.poll()
            log.info("terminal: %s child %d exited (rc=%s)", sid, sess.proc.pid, rc)
            reason = f"shell exited with code {rc}" if rc is not None else "shell exited"
            try:
                await self.emit("terminal:exit", {"reason": reason}, room=sid)
            except Exception:
                pass
            try:
                await self.disconnect(sid)
            except Exception:
                pass
            return
        try:
            await self.emit("terminal:output", {"data": data.decode("utf-8", "replace")}, room=sid)
        except Exception:
            log.exception("terminal: emit failed")


# ── helpers ───────────────────────────────────────────────────────────


def _terminal_cfg() -> dict:
    ctx = broker.state.data.get("app_ctx")
    if ctx is None:
        return {}
    return ((getattr(ctx.config, "web", None) or {}).get("terminal") or {})


def _extract_token(auth: dict | None, environ: dict) -> str | None:
    if isinstance(auth, dict) and auth.get("token"):
        return str(auth["token"])
    cookies = environ.get("HTTP_COOKIE", "")
    for piece in cookies.split(";"):
        piece = piece.strip()
        if piece.startswith("dw_access="):
            return piece[len("dw_access="):]
    return None


async def _audit(kind: str, sid: str, username: str, data: str | None) -> None:
    """Persist one audit row. Truncates to 4 KB so a paste-bomb doesn't
    bloat the table; if the user pastes a giant blob we still get the
    head + a marker."""
    from db.models import TerminalAuditEntry

    sm = broker.state.data.get("db_session_maker")
    if sm is None:
        return
    try:
        async with sm() as session:
            session.add(TerminalAuditEntry(
                ts=time.time(),
                sid=sid,
                username=username,
                kind=kind,
                data=(data[:4096] + "…[truncated]" if data and len(data) > 4096 else data),
            ))
            await session.commit()
    except Exception:
        log.exception("terminal: audit insert failed (kind=%s)", kind)


async def _alert(kind: str, username: str, detail: str) -> None:
    """Fire an alert event so the lifecycle shows up in RecentAlertsFeed."""
    from db.models import AlertEvent

    sm = broker.state.data.get("db_session_maker")
    if sm is None:
        return
    try:
        async with sm() as session:
            row = AlertEvent(
                ts=time.time(),
                name=f"terminal:{username}",
                level="ok" if kind != "kill" else "warn",
                kind=kind,
                detail=detail,
                metrics=None,
            )
            session.add(row)
            await session.commit()
    except Exception:
        log.exception("terminal: alert insert failed (kind=%s)", kind)

    # Also push live so the right column highlights without polling.
    from web.sockets import emit
    try:
        await emit("/alerts", "alert:fired", {
            "ts": time.time(),
            "name": f"terminal:{username}",
            "level": "ok" if kind != "kill" else "warn",
            "kind": kind,
            "detail": detail,
            "metrics": None,
        })
    except Exception:
        pass
