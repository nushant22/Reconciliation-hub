"""
engine.py  (formerly run_recon.py)
Airline reconciliation engine. Runs the full pipeline:
  1. Auto-detect the input files (by column signature, not filename)
  2. Load + clean each (unmerge, header rows, drop junk rows)
  3. Apply source-specific prep (split, status, dedup, $-drop, etc.)
  4. Match each airline against Ledger A and Ledger B by ticket number
  5. Build one formatted Excel report per airline
  6. Save with a timestamp into the output folder

Every rule (names, header rows, match keys, amount columns, report fields,
prep switches, refund/cashback/aging logic, state files) lives in
``config.yaml`` next to this file - edit that, not the Python.

Used two ways:
  * The Streamlit "Airlines" page calls ``run_airline_reconciliation``.
  * From a terminal:  python -m backend.airlines.engine <input_dir> [output_dir]
"""
import os
import sys
import shutil
import tempfile
import traceback
from datetime import datetime

import pandas as pd
import yaml

from . import file_mapper
from . import loader
from . import prep
from . import matcher
from . import report
from . import refund_state
from .utils import select_fields_by_letters

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(PACKAGE_DIR))
CONFIG_PATH = os.path.join(PACKAGE_DIR, "config.yaml")

# Persistent folder for the carry-forward state (pending refunds, Fonepay
# RRN history). Deliberately OUTSIDE any per-run temp folder so it survives
# between runs. Override with the AIRLINES_STATE_DIR environment variable.
STATE_DIR = os.environ.get("AIRLINES_STATE_DIR") or os.path.join(REPO_ROOT, "airlines_state")

# Kept for backwards compatibility with the original script's globals; the
# real folders are now passed into run()/detect_input_files() explicitly.
BASE_DIR = STATE_DIR
INPUT_DIR = os.path.join(REPO_ROOT, "airlines_input")
OUTPUT_DIR = os.path.join(REPO_ROOT, "airlines_output")


def resolve_state_path(state_file):
    """Turns a config.yaml state_file value into a real path.

    * relative path            -> inside STATE_DIR (persistent app folder)
    * absolute path            -> used exactly as written
    * Windows drive path (e.g. "C:/ReconState/x.csv") on a non-Windows
      host (Linux/macOS/cloud) can't exist there, so it falls back to
      STATE_DIR/<file name> and prints a note instead of silently creating
      a stray "C:" folder.
    Returns None if state_file is empty."""
    if not state_file:
        return None
    state_file = str(state_file)
    looks_windows = len(state_file) > 2 and state_file[1] == ":" and state_file[0].isalpha()
    if looks_windows and os.name != "nt":
        fallback = os.path.join(STATE_DIR, os.path.basename(state_file.replace("\\", "/")))
        print(f"  [state] '{state_file}' is a Windows path and this server is not Windows - "
              f"using {fallback} instead.")
        return fallback
    if os.path.isabs(state_file):
        return state_file
    return os.path.join(STATE_DIR, state_file)


SOURCES = ["ledger_a", "ledger_b", "airline_1", "airline_2", "airline_3"]
AIRLINES = ["airline_1", "airline_2", "airline_3"]
LEDGERS = ["ledger_a", "ledger_b"]


