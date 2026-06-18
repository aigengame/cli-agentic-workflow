"""Durable Run State persisted as SQLite inside the run directory.

Schema changes are ADDITIVE: every table is created with ``CREATE TABLE IF NOT
EXISTS`` and existing tables are never ``ALTER``ed, so reopening a State database
written by an older caw version adds any new table without a destructive migration
(the #76 lesson — a column added to an existing table is a silent no-op that
crashes the first write, which a new TABLE never does). The Run Group membership
table (#15) follows this rule: it is a NEW table, so an older single-run directory
simply gains it on reopen. It carries NO foreign key to ``run`` because it is a
denormalized, queryable MIRROR of the controller state authoritatively held in the
group's ``group.json`` (a Pattern Controller writes the membership row by
re-opening the iteration's already-finalized State); ``group.json`` — not this
table — is the source of truth for the Run Group's control flow.
"""

import json
import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Any

from caw.status import AWAITING, ERRORED, PARKED, RUNNING, SKIPPED, NodeStatus, RunStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
    run_id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    definition_checksum TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS node (
    run_id TEXT NOT NULL REFERENCES run (run_id),
    node_id TEXT NOT NULL,
    status TEXT NOT NULL,
    cause TEXT,
    PRIMARY KEY (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS attempt (
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_status INTEGER,
    output_json TEXT,
    PRIMARY KEY (run_id, node_id, attempt),
    FOREIGN KEY (run_id, node_id) REFERENCES node (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS run_group_membership (
    run_id TEXT PRIMARY KEY,
    run_group_id TEXT NOT NULL,
    iteration_index INTEGER NOT NULL
);
"""


class StateStore:
    """Owns the State database of one Run."""

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        # A Reporter renders from persisted State and must never mutate it (#12), so
        # ``read_only`` opens the database with the SQLite ``mode=ro`` URI: no schema
        # creation, no commit, and a missing file raises rather than being created
        # (the writing path would silently create an empty database).
        if read_only:
            self._connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            return
        self._connection = sqlite3.connect(path)
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.executescript(_SCHEMA)
            self._connection.commit()
        except BaseException:
            self._connection.close()
            raise

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def record_run_started(
        self, run_id: str, workflow_name: str, definition_checksum: str, created_at: str
    ) -> None:
        self._execute(
            "INSERT INTO run (run_id, workflow_name, definition_checksum, status, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (run_id, workflow_name, definition_checksum, RUNNING, created_at),
        )

    def record_run_finished(self, run_id: str, status: RunStatus, finished_at: str) -> None:
        self._execute(
            "UPDATE run SET status = ?, finished_at = ? WHERE run_id = ?",
            (status, finished_at, run_id),
        )

    def record_run_running(self, run_id: str) -> None:
        """Flip a finished Run row back to ``running`` for a resume (#6).

        A resume reuses the same Run row; setting it ``running`` and clearing the
        prior ``finished_at`` / ``error`` reflects that the Run is in flight again,
        so the row's final state after the resume is the resumed outcome, not a
        stale mix of the interrupted run's terminal fields.
        """
        self._execute(
            "UPDATE run SET status = ?, finished_at = NULL, error = NULL WHERE run_id = ?",
            (RUNNING, run_id),
        )

    def record_run_errored(self, run_id: str, error: str, finished_at: str) -> None:
        self._execute(
            "UPDATE run SET status = ?, error = ?, finished_at = ? WHERE run_id = ?",
            (ERRORED, error, finished_at, run_id),
        )

    def record_run_parked(self, run_id: str) -> None:
        """Mark a Run ``parked`` at a Human Gate (#10, ADR 0010).

        A parked Run is not finished — it awaits approval and will resume — so,
        unlike ``record_run_finished``, it sets no ``finished_at``. A resume flips
        it back to ``running`` (``record_run_running``) before advancing.
        """
        self._execute(
            "UPDATE run SET status = ? WHERE run_id = ?",
            (PARKED, run_id),
        )

    def record_node_started(self, run_id: str, node_id: str) -> None:
        self._execute(
            "INSERT INTO node (run_id, node_id, status) VALUES (?, ?, ?)",
            (run_id, node_id, RUNNING),
        )

    def record_node_running(self, run_id: str, node_id: str) -> None:
        """Flip an existing Node row back to ``running`` for a re-Attempt (#6).

        A retry re-launch within a Run, and a resume re-running an incomplete
        Node, both target a Node whose row already exists, so they UPDATE its
        status rather than INSERT (which would breach the ``(run_id, node_id)``
        PK). The first launch of a Node still goes through ``record_node_started``.
        """
        self._execute(
            "UPDATE node SET status = ? WHERE run_id = ? AND node_id = ?",
            (RUNNING, run_id, node_id),
        )

    def record_node_finished(
        self, run_id: str, node_id: str, status: NodeStatus, cause: str | None = None
    ) -> None:
        """Drive an existing Node row to a terminal status, with an optional cause.

        ``cause`` names WHY a Node was skipped (#7) — a closed `when` gate, a
        failed blocker, or a tolerant join with no executed branch — when a Node
        that already has a row is flipped to ``skipped`` (the resume re-skip
        path). For a non-skip terminal status it stays ``None``.
        """
        self._execute(
            "UPDATE node SET status = ?, cause = ? WHERE run_id = ? AND node_id = ?",
            (status, cause, run_id, node_id),
        )

    def record_node_skipped(self, run_id: str, node_id: str, cause: str | None = None) -> None:
        """Record a Node that was never attempted, with WHY it was skipped (#7).

        A skipped Node has no prior ``running`` row and no Attempt, so it is
        inserted straight into its terminal ``skipped`` status (#4). ``cause``
        records whether the skip came from a closed `when` gate (``when_false``),
        a failed blocker (``blocked``), or a tolerant join with no executed
        branch (``all_branches_skipped``), so a Reporter can distinguish them.
        """
        self._execute(
            "INSERT INTO node (run_id, node_id, status, cause) VALUES (?, ?, ?, ?)",
            (run_id, node_id, SKIPPED, cause),
        )

    def record_node_awaiting(self, run_id: str, node_id: str) -> None:
        """Record a human_gate Node as ``awaiting`` approval (#10, ADR 0010).

        A gate launches no Attempt, so like a skipped Node it has no prior
        ``running`` row on first park: it is inserted straight into ``awaiting``.
        A plain resume of an already-parked Run re-parks the same gate, whose row
        now exists, so the write UPSERTs on the ``(run_id, node_id)`` PK to stay
        idempotent rather than breaching it.
        """
        self._execute(
            "INSERT INTO node (run_id, node_id, status) VALUES (?, ?, ?) "
            "ON CONFLICT(run_id, node_id) DO UPDATE SET status = excluded.status",
            (run_id, node_id, AWAITING),
        )

    def record_attempt(
        self,
        run_id: str,
        node_id: str,
        attempt: int,
        started_at: str,
        finished_at: str,
        exit_status: int,
        output: dict[str, Any],
    ) -> None:
        self._execute(
            "INSERT INTO attempt"
            " (run_id, node_id, attempt, started_at, finished_at, exit_status, output_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, node_id, attempt, started_at, finished_at, exit_status, json.dumps(output)),
        )

    def record_run_group_membership(
        self, run_id: str, run_group_id: str, iteration_index: int
    ) -> None:
        """Record that a Run belongs to a Run Group at a given iteration index (#15).

        A Pattern Controller writes this after ``execute_run`` finalized the
        iteration's Run, so the Run itself carries which group and which iteration
        it is — queryable from the Run, satisfying AC3. The row is a denormalized
        mirror of the authoritative ``group.json``; ``INSERT OR REPLACE`` keeps a
        re-materialized membership write idempotent.
        """
        self._execute(
            "INSERT OR REPLACE INTO run_group_membership"
            " (run_id, run_group_id, iteration_index) VALUES (?, ?, ?)",
            (run_id, run_group_id, iteration_index),
        )

    def run_group_membership(self, run_id: str) -> tuple[str, int] | None:
        """The ``(run_group_id, iteration_index)`` of a Run, or ``None`` if standalone.

        A Run not materialized by a Pattern Controller has no membership row, so a
        standalone single-Run directory reads ``None`` and is undisturbed by the
        additive table.
        """
        row = self._connection.execute(
            "SELECT run_group_id, iteration_index FROM run_group_membership WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return None if row is None else (str(row[0]), int(row[1]))

    def node_table_has_cause(self) -> bool:
        """Whether the `node` table carries the `cause` column (#76).

        The `cause` column was added (#7) via ``CREATE TABLE IF NOT EXISTS`` only,
        which is a no-op against a `node` table that already exists, so a run
        directory created before that column has a `node` table lacking it. Every
        terminal Node write goes through ``record_node_finished``, which always
        sets `cause`, so a missing column makes the FIRST such write crash with a
        raw ``sqlite3.OperationalError``. Resume reads this to refuse a pre-`cause`
        run directory up front with an actionable error instead (#76).
        """
        columns = self._connection.execute("PRAGMA table_info(node)").fetchall()
        return any(column[1] == "cause" for column in columns)

    def run_status(self, run_id: str) -> str | None:
        """The recorded status of a Run, or ``None`` if no such Run exists.

        Resume reads this to gate eligibility: a Run that already ``succeeded``
        has nothing to do, an unknown Run id (``None``) is refused, and any other
        terminal/interrupted status is resumable (#6). Returning ``None`` rather
        than raising lets the resume entry point own the error message.
        """
        row = self._connection.execute(
            "SELECT status FROM run WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else str(row[0])

    def run_created_at(self, run_id: str) -> str | None:
        """The recorded creation timestamp of a Run, or ``None`` if no such Run exists."""
        row = self._connection.execute(
            "SELECT created_at FROM run WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else str(row[0])

    def node_statuses(self, run_id: str) -> dict[str, str]:
        """Map each recorded Node of a Run to its status.

        Resume classifies from this map: a ``succeeded`` Node is done (seeded
        satisfied so its dependents can run); every other recorded Node — and any
        Node with no row at all (never started) — is eligible to (re-)run (#6).
        """
        return {
            str(node_id): str(status)
            for node_id, status in self._connection.execute(
                "SELECT node_id, status FROM node WHERE run_id = ?", (run_id,)
            )
        }

    def node_causes(self, run_id: str) -> dict[str, str | None]:
        """Map each recorded Node of a Run to its skip cause, or ``None``.

        A skipped Node records WHY it was skipped (#7) — ``when_false``, ``blocked``,
        or ``all_branches_skipped``; every other Node has no cause. A Reporter reads
        this so the three skip reasons render distinctly rather than as a generic
        ``skipped`` (ADR 0007).
        """
        return {
            str(node_id): (None if cause is None else str(cause))
            for node_id, cause in self._connection.execute(
                "SELECT node_id, cause FROM node WHERE run_id = ?", (run_id,)
            )
        }

    def node_output(self, run_id: str, node_id: str) -> dict[str, Any] | None:
        """The latest Attempt's persisted normalized output for a Node, or ``None``.

        On resume a `when` predicate may reference a dependency that was a prior
        SUCCESS (seeded ``satisfied`` with no in-memory NodeResult), so its output
        must be read back from State to evaluate the predicate (#7). The latest
        Attempt (highest ``attempt`` number) is the terminal one. A Node with no
        recorded Attempt returns ``None``.
        """
        row = self._connection.execute(
            "SELECT output_json FROM attempt WHERE run_id = ? AND node_id = ?"
            " ORDER BY attempt DESC LIMIT 1",
            (run_id, node_id),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        loaded: dict[str, Any] = json.loads(row[0])
        return loaded

    def max_attempt_per_node(self, run_id: str) -> dict[str, int]:
        """Map each Node of a Run to the highest Attempt number it has recorded.

        Resume continues numbering a re-run Node from ``max + 1`` so a fresh
        Attempt never collides with one already in the ``attempt`` table for that
        Node (the ``(run_id, node_id, attempt)`` PK); a Node with no Attempt is
        simply absent and starts at Attempt 1 (#6).
        """
        return {
            str(node_id): int(highest)
            for node_id, highest in self._connection.execute(
                "SELECT node_id, MAX(attempt) FROM attempt WHERE run_id = ? GROUP BY node_id",
                (run_id,),
            )
        }

    def _execute(self, query: str, parameters: tuple[Any, ...]) -> None:
        self._connection.execute(query, parameters)
        self._connection.commit()
