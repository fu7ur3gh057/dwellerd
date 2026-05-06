"""SQLite access for the bot.

The bot is a separate process from the daemon; both open the same
`dwellerd.sqlite` file with WAL mode + foreign keys ON, which SQLite handles
fine for our concurrency level (a handful of writes per minute).

Sync engine is intentional — aiogram handlers are async but the bot's DB
queries are tiny (single-row lookups on indexed columns). Using sync
sqlmodel here keeps the code obvious without measurable latency cost.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import yaml
from sqlalchemy import create_engine, event
from sqlmodel import Session, SQLModel

# Pull models from the daemon's package — `make run-bot` puts server/ on
# PYTHONPATH so this resolves. BotSession is the new table this file relies
# on; the import also forces SQLModel.metadata.create_all() to know about it.
from db.models import BotSession, User  # noqa: F401  (registered with metadata)


_PROD_CONFIG = Path("/etc/dwellerd/config.yaml")
_DEV_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
_DEV_DB = Path(__file__).resolve().parents[1] / "data" / "dwellerd.sqlite"


def _resolve_db_path() -> Path:
    """Match the daemon's resolution: read `db.path` from config.yaml if
    present, else fall back to ./data/dwellerd.sqlite. The bot must point
    at the same file the daemon uses — that's how login picks up users
    that the wizard / web seeded into the `users` table.
    """
    for p in (_PROD_CONFIG, _DEV_CONFIG):
        if p.exists():
            try:
                cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                continue
            if (db := (cfg.get("db") or {}).get("path")):
                return Path(db)
    return _DEV_DB


_engine = None


def _set_pragmas(dbapi_conn, _) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def init_db() -> None:
    """Open the engine and make sure the bot's table exists. Safe to call
    multiple times (no-op after first). Should be called once at bot
    startup so the first /login query doesn't pay table-creation cost."""
    global _engine
    if _engine is not None:
        return
    path = _resolve_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(f"sqlite:///{path}", echo=False, future=True)
    event.listen(_engine, "connect", _set_pragmas)
    SQLModel.metadata.create_all(_engine)


def get_engine():
    if _engine is None:
        init_db()
    return _engine


@contextmanager
def db_session():
    """Short-lived sync session. Use within a single handler call only —
    don't hold across `await` boundaries (the underlying connection is
    not bound to the asyncio event loop)."""
    with Session(get_engine()) as s:
        yield s
