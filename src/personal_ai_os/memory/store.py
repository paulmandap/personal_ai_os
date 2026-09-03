"""SQLite connection and schema management.

One file, one connection, versioned schema. No ORM: the queries here are small
enough that an ORM would add a dependency and a mental model without removing
any work.

**Threading.** `Tool.execute` runs `Tool.run` inside a single-worker
`ThreadPoolExecutor`, so a store opened on the main thread is used from a
worker thread. Hence `check_same_thread=False`, plus an `RLock` around every
operation. The lock is not defending against the sync core running two things
at once (it does not -- ADR-002); it is defending against the one case where
concurrency *can* happen: a tool that exceeded its timeout keeps running in the
background while the caller has already moved on.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from personal_ai_os.core.errors import PersonalAIOSError
from personal_ai_os.observability.logging import get_logger

log = get_logger("store")

IN_MEMORY = ":memory:"


class StoreError(PersonalAIOSError):
    """The database could not be opened, migrated, or queried."""


#: Ordered migrations. Append only -- never edit a shipped migration, because
#: a database that already applied it will not see the change.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            title        TEXT NOT NULL,
            notes        TEXT NOT NULL DEFAULT '',
            status       TEXT NOT NULL DEFAULT 'todo',
            priority     TEXT NOT NULL DEFAULT 'normal',
            due_date     TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_date);
        """,
    ),
    (
        2,
        # Money is stored as INTEGER minor units (centavos), never REAL.
        # Binary floating point cannot represent 0.10, and a finance system
        # that drifts by fractions of a peso is worse than one that refuses
        # to run. See docs/decisions.md ADR-023.
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL UNIQUE,
            currency      TEXT NOT NULL DEFAULT 'PHP',
            balance_minor INTEGER NOT NULL DEFAULT 0,
            updated_at    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            occurred_on  TEXT NOT NULL,
            amount_minor INTEGER NOT NULL,
            category     TEXT NOT NULL DEFAULT 'uncategorised',
            description  TEXT NOT NULL DEFAULT '',
            created_at   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS commitments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            amount_minor INTEGER NOT NULL,
            day_of_month INTEGER NOT NULL,
            category     TEXT NOT NULL DEFAULT 'bills',
            active       INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS goals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            target_minor INTEGER NOT NULL,
            saved_minor  INTEGER NOT NULL DEFAULT 0,
            target_date  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_txn_account ON transactions(account_id);
        CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(occurred_on);
        """,
    ),
    (
        3,
        # Plans: what a top-level run actually DID, so it survives a restart.
        #
        # This records what the runtime OBSERVED, never what a model declared
        # (ADR-065). A step row is written from a real `AgentResult`, so it
        # cannot disagree with what happened -- unlike an agent's own account of
        # itself, which ADR-038 measured being wrong while sounding right.
        #
        # The CHECK constraints encode both state machines, so an invalid status
        # is impossible to store rather than merely discouraged:
        #   plan  running -> done | failed | abandoned   (terminal is terminal)
        #   step  pending -> done | failed
        # A crash leaves a plan `running` and its step `pending`; nothing else
        # can produce that combination, which is what makes it recoverable.
        """
        CREATE TABLE IF NOT EXISTS plans (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id       TEXT NOT NULL UNIQUE,
            agent        TEXT NOT NULL,
            objective    TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'running'
                CHECK (status IN ('running','done','failed','abandoned')),
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS plan_steps (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id    INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
            seq        INTEGER NOT NULL,
            run_id     TEXT NOT NULL,
            agent      TEXT NOT NULL,
            objective  TEXT NOT NULL,
            status     TEXT NOT NULL
                CHECK (status IN ('pending','done','failed')),
            output     TEXT NOT NULL DEFAULT '',
            error      TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (plan_id, seq)
        );
        CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status);
        CREATE INDEX IF NOT EXISTS idx_plan_steps_plan ON plan_steps(plan_id);
        """,
    ),
    (
        4,
        # How deep the delegation that produced this step was.
        #
        # v3 shipped before any agent but the Master held `delegate`, so every
        # step was necessarily depth 1 and a flat `seq` list described the run
        # exactly. Phase 7.8 puts a coordinator between the Master and the
        # specialists, and then `seq` alone reads a grandchild as its parent's
        # sibling -- the store would be describing a shape that did not happen.
        #
        # DEFAULT 1 is a PROOF, not a guess. No manifest but `master.yaml`
        # listed `delegate` before this migration, so no pre-existing row can
        # have been written at any other depth.
        #
        # `seq` still orders the whole plan depth-first, because `begin_step`
        # commits the intent before the sub-agent runs (INV-1): a parent's row
        # is created first and finished last. `seq` plus `depth` reconstructs
        # the tree; neither does alone.
        """
        ALTER TABLE plan_steps ADD COLUMN depth INTEGER NOT NULL DEFAULT 1;
        """,
    ),
]

LATEST_VERSION = MIGRATIONS[-1][0] if MIGRATIONS else 0


class Store:
    """Owns the SQLite file and its schema."""

    def __init__(self, path: Path | str) -> None:
        self.path = path if path == IN_MEMORY else Path(path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # --- lifecycle ---------------------------------------------------------

    @classmethod
    def in_memory(cls) -> Store:
        """An ephemeral store. Used by tests; never touches the filesystem."""
        store = cls(IN_MEMORY)
        store.connect()
        return store

    def connect(self) -> sqlite3.Connection:
        """Open the database and bring its schema up to date. Idempotent."""
        with self._lock:
            if self._conn is not None:
                return self._conn

            if self.path != IN_MEMORY:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)

            try:
                conn = sqlite3.connect(
                    self.path if self.path == IN_MEMORY else str(self.path),
                    check_same_thread=False,
                )
            except sqlite3.Error as exc:
                raise StoreError(f"cannot open database at {self.path}: {exc}") from exc

            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            if self.path != IN_MEMORY:
                # WAL survives a crash better and tolerates a reader during a
                # write. Meaningless for :memory:, which rejects it.
                conn.execute("PRAGMA journal_mode = WAL")

            self._conn = conn
            self._migrate()
            return conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn or self.connect()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> Store:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- schema ------------------------------------------------------------

    def _migrate(self) -> None:
        conn = self._conn
        assert conn is not None
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        current = row["version"] if row else 0

        if current == 0 and row is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (0)")

        for version, sql in MIGRATIONS:
            if version <= current:
                continue
            log.debug("applying migration %d to %s", version, self.path)
            try:
                conn.executescript(sql)
            except sqlite3.Error as exc:
                conn.rollback()
                raise StoreError(f"migration {version} failed: {exc}") from exc
            conn.execute("UPDATE schema_version SET version = ?", (version,))
            current = version

        conn.commit()

    @property
    def version(self) -> int:
        row = self.conn.execute("SELECT version FROM schema_version").fetchone()
        return int(row["version"]) if row else 0

    # --- queries -----------------------------------------------------------

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """A guarded write transaction: commits on success, rolls back on error."""
        with self._lock:
            conn = self.conn
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            conn.commit()

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            try:
                return self.conn.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                raise StoreError(f"query failed: {exc}") from exc

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None
