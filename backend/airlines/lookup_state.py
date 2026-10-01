"""
lookup_state.py
Diagnostic tool: search for a specific ticket number and/or RRN across
the pipeline's persistent state files, to answer "is this actually in
there, and if so, in what exact form?" - without having to open CSVs by
hand and eyeball them.

Checks BOTH raw string matches (exact, what you typed) AND normalized
matches (via the same normalize_ticket() used everywhere in the
pipeline) - if something matches only when normalized but not exactly,
that itself tells you there's a formatting difference between sources
(the kind of thing that's silently broken a match before in this project).

USAGE:
    python -m backend.airlines.lookup_state --ticket 32386504
    python -m backend.airlines.lookup_state --rrn RRN123456
    python -m backend.airlines.lookup_state --ticket 32386504 --rrn RRN123456
"""
import sys
import argparse
import pandas as pd

from . import prep
from . import refund_state
from . import engine as run_recon

load_config = run_recon.load_config


def resolve_state_path(cfg, dotted_path, default=None):
    """dotted_path like 'refund_reconciliation.state_file'."""
    node = cfg
    for part in dotted_path.split("."):
        node = node.get(part, {}) if isinstance(node, dict) else {}
    value = node if isinstance(node, str) else default
    if not value:
        return None
    return run_recon.resolve_state_path(value)


def search_df(df, label, search_value, columns_to_check):
    """Searches the given columns for an exact raw match and a
    normalize_ticket() match, printing what it finds (or doesn't)."""
    if df is None or df.empty:
        print(f"  [{label}] file is empty or missing - nothing to search")
        return

    norm_search = prep.normalize_ticket(search_value)
    found_any = False

    for col in columns_to_check:
        if col not in df.columns:
            continue
        raw_series = df[col].astype(str)
        exact_hits = df[raw_series == str(search_value)]
        norm_hits = df[raw_series.apply(prep.normalize_ticket) == norm_search]

        if not exact_hits.empty:
            found_any = True
            print(f"  [{label}] EXACT match in column '{col}':")
            print(exact_hits.to_string(index=False))
        elif not norm_hits.empty:
            found_any = True
            print(f"  [{label}] found in column '{col}' but NOT an exact string match "
                  f"(normalizes the same - likely a formatting difference):")
            print(norm_hits.to_string(index=False))
            for raw_val in norm_hits[col].astype(str).unique():
                print(f"           you searched: {search_value!r}   file has: {raw_val!r}")

    if not found_any:
        print(f"  [{label}] NOT found in any of: {columns_to_check}")


def main():
    parser = argparse.ArgumentParser(description="Search state files for a ticket number or RRN.")
    parser.add_argument("--ticket", help="Ticket number to search for")
    parser.add_argument("--rrn", help="Retrieval Reference Number to search for")
    parser.add_argument("--input-dir", default=None,
                         help="Folder holding today's input files (default: airlines_input/)")
    parser.add_argument("--skip-today", action="store_true",
                         help="Only check persistent state files, skip loading today's input files")
    args = parser.parse_args()

    if not args.ticket and not args.rrn:
        print("Provide at least --ticket or --rrn. See --help.")
        sys.exit(1)

    cfg = load_config()

    pending_path = resolve_state_path(cfg, "refund_reconciliation.state_file")
    rrn_path = resolve_state_path(cfg, "fonepay_rrn_lookup.state_file")
    refund_ticket_col = cfg.get("airline_3_refund_check", {}).get("ticket_column", "TICKET NO")
    rrn_col = cfg.get("fonepay_rrn_lookup", {}).get("ledger_b_rrn_column", "Retrieval Reference No")
    rrn_col_refund = cfg["match_keys"].get("fonepay_refund", "RETRIEVAL_REFERENCE_NUMBER")

    if args.ticket:
        print(f"\n=== Searching for ticket: {args.ticket} ===\n")

        print(f"1. Pending Buddha refunds (carry-forward backlog): {pending_path}")
        pending_df = refund_state.load_pending(pending_path) if pending_path else pd.DataFrame()
        search_df(pending_df, "pending_refunds", args.ticket, [refund_ticket_col])

        print(f"\n2. Fonepay RRN lookup table (accumulated history): {rrn_path}")
        rrn_df = refund_state.load_reference_lookup(rrn_path) if rrn_path else pd.DataFrame()
        search_df(rrn_df, "fonepay_rrn_lookup", args.ticket, ["Ticket No"])

    if args.rrn:
        print(f"\n=== Searching for RRN: {args.rrn} ===\n")

        print(f"Fonepay RRN lookup table (accumulated history): {rrn_path}")
        rrn_df = refund_state.load_reference_lookup(rrn_path) if rrn_path else pd.DataFrame()
        search_df(rrn_df, "fonepay_rrn_lookup", args.rrn, [rrn_col])

    if not args.skip_today:
        print("\n=== Checking TODAY's actual input files (this is what a match is decided against) ===")
        try:
            file_paths = run_recon.detect_input_files(cfg, args.input_dir)
        except Exception as e:
            print(f"  Could not detect today's input files: {e}")
            file_paths = {}

        cancellation_source = cfg.get("refund_reconciliation", {}).get("cancellation_source", "cancellation_report")
        cancellation_match_col = cfg["match_keys"].get(cancellation_source)

        if args.ticket:
            print(f"\n3. Today's eSewa Refund file (column '{cancellation_match_col}'):")
            try:
                cancellation_df = run_recon.load_cancellation_report(cfg, file_paths)
            except Exception as e:
                cancellation_df = None
                print(f"  Could not load today's eSewa Refund file: {e}")
            if cancellation_df is not None and not cancellation_df.empty and cancellation_match_col in cancellation_df.columns:
                norm_search = prep.normalize_ticket(args.ticket)
                found = False
                for idx, raw in cancellation_df[cancellation_match_col].astype(str).items():
                    tickets_in_row = [t.strip() for t in raw.split(",")]
                    if any(prep.normalize_ticket(t) == norm_search for t in tickets_in_row):
                        found = True
                        print(f"  [today's eSewa Refund] found in row: {raw}")
                if not found:
                    print(f"  [today's eSewa Refund] NOT found - eSewa hasn't posted this cancellation TODAY "
                          "(may post on a future day and get picked up automatically then).")
            else:
                print("  [today's eSewa Refund] file not found/empty today.")

        if args.rrn or args.ticket:
            print(f"\n4. Today's Fonepay Refund file (column '{rrn_col_refund}'):")
            try:
                fonepay_refund_raw = run_recon.load_fonepay_refund(cfg, file_paths)
            except Exception as e:
                fonepay_refund_raw = None
                print(f"  Could not load today's Fonepay Refund file: {e}")
            if fonepay_refund_raw is not None and not fonepay_refund_raw.empty and rrn_col_refund in fonepay_refund_raw.columns:
                search_value = args.rrn if args.rrn else None
                if search_value:
                    search_df(fonepay_refund_raw, "today's Fonepay Refund", search_value, [rrn_col_refund])
                if args.ticket and not args.rrn:
                    print("  (searched by --rrn only - if you know the RRN for this ticket from the "
                        "lookup table above, re-run with --rrn <that value> to check today's Fonepay "
                        "Refund file directly; the Refund file itself has no ticket number column.)")
            else:
                print("  [today's Fonepay Refund] file not found/empty today.")

    print("\nDone.")


if __name__ == "__main__":
    main()
