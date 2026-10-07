"""
refund_state.py
Handles the day-to-day timing lag between an airline-side refund (e.g.
initiated on the 1st) and the corresponding ledger-side cancellation entry
(e.g. posted on the 2nd). Since each daily run only sees ONE day's files,
an unmatched refund can't just be re-tried automatically against a
"tomorrow" file within the same run - it has to be carried forward on
disk and re-checked on the NEXT day's run.

Responsibilities:
- parse the (messy, differently-formatted) refund date on each side
- load/save a small pending-refunds CSV between runs
- merge today's fresh refunds with any still-pending ones from prior runs
- age each still-unmatched refund and tag it Pending (within the normal
  settlement window) vs Overdue (past it), so a same-day Unrecon list
  isn't a false alarm for something that's still in-flight.
"""
import os
import pandas as pd


def parse_buddha_refund_date(series):
    """Buddha's 'ISSUE/RF' column packs two different-precision dates
    separated by '/', e.g. '2026-08-18/2026-07-30T12:42:18.970702'.
    Confirmed: the FIRST part (short date, before the '/') is the refund
    date to use. Returns a pandas Series of date objects (NaT on failure)."""
    def _parse_one(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return pd.NaT
        text = str(v).strip()
        if not text:
            return pd.NaT
        first_part = text.split("/", 1)[0].strip()
        return pd.to_datetime(first_part, errors="coerce")

    return series.apply(_parse_one)


def parse_generic_date(series):
    """Cancellation report's 'Txn_date' column, e.g. '8/18/2026 7:08:12 AM'.
    Uses pandas' general parser rather than a fixed format string, since
    exports can vary in zero-padding/AM-PM spacing."""
    return pd.to_datetime(series, errors="coerce")


def load_pending(state_path):
    """Returns the pending-refunds DataFrame from the last run, or an
    empty DataFrame if no state file exists yet (e.g. first-ever run).

    Reads every column as a string (dtype=str). Without this, pandas
    auto-detects numeric-looking columns - and a ticket number with a
    leading zero (e.g. "0912223334445") silently loses that zero the
    moment it's parsed as a number, permanently corrupting it on this
    round-trip alone (fresh same-day loads from Excel aren't affected,
    only refunds that pass through this pending-state file)."""
    if not state_path or not os.path.exists(state_path):
        return pd.DataFrame()
    try:
        df = pd.read_csv(state_path, dtype=str, keep_default_na=False, na_values=[""])
        if "_RefundDate" in df.columns:
            df["_RefundDate"] = pd.to_datetime(df["_RefundDate"], errors="coerce")
        return df
    except Exception as e:
        print(f"  [refund-state] Could not read pending-refunds state file {state_path}: {e}")
        return pd.DataFrame()


def save_pending(df, state_path):
    """Writes the still-unmatched refunds to disk so the NEXT run can
    retry them against that day's cancellation file."""
    if not state_path:
        return
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    out = df.copy()
    if "_RefundDate" in out.columns:
        out["_RefundDate"] = pd.to_datetime(out["_RefundDate"], errors="coerce").dt.strftime("%Y-%m-%d")
    out.to_csv(state_path, index=False)


def tag_aging(df, refund_date_col="_RefundDate", grace_period_days=2, as_of=None):
    """Adds 'Days Outstanding' and 'Refund Status' (Pending/Overdue/Unknown
    date) columns to an unmatched-refunds dataframe."""
    if df.empty:
        df = df.copy()
        df["Days Outstanding"] = pd.Series(dtype="Int64")
        df["Refund Status"] = pd.Series(dtype="object")
        return df

    as_of = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp.now().normalize()
    df = df.copy()
    dates = pd.to_datetime(df[refund_date_col], errors="coerce")
    days = (as_of.normalize() - dates.dt.normalize()).dt.days

    def _status(d):
        if pd.isna(d):
            return "Unknown date"
        return "Overdue" if d > grace_period_days else "Pending"

    df["Days Outstanding"] = days
    df["Refund Status"] = days.apply(_status)
    return df


def load_reference_lookup(state_path):
    """Loads an accumulated key->details reference lookup table (e.g.
    Fonepay RRN -> Ticket No) built up across daily runs. Returns empty
    DataFrame if none exists yet (first-ever run)."""
    if not state_path or not os.path.exists(state_path):
        return pd.DataFrame()
    try:
        df = pd.read_csv(state_path, dtype=str, keep_default_na=False, na_values=[""])
        if "_seen_date" in df.columns:
            df["_seen_date"] = pd.to_datetime(df["_seen_date"], errors="coerce")
        return df
    except Exception as e:
        print(f"  [refund-state] Could not read reference lookup state file {state_path}: {e}")
        return pd.DataFrame()


def save_reference_lookup(df, state_path):
    if not state_path:
        return
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    out = df.copy()
    if "_seen_date" in out.columns:
        out["_seen_date"] = pd.to_datetime(out["_seen_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    out.to_csv(state_path, index=False)


def merge_reference_lookup(today_df, key_col, state_path, retention_days=180, as_of=None,
                            date_col=None, dedup_cols=None, save=True):
    """Solves the same class of problem as the pending-refunds carry-forward,
    but for REFERENCE DATA instead of unmatched transactions: a daily
    single-day ledger file only contains that day's rows, so a lookup
    keyed on something from an EARLIER day (e.g. a Fonepay RRN created on
    day N, referenced by a refund that only posts on day N+6) fails
    purely because that earlier day's file no longer exists by the time
    you need it - not because the data doesn't exist anywhere.

    This accumulates key_col -> other-column mappings across every day's
    run into a persistent lookup table (today's fresh values win on
    conflict), so a lookup can succeed regardless of which day the
    original record was created on. Entries not seen again within
    retention_days are pruned automatically to keep the file from growing
    unbounded forever - set this comfortably longer than your worst-case
    expected settlement lag.

    date_col: optional column in today_df holding each row's OWN date
        (e.g. 'Recorded Date'). When given, each row's _seen_date is set
        from its actual transaction date instead of a single blanket
        as_of - this matters for backfilling historical data (e.g. 2
        months of past transactions in one go), so retention pruning
        stays accurate rather than treating every backfilled row as if
        it happened today. Falls back to as_of for any row where the date
        can't be parsed.

    dedup_cols: optional list of columns identifying a unique record.
        Defaults to [key_col, 'Ticket No'] if 'Ticket No' is present, or all
        non-date columns, ensuring multi-ticket bookings under the same RRN
        are preserved rather than silently dropped to a single ticket.

    save: when False the merge is computed in memory only (nothing is
        written to disk) - used for "preview" runs.

    Returns the full merged+pruned table, already saved back to disk for
    tomorrow's run (unless save=False)."""
    if today_df is None:
        today_df = pd.DataFrame(columns=[key_col])
    as_of = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp.now().normalize()

    today = today_df.copy()
    if key_col not in today.columns:
        today[key_col] = pd.Series(dtype=str)
    today[key_col] = today[key_col].astype(str)
    today = today[today[key_col].str.strip() != ""]

    if dedup_cols is None:
        if "Ticket No" in today.columns:
            dedup_cols = [key_col, "Ticket No"]
        else:
            dedup_cols = [c for c in today.columns if c not in ("_seen_date",)]

    dedup_cols = [c for c in dedup_cols if c in today.columns]
    if not dedup_cols:
        dedup_cols = [key_col]

    today = today.drop_duplicates(subset=dedup_cols, keep="first")

    if date_col and date_col in today.columns:
        parsed_dates = pd.to_datetime(today[date_col], errors="coerce")
        today["_seen_date"] = parsed_dates.fillna(as_of)
    else:
        today["_seen_date"] = as_of

    historical = load_reference_lookup(state_path)

    if historical.empty:
        merged = today
    else:
        # today's fresh rows win on conflict for the same key(s); historical
        # rows for keys NOT seen today are kept as-is (their _seen_date
        # doesn't refresh, so they age out normally if never seen again)
        match_cols = [c for c in dedup_cols if c in historical.columns and c in today.columns]
        if match_cols:
            hist_key = historical[match_cols].astype(str).agg("||".join, axis=1)
            today_key = today[match_cols].astype(str).agg("||".join, axis=1)
            historical_only = historical[~hist_key.isin(today_key)]
        else:
            historical_only = historical[~historical[key_col].isin(today[key_col])]
        merged = pd.concat([historical_only, today], ignore_index=True, sort=False)

    if "_seen_date" in merged.columns:
        merged["_seen_date"] = pd.to_datetime(merged["_seen_date"], errors="coerce")
        age_days = (as_of - merged["_seen_date"]).dt.days
        before = len(merged)
        merged = merged[age_days.isna() | (age_days <= retention_days)].reset_index(drop=True)
        pruned = before - len(merged)
        if pruned:
            print(f"  [refund-state] pruned {pruned} reference lookup entry(ies) "
                  f"older than {retention_days} days from {os.path.basename(state_path)}")

    if save:
        save_reference_lookup(merged, state_path)
    return merged
