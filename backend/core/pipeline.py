"""End-to-end orchestration: ingest -> sanitize -> bucket -> export -> audit.

This is the only module the UI (Streamlit or FastAPI) needs to import. Keeping
the coordinator thin and the stages pure means the same code path serves the
web app, a cron job, and the test suite without a second implementation drifting
out of sync.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import polars as pl

from . import audit
from .config import LoadSpec, MatchConfig
from .csv_exporter import build_csv_archive
from .loader import read_table
from .matcher import ReconResult, compute_buckets

log = logging.getLogger(__name__)


@dataclass
class RunOutcome:
    result: ReconResult
    workbook: bytes
    run_id: str
    filename: str
    duplicate_of: dict | None = None


def run_reconciliation(
    *,
    file_a: bytes,
    name_a: str,
    file_b: bytes,
    name_b: str,
    config: MatchConfig,
    spec_a: LoadSpec | None = None,
    spec_b: LoadSpec | None = None,
    operator: str = "unknown",
    db_path: str = audit.DEFAULT_DB,
    row_cap: int | None = None,
) -> RunOutcome:
    """Execute a full reconciliation and return the workbook plus audit handle."""
    started = datetime.now(timezone.utc)
    sha_a, sha_b = audit.fingerprint(file_a), audit.fingerprint(file_b)
    schema_hash = config.schema_hash()
    log.info("run start operator=%s profile=%s schema=%s", operator, config.profile_name, schema_hash)

    duplicate = audit.find_duplicate_run(schema_hash, sha_a, sha_b, db_path)

    df_a = read_table(file_a, name_a, spec_a)
    df_b = read_table(file_b, name_b, spec_b)

    result = compute_buckets(df_a, df_b, config)

    stamp = started.strftime("%Y%m%d-%H%M%S")
    run_id = f"run-{stamp}-{schema_hash[:6]}"
    filename = f"recon_{_slug(config.profile_name)}_{stamp}.zip"

    if row_cap is None and os.environ.get("RECON_SHEET_ROW_CAP"):
        row_cap = int(os.environ["RECON_SHEET_ROW_CAP"])

    workbook = build_csv_archive(
        result,
        {
            "generated_at": started.isoformat(timespec="seconds"),
            "run_id": run_id,
            "operator": operator,
            "file_a": name_a,
            "file_b": name_b,
        },
        row_cap=row_cap,
    )

    audit.record_run(
        {
            "run_id": run_id,
            "created_at": started.isoformat(timespec="seconds"),
            "operator": operator,
            "profile_name": config.profile_name,
            "schema_hash": schema_hash,
            "file_a_name": name_a,
            "file_b_name": name_b,
            "file_a_sha256": sha_a,
            "file_b_sha256": sha_b,
            "rows_read_a": result.counts.rows_read_a,
            "rows_read_b": result.counts.rows_read_b,
            "exact_matches": result.counts.exact_matches,
            "value_mismatches": result.counts.value_mismatches,
            "orphans_a": result.counts.orphans_a,
            "orphans_b": result.counts.orphans_b,
            "duration_s": result.duration_s,
            "mapping": {
                "keys": [(k.col_a, k.col_b, k.date_mode) for k in config.keys],
                "values": [(v.col_a, v.col_b, v.numeric) for v in config.values],
                "epsilon": config.epsilon,
            },
            "warnings": result.counts.warnings,
        },
        db_path,
    )
    return RunOutcome(result, workbook, run_id, filename, duplicate)


def preview(df: pl.DataFrame, rows: int = 25) -> pl.DataFrame:
    return df.head(rows)


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in text).strip("_").lower() or "custom"
