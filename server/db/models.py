"""SQLModel tables for dwellerd.

All tables are designed to be cheap to write per-event and small enough to
keep on disk indefinitely with a periodic cleanup.

- CheckResult: append-only history of every check execution (drives metric
  graphs in the admin UI).
- AlertEvent: append-only log of each Alert that left a notifier (timeline
  of "what fired and when").
- CheckStateEntry: latest known severity per check — replaces the in-memory
  StateTracker so transitions survive restarts.
- LogSignatureEntry: dedup table for LogProcessor; first-seen state survives
  across daemon restarts so we don't re-fire `notify_log_first` for old
  errors that were already digested.
- LogEvent: append-only log of every matched line LogProcessor records.
  Pruned periodically per `retention_days` / `max_rows` so the table stays
  bounded.
- TerminalAuditEntry: audit log for the in-browser web terminal.
  EVERY keystroke chunk is recorded — including whatever was typed, even
  passwords and tokens. Treat the table as sensitive: same access level
  as the JWT secret and DB itself.
- Settings: singleton (id=1) holding what used to live in config.yaml's
  notifiers/checks/report/logs sections. config.yaml is now boot-only:
  it seeds the DB on first run and provides db.path / web.host / port /
  jwt.secret / prefix. Everything else is editable at runtime.
- User: replaces the single `web.user.{username, password_hash}` block
  in config.yaml. Multiple users with role + active flag, normal
  login flow goes through this table.

Schema improvements over the Blackbox port:

  - Composite indexes `(name, ts)` / `(source, ts)` on hot read paths
    (`/alerts?check=…`, `/checks/<name>/results`, `/logs?source=…`) instead
    of separate single-column indexes that SQLite has to merge.
  - `sqlite_autoincrement=True` on append-only tables (CheckResult,
    AlertEvent, LogEvent, TerminalAuditEntry). Without it SQLite recycles
    rowids after the row with `max(rowid)` is deleted — FIFO pruning that
    walks `id <= cutoff_id` could in theory bite recycled ids.
  - `Settings.id` is a non-nullable Int with a CHECK constraint enforcing
    the singleton invariant at the DB level (was `int | None = …` plus
    convention).
"""

from sqlalchemy import JSON, CheckConstraint, Column, Index
from sqlmodel import Field, SQLModel