def load_config(path=None):
    with open(path or CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_and_prep_all(cfg, file_paths):
    """Returns dict source -> cleaned+prepped dataframe"""
    prepped = {}
    for source in SOURCES:
        fp = file_paths[source]
        header_cfg = cfg["header_rows"][source]
        match_key = cfg["match_keys"][source]
        sheet_name = (cfg.get("sheet_names") or {}).get(source)

        df = loader.load_source(fp, header_cfg, sheet_name=sheet_name)
        df = loader.drop_junk_rows(df, match_key)

        if source == "ledger_a":
            # eSewa now contains everything (Complete + Reversed rows) in
            # one file - the old separate "eSewa 2" lookup file is
            # redundant and no longer loaded/used.
            ledger_a_prep_cfg = cfg.get("prep", {}).get("ledger_a", {})
            df = prep.prep_ledger_a(
                df,
                match_key_col=match_key,
                txn_code_col=ledger_a_prep_cfg.get("txn_code_column", "Parent_Txn_code"),
                child_txn_col=ledger_a_prep_cfg.get("child_txn_column", "child_txn"),
                amount_col=ledger_a_prep_cfg.get("amount_column", "Txn_Amount"),
                child_status_col=ledger_a_prep_cfg.get("child_status_column", "Child_Status"),
                reversed_status_values=ledger_a_prep_cfg.get(
                    "reversed_status_values", ["FULL_REFUND", "PARTIAL_REFUND", "REVERTED"]
                ),
            )
        else:
            df = prep.PREP_FUNCS[source](df)

        prepped[source] = df
        print(f"  [OK] {source}: {len(df)} rows, {len(df.columns)} columns")
    return prepped


def load_airline3_refund_data(cfg, file_paths):
    """Loads the 'Refunds' sheet from the Airline 3 workbook. Returns
    (refund_df, ticket_col, refund_ticket_set), or (None, None, None) if
    disabled or the sheet/column can't be found."""
    refund_cfg = cfg.get("airline_3_refund_check", {})
    if not refund_cfg.get("enabled"):
        return None, None, None

    fp = file_paths.get("airline_3")
    if not fp:
        print("  [refund-check] airline_3 file not found - skipping Refund? tagging")
        return None, None, None

    sheet_name = refund_cfg.get("sheet_name", "Refunds")
    header_cfg = refund_cfg.get("header_rows", 1)
    ticket_col = refund_cfg.get("ticket_column", "TICKET NO")

    try:
        df = loader.load_source(fp, header_cfg, sheet_name=sheet_name)
    except Exception as e:
        print(f"  [refund-check] Could not read '{sheet_name}' sheet in {os.path.basename(fp)}: {e}")
        return None, None, None

    if ticket_col not in df.columns:
        print(f"  [refund-check] '{ticket_col}' not found in '{sheet_name}' sheet columns: "
              f"{list(df.columns)} - skipping Refund? tagging")
        return None, None, None

    df = loader.drop_junk_rows(df, ticket_col)
    df = prep.prep_airline_3(df)

    tickets = set(
        df[ticket_col].apply(prep.normalize_ticket)
        .replace({"": None, "NAN": None, "N/A": None, "NA": None, "NONE": None})
        .dropna()
    )
    print(f"  [refund-check] Loaded {len(tickets)} refunded ticket number(s) from '{sheet_name}' sheet")
    return df, ticket_col, tickets


def load_cancellation_report(cfg, file_paths):
    """Loads the optional Cancellation Report file, if configured and
    present. Returns a cleaned dataframe, or None if not
    applicable."""
    refund_cfg = cfg.get("refund_reconciliation", {})
    if not refund_cfg.get("enabled"):
        return None
    source = refund_cfg.get("cancellation_source", "cancellation_report")
    fp = file_paths.get(source)
    if not fp:
        print(f"  [refund-recon] '{source}' not found in input/ - skipping refund reconciliation "
              "(Refund? tag alone will still be used).")
        return None

    header_cfg = cfg["header_rows"][source]
    match_key = cfg["match_keys"][source]
    df = loader.load_source(fp, header_cfg)
    df = loader.drop_junk_rows(df, match_key)
    print(f"  [OK] {source}: {len(df)} rows, {len(df.columns)} columns (cancellation report)")
    return df


def load_fonepay_refund(cfg, file_paths):
    """Loads the optional Fonepay Refund file, if configured and
    present. Returns a cleaned dataframe, or None if not
    applicable."""
    source = "fonepay_refund"
    fp = file_paths.get(source)
    if not fp:
        return None

    header_cfg = cfg.get("header_rows", {}).get(source, 1)
    match_key = cfg.get("match_keys", {}).get(source, "RETRIEVAL_REFERENCE_NUMBER")
    df = loader.load_source(fp, header_cfg)
    df = loader.drop_junk_rows(df, match_key)
    print(f"  [OK] {source}: {len(df)} rows, {len(df.columns)} columns (Fonepay refund report)")
    return df


def attach_cashback(recon_df, cashback_lookup, cashback_ticket_map, cancellation_name,
                     txn_code_col="Parent_Txn_code", ticket_col=None):
    """Adds 'Cashback Per Ticket', 'eSewa 2 Profile', and 'Cashback Lookup
    Method' columns to a matched Refund Recon dataframe."""
    if recon_df.empty:
        return recon_df

    txn_col = f"{cancellation_name}: {txn_code_col}"
    count_col = f"{cancellation_name}: Confirmed Ticket Count"
    ticket_col_full = f"{cancellation_name}: {ticket_col}" if ticket_col else None

    if txn_col not in recon_df.columns or count_col not in recon_df.columns:
        print(f"  [cashback-lookup] skipped attaching to Refund Recon - expected columns "
              f"'{txn_col}' / '{count_col}' not found. Available: {list(recon_df.columns)}")
        recon_df["Cashback Per Ticket"] = 0.0
        recon_df["eSewa 2 Profile"] = ""
        recon_df["Cashback Lookup Method"] = "not found"
        return recon_df

    def _cashback_row(row):
        method = "not found"
        txn_key = prep.normalize_ticket(row.get(txn_col, ""))
        entry = cashback_lookup.get(txn_key)
        if entry:
            method = "txn_code"
        elif ticket_col_full and ticket_col_full in recon_df.columns:
            ticket_val = prep.normalize_ticket(row.get(ticket_col_full, ""))
            fallback_txn_key = cashback_ticket_map.get(ticket_val)
            if fallback_txn_key:
                entry = cashback_lookup.get(fallback_txn_key)
                if entry:
                    method = "ticket_number_fallback"

        if not entry:
            return pd.Series({"Cashback Per Ticket": 0.0, "eSewa 2 Profile": "", "Cashback Lookup Method": method})
        if not entry["is_agent"]:
            return pd.Series({"Cashback Per Ticket": 0.0, "eSewa 2 Profile": entry["profile"], "Cashback Lookup Method": method})
        n = row.get(count_col)
        try:
            n = float(n)
        except (TypeError, ValueError):
            n = 0
        cashback = (entry["total_cashback"] / n) if n and n > 0 else 0.0
        return pd.Series({
            "Cashback Per Ticket": round(cashback, 2),
            "eSewa 2 Profile": entry["profile"],
            "Cashback Lookup Method": method,
        })

    extra = recon_df.apply(_cashback_row, axis=1)
    return pd.concat([recon_df, extra], axis=1)


def build_refund_reconciliation(cfg, refund_df, refund_ticket_col, cancellation_df, cancellation_match_col,
                                 refund_name="Airline Refund", cancellation_name="Cancellation Report"):
    """Airline Refunds vs eSewa Refund (Cancellation Report), taking Airline
    Refunds as the source of truth, with carry-forward across daily runs.

    Uses prep.prep_cancellation_report for the ledger side - the SAME
    tested logic used everywhere else in this pipeline for
    confirmed-ticket-count divisors and data-issue/pending tagging -
    instead of a separate hand-rolled reimplementation.

    Returns (combined_airline_refunds, esewa_recon_df, matched_keys)."""
    refund_cfg = cfg.get("refund_reconciliation", {})
    state_path = resolve_state_path(refund_cfg.get("state_file"))
    refund_date_col = refund_cfg.get("refund_date_column")
    txn_code_col = refund_cfg.get("txn_code_column", "Parent_Txn_code")

    if refund_df is None:
        refund_df = pd.DataFrame(columns=[refund_ticket_col])

    # --- parse refund date + bring in anything still pending from prior runs ---
    today_refunds = refund_df.copy()
    if refund_date_col and refund_date_col in today_refunds.columns:
        today_refunds["_RefundDate"] = refund_state.parse_buddha_refund_date(today_refunds[refund_date_col])
    else:
        if not today_refunds.empty:
            print(f"  [refund-recon] refund_date_column '{refund_date_col}' not found - "
                  "aging/carry-forward will show 'Unknown date' for all unmatched refunds")
        today_refunds["_RefundDate"] = pd.NaT

    if refund_ticket_col in today_refunds.columns:
        today_refunds["_norm_ticket"] = today_refunds[refund_ticket_col].apply(prep.normalize_ticket)
    else:
        today_refunds["_norm_ticket"] = pd.Series(dtype=str)

    # IMPORTANT: pending gets loaded and merged EVEN IF today's fresh
    # Airline Refunds export happens to have zero rows (a normal day with
    # no new refunds initiated) - previously this function returned early
    # before ever loading pending, which would silently WIPE OUT the
    # entire multi-day carry-forward backlog the first time a refund-free
    # day came along, since the caller only re-saves whatever this
    # function returns.
    pending_df = refund_state.load_pending(state_path)
    if not pending_df.empty:
        print(f"  [refund-recon] carrying forward {len(pending_df)} still-unmatched "
              f"refund(s) from prior run(s)")
        if refund_ticket_col in pending_df.columns:
            pending_df["_norm_ticket"] = pending_df[refund_ticket_col].apply(prep.normalize_ticket)
        combined = pd.concat([pending_df, today_refunds], ignore_index=True, sort=False)
    else:
        combined = today_refunds

    # today's fresh copy of a ticket wins over a stale carried-forward one
    if "_norm_ticket" in combined.columns:
        combined = combined[~combined["_norm_ticket"].duplicated(keep="last")].reset_index(drop=True)

    if cancellation_df is None or cancellation_df.empty:
        return combined, pd.DataFrame(), set()

    # airline refund tickets are the source of truth for confirmed-count
    # divisor logic inside prep_cancellation_report (same as everywhere
    # else in this pipeline - see prep.py docstring)
    confirmed_tickets = set(combined["_norm_ticket"].replace("", None).dropna())
    prepped_cancellation = prep.prep_cancellation_report(
        cancellation_df,
        match_key_col=cancellation_match_col,
        airline_confirmed_tickets=confirmed_tickets,
        txn_code_col=txn_code_col,
    )

    # Reuses the SAME matcher used for the main airline-vs-ledger recon,
    # instead of a separate hand-rolled merge - one matching mechanism for
    # the whole pipeline, side-by-side prefixed columns exactly like every
    # other Recon sheet, and configurable via report_fields.refund_recon.
    refund_amount_col = refund_cfg.get("refund_amount_column", "REF")
    recon_df, matched_keys = matcher.match_airline_to_ledger(
        combined, refund_ticket_col,
        prepped_cancellation, cancellation_match_col,
        ledger_prefix=cancellation_name, airline_prefix=refund_name,
        ledger_amount_col="Price Per Ticket", airline_amount_col=refund_amount_col,
    )
    if not recon_df.empty:
        letters = cfg.get("report_fields", {}).get("refund_recon", "")
        if letters:
            recon_df = select_fields_by_letters(recon_df, letters)

    return combined, recon_df, matched_keys


def build_fonepay_refund_reconciliation(cfg, fonepay_refund_df, combined_airline_refunds,
                                         refund_ticket_col,
                                         refund_name="Airline Refund", fonepay_name="Fonepay Refund",
                                         fonepay_sales_tickets=None):
    """Matches Fonepay Refund (with looked-up Ticket No via prep_fonepay_refund_lookup)
    against Airline Refunds - same matcher.match_airline_to_ledger mechanism
    used everywhere else in this pipeline, instead of a separate hand-rolled
    dict-based implementation. Returns (fonepay_recon_df, matched_ticket_keys).

    fonepay_sales_tickets: optional set of normalized ticket numbers known
    to have been sold via Fonepay (from ledger_b) - used ONLY to give a
    definitive diagnostic when 0 refunds match: were these tickets ever
    sold via Fonepay in the first place? If none were, 0 matches is
    CORRECT (they were paid via eSewa, so only eSewa Refund Recon can
    ever resolve them) - not a bug to chase."""
    if fonepay_refund_df is None or fonepay_refund_df.empty:
        return pd.DataFrame(), set()
    if combined_airline_refunds is None or combined_airline_refunds.empty:
        return pd.DataFrame(), set()
    if "Ticket No" not in fonepay_refund_df.columns:
        print("  [fonepay-refund] 'Ticket No' not found on Fonepay Refund rows - "
              "the RRN lookup against Fonepay sales (ledger_b) may have failed "
              "or found no matches. Skipping Fonepay Refund Recon.")
        return pd.DataFrame(), set()

    refund_cfg = cfg.get("refund_reconciliation", {})
    refund_amount_col = refund_cfg.get("refund_amount_column", "REF")
    fonepay_amount_col = cfg.get("amount_fields", {}).get("fonepay_refund", "REFUND_AMOUNT")

    recon_df, matched_keys = matcher.match_airline_to_ledger(
        combined_airline_refunds, refund_ticket_col,
        fonepay_refund_df, "Ticket No",
        ledger_prefix=fonepay_name, airline_prefix=refund_name,
        ledger_amount_col=fonepay_amount_col, airline_amount_col=refund_amount_col,
    )

    if recon_df.empty:
        airline_tickets = set(combined_airline_refunds[refund_ticket_col].apply(prep.normalize_ticket)) - {""}
        fonepay_tickets = set(fonepay_refund_df["Ticket No"].apply(prep.normalize_ticket)) - {""}
        print(f"  [fonepay-refund] 0 matches: {len(airline_tickets)} distinct {refund_name} ticket(s) "
              f"vs {len(fonepay_tickets)} distinct {fonepay_name} ticket(s) (after Ticket No lookup) - "
              "no overlap between them today.")

        if fonepay_sales_tickets is not None:
            # The definitive check: were these refund tickets EVER sold via
            # Fonepay at all, regardless of today's data? If not, 0 matches
            # is expected - they were paid via eSewa, only eSewa Refund
            # Recon can ever resolve them.
            ever_sold_via_fonepay = airline_tickets & fonepay_sales_tickets
            if not ever_sold_via_fonepay:
                print(f"  [fonepay-refund] CONFIRMED EXPECTED: 0 of these {len(airline_tickets)} "
                      f"{refund_name} ticket(s) were EVER sold via Fonepay (checked against "
                      f"Fonepay's own sales ledger) - they were all paid via eSewa, so Fonepay "
                      f"Refund Recon correctly has nothing to match. Not a bug.")
            else:
                print(f"  [fonepay-refund] WORTH INVESTIGATING: {len(ever_sold_via_fonepay)} of these "
                      f"ticket(s) WERE sold via Fonepay at some point, but none show up in TODAY's "
                      f"Fonepay Refund file yet - Fonepay likely just hasn't posted the refund yet "
                      f"(same as the earlier T100/RRN case). Example ticket(s): "
                      f"{list(ever_sold_via_fonepay)[:5]}")
        else:
            sample_airline = list(airline_tickets)[:5]
            sample_fonepay = list(fonepay_tickets)[:5]
            print(f"  [fonepay-refund] sample {refund_name} tickets:  {sample_airline}")
            print(f"  [fonepay-refund] sample {fonepay_name} tickets: {sample_fonepay}")
            print(f"  [fonepay-refund] if these look like they SHOULD overlap but visibly differ in format "
                  f"(length, prefix, letters vs digits), that's a real mismatch worth investigating with "
                  f"lookup_state.py. If they look like genuinely different tickets, these {refund_name} "
                  f"refunds simply weren't paid via Fonepay - check eSewa Refund Recon instead.")

    if not recon_df.empty:
        letters = cfg.get("report_fields", {}).get("refund_recon", "")
        if letters:
            recon_df = select_fields_by_letters(recon_df, letters)

    return recon_df, matched_keys


def tag_refund_column(unrecon_df, unrecon_df_full, airline_match_col, refund_tickets):
    """Adds a 'Refund?' Yes/No column to an Unrecon dataframe. unrecon_df_full
    is the pre-field-selection unrecon dataframe (same row order/index as
    unrecon_df, still has the match-key column) - used to look up each row's
    ticket number regardless of which columns made it into the final report."""
    if refund_tickets is None or unrecon_df.empty:
        return unrecon_df
    if airline_match_col not in unrecon_df_full.columns:
        return unrecon_df

    keys = unrecon_df_full[airline_match_col].apply(prep.normalize_ticket)
    is_refund = keys.isin(refund_tickets)
    unrecon_df = unrecon_df.copy()
    unrecon_df["Refund?"] = is_refund.reindex(unrecon_df.index).map({True: "Yes", False: "No"}).fillna("No")
    return unrecon_df


def tag_reverted_column(unrecon_df, unrecon_df_full, airline_match_col, reverted_tickets):
    """Adds a 'Reverted?' Yes/No column to an Unrecon dataframe - Yes means
    this ticket lines up with a reversed/refunded record on OUR side
    (a REVERSED eSewa Ledger row, an eSewa Refund entry, or a Fonepay
    Refund entry - see the unified reverted_tickets set built in run()).

    Applied uniformly to EVERY airline's Unrecon sheet. This is purely
    informational, never a match: main recon always matches every airline
    against Complete-only ledger rows, with no exceptions, so a ticket
    tagged here was never pulled into any airline's matched Recon pool.
    For Buddha specifically, its own separate Refund Recon/Unrecon/
    Cashback pipeline (Airline Refunds vs eSewa/Fonepay Refund) remains
    the authoritative, detailed reconciliation - this tag just gives
    Shree/Yeti (which have no airline-side refund data to reconcile
    against) the same "this isn't a mystery" visibility Buddha's detailed
    pipeline already provides."""
    if reverted_tickets is None or unrecon_df.empty:
        return unrecon_df
    if airline_match_col not in unrecon_df_full.columns:
        return unrecon_df

    keys = unrecon_df_full[airline_match_col].apply(prep.normalize_ticket)
    is_reverted = keys.isin(reverted_tickets)
    unrecon_df = unrecon_df.copy()
    unrecon_df["Reverted?"] = is_reverted.reindex(unrecon_df.index).map({True: "Yes", False: "No"}).fillna("No")
    return unrecon_df


def apply_field_selection(cfg, prepped):
    """Returns dict source -> dataframe with only configured report fields"""
    selected = {}
    for source in SOURCES:
        letters = cfg["report_fields"].get(source, "")
        df = prepped[source]
        try:
            sel = select_fields_by_letters(df, letters)
            if sel.empty and not df.empty and letters:
                raise ValueError("Field selection produced 0 columns")
            selected[source] = sel
        except Exception as e:
            print(f"  [WARN] field selection failed for {source} ({e}); using full column set")
            selected[source] = df
        # always keep the match key column available even if not selected,
        # since matching happens on the ORIGINAL (unfiltered) match key
    return selected


def apply_report_field_selection(cfg, report_key, df):
    """Apply configured Excel-letter fields to a generated report dataframe.

    Generated reports such as Refund Recon do not have a source key in
    ``SOURCES``, so they use this small shared wrapper instead of being
    silently written with every column.
    """
    letters = (cfg.get("report_fields") or {}).get(report_key, "")
    if not letters:
        return df
    try:
        selected = select_fields_by_letters(df, letters)
        if selected.empty and not df.empty:
            raise ValueError("Field selection produced 0 columns")
        return selected
    except Exception as e:
        print(f"  [WARN] field selection failed for {report_key} ({e}); using full column set")
        return df


def _normalized_key_series(df, key_col):
    return df[key_col].astype(str).str.strip().str.upper()


def _amount_series(df, amount_col, warn_label=None):
    """Convert common Excel/accounting amount formats into numbers.
    Falls back to a case-insensitive column match (source headers can
    differ in case between sheets, e.g. 'NET' vs 'Net'), and WARNS instead
    of silently returning 0 when the column truly can't be found - a
    missing amount column previously summed to a silent 0 with no signal
    that anything was wrong."""
    if not amount_col:
        return pd.Series(dtype="float64")

    resolved_col = amount_col
    if resolved_col not in df.columns:
        # case-insensitive fallback
        lower_map = {str(c).strip().lower(): c for c in df.columns}
        match = lower_map.get(str(amount_col).strip().lower())
        if match is not None:
            resolved_col = match
        else:
            label = f" ({warn_label})" if warn_label else ""
            print(f"  [amount] WARNING{label}: column '{amount_col}' not found - "
                  f"available columns: {list(df.columns)}. Treating amount as 0 - "
                  "check config.yaml amount_fields / column naming.")
            return pd.Series(dtype="float64")

    series = df[resolved_col]
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce").fillna(0)

    cleaned = (
        series.astype(str)
        .str.strip()
        .str.replace(",", "", regex=False)
        .str.replace("NPR", "", case=False, regex=False)
        .str.replace("Rs.", "", case=False, regex=False)
        .str.replace("Rs", "", case=False, regex=False)
        .str.replace("रु", "", regex=False)
        .str.replace(r"^\((.*)\)$", r"-\1", regex=True)
        .str.replace(r"[^0-9.\-]", "", regex=True)
    )
    return pd.to_numeric(cleaned, errors="coerce").fillna(0)


def _ensure_column_from_source(target_df, source_df, column_name):
    """Keep report field selections but always include key amount columns."""
    if column_name and column_name in source_df.columns and column_name not in target_df.columns:
        target_df[column_name] = source_df[column_name].values
    return target_df


def _sum_amount_for_keys(df, key_col, amount_col, keys):
    if not amount_col or key_col not in df.columns or amount_col not in df.columns:
        return 0.0
    tmp = df.copy()
    tmp["_key"] = _normalized_key_series(tmp, key_col)
    tmp["_amount"] = _amount_series(tmp, amount_col)
    return tmp[tmp["_key"].isin(keys)]["_amount"].sum()


def _sum_amount_not_in_keys(df, key_col, amount_col, keys):
    if not amount_col or key_col not in df.columns or amount_col not in df.columns:
        return 0.0
    tmp = df.copy()
    tmp["_key"] = _normalized_key_series(tmp, key_col)
    tmp["_amount"] = _amount_series(tmp, amount_col)
    return tmp[~tmp["_key"].isin(keys)]["_amount"].sum()


def detect_input_files(cfg, input_dir=None):
    """Detects every input file in INPUT_DIR by column signature (not
    filename). Extracted as its own function so any tool that needs
    today's actual files - not just the main run() - uses the EXACT same
    detection logic instead of a second copy that could quietly drift out
    of sync (e.g. lookup_state.py's diagnostic search)."""
    extra_signatures = {}
    optional_sources = set()
    min_required_overrides = {}
    # NOTE: eSewa 2 (the old separate ticket-number lookup file) is no
    # longer used - eSewa now contains everything in one file, so there's
    # no separate lookup-file signature to detect anymore.

    refund_recon_cfg = cfg.get("refund_reconciliation", {})
    if refund_recon_cfg.get("enabled"):
        cancellation_source = refund_recon_cfg.get("cancellation_source", "cancellation_report")
        sig_cols = [cfg["match_keys"].get(cancellation_source)] + list(refund_recon_cfg.get("signature_columns") or [])
        sig_cols = [c for c in sig_cols if c]
        if sig_cols:
            extra_signatures[cancellation_source] = sig_cols
            optional_sources.add(cancellation_source)
            min_required_overrides[cancellation_source] = 2

    # Fonepay refund detection
    fp_refund_source = "fonepay_refund"
    fp_refund_sig = ["RETRIEVAL_REFERENCE_NUMBER", "REFUND_AMOUNT", "MERCHANT_PAYMENT_ADVICE_ID"]
    extra_signatures[fp_refund_source] = fp_refund_sig
    optional_sources.add(fp_refund_source)
    min_required_overrides[fp_refund_source] = 2

    return file_mapper.detect_files(
        input_dir or INPUT_DIR,
        extra_signatures=extra_signatures,
        optional_sources=optional_sources,
        min_required_overrides=min_required_overrides,
    )


def run(cfg=None, input_dir=None, output_dir=None):
    """Runs the whole pipeline. Returns a list of per-airline result dicts:
        {"airline_key", "airline_name", "path", "summary", "recon", "unrecon"}
    where summary / recon / unrecon are the same DataFrames that were
    written into the Excel report (so a UI can preview them)."""
    input_dir = input_dir or INPUT_DIR
    output_dir = output_dir or OUTPUT_DIR
    print("=" * 60)
    print("AIRLINE RECONCILIATION - starting run")
    print("=" * 60)

    cfg = cfg or load_config()
    names = cfg["names"]

    print(f"\n1. Detecting input files in: {input_dir}")
    file_paths = detect_input_files(cfg, input_dir)
    for k, v in file_paths.items():
        print(f"   {k:10s} -> {os.path.basename(v)}")

    print("\n2. Loading + cleaning + prepping each source...")
    prepped = load_and_prep_all(cfg, file_paths)
    airline3_refund_df, airline3_refund_ticket_col, airline3_refund_tickets = \
        load_airline3_refund_data(cfg, file_paths)
    cancellation_df = load_cancellation_report(cfg, file_paths)
    fonepay_refund_raw = load_fonepay_refund(cfg, file_paths)

    # Fonepay's Refund file only has a Retrieval Reference Number (RRN),
    # not a ticket number - the ticket number has to be looked up from
    # Fonepay's own Ledger. But that Ledger is a single-day file, and a
    # refund can post many days after the original RRN was created (e.g.
    # RRN from Aug 10, refunded Aug 16) - by then, Aug 10's ledger file no
    # longer exists to look up against. So this accumulates RRN->Ticket
    # mappings across every day's run into a persistent lookup table
    # (same carry-forward principle as pending refunds, just for
    # reference data instead of unmatched transactions), and looks the
    # refund up against that accumulated history instead of just today.
    # Fonepay RRN -> Ticket No reference lookup accumulation.
    # Accumulate Fonepay sales transactions on EVERY run so that when refunds
    # post days or weeks later, their RRN can always resolve - even on days
    # when no Fonepay Refund file is dropped.
    rrn_lookup_cfg = cfg.get("fonepay_rrn_lookup", {})
    rrn_state_file = rrn_lookup_cfg.get("state_file")
    rrn_col_ledger = cfg["match_keys"].get("fonepay_refund", "RETRIEVAL_REFERENCE_NUMBER")
    ledger_b_rrn_col = rrn_lookup_cfg.get("ledger_b_rrn_column", "Retrieval Reference No")
    fonepay_refund_df = None
    accumulated_ledger_b = None

    if rrn_state_file:
        rrn_state_path = resolve_state_path(rrn_state_file)
        lookup_fields = rrn_lookup_cfg.get("lookup_fields", ["Ticket No"])
        date_col_for_lookup = rrn_lookup_cfg.get("date_column", "Recorded Date")

        today_ledger_b = prepped.get("ledger_b")
        if today_ledger_b is not None and not today_ledger_b.empty:
            keep_cols = [ledger_b_rrn_col] + [c for c in lookup_fields if c != ledger_b_rrn_col]
            if date_col_for_lookup not in keep_cols and date_col_for_lookup in today_ledger_b.columns:
                keep_cols.append(date_col_for_lookup)
            keep_cols = [c for c in keep_cols if c in today_ledger_b.columns]
            slim_ledger_b = today_ledger_b[keep_cols].copy()

            accumulated_ledger_b = refund_state.merge_reference_lookup(
                slim_ledger_b, ledger_b_rrn_col, rrn_state_path,
                retention_days=rrn_lookup_cfg.get("retention_days", 180),
                date_col=date_col_for_lookup,
            )
            print(f"   [fonepay-rrn-lookup] {len(accumulated_ledger_b)} RRN mapping(s) available for lookup "
                  f"(accumulated across days, not just today's Fonepay Ledger)")
        else:
            accumulated_ledger_b = refund_state.load_reference_lookup(rrn_state_path)
    else:
        print("  [fonepay-rrn-lookup] WARNING: no fonepay_rrn_lookup.state_file configured - "
              "falling back to TODAY's Fonepay Ledger only. A refund for an RRN created on an "
              "earlier day will NOT be found. Set fonepay_rrn_lookup.state_file in config.yaml.")
        accumulated_ledger_b = prepped.get("ledger_b")

    if fonepay_refund_raw is not None:
        fonepay_refund_df = prep.prep_fonepay_refund_lookup(
            fonepay_refund_raw, accumulated_ledger_b,
            rrn_refund_col=rrn_col_ledger, rrn_ledger_col=ledger_b_rrn_col,
            airline_confirmed_tickets=airline3_refund_tickets,
        )

    # Unified 'Reverted?' ticket set - ANY ticket that shows up as reverted
    # on OUR side (not the airline's), from any of the three sources below.
    # Applied as a purely informational tag on EVERY airline's Unrecon
    # sheet (see tag_reverted_column) - main recon always matches against
    # Complete-only ledger rows for every airline, uniformly, no
    # exceptions. This tag never feeds back into the matched Recon pool
    # for any airline - it just tells a reviewer "this Unrecon ticket
    # isn't a mystery, our own ledger already shows it reversed."
    ledger_a_match_col = cfg["match_keys"].get("ledger_a", "ticket_number")
    status_col = "Status"
    reverted_tickets = set()

    if status_col in prepped["ledger_a"].columns and ledger_a_match_col in prepped["ledger_a"].columns:
        reverted_mask = prepped["ledger_a"][status_col] == "REVERSED"
        reverted_tickets |= set(
            prepped["ledger_a"].loc[reverted_mask, ledger_a_match_col]
            .apply(prep.normalize_ticket)
            .replace({"": None})
            .dropna()
        )

    if cancellation_df is not None and not cancellation_df.empty:
        cancellation_match_col = cfg["match_keys"].get("cancellation_report")
        if cancellation_match_col in cancellation_df.columns:
            for raw in cancellation_df[cancellation_match_col].astype(str):
                for t in raw.split(","):
                    t_norm = prep.normalize_ticket(t)
                    if t_norm:
                        reverted_tickets.add(t_norm)

    if fonepay_refund_df is not None and not fonepay_refund_df.empty and "Ticket No" in fonepay_refund_df.columns:
        reverted_tickets |= set(
            fonepay_refund_df["Ticket No"].apply(prep.normalize_ticket)
            .replace({"": None})
            .dropna()
        )

    print(f"   [reverted-tag] {len(reverted_tickets)} ticket(s) tagged Reverted across all sources "
          f"(informational only - never matched into any airline's Recon)")

    # Refund cashback lookup - eSewa now contains everything in one file,
    # so this reuses the SAME already-loaded+prepped ledger_a dataframe
    # (prep_ledger_a doesn't drop any columns, so Child_Status/Profile/
    # Total_Cashback/Parent_Txn_code are all still present per row).
    cashback_cfg = cfg.get("cashback_lookup", {})
    cashback_lookup, cashback_ticket_map = {}, {}
    if cashback_cfg.get("enabled"):
        cashback_lookup, cashback_ticket_map = prep.build_cashback_lookup(
            prepped["ledger_a"],
            txn_code_col=cashback_cfg.get("txn_code_column", "Parent_Txn_code"),
            child_status_col=cashback_cfg.get("child_status_column", "Child_Status"),
            included_statuses=cashback_cfg.get("included_statuses", ["full_refund", "partial_refund"]),
            profile_col=cashback_cfg.get("profile_column", "Profile"),
            agent_profile_value=cashback_cfg.get("agent_profile_value", "Agent"),
            cashback_amount_col=cashback_cfg.get("cashback_amount_column", "Total_Cashback"),
            ticket_number_col=cfg["match_keys"].get("ledger_a", "ticket_number"),
        )
        print(f"   [cashback-lookup] {len(cashback_lookup)} refund transaction(s) found in eSewa "
              f"({sum(1 for v in cashback_lookup.values() if v['is_agent'])} Agent-profile), "
              f"{len(cashback_ticket_map)} ticket(s) available for txn_code fallback")

    print("\n3. Matching airlines against ledgers by ticket number...")
    field_selected = apply_field_selection(cfg, prepped)

    os.makedirs(output_dir, exist_ok=True)
    results = []
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    output_paths = []

    for airline_key in AIRLINES:
        airline_full = prepped[airline_key]
        airline_match_col = cfg["match_keys"][airline_key]
        airline_name = names[airline_key]
        airline_amount_col = cfg["amount_fields"].get(airline_key)

        recon_sheets = {}
        unrecon_sheets = {}
        summary_rows_by_ledger = {ledger_key: [] for ledger_key in LEDGERS}
        matched_keys_by_ledger = {}

        for ledger_key in LEDGERS:
            ledger_full = prepped[ledger_key]
            ledger_match_col = cfg["match_keys"][ledger_key]
            ledger_name = names[ledger_key]
            ledger_amount_col = cfg["amount_fields"].get(ledger_key)

            if ledger_key == "ledger_a":
                # eSewa contains both Complete and Reversed rows in one
                # file. Main recon matches against COMPLETE rows only, for
                # EVERY airline, uniformly - no exceptions. Reversed rows
                # never enter any airline's matched Recon pool; they only
                # feed the informational 'Reverted?' tag on Unrecon sheets
                # (built once, above, from all three ledger-side sources).
                status_col = "Status"
                if status_col in ledger_full.columns:
                    before = len(ledger_full)
                    ledger_full = ledger_full[ledger_full[status_col] == "COMPLETE"].reset_index(drop=True)
                    print(f"   [main-recon] {airline_name}: matching against Complete-only "
                          f"eSewa rows ({before} -> {len(ledger_full)} rows)")

            # select reporting fields, but ensure match key column is present
            airline_sel = field_selected[airline_key].copy()
            if airline_match_col not in airline_sel.columns:
                airline_sel[airline_match_col] = airline_full[airline_match_col]
            airline_sel = _ensure_column_from_source(
                airline_sel, airline_full, airline_amount_col
            )

            if ledger_key == "ledger_a":
                # rebuilt fresh from the (possibly row-filtered) ledger_full
                # above, rather than the precomputed field_selected dict,
                # so row alignment always matches ledger_full exactly.
                letters = cfg["report_fields"].get("ledger_a", "")
                ledger_sel = select_fields_by_letters(ledger_full, letters).copy() if letters else ledger_full.copy()
            else:
                ledger_sel = field_selected[ledger_key].copy()
            if ledger_match_col not in ledger_sel.columns:
                ledger_sel[ledger_match_col] = ledger_full[ledger_match_col]
            ledger_sel = _ensure_column_from_source(
                ledger_sel, ledger_full, ledger_amount_col
            )

            recon_df, matched_keys = matcher.match_airline_to_ledger(
                airline_sel, airline_match_col,
                ledger_sel, ledger_match_col,
                ledger_prefix=ledger_name, airline_prefix=airline_name,
                ledger_amount_col=ledger_amount_col, airline_amount_col=airline_amount_col,
            )
            matched_keys_by_ledger[ledger_key] = matched_keys

            sheet_name = f"{ledger_name} vs {airline_name}"
            recon_sheets[sheet_name] = recon_df
            print(f"   {sheet_name}: {len(recon_df)} matched rows")

        # Unrecon = not found in EITHER ledger, using full (unfiltered) airline data
        unrecon_df_full = matcher.build_unrecon(
            airline_full, airline_match_col,
            matched_keys_by_ledger["ledger_a"], matched_keys_by_ledger["ledger_b"]
        )
        unrecon_letters = cfg["report_fields"].get(airline_key, "")
        try:
            unrecon_df = select_fields_by_letters(unrecon_df_full, unrecon_letters) \
                if unrecon_letters else unrecon_df_full
        except Exception:
            unrecon_df = unrecon_df_full
        unrecon_df = _ensure_column_from_source(
            unrecon_df, unrecon_df_full, airline_amount_col
        )

        # 'Reverted?' applies to EVERY airline uniformly - purely
        # informational, sourced only from our own ledgers (eSewa
        # Reversed rows, eSewa Refund, Fonepay Refund). Never affects
        # matching for any airline.
        unrecon_df = tag_reverted_column(
            unrecon_df, unrecon_df_full, airline_match_col, reverted_tickets
        )

        # 'Refund?' is Buddha-only - it's the only airline with its own
        # embedded Refunds sheet, so it's the only one this check applies to.
        if airline_key == "airline_3":
            unrecon_df = tag_refund_column(
                unrecon_df, unrecon_df_full, airline_match_col, airline3_refund_tickets
            )

        unrecon_sheet_name = "Unrecon"
        unrecon_sheets[unrecon_sheet_name] = unrecon_df
        print(f"   {unrecon_sheet_name} ({airline_name}): {len(unrecon_df)} unmatched rows")

        # Airline 3 only: Refund reconciliation (Airline Refunds vs Cancellation Report & Fonepay Refund)
        esewa_refund_summary = None
        fonepay_refund_summary = None

        if airline_key == "airline_3" and airline3_refund_df is not None:
            cancellation_source = cfg.get("refund_reconciliation", {}).get(
                "cancellation_source", "cancellation_report"
            )
            cancellation_match_col = cfg["match_keys"].get(cancellation_source)
            cancellation_name = names.get(cancellation_source, "eSewa Refund")

            combined_airline_refunds, esewa_refund_recon_df, esewa_matched_keys = build_refund_reconciliation(
                cfg, airline3_refund_df, airline3_refund_ticket_col,
                cancellation_df, cancellation_match_col,
                refund_name=f"{airline_name} Refund",
                cancellation_name=cancellation_name,
            )

            if not esewa_refund_recon_df.empty:
                esewa_refund_recon_df = attach_cashback(
                    esewa_refund_recon_df, cashback_lookup, cashback_ticket_map, cancellation_name,
                    txn_code_col=cfg.get("refund_reconciliation", {}).get("txn_code_column", "Parent_Txn_code"),
                    ticket_col=cancellation_match_col,
                )
                recon_sheets["eSewa Refund Recon"] = esewa_refund_recon_df
                print(f"   eSewa Refund Recon: {len(esewa_refund_recon_df)} matched rows")

            # Fonepay Refund Recon
            # IMPORTANT: use the ACCUMULATED Fonepay ticket history
            # (fonepay_rrn_lookup.csv, built up across every day's run),
            # NOT prepped["ledger_b"] (today's single-day ~800-900 row
            # snapshot only) - a ticket sold via Fonepay weeks ago is long
            # gone from today's Ledger file but still present in the
            # accumulated history, and using only today's snapshot here
            # would wrongly conclude "never sold via Fonepay" for tickets
            # that genuinely were, just not today.
            ledger_b_ticket_col = "Ticket No"
            fonepay_sales_tickets = None
            if isinstance(accumulated_ledger_b, pd.DataFrame) and ledger_b_ticket_col in accumulated_ledger_b.columns:
                fonepay_sales_tickets = set(
                    accumulated_ledger_b[ledger_b_ticket_col].apply(prep.normalize_ticket)
                ) - {""}

            fonepay_refund_recon_df, fonepay_matched_keys = build_fonepay_refund_reconciliation(
                cfg, fonepay_refund_df, combined_airline_refunds,
                airline3_refund_ticket_col,
                refund_name=f"{airline_name} Refund",
                fonepay_name=names.get("fonepay_refund", "Fonepay Refund"),
                fonepay_sales_tickets=fonepay_sales_tickets,
            )

            if not fonepay_refund_recon_df.empty:
                recon_sheets["Fonepay Refund Recon"] = fonepay_refund_recon_df
                print(f"   Fonepay Refund Recon: {len(fonepay_refund_recon_df)} matched rows")

            # Combined Unrecon (not matched in either eSewa or Fonepay)
            all_matched_refund_keys = esewa_matched_keys | fonepay_matched_keys
            unrecon_refunds = combined_airline_refunds[~combined_airline_refunds["_norm_ticket"].isin(all_matched_refund_keys)].drop(columns=["_norm_ticket"], errors="ignore").reset_index(drop=True)
            refund_unrecon_df = refund_state.tag_aging(unrecon_refunds, grace_period_days=cfg.get("refund_reconciliation", {}).get("grace_period_days", 2))

            # Persist unmatched refunds for next run
            state_file_cfg = cfg.get("refund_reconciliation", {}).get("state_file", "C:/ReconState/pending_refunds.csv")
            state_path = resolve_state_path(state_file_cfg)
            refund_state.save_pending(
                refund_unrecon_df.drop(columns=["Days Outstanding", "Refund Status"], errors="ignore"),
                state_path,
            )

            # Refund Unrecon field selection is fully configurable via
            # report_fields.refund_unrecon - no hardcoded Buddha-specific
            # column list. Blank/unset means "show everything".
            unrecon_letters_refund = cfg.get("report_fields", {}).get("refund_unrecon", "")
            unrecon_sheets["Refund Unrecon"] = (
                select_fields_by_letters(refund_unrecon_df, unrecon_letters_refund)
                if unrecon_letters_refund else refund_unrecon_df
            )

            print(f"   Refund Unrecon: {len(refund_unrecon_df)} still-unmatched refund(s) "
                  f"({(refund_unrecon_df['Refund Status'] == 'Overdue').sum() if not refund_unrecon_df.empty else 0} overdue, "
                  f"{(refund_unrecon_df['Refund Status'] == 'Pending').sum() if not refund_unrecon_df.empty else 0} still within grace period)")

            refund_amount_col = cfg.get("refund_reconciliation", {}).get("refund_amount_column", "REF")
            fonepay_amount_col = cfg.get("amount_fields", {}).get("fonepay_refund", "REFUND_AMOUNT")

            # ---- eSewa refund summary numbers ----
            if not esewa_refund_recon_df.empty:
                esewa_airline_amt = _amount_series(esewa_refund_recon_df, f"{airline_name} Refund: {refund_amount_col}").sum()
                esewa_cancel_amt = _amount_series(esewa_refund_recon_df, f"{cancellation_name}: Price Per Ticket").sum()
                esewa_cashback_amt = _amount_series(esewa_refund_recon_df, "Cashback Per Ticket").sum()
                esewa_unrecon_amt = _amount_series(refund_unrecon_df, refund_amount_col).sum() if not refund_unrecon_df.empty else 0.0

                esewa_refund_summary = {
                    "Refund Recon Count": len(esewa_refund_recon_df),
                    f"Refund Amount ({airline_name})": round(esewa_airline_amt, 2),
                    f"Refund Amount ({cancellation_name})": round(esewa_cancel_amt, 2),
                    "Refund Difference": round(esewa_cancel_amt - esewa_airline_amt, 2),
                    "Refund Cashback Amount": round(esewa_cashback_amt, 2),
                    "Refund Pending Count": int((refund_unrecon_df["Refund Status"] == "Pending").sum()) if not refund_unrecon_df.empty else 0,
                    "Refund Overdue Count": int((refund_unrecon_df["Refund Status"] == "Overdue").sum()) if not refund_unrecon_df.empty else 0,
                    "Refund Unrecon Amount": round(esewa_unrecon_amt, 2),
                }

            # ---- Fonepay refund summary numbers ----
            if not fonepay_refund_recon_df.empty:
                fonepay_name = names.get("fonepay_refund", "Fonepay Refund")
                fp_airline_amt = _amount_series(fonepay_refund_recon_df, f"{airline_name} Refund: {refund_amount_col}").sum()
                fp_cancel_amt = _amount_series(fonepay_refund_recon_df, f"{fonepay_name}: {fonepay_amount_col}").sum()
                fp_unrecon_amt = _amount_series(refund_unrecon_df, refund_amount_col).sum() if not refund_unrecon_df.empty else 0.0

                fonepay_refund_summary = {
                    "Refund Recon Count": len(fonepay_refund_recon_df),
                    f"Refund Amount ({airline_name})": round(fp_airline_amt, 2),
                    f"Refund Amount ({fonepay_name})": round(fp_cancel_amt, 2),
                    "Refund Difference": round(fp_cancel_amt - fp_airline_amt, 2),
                    "Refund Pending Count": int((refund_unrecon_df["Refund Status"] == "Pending").sum()) if not refund_unrecon_df.empty else 0,
                    "Refund Overdue Count": int((refund_unrecon_df["Refund Status"] == "Overdue").sum()) if not refund_unrecon_df.empty else 0,
                    "Refund Unrecon Amount": round(fp_unrecon_amt, 2),
                }

        # ---- summary row (single airline, one row per ledger) ----
        total_tickets = len(airline_full)
        recon_a = len(matched_keys_by_ledger["ledger_a"])
        recon_b = len(matched_keys_by_ledger["ledger_b"])
        unrecon_count = len(unrecon_df_full)

        matched_a_keys = matched_keys_by_ledger["ledger_a"]
        matched_b_keys = matched_keys_by_ledger["ledger_b"]
        matched_all_keys = matched_a_keys | matched_b_keys

        ledger_a_amount = _sum_amount_for_keys(
            prepped["ledger_a"], cfg["match_keys"]["ledger_a"],
            cfg["amount_fields"].get("ledger_a"), matched_a_keys
        )
        airline_via_a_amount = _sum_amount_for_keys(
            airline_full, airline_match_col, airline_amount_col, matched_a_keys
        )
        ledger_b_amount = _sum_amount_for_keys(
            prepped["ledger_b"], cfg["match_keys"]["ledger_b"],
            cfg["amount_fields"].get("ledger_b"), matched_b_keys
        )
        airline_via_b_amount = _sum_amount_for_keys(
            airline_full, airline_match_col, airline_amount_col, matched_b_keys
        )
        unrecon_amount = _sum_amount_not_in_keys(
            airline_full, airline_match_col, airline_amount_col, matched_all_keys
        )

        summary_row_a = {
            "Airline": airline_name,
            "Total Airline Tickets": total_tickets,
            "Recon Count": recon_a,
            names["ledger_a"] + " Amount": round(ledger_a_amount, 2),
            "Airline Amount": round(airline_via_a_amount, 2),
            "Difference": round(ledger_a_amount - airline_via_a_amount, 2),
            "Unrecon Count": unrecon_count,
            "Unrecon Amount": round(unrecon_amount, 2),
        }
        if esewa_refund_summary is not None:
            summary_row_a.update(esewa_refund_summary)

        summary_row_b = {
            "Airline": airline_name,
            "Total Airline Tickets": total_tickets,
            "Recon Count": recon_b,
            names["ledger_b"] + " Amount": round(ledger_b_amount, 2),
            "Airline Amount": round(airline_via_b_amount, 2),
            "Difference": round(ledger_b_amount - airline_via_b_amount, 2),
            "Unrecon Count": unrecon_count,
            "Unrecon Amount": round(unrecon_amount, 2),
        }
        if fonepay_refund_summary is not None:
            summary_row_b.update(fonepay_refund_summary)

        summary_rows_by_ledger["ledger_a"].append(summary_row_a)
        summary_rows_by_ledger["ledger_b"].append(summary_row_b)

        summary_df = {
            f"Summary {names['ledger_a']}": pd.DataFrame(summary_rows_by_ledger["ledger_a"]),
            f"Summary {names['ledger_b']}": pd.DataFrame(summary_rows_by_ledger["ledger_b"]),
        }

        safe_airline_name = "".join(c if c.isalnum() else "_" for c in airline_name)
        output_path = os.path.join(
            output_dir, f"Recon_Report_{safe_airline_name}_{timestamp}.xlsx"
        )
        report.build_report(recon_sheets, unrecon_sheets, summary_df, output_path)
        output_paths.append(output_path)
        results.append({
            "airline_key": airline_key,
            "airline_name": airline_name,
            "path": output_path,
            "summary": summary_df,
            "recon": recon_sheets,
            "unrecon": unrecon_sheets,
        })
        print(f"   -> saved {os.path.basename(output_path)}\n")

    print(f"\n{'=' * 60}")
    print("DONE. Reports saved to:")
    for p in output_paths:
        print(f"  {p}")
    print("=" * 60)
    return results


def detect_uploaded_files(uploads, cfg=None):
    """Cheap pre-flight check for the UI: figures out which uploaded file is
    which source (by column signature) WITHOUT running the reconciliation.
    Returns {source_key: uploaded file name}. Raises RuntimeError with the
    detector's readable message if a required file is missing/unrecognised."""
    import io
    import contextlib

    cfg = cfg or load_config()
    tmp_in = tempfile.mkdtemp(prefix="airlines_detect_")
    name_by_path = {}
    try:
        for i, (fname, data) in enumerate(uploads):
            safe = os.path.basename(fname) or f"file_{i}"
            path = os.path.join(tmp_in, f"{i:02d}_{safe}")
            with open(path, "wb") as fh:
                fh.write(data)
            name_by_path[path] = safe
        with contextlib.redirect_stdout(io.StringIO()):
            detected_paths = detect_input_files(cfg, tmp_in)
        return {k: name_by_path.get(v, os.path.basename(v)) for k, v in detected_paths.items()}
    finally:
        shutil.rmtree(tmp_in, ignore_errors=True)


def run_airline_reconciliation(uploads, cfg=None, output_dir=None):
    """Entry point for the Streamlit page.

    uploads: list of (file_name, bytes) - any mix of the ledger / airline /
        refund files. They are written to a private temp folder, detected
        by column signature (file names don't matter), and reconciled.
    cfg: parsed config dict (defaults to the saved config.yaml).
    Returns (results, detected, log_text) where detected maps
        source_key -> original uploaded file name.
    Raises RuntimeError with a readable message if a required file is
    missing / unrecognised (the log so far is attached as .log_text)."""
    import io
    import contextlib

    cfg = cfg or load_config()
    tmp_in = tempfile.mkdtemp(prefix="airlines_in_")
    out_dir = output_dir or tempfile.mkdtemp(prefix="airlines_out_")
    buf = io.StringIO()
    name_by_path = {}
    try:
        for i, (fname, data) in enumerate(uploads):
            safe = os.path.basename(fname) or f"file_{i}"
            path = os.path.join(tmp_in, f"{i:02d}_{safe}")
            with open(path, "wb") as fh:
                fh.write(data)
            name_by_path[path] = safe
        with contextlib.redirect_stdout(buf):
            try:
                detected_paths = detect_input_files(cfg, tmp_in)
                results = run(cfg=cfg, input_dir=tmp_in, output_dir=out_dir)
            except Exception as exc:
                exc.log_text = buf.getvalue()
                raise
        detected = {k: name_by_path.get(v, os.path.basename(v)) for k, v in detected_paths.items()}
        # results' xlsx files live in out_dir; read them into memory so the
        # temp folders can go away.
        for r in results:
            with open(r["path"], "rb") as fh:
                r["bytes"] = fh.read()
            r["filename"] = os.path.basename(r["path"])
        return results, detected, buf.getvalue()
    finally:
        shutil.rmtree(tmp_in, ignore_errors=True)
        if not output_dir:
            shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        in_dir = sys.argv[1] if len(sys.argv) > 1 else INPUT_DIR
        out_dir = sys.argv[2] if len(sys.argv) > 2 else OUTPUT_DIR
        run(input_dir=in_dir, output_dir=out_dir)
    except Exception as e:
        print("\n" + "!" * 60)
        print("ERROR: The reconciliation run failed.")
        print("!" * 60)
        print(f"\n{type(e).__name__}: {e}\n")
        traceback.print_exc()
        sys.exit(1)
