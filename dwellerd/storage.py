"""SQLite state — four small tables, stdlib `sqlite3`, no ORM.

Why a database at all in a "simple" daemon: three things must survive a
restart or the daemon gets noisy and useless.

  1. `check_state` — the last level per check, so `decide_transition` knows
     whether an observation is news. Without it every restart re-announces
     everything that is currently bad.
  2. `log_signatures` — which kinds of error lines we have already reported.
     Without it a restart re-fires "new error" for every line in the file.
  3. `log_events` / `alerts` — the history the periodic report and the log
     digest summarise.

Every call is synchronous and microscopic; the daemon wraps them in
`asyncio.to_thread` so the event loop never blocks on the disk. One
connection guarded by a lock is plenty for this write volume.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS check_state (
    name        TEXT PRIMARY KEY,
    level       TEXT NOT NULL,
    since       REAL NOT NULL,
    last_run    REAL NOT NULL,
    last_detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS alerts (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    name   TEXT NOT NULL,
    level  TEXT NOT NULL,
    kind   TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);

CREATE TABLE IF NOT EXISTS log_signatures (
    sig        TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    sample     TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    count      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS log_events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    source TEXT NOT NULL,
    sig    TEXT NOT NULL,
    line   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_log_events_ts ON log_events(ts);
CREATE INDEX IF NOT EXISTS idx_log_events_sig_ts ON log_events(sig, ts);
"""


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = sqlite3.connect(
                self.path, check_same_thread=False, timeout=10.0,
            )
        except sqlite3.OperationalError as e:
            raise RuntimeError(
                f"cannot open the database at {self.path}: {e}. "
                f"If running under systemd, make sure the dwellerd user owns "
                f"{self.path.parent} (chown -R dwellerd:dwellerd {self.path.parent})."
            ) from e
        conn.row_factory = sqlite3.Row
        # WAL so a long-running read (the report) never blocks a check write.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn
        log.info("database ready at %s", self.path)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def _c(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("storage used before connect()")
        return self._conn

    # ── check state ──────────────────────────────────────────────────────

    def get_level(self, name: str) -> str | None:
        with self._lock:
            row = self._c.execute(
                "SELECT level FROM check_state WHERE name = ?", (name,),
            ).fetchone()
        return row["level"] if row else None

    def set_level(self, name: str, level: str, detail: str = "") -> None:
        """Upsert the current level. `since` only moves when the level
        actually changes, so it reads as 'crit since 14:02', not 'crit since
        one interval ago'."""
        now = time.time()
        with self._lock:
            self._c.execute(
                """
                INSERT INTO check_state (name, level, since, last_run, last_detail)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    level       = excluded.level,
                    last_run    = excluded.last_run,
                    last_detail = excluded.last_detail,
                    since       = CASE WHEN check_state.level = excluded.level
                                       THEN check_state.since
                                       ELSE excluded.since END
                """,
                (name, level, now, now, detail),
            )
            self._c.commit()

    def all_states(self) -> list[dict]:
        with self._lock:
            rows = self._c.execute(
                "SELECT * FROM check_state ORDER BY name",
            ).fetchall()
        return [dict(r) for r in rows]

    # ── alerts ───────────────────────────────────────────────────────────

    def record_alert(self, name: str, level: str, kind: str, detail: str) -> None:
        with self._lock:
            self._c.execute(
                "INSERT INTO alerts (ts, name, level, kind, detail) VALUES (?,?,?,?,?)",
                (time.time(), name, level, kind, detail),
            )
            self._c.commit()

    def alerts_since(self, ts: float, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._c.execute(
                "SELECT * FROM alerts WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                (ts, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── logs ─────────────────────────────────────────────────────────────

    def record_log(self, source: str, line: str, sig: str, ts: float) -> bool:
        """Store one matched line. Returns True if this signature has never
        been seen before — the caller turns that into an instant alert.

        The INSERT-then-UPDATE dance is done in one transaction so two
        sources racing on the same signature can't both claim 'first'.
        """
        with self._lock:
            cur = self._c.execute(
                """
                INSERT INTO log_signatures (sig, source, sample, first_seen, last_seen, count)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(sig) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    count     = log_signatures.count + 1
                """,
                (sig, source, line[:500], ts, ts),
            )
            # rowcount is 1 for both branches of the upsert, so ask the row
            # itself: count == 1 means this INSERT created it.
            row = self._c.execute(
                "SELECT count FROM log_signatures WHERE sig = ?", (sig,),
            ).fetchone()
            is_first = bool(row) and row["count"] == 1
            self._c.execute(
                "INSERT INTO log_events (ts, source, sig, line) VALUES (?,?,?,?)",
                (ts, source, sig, line[:4000]),
            )
            self._c.commit()
            del cur
        return is_first

    def log_summary_since(self, ts: float, limit: int = 25) -> list[dict]:
        """Per-signature counts for the digest, busiest first."""
        with self._lock:
            rows = self._c.execute(
                """
                SELECT sig, source, COUNT(*) AS count,
                       MIN(line) AS sample, MAX(ts) AS last_ts
                FROM log_events
                WHERE ts >= ?
                GROUP BY sig
                ORDER BY count DESC
                LIMIT ?
                """,
                (ts, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def log_count_since(self, ts: float) -> int:
        with self._lock:
            row = self._c.execute(
                "SELECT COUNT(*) AS n FROM log_events WHERE ts >= ?", (ts,),
            ).fetchone()
        return int(row["n"]) if row else 0

    # ── retention ────────────────────────────────────────────────────────

    def prune(self, retention_days: int, max_rows: int) -> tuple[int, int]:
        """Two-pass prune: drop anything older than the window, then FIFO
        down to `max_rows` as a safety belt against a log storm filling the
        disk between two prune runs. Returns (by_age, by_count)."""
        cutoff = time.time() - retention_days * 86400
        with self._lock:
            by_age = self._c.execute(
                "DELETE FROM log_events WHERE ts < ?", (cutoff,),
            ).rowcount
            by_count = 0
            row = self._c.execute("SELECT COUNT(*) AS n FROM log_events").fetchone()
            total = int(row["n"]) if row else 0
            if total > max_rows:
                by_count = self._c.execute(
                    """
                    DELETE FROM log_events WHERE id IN (
                        SELECT id FROM log_events ORDER BY id ASC LIMIT ?
                    )
                    """,
                    (total - max_rows,),
                ).rowcount
            # Signatures with nothing left behind them are dead weight, and
            # dropping them lets a long-gone error legitimately alert again.
            self._c.execute(
                """
                DELETE FROM log_signatures
                WHERE last_seen < ?
                  AND sig NOT IN (SELECT DISTINCT sig FROM log_events)
                """,
                (cutoff,),
            )
            self._c.execute("DELETE FROM alerts WHERE ts < ?", (cutoff,))
            self._c.commit()
        return max(0, by_age), max(0, by_count)


# ── async wrappers ───────────────────────────────────────────────────────
# The daemon is asyncio; SQLite is blocking. These keep the loop free.


async def run(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)
