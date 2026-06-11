"""Durable Run State persisted as SQLite inside the run directory."""

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE run (
    run_id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    definition_checksum TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE node (
    run_id TEXT NOT NULL REFERENCES run (run_id),
    node_id TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (run_id, node_id)
);
CREATE TABLE attempt (
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
"""


def initialize_state(path: Path) -> None:
    """Create the State database with its schema in the run directory."""
    with sqlite3.connect(path) as connection:
        connection.executescript(_SCHEMA)
