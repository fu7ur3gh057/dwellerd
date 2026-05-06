"""Async SQLite engine + session factory, attached to broker.state.

Both TaskIQ tasks and FastAPI handlers reach the DB through the same
session_maker, kept on the module-level broker singleton — no second
source of truth across worker/web.

Engine pragmas applied at every new connection:
  - `journal_mode=WAL`     — readers don't block writers; survives crash
                             cleanly. Must be set per-connection because
                             aiosqlite opens fresh connections from a pool.
  - `synchronous=NORMAL`   — safe with WAL, much faster than FULL.
  - `foreign_keys=ON`      — enforce referential integrity (off by default
                             in SQLite). Cheap insurance even if we don't
                             declare FKs today.
  - `busy_timeout=5000`    — block up to 5s on a locked write instead of
                             erroring immediately. With WAL writes are
                             rare-conflict but periodic prune + scheduler
                             can race the web layer.
"""

import logging
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from db import models  # noqa: F401  — registers tables in metadata
from services.taskiq.broker import broker

log = logging.getLogger(__name__)


def _attach_pragmas(sync_engine) -> None:
    """Wire a `connect` listener on the underlying sync engine so every new
    SQLite connection gets the correct PRAGMAs set before any query runs."""

    @event.listens_for(sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _conn_record):
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode = WAL")
            cur.execute("PRAGMA synchronous = NORMAL")
            cur.execute("PRAGMA foreign_keys = ON")
            cur.execute("PRAGMA busy_timeout = 5000")
        finally:
            cur.close()


async def init_db(db_path: str | Path) -> AsyncEngine:
    """Create the engine + tables, attach to broker.state. Idempotent."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(url, echo=False, future=True)
    _attach_pragmas(engine.sync_engine)

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    session_maker = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    broker.state.db_engine = engine
    broker.state.db_session_maker = session_maker
    log.info("db ready at %s (WAL + foreign_keys=ON)", db_path)
    return engine


async def shutdown_db() -> None:
    engine: AsyncEngine | None = broker.state.data.get("db_engine")
    if engine is None:
        return
    await engine.dispose()
