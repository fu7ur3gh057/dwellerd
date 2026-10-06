"""SQLite state — a handful of small tables, stdlib `sqlite3`, no ORM.

Why a database at all in a "simple" daemon: three things must survive a
restart or the daemon gets noisy and useless.

  1. `check_state` — the last level per check, so `decide_transition` knows
     whether an observation is news. Without it every restart re-announces
     everything that is currently bad.
  2. `log_signatures` — which kinds of error lines we have already reported.
     Without it a restart re-fires "new error" for every line in the file.
  3. `log_events` / `alerts` — the history the periodic report and the log
     digest summarise.

Also here: `mutes` (temporary silences set from the CLI) and `meta` (the
heartbeat that lets the next start notice the last run died uncleanly).
`check_state` additionally carries the alert gate's streak and reminder
state — see `gate.py`.

Every call is synchronous and microscopic; the daemon wraps them in
`asyncio.to_thread` so the event loop never blocks on the disk. One
connection guarded by a lock is plenty for this write volume.
"""
from __future__ import annotations

import asyncio
import fnmatch
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

CREATE TABLE IF NOT EXISTS mutes (
    pattern TEXT PRIMARY KEY,
    until   REAL NOT NULL,
    created REAL NOT NULL
);

-- Short ids for Telegram buttons: callback_data is capped at 64 bytes and
-- an alert name (a container, a mount point) can be longer than that.
CREATE TABLE IF NOT EXISTS targets (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    ts   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
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


# Columns added to `check_state` after the first release. Existing databases
# get them via ALTER TABLE; a NULL means "written by an older version".
_GATE_COLUMNS = {
    "confirmed":     "TEXT",
    "pending":       "TEXT",
    "pending_count": "INTEGER NOT NULL DEFAULT 0",
    "silenced":      "INTEGER NOT NULL DEFAULT 0",
    "last_alert":    "REAL",
    "acked":         "INTEGER NOT NULL DEFAULT 0",
}


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
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(check_state)")}
        for column, decl in _GATE_COLUMNS.items():
            if column in existing:
                continue
            try:
                conn.execute(f"ALTER TABLE check_state ADD COLUMN {column} {decl}")
            except sqlite3.OperationalError as e:
                # The CLI and the daemon can open a fresh database at the
                # same moment; whoever loses the race just carries on.
                if "duplicate column" not in str(e):
                    raise
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

    def get_gate(self, name: str) -> dict | None:
        """The persisted gate state for one check, or None if never seen.

        `confirmed` falls back to the stored `level` for rows written before
        the gate existed, so an upgrade does not re-announce every problem
        that is already known.
        """
        with self._lock:
            row = self._c.execute(
                "SELECT level, since, confirmed, pending, pending_count, "
                "silenced, last_alert, acked FROM check_state WHERE name = ?",
                (name,),
            ).fetchone()
        if row is None:
            return None
        return {
            "confirmed": row["confirmed"] or row["level"],
            "pending": row["pending"],
            "pending_count": row["pending_count"] or 0,
            "silenced": bool(row["silenced"]),
            "last_alert": row["last_alert"],
            "acked": bool(row["acked"]),
            "since": row["since"],
        }

    def save_gate(
        self, name: str, level: str, detail: str, *, confirmed: str | None,
        pending: str | None, pending_count: int, silenced: bool,
        last_alert: float | None, reset_ack: bool = False,
    ) -> None:
        """Upsert the observed level (what the report shows) together with
        the gate state, in one statement so they never disagree.

        `acked` is deliberately not a parameter: it is written from the bot
        while a check step may be mid-flight, and a step that read the old
        value must not write it back over an acknowledgement. The step can
        only *clear* it (`reset_ack`), never set it."""
        now = time.time()
        with self._lock:
            self._c.execute(
                """
                INSERT INTO check_state
                    (name, level, since, last_run, last_detail,
                     confirmed, pending, pending_count, silenced, last_alert)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    level         = excluded.level,
                    last_run      = excluded.last_run,
                    last_detail   = excluded.last_detail,
                    confirmed     = excluded.confirmed,
                    pending       = excluded.pending,
                    pending_count = excluded.pending_count,
                    silenced      = excluded.silenced,
                    last_alert    = excluded.last_alert,
                    acked         = CASE WHEN ? THEN 0 ELSE check_state.acked END,
                    since         = CASE WHEN check_state.level = excluded.level
                                         THEN check_state.since
                                         ELSE excluded.since END
                """,
                (name, level, now, now, detail, confirmed, pending,
                 pending_count, int(silenced), last_alert, int(reset_ack)),
            )
            self._c.commit()

    def set_acked(self, name: str) -> str:
        """Acknowledge a problem: stop reminding about it until it recovers
        or changes. Returns "unknown" (no such check), "ok" (nothing to
        acknowledge) or "acked"."""
        with self._lock:
            row = self._c.execute(
                "SELECT level, confirmed FROM check_state WHERE name = ?", (name,),
            ).fetchone()
            if row is None:
                return "unknown"
            if (row["confirmed"] or row["level"]) == "ok":
                return "ok"
            self._c.execute("UPDATE check_state SET acked = 1 WHERE name = ?", (name,))
            self._c.commit()
        return "acked"

    def bad_checks(self) -> list[dict]:
        """Checks whose confirmed level is not ok — what there is to ack."""
        return [
            s for s in self.all_states()
            if (s.get("confirmed") or s["level"]) != "ok"
        ]

    # ── button targets ───────────────────────────────────────────────────

    def target_id(self, name: str) -> int:
        """A stable short id for an alert name, for Telegram callback data."""
        with self._lock:
            self._c.execute(
                "INSERT INTO targets (name, ts) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET ts = excluded.ts",
                (name, time.time()),
            )
            row = self._c.execute(
                "SELECT id FROM targets WHERE name = ?", (name,),
            ).fetchone()
            self._c.commit()
        return int(row["id"])

    def target_name(self, target_id: int) -> str | None:
        with self._lock:
            row = self._c.execute(
                "SELECT name FROM targets WHERE id = ?", (target_id,),
            ).fetchone()
        return row["name"] if row else None

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

    # ── mutes ────────────────────────────────────────────────────────────
    # A mute is a glob over alert names ("cpu", "docker:*", "log:*", "*")
    # plus an expiry. It lives in the database, not the YAML, because it is
    # temporary and set from the CLI while the daemon is running.

    def add_mute(self, pattern: str, until: float) -> None:
        with self._lock:
            self._c.execute(
                "INSERT INTO mutes (pattern, until, created) VALUES (?,?,?) "
                "ON CONFLICT(pattern) DO UPDATE SET until = excluded.until",
                (pattern, until, time.time()),
            )
            self._c.commit()

    def remove_mute(self, pattern: str | None = None) -> int:
        """Drop one mute, or every mute when `pattern` is None."""
        with self._lock:
            if pattern is None:
                n = self._c.execute("DELETE FROM mutes").rowcount
            else:
                n = self._c.execute(
                    "DELETE FROM mutes WHERE pattern = ?", (pattern,),
                ).rowcount
            self._c.commit()
        return max(0, n)

    def active_mutes(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        with self._lock:
            self._c.execute("DELETE FROM mutes WHERE until <= ?", (now,))
            rows = self._c.execute(
                "SELECT pattern, until FROM mutes ORDER BY until",
            ).fetchall()
            self._c.commit()
        return [dict(r) for r in rows]

    def is_muted(self, name: str, now: float | None = None) -> bool:
        return any(
            fnmatch.fnmatchcase(name, m["pattern"])
            for m in self.active_mutes(now)
        )

    # ── meta ─────────────────────────────────────────────────────────────

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._c.execute(
                "SELECT value FROM meta WHERE key = ?", (key,),
            ).fetchone()
        return row["value"] if row else None

    def set_meta(self, **items: str) -> None:
        with self._lock:
            for key, value in items.items():
                self._c.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value)),
                )
            self._c.commit()

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
            # Buttons on a message older than this answer "expired". Never
            # shorter than a week, whatever the log retention is.
            self._c.execute(
                "DELETE FROM targets WHERE ts < ?",
                (time.time() - max(retention_days, 7) * 86400,),
            )
            self._c.commit()
        return max(0, by_age), max(0, by_count)


# ── async wrappers ───────────────────────────────────────────────────────
# The daemon is asyncio; SQLite is blocking. These keep the loop free.


async def run(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)