class CheckResult(SQLModel, table=True):
    __tablename__ = "check_results"
    __table_args__ = (
        Index("ix_check_results_name_ts", "name", "ts"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    ts: float
    name: str
    kind: str
    level: str  # ok | warn | crit
    detail: str | None = None
    metrics: dict | None = Field(default=None, sa_column=Column(JSON))


class AlertEvent(SQLModel, table=True):
    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_name_ts", "name", "ts"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    ts: float
    name: str
    level: str
    kind: str | None = None
    detail: str | None = None
    metrics: dict | None = Field(default=None, sa_column=Column(JSON))


class CheckStateEntry(SQLModel, table=True):
    __tablename__ = "check_state"

    name: str = Field(primary_key=True)
    level: str
    updated_at: float


class LogSignatureEntry(SQLModel, table=True):
    __tablename__ = "log_signatures"

    sig: str = Field(primary_key=True)
    source: str
    sample: str
    first_seen: float
    total: int = 0


class LogEvent(SQLModel, table=True):
    __tablename__ = "log_events"
    __table_args__ = (
        Index("ix_log_events_source_ts", "source", "ts"),
        Index("ix_log_events_sig_ts", "sig", "ts"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    ts: float
    source: str
    sig: str
    first: bool = False
    line: str


class TerminalAuditEntry(SQLModel, table=True):
    """Append-only audit log for `/terminal` WS sessions.

    Records session lifecycle events (open/close/kill) and every input
    chunk the user sends through the PTY. The data column holds the raw
    bytes decoded as UTF-8 (replace errors), so passwords typed at a
    prompt end up here verbatim — guard the DB accordingly.
    """

    __tablename__ = "terminal_audit"
    __table_args__ = (
        Index("ix_terminal_audit_sid_ts", "sid", "ts"),
        Index("ix_terminal_audit_username_ts", "username", "ts"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    ts: float
    sid: str
    username: str
    kind: str  # "open" | "input" | "close" | "kill" | "denied"
    data: str | None = None


class Settings(SQLModel, table=True):
    """Runtime-editable settings — was the contents of config.yaml's
    notifiers/checks/report/logs/terminal sections. One row, id=1.

    Each section is a JSON column so we can `UPDATE settings SET checks=?`
    without serialising the whole blob. Order of `notifiers` / `checks` /
    `report.docker` matters and JSON arrays preserve it.

    The CHECK constraint on `id` enforces the singleton at the DB level —
    inserting a second row fails with IntegrityError instead of silently
    creating a parallel config that nothing reads.
    """

    __tablename__ = "settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="settings_singleton"),
    )

    id: int = Field(default=1, primary_key=True)
    notifiers: list | None = Field(default=None, sa_column=Column(JSON))
    checks:    list | None = Field(default=None, sa_column=Column(JSON))
    report:    dict | None = Field(default=None, sa_column=Column(JSON))
    logs:      dict | None = Field(default=None, sa_column=Column(JSON))
    terminal:  dict | None = Field(default=None, sa_column=Column(JSON))
    updated_at: float = 0.0


class User(SQLModel, table=True):
    """Web admin user. Replaces the single web.user.{username,
    password_hash} block in config.yaml — multiple users with a role
    and an active flag.
    """

    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    role: str = "admin"          # "admin" | future roles ("viewer", etc)
    is_active: bool = True
    created_at: float = 0.0
    last_login_ts: float | None = None


class Session(SQLModel, table=True):
    """Server-side session row backing one issued refresh-token.

    Refresh tokens are opaque random strings stored hashed (sha256 of the
    plaintext) — the plaintext only ever lives client-side in an httpOnly
    cookie. Each access JWT carries `sid = sessions.id` so the auth
    middleware can revoke a token mid-life by setting `revoked_at`.

    Lifecycle:
      - Login                 → INSERT Session, return access JWT + refresh
      - Refresh endpoint      → look up by hash, mark old `revoked_at`, INSERT new
                                row, return new access + new refresh (rotation)
      - Logout                → mark current `revoked_at = now`
      - Hourly prune          → delete rows where `expires_at < now - 7d` and
                                `revoked_at IS NOT NULL` (audit retention)
    """

    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_user_id", "user_id"),
        Index("ix_sessions_refresh_hash", "refresh_token_hash", unique=True),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    refresh_token_hash: str            # sha256 hex
    issued_at: float
    last_used_at: float
    expires_at: float                  # refresh token expiry (typically issued_at + 30d)
    ip: str | None = None
    user_agent: str | None = None
    revoked_at: float | None = None    # null = active


class BotSession(SQLModel, table=True):
    """Authenticated link between a Telegram user and a Dwellerd User.

    Created on `/login` after credentials verify, deleted on `/logout`. One
    row per Telegram user — re-login replaces the row. Persisted in the
    same SQLite as `users` so logins survive bot process restarts (FSM
    memory storage in aiogram is per-process).

    Telegram user ids are 64-bit ints; using them as the primary key keeps
    the table single-row-per-tg-user without a separate uniqueness index.
    """

    __tablename__ = "bot_sessions"
    __table_args__ = (
        Index("ix_bot_sessions_user_id", "user_id"),
    )

    tg_user_id: int = Field(primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    started_at: float
    last_seen_at: float
    # REST tokens cached at login time so bot can call privileged daemon
    # endpoints (action commands like /run, /restart). Null when web is
    # disabled — admin commands fall back to "REST not available" errors.
    access_token: str | None = None
    refresh_token: str | None = None
    access_expires_at: float | None = None


class BotSubscription(SQLModel, table=True):
    """Per-TG-user push subscription.

    A logged-in user opts in to having alerts / log events / per-check
    results DM'd to them by the bot. The daemon's task layer (alerts,
    logs.processor) writes events as usual; a small dispatcher in the
    bot reads new rows from the source tables and forwards each match
    to all subscribers.

    `topic` is one of: 'alerts', 'logs', 'checks'.
    `filter` is an optional narrowing — for 'checks' it's the check name,
    for 'logs' it's the source name; null = all.
    """

    __tablename__ = "bot_subscriptions"
    __table_args__ = (
        Index("ix_bot_subscriptions_topic", "topic"),
        Index("ix_bot_subscriptions_tg", "tg_user_id"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    tg_user_id: int = Field(index=True)
    topic: str                              # 'alerts' | 'logs' | 'checks'
    filter: str | None = None               # check name / log source / null
    created_at: float
