"""
backfill_fonepay_rrn.py
One-time (or occasional) bulk-seed of the Fonepay RRN -> Ticket No lookup
table (fonepay_rrn_lookup.csv) from historical Fonepay Ledger data, so a
refund posting today can resolve against an RRN from weeks/months ago
without waiting for the normal day-by-day accumulation to build that
history up naturally.

Handles BOTH:
  - one single file containing multiple months of combined transactions
  - a folder of separate daily files

Each row's OWN transaction date (from the configured date column, default
'Recorded Date') is preserved as that entry's _seen_date, so retention
pruning stays accurate afterward - a transaction from 2 months ago is
correctly treated as 2 months old, not as if it happened today.

USAGE:
    python -m backend.airlines.backfill_fonepay_rrn <path-to-file-or-folder>

Examples:
    python -m backend.airlines.backfill_fonepay_rrn "C:/Backfill/fonepay_last_2_months.xlsx"
    python -m backend.airlines.backfill_fonepay_rrn "C:/Backfill/daily_files/"

Reads header_rows.ledger_b, match_keys.ledger_b, and fonepay_rrn_lookup
settings straight from config.yaml, so it stays in sync with the main
pipeline automatically - no separate settings to maintain here.
"""
import os
import sys
import glob
import pandas as pd

from . import loader
from . import refund_state
from . import engine

SUPPORTED_EXT = (".xlsx", ".xlsm", ".xls", ".csv")


load_config = engine.load_config


def collect_input_files(input_path):
    """Returns a list of file paths - handles a single file OR a folder
    containing multiple files, in either case returning every supported
    file found (skips Excel lock files like ~$file.xlsx)."""
    if os.path.isfile(input_path):
        return [input_path]
    if os.path.isdir(input_path):
        files = []
        for ext in SUPPORTED_EXT:
            files.extend(glob.glob(os.path.join(input_path, f"*{ext}")))
        files = [f for f in files if not os.path.basename(f).startswith("~$")]
        return sorted(files)
    raise FileNotFoundError(f"Not a file or folder: {input_path}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m backend.airlines.backfill_fonepay_rrn <path-to-file-or-folder>")
        sys.exit(1)

    input_path = sys.argv[1]
    cfg = load_config()

    header_cfg = cfg["header_rows"]["ledger_b"]
    match_key = cfg["match_keys"]["ledger_b"]
    rrn_lookup_cfg = cfg.get("fonepay_rrn_lookup", {})
    rrn_col = rrn_lookup_cfg.get("ledger_b_rrn_column", "Retrieval Reference No")
    date_col = rrn_lookup_cfg.get("date_column", "Recorded Date")
    lookup_fields = rrn_lookup_cfg.get("lookup_fields", ["Ticket No"])
    retention_days = rrn_lookup_cfg.get("retention_days", 180)
    state_file = rrn_lookup_cfg.get("state_file")

    if not state_file:
        print("ERROR: fonepay_rrn_lookup.state_file is not set in config.yaml - nothing to backfill into.")
        sys.exit(1)
    state_path = engine.resolve_state_path(state_file)

    files = collect_input_files(input_path)
    if not files:
        print(f"No supported files ({', '.join(SUPPORTED_EXT)}) found at {input_path}")
        sys.exit(1)

    print(f"Found {len(files)} file(s) to backfill from:")
    for f in files:
        print(f"   {os.path.basename(f)}")

    all_rows = []
    for f in files:
        try:
            df = loader.load_source(f, header_cfg)
            df = loader.drop_junk_rows(df, match_key)
        except Exception as e:
            print(f"  [SKIP] Could not read {os.path.basename(f)}: {e}")
            continue

        if rrn_col not in df.columns:
            print(f"  [SKIP] {os.path.basename(f)}: column '{rrn_col}' not found. "
                  f"Available columns: {list(df.columns)}")
            continue

        if date_col not in df.columns:
            print(f"  [WARN] {os.path.basename(f)}: date column '{date_col}' not found - "
                  "rows from this file will be dated as of today instead of their real "
                  "transaction date. Available columns: {list(df.columns)}")

        # Same lean field set as the daily accumulation in run_recon.py -
        # this table is meant to grow large over months, so only keep
        # RRN + configured lookup_fields + date, not all ~25 Ledger B columns.
        keep_cols = [rrn_col] + [c for c in lookup_fields if c != rrn_col]
        if date_col not in keep_cols and date_col in df.columns:
            keep_cols.append(date_col)
        keep_cols = [c for c in keep_cols if c in df.columns]
        df = df[keep_cols].copy()

        print(f"  [OK] {os.path.basename(f)}: {len(df)} rows")
        all_rows.append(df)

    if not all_rows:
        print("\nNothing usable was read from any file - nothing was backfilled.")
        sys.exit(1)

    combined = pd.concat(all_rows, ignore_index=True, sort=False)
    print(f"\nTotal combined rows before dedup: {len(combined)}")

    # if a row's date can't be found/parsed, it falls back to 'today' inside
    # merge_reference_lookup - that's fine for a handful of stragglers but
    # would silently misdate everything if the date column is wrong, hence
    # the per-file WARN above.
    result = refund_state.merge_reference_lookup(
        combined, rrn_col, state_path,
        retention_days=retention_days,
        as_of=None,          # real "today" - retention pruning must be relative to now
        date_col=date_col,   # each row keeps its OWN historical date
    )

    print(f"\nDone. Lookup table now has {len(result)} RRN(s) total, saved to:")
    print(f"   {state_path}")
    print("\nRun this again any time you want to top up the lookup table with more "
          "historical data - re-running is safe, duplicate RRNs just get refreshed.")


if __name__ == "__main__":
    main()
