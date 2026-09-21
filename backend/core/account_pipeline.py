"""Account-config–driven reconciliation pipeline.

Drop-in companion to `pipeline.py`. Instead of accepting a hand-built
`MatchConfig`, this module reads the account definition from `accounts.json`,
pre-processes each side (filters → transforms → status normalisation) and
then delegates to the same `compute_buckets` + `build_workbook` machinery.

The UI only needs to call `run_account_reconciliation`; no mapping dropdowns
are required.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from . import audit
from .account_config import (
    AccountConfig,
    get_account,
    make_match_config,
    prepare_side,
)
from .config import LoadSpec
from .exporter import build_workbook
from .loader import read_table
from .matcher import ReconResult, compute_buckets
from .pipeline import RunOutcome, _slug  # reuse the slug helper & RunOutcome dataclass

log = logging.getLogger(__name__)


def run_account_reconciliation(
    *,
    account_name: str,
    file_left: bytes,
    name_left: str,
    file_right: bytes,
    name_right: str,
    operator: str = "unknown",
    db_path: str = audit.DEFAULT_DB,
    row_cap: int | None = None,
) -> RunOutcome:
    """Execute a full account-config–driven reconciliation.

    Steps
    -----
    1. Load the `AccountConfig` for `account_name` from accounts.json.
    2. Parse both files using the header_row from the config (no UI slider needed).
    3. Apply filters, transforms and status normalisation to each side.
    4. Build a `MatchConfig` from the config's key_col / amount_col definitions.
    5. Run `compute_buckets` → `build_workbook` → `audit.record_run`.
    6. Return a `RunOutcome` identical to what `run_reconciliation` returns, so
       the UI code can stay the same after the swap.
    """
    started = datetime.now(timezone.utc)

    account: AccountConfig = get_account(account_name)
    left_cfg  = account.left
    right_cfg = account.right

    log.info(
        "account run start account=%s operator=%s left=%s right=%s",
        account_name, operator, name_left, name_right,
    )

    # ── 1. Ingest ────────────────────────────────────────────────────────────
    spec_left  = LoadSpec(header_row=left_cfg.header_row)
    spec_right = LoadSpec(header_row=right_cfg.header_row)

    sha_left  = audit.fingerprint(file_left)
    sha_right = audit.fingerprint(file_right)

    df_left  = read_table(file_left,  name_left,  spec_left)
    df_right = read_table(file_right, name_right, spec_right)

    # ── 2. Pre-process (filters + transforms + status normalisation) ──────────
    df_left  = prepare_side(df_left,  left_cfg)
    df_right = prepare_side(df_right, right_cfg)

    # ── 3. Build MatchConfig from account definition ──────────────────────────
    config = make_match_config(account)

    # ── 4. Dedup-check ────────────────────────────────────────────────────────
    schema_hash = config.schema_hash()
    duplicate   = audit.find_duplicate_run(schema_hash, sha_left, sha_right, db_path)

    # ── 5. Match ──────────────────────────────────────────────────────────────
    result: ReconResult = compute_buckets(df_left, df_right, config)

    # ── 6. Export ─────────────────────────────────────────────────────────────
    stamp    = started.strftime("%Y%m%d-%H%M%S")
    run_id   = f"run-{stamp}-{schema_hash[:6]}"
    filename = f"recon_{_slug(account.output_prefix)}_{stamp}.xlsx"

    if row_cap is None and os.environ.get("RECON_SHEET_ROW_CAP"):
        row_cap = int(os.environ["RECON_SHEET_ROW_CAP"])

    workbook = build_workbook(
        result,
        {
            "generated_at": started.isoformat(timespec="seconds"),
            "run_id": run_id,
            "operator": operator,
            "file_a": name_left,
            "file_b": name_right,
        },
        row_cap=row_cap,
    )

    # ── 7. Audit ──────────────────────────────────────────────────────────────
    audit.record_run(
        {
            "run_id": run_id,
            "created_at": started.isoformat(timespec="seconds"),
            "operator": operator,
            "profile_name": account_name,
            "schema_hash": schema_hash,
            "file_a_name": name_left,
            "file_b_name": name_right,
            "file_a_sha256": sha_left,
            "file_b_sha256": sha_right,
            "rows_read_a": result.counts.rows_read_a,
            "rows_read_b": result.counts.rows_read_b,
            "exact_matches": result.counts.exact_matches,
            "value_mismatches": result.counts.value_mismatches,
            "orphans_a": result.counts.orphans_a,
            "orphans_b": result.counts.orphans_b,
            "duration_s": result.duration_s,
            "mapping": {
                "account": account_name,
                "left_key": left_cfg.key_col,
                "right_key": right_cfg.key_col,
                "amount_left": left_cfg.amount_col,
                "amount_right": right_cfg.amount_col,
            },
            "warnings": result.counts.warnings,
        },
        db_path,
    )

    log.info(
        "account run complete account=%s exact=%d mismatch=%d orphan_a=%d orphan_b=%d in %.3fs",
        account_name,
        result.counts.exact_matches,
        result.counts.value_mismatches,
        result.counts.orphans_a,
        result.counts.orphans_b,
        result.duration_s,
    )

    return RunOutcome(result, workbook, run_id, filename, duplicate)
