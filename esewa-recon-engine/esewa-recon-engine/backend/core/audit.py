"""Lightweight run-audit log (SQLite by default, Postgres-compatible schema).

Reconciliation output is evidence. Six weeks later someone will ask *which*
mapping produced the workbook that closed a NPR 40,000 break — this table is
the answer. Idempotency check: same schema hash + same input fingerprints =>
same buckets, so a repeated run is detectable rather than re-litigated.

No ORM by design (see the engineering rules): one table, three statements.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_DB = os.environ.get("RECON_AUDIT_DB", "recon_audit.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recon_runs (
    run_id            TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL,
    operator          TEXT,
    profile_name      TEXT,
    schema_hash       TEXT NOT NULL,
    file_a_name       TEXT,
    file_b_name       TEXT,
    file_a_sha256     TEXT,
    file_b_sha256     TEXT,
    rows_read_a       INTEGER NOT NULL,
    rows_read_b       INTEGER NOT NULL,
    exact_matches     INTEGER NOT NULL,
    value_mismatches  INTEGER NOT NULL,
    orphans_a         INTEGER NOT NULL,
    orphans_b         INTEGER NOT NULL,
    duration_s        REAL,
    mapping_json      TEXT,
    warnings_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_recon_runs_created ON recon_runs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_recon_runs_hash ON recon_runs (schema_hash);
"""

_INSERT = """
INSERT OR REPLACE INTO recon_runs (
    run_id, created_at, operator, profile_name, schema_hash,
    file_a_name, file_b_name, file_a_sha256, file_b_sha256,
    rows_read_a, rows_read_b, exact_matches, value_mismatches,
    orphans_a, orphans_b, duration_s, mapping_json, warnings_json
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def fingerprint(raw: bytes) -> str:
    """Content hash of an uploaded file — identifies a re-run of the same inputs."""
    return hashlib.sha256(raw).hexdigest()[:32]


def init_db(db_path: str = DEFAULT_DB) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


def record_run(record: dict, db_path: str = DEFAULT_DB) -> str:
    """Persist one run. Never raises into the request path — an audit-write
    failure must not destroy a reconciliation the operator already waited for;
    it is logged loudly instead."""
    run_id = record.get("run_id") or datetime.now(timezone.utc).strftime("run-%Y%m%d-%H%M%S-%f")
    try:
        init_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                _INSERT,
                (
                    run_id,
                    record.get("created_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    record.get("operator"),
                    record.get("profile_name"),
                    record["schema_hash"],
                    record.get("file_a_name"),
                    record.get("file_b_name"),
                    record.get("file_a_sha256"),
                    record.get("file_b_sha256"),
                    int(record["rows_read_a"]),
                    int(record["rows_read_b"]),
                    int(record["exact_matches"]),
                    int(record["value_mismatches"]),
                    int(record["orphans_a"]),
                    int(record["orphans_b"]),
                    float(record.get("duration_s", 0.0)),
                    json.dumps(record.get("mapping", {}), default=str),
                    json.dumps(record.get("warnings", [])),
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.error("audit write failed for %s: %s", run_id, exc)
    return run_id


def recent_runs(limit: int = 20, db_path: str = DEFAULT_DB) -> list[dict]:
    try:
        init_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM recon_runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.error("audit read failed: %s", exc)
        return []


def find_duplicate_run(schema_hash: str, sha_a: str, sha_b: str, db_path: str = DEFAULT_DB) -> dict | None:
    """Idempotency probe: has this exact mapping already been run on these exact files?"""
    try:
        init_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM recon_runs WHERE schema_hash=? AND file_a_sha256=? AND file_b_sha256=?"
                " ORDER BY created_at DESC LIMIT 1",
                (schema_hash, sha_a, sha_b),
            ).fetchone()
            return dict(row) if row else None
    except Exception as exc:  # noqa: BLE001
        log.error("audit probe failed: %s", exc)
        return None
