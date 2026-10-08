"""
prep.py
Applies the source-specific cleaning rules we agreed on:
- Ledger A: split comma-joined ticket numbers into rows, add Status,
  compute Price Per Ticket
- Ledger B: add Status
- Airline 1: drop $ columns, add Total Amount = Net + Comm
- Airline 2: dedup on ticket number (keep first)
- Airline 3: drop USD block, keep NPR only
"""
import re
import pandas as pd
import numpy as np


def normalize_ticket(value):
    """Shared ticket-number normalization used everywhere a ticket gets
    compared/matched (refund confirmation, cashback lookup, and ideally
    the main airline-vs-ledger matching too - the same mismatch class can
    silently affect either). Beyond strip+upper, this also handles two
    common real-world causes of "identical-looking but not equal" tickets:
      - Excel silently float-ifying a numeric-looking ticket number,
        leaving a trailing '.0' on one side but not the other.
      - A hidden/internal space (not just leading/trailing) from a
        copy-paste or export quirk that .strip() alone won't catch.
    Intentionally does NOT strip dashes or other punctuation, since ticket
    formats may legitimately depend on them - only whitespace and the
    float artifact are treated as "definitely not part of the real value".
    """
    if value is None:
        return ""
    s = str(value).strip().upper()
    s = re.sub(r"\.0$", "", s)      # "1112223334445.0" -> "1112223334445"
    s = re.sub(r"\s+", "", s)       # remove ALL whitespace, not just edges
    return s


def _amount_series(series):
    """Convert common Excel/accounting amount formats into numbers."""
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce")

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
    return pd.to_numeric(cleaned, errors="coerce")


_BLANK_TOKENS = ("", "N/A", "NA", "N.A", "NAN", "NONE")


def _is_blank_ticket(v):
    return str(v).strip().upper() in _BLANK_TOKENS


def _is_two_way(v):
    return "two way" in str(v).strip().lower()


def apply_ledger_a_lookup_DEPRECATED(df, lookup_df, ledger_match_col, lookup_match_col,
                                      ticket_number_col_lookup, two_way_col="Is One/Two Way?",
                                      ticket_number_col="Outbound_Ticket_Numbers"):
    """DEPRECATED - kept only for reference. eSewa merged what used to be a
    separate "eSewa 2" lookup file into the main eSewa export; every ticket
    now arrives as its own row already, so there's nothing left to look up
    or fill in. No longer called anywhere in the pipeline."""
    df = df.copy()
    if lookup_df is None or lookup_df.empty:
        print("  [lookup] Ledger A: skipped - lookup file is missing or empty")
        return df
    if ledger_match_col not in df.columns:
        print(f"  [lookup] Ledger A: skipped - '{ledger_match_col}' not found in Ledger A columns: "
              f"{list(df.columns)}")
        return df
    if lookup_match_col not in lookup_df.columns:
        print(f"  [lookup] Ledger A: skipped - '{lookup_match_col}' not found in lookup file columns: "
              f"{list(lookup_df.columns)}")
        return df
    if ticket_number_col_lookup not in lookup_df.columns:
        print(f"  [lookup] Ledger A: skipped - '{ticket_number_col_lookup}' not found in lookup file columns: "
              f"{list(lookup_df.columns)}")
        return df

    needs_lookup = df.apply(
        lambda r: _is_two_way(r.get(two_way_col, "")) and _is_blank_ticket(r.get(ticket_number_col, "")),
        axis=1
    )
    if not needs_lookup.any():
        print(f"  [lookup] Ledger A: no rows needed filling (checked '{two_way_col}' contains "
              f"'two way' AND '{ticket_number_col}' is blank/N-A)")
        return df

    lu = lookup_df.copy()
    lu["_key"] = lu[lookup_match_col].astype(str).str.strip().str.upper()
    lu["_ticket"] = lu[ticket_number_col_lookup].astype(str).str.strip()
    lu = lu[~lu["_ticket"].apply(_is_blank_ticket)]

    grouped = (
        lu.drop_duplicates(subset=["_key", "_ticket"])
        .groupby("_key")["_ticket"]
        .apply(lambda s: ",".join(s))
    )

    filled = 0
    not_found = 0
    for idx in df.index[needs_lookup]:
        key = str(df.at[idx, ledger_match_col]).strip().upper()
        if key in grouped.index and grouped.loc[key]:
            df.at[idx, ticket_number_col] = grouped.loc[key]
            filled += 1
        else:
            not_found += 1

    print(f"  [lookup] Ledger A: filled {filled} blank two-way ticket number(s) "
          f"from lookup file ({not_found} still unmatched/blank)")
    return df


def _has_child_txn(v):
    """A row is a Reversal (same-day-cancelled or similar), not a normal
    Complete sale, if its child_txn column is populated."""
    if v is None:
        return False
    s = str(v).strip()
    return s != "" and s.lower() not in ("nan", "none", "n/a", "na")


def prep_ledger_a(df, match_key_col="ticket_number", txn_code_col="Parent_Txn_code",
                   child_txn_col="child_txn", amount_col="Txn_Amount",
                   child_status_col="Child_Status",
                   reversed_status_values=("FULL_REFUND", "PARTIAL_REFUND", "REVERTED")):
    """
    eSewa's new format: every ticket is already its own row (round-trip
    bookings appear as multiple rows sharing the same Parent_Txn_code,
    rather than one row with a comma-joined ticket list) - no explode
    needed anymore. But the amount (Txn_Amount) is a PARENT-level total
    that repeats identically across every row sharing that Parent_Txn_code
    (e.g. a two-way trip = 2 rows, same Txn_Amount on both), so:

        Price Per Ticket = Txn_Amount / (number of rows sharing that
                                          Parent_Txn_code)

    Status is derived from Child_Status (confirmed, not a guess):
      Child_Status == "N/A"                                  -> COMPLETE
      Child_Status in (FULL_REFUND, PARTIAL_REFUND, REVERTED) -> REVERSED
    Falls back to the old "child_txn populated" heuristic only if the
    Child_Status column is missing entirely (defensive, in case of another
    format change) - a WARNING prints either way so this doesn't silently
    misclassify rows again like it did before.

    Reversed rows are NOT dropped here; the caller applies uniform
    Complete-only filtering for main recon (all airlines, no exceptions)
    and builds a separate informational 'Reverted?' tag from these rows
    instead (see tag_reverted_column in run_recon.py).
    """
    df = df.copy()

    missing_cols = [c for c in (txn_code_col, amount_col, match_key_col)
                    if c not in df.columns]
    if missing_cols:
        print(f"  [ledger_a] WARNING: column(s) {missing_cols} not found - Price Per Ticket "
              f"may be wrong. Available columns: {list(df.columns)}. "
              "Check prep.ledger_a settings in config.yaml against the actual eSewa headers.")

    # Price Per Ticket: group by Parent_Txn_code, divide the (repeated)
    # Txn_Amount by how many rows/tickets share that code.
    if txn_code_col in df.columns:
        txn_key = df[txn_code_col].astype(str).str.strip().str.upper()
        group_counts = txn_key.groupby(txn_key).transform("size")
    else:
        group_counts = pd.Series([1] * len(df), index=df.index)

    def price_per_ticket(amount_val, count):
        try:
            amount = float(amount_val) if amount_val is not None else np.nan
            if pd.isna(amount) or not count or count <= 0:
                return "Reschedule/Refund"
            return amount / count
        except Exception:
            return "Reschedule/Refund"

    amount_series = df[amount_col] if amount_col in df.columns else pd.Series([np.nan] * len(df), index=df.index)
    df["Price Per Ticket"] = [
        price_per_ticket(a, c) for a, c in zip(amount_series, group_counts)
    ]

    # Status: Child_Status is the authoritative signal.
    if child_status_col in df.columns:
        reversed_set = {str(v).strip().upper() for v in reversed_status_values}

        def _status_from_child_status(v):
            s = str(v).strip().upper()
            return "REVERSED" if s in reversed_set else "COMPLETE"

        df["Status"] = df[child_status_col].apply(_status_from_child_status)
    elif child_txn_col in df.columns:
        print(f"  [ledger_a] WARNING: '{child_status_col}' not found - falling back to "
              f"'{child_txn_col}' presence to determine Status. Check prep.ledger_a."
              "child_status_column in config.yaml against the actual eSewa headers.")
        df["Status"] = df[child_txn_col].apply(lambda v: "REVERSED" if _has_child_txn(v) else "COMPLETE")
    else:
        print(f"  [ledger_a] WARNING: neither '{child_status_col}' nor '{child_txn_col}' found - "
              "defaulting every row to Status=COMPLETE (Reversed rows will NOT be detected).")
        df["Status"] = "COMPLETE"

    # match key: every row is already one ticket, just clean it up
    if match_key_col in df.columns:
        df[match_key_col] = df[match_key_col].astype(str).str.strip()

    return df


def prep_ledger_b(df):
    df = df.copy()
    df["Status"] = "COMPLETE"
    return df


def prep_airline_1(df):
    df = df.copy()
    dollar_cols = [c for c in df.columns if str(c).strip().startswith("$")]
    df = df.drop(columns=dollar_cols, errors="ignore")

    for col in ("Net", "Comm"):
        if col in df.columns:
            df[col] = _amount_series(df[col])

    df["Total Amount"] = df.get("Net", 0).fillna(0) + df.get("Comm", 0).fillna(0)
    return df


def prep_airline_2(df, match_key_col="Ticket"):
    df = df.copy()
    if match_key_col in df.columns:
        df = df.drop_duplicates(subset=[match_key_col], keep="first")
    return df.reset_index(drop=True)


def prep_airline_3(df):
    df = df.copy()
    # combined header names look like "NPR_FARE" / "USD_FARE" per loader logic.
    usd_cols = [c for c in df.columns if c.upper().startswith("USD_")]
    df = df.drop(columns=usd_cols, errors="ignore")
    # rename NPR_xxx -> xxx for simplicity, keep NPR columns as the real amounts
    rename_map = {c: c.replace("NPR_", "").replace("NPR", "").strip("_")
                  for c in df.columns if c.upper().startswith("NPR")}
    df = df.rename(columns=rename_map)
    return df


def prep_cancellation_report(df, match_key_col="Ticket Number", airline_confirmed_tickets=None,
                              txn_code_col="Parent_txn_Code"):
    """Splits comma-joined ticket numbers into separate rows and calculates
    'Price Per Ticket' (Child_Amount / number of tickets ACTUALLY refunded).

    Refunds are always initiated by the airline first, and the ledger side
    follows - so the airline's Refunds sheet is the source of truth for
    which tickets were genuinely refunded. The ledger's own Ticket Number
    field is known to sometimes list more tickets than were actually
    refunded (a data-entry issue on our side that isn't getting fixed
    upstream any time soon), which previously understated Price Per Ticket
    for the real matched tickets and left a stray "phantom" ticket with no
    real airline-side refund behind it.

    airline_confirmed_tickets: set/list of ticket numbers from the airline's
        Refunds sheet (the ground truth). When provided:
      - The divisor for Price Per Ticket uses the count of THIS
        transaction's tickets that the airline actually confirmed, not the
        raw count the ledger listed - as long as at least one ticket in
        the transaction is airline-confirmed (if NONE are confirmed yet,
        that's more likely a timing lag than a data error, so it falls
        back to the raw count and gets tagged "pending" rather than "data
        issue").
      - Each exploded ticket row gets an 'Airline Confirmed' bool and a
        'Refund Status vs Airline' tag so mismatches are visible for
        review instead of silently skewing the totals:
          'Confirmed by Airline'
          'NOT confirmed by airline - possible ledger data issue'
              (siblings in the same transaction WERE confirmed, this one wasn't)
          'Not yet confirmed by airline (pending)'
              (no ticket in this transaction matched yet - likely timing lag)

    txn_code_col: groups tickets by transaction (Parent_txn_Code) rather
        than by raw row, so this stays correct even if a transaction is
        ever split across more than one line in the Cancellation Report.
        Falls back to grouping by row if this column isn't present.
    If airline_confirmed_tickets is None, behavior is unchanged from before
    (divide by the raw count of tickets listed in the row) and no new
    columns are added.
    """
    df = df.copy()

    confirmed_set = None
    if airline_confirmed_tickets:
        confirmed_set = {normalize_ticket(t) for t in airline_confirmed_tickets}

    # Determine child amount column dynamically
    child_amt_col = "Child_Amount" if "Child_Amount" in df.columns else None
    if not child_amt_col:
        for c in df.columns:
            if "child" in str(c).lower() and "amount" in str(c).lower():
                child_amt_col = c
                break
    if not child_amt_col:
        for c in ["Parent_Amount", "Amount"]:
            if c in df.columns:
                child_amt_col = c
                break

    def _raw_ticket_list(value):
        return [t.strip() for t in str(value).split(",") if t.strip()]

    def _ticket_divisor(row):
        """Same n_tickets logic used for Price Per Ticket, exposed as its
        own column so other steps (e.g. cashback splitting) can reuse the
        exact same number instead of recomputing it separately."""
        raw_tickets = _raw_ticket_list(row.get(match_key_col, ""))
        if confirmed_set is not None:
            confirmed_tickets = [t for t in raw_tickets if normalize_ticket(t) in confirmed_set]
            return len(confirmed_tickets) if confirmed_tickets else len(raw_tickets)
        return len(raw_tickets)

    def price_per_ticket(row):
        try:
            amt_val = row.get(child_amt_col) if child_amt_col else np.nan
            if pd.isna(amt_val) or str(amt_val).strip() == "":
                amt_val = row.get("Parent_Amount")

            amt_series = _amount_series(pd.Series([amt_val]))
            amt = float(amt_series.iloc[0]) if not amt_series.isna().iloc[0] else np.nan

            n_tickets = _ticket_divisor(row)

            if n_tickets > 0 and pd.notna(amt):
                return amt / n_tickets
            return amt
        except Exception:
            return np.nan

    df["Price Per Ticket"] = df.apply(price_per_ticket, axis=1)
    df["Confirmed Ticket Count"] = df.apply(_ticket_divisor, axis=1)

    # group id used to tell "this ticket is unconfirmed but its transaction
    # siblings WERE confirmed" (data issue) apart from "nothing in this
    # whole transaction is confirmed yet" (pending). Prefer the real
    # Parent_txn_Code so this stays correct even if a transaction is ever
    # split across multiple lines; falls back to row id if that column is
    # missing (each row is its own group either way, so results are
    # identical when txn_code is guaranteed one-line-per-transaction).
    if txn_code_col and txn_code_col in df.columns:
        df["_group_id"] = df[txn_code_col].astype(str)
    else:
        if confirmed_set is not None:
            print(f"  [refund-recon] txn_code_col '{txn_code_col}' not found in Cancellation "
                  "Report - grouping by row instead (fine as long as each transaction is on "
                  "exactly one line).")
        df["_group_id"] = range(len(df))

    df[match_key_col] = df[match_key_col].astype(str)
    df = df.assign(**{match_key_col: df[match_key_col].str.split(",")})
    df = df.explode(match_key_col, ignore_index=True)
    df[match_key_col] = df[match_key_col].str.strip()

    if confirmed_set is not None:
        key_norm = df[match_key_col].apply(normalize_ticket)
        df["Airline Confirmed"] = key_norm.isin(confirmed_set)
        group_has_confirmed = df.groupby("_group_id")["Airline Confirmed"].transform("any")

        def _status(confirmed, group_confirmed):
            if confirmed:
                return "Confirmed by Airline"
            if group_confirmed:
                return "NOT confirmed by airline - possible ledger data issue"
            return "Not yet confirmed by airline (pending)"

        df["Refund Status vs Airline"] = [
            _status(c, g) for c, g in zip(df["Airline Confirmed"], group_has_confirmed)
        ]

    df = df.drop(columns=["_group_id"])
    return df


def build_cashback_lookup(lookup_df, txn_code_col="Parent_Txn_code", child_status_col="Child_Status",
                           included_statuses=("full_refund", "partial_refund"),
                           profile_col="Profile", agent_profile_value="Agent",
                           cashback_amount_col="Total_Cashback", ticket_number_col="ticket_number"):
    """Builds a {normalized txn_code: {profile, is_agent, total_cashback}}
    lookup from the eSewa 2 file, restricted to rows whose Child_Status is
    a refund status. Total_Cashback/Profile are transaction-level values
    that repeat across every ticket row for the same Parent_Txn_code in
    this file, so we take the first row per transaction rather than
    summing (summing would multiply the cashback by however many tickets
    happen to be in that transaction).

    Also returns a {normalized ticket_number: txn_code} map, so the caller
    can fall back to a ticket-number lookup when the Cancellation Report's
    own Parent_Txn_code is missing/wrong for a given transaction (this is
    a known issue on the ledger side - the airline/eSewa 2 ticket number is
    more reliable than our own txn_code field)."""
    empty = ({}, {})
    if lookup_df is None or lookup_df.empty:
        return empty
    missing = [c for c in (txn_code_col, child_status_col, profile_col, cashback_amount_col)
               if c not in lookup_df.columns]
    if missing:
        print(f"  [cashback-lookup] skipped - column(s) not found in eSewa 2: {missing}. "
              f"Available columns: {list(lookup_df.columns)}")
        return empty

    lu = lookup_df.copy()
    status_norm = lu[child_status_col].astype(str).str.strip().str.lower()
    included_norm = {s.strip().lower() for s in included_statuses}
    lu = lu[status_norm.isin(included_norm)]
    if lu.empty:
        return empty

    lu["_txn_key"] = lu[txn_code_col].apply(normalize_ticket)

    result = {}
    grouped = lu.groupby("_txn_key").first()
    for txn_key, row in grouped.iterrows():
        profile_val = str(row.get(profile_col, "")).strip()
        is_agent = profile_val.strip().lower() == str(agent_profile_value).strip().lower()
        cashback_series = _amount_series(pd.Series([row.get(cashback_amount_col)]))
        cashback_amt = float(cashback_series.iloc[0]) if not cashback_series.isna().iloc[0] else 0.0
        result[txn_key] = {
            "profile": profile_val,
            "is_agent": is_agent,
            "total_cashback": cashback_amt,
        }

    ticket_to_txn = {}
    if ticket_number_col in lu.columns:
        for _, row in lu.iterrows():
            t = normalize_ticket(row.get(ticket_number_col, ""))
            if t:
                ticket_to_txn[t] = row["_txn_key"]

    return result, ticket_to_txn


def prep_fonepay_refund_lookup(fonepay_refund_df, ledger_b_df,
                               rrn_refund_col="RETRIEVAL_REFERENCE_NUMBER",
                               rrn_ledger_col="Retrieval Reference No",
                               airline_confirmed_tickets=None,
                               refund_amount_col="REFUND_AMOUNT"):
    """Looks up RETRIEVAL_REFERENCE_NUMBER from Fonepay Refund in Fonepay sales (ledger_b)
    to attach Ticket No, PNR No, Sector, Passenger, Flight Date, etc.

    Uses the SAME normalize_ticket() as every other ticket/reference match
    in this pipeline (strip+upper, strips Excel float artifacts, removes
    internal whitespace) - previously this used its own weaker regex
    (strip + trailing-.0 only, no uppercasing, no internal-whitespace
    handling), which silently failed to match RRNs that differed only in
    case or a stray space, leaving 'Ticket No' blank on every row and
    making Fonepay Refund Recon match essentially nothing downstream.

    Handles multi-ticket bookings under the same RRN by grouping Fonepay
    refund rows, summing their amount, and assigning an equal share to each
    distinct ticket linked to that RRN. The divisor comes from all ticket
    occurrences in the sales lookup, even when only some tickets are in the
    current airline refund file. Confirmed tickets are listed first when an
    airline refund ticket set is supplied, but all linked tickets are still
    included in the split.
    """
    if fonepay_refund_df is None or fonepay_refund_df.empty:
        return fonepay_refund_df
    if ledger_b_df is None or ledger_b_df.empty:
        return fonepay_refund_df

    fr = fonepay_refund_df.copy()
    l_df = ledger_b_df.copy()

    if rrn_refund_col not in fr.columns or rrn_ledger_col not in l_df.columns:
        print(f"  [lookup] Fonepay Refund: WARNING - '{rrn_refund_col}' or '{rrn_ledger_col}' "
              f"not found. Fonepay columns: {list(fr.columns)}. Ledger B columns: {list(l_df.columns)}")
        return fonepay_refund_df

    fr["_rrn"] = fr[rrn_refund_col].apply(normalize_ticket)
    l_df["_rrn"] = l_df[rrn_ledger_col].apply(normalize_ticket)

    extra_cols = ["Ticket No", "PNR No", "Sector", "Passenger", "Flight Date", "Flight No", "Total Fare"]
    cols_to_pull = [c for c in extra_cols if c in l_df.columns]

    # Distinct ticket entries per RRN from sales ledger
    if "Ticket No" in cols_to_pull:
        l_subset = l_df[["_rrn"] + cols_to_pull].drop_duplicates(subset=["_rrn", "Ticket No"]).copy()
    else:
        l_subset = l_df[["_rrn"] + cols_to_pull].drop_duplicates(subset=["_rrn"]).copy()

    confirmed_set = {
        normalize_ticket(t) for t in (airline_confirmed_tickets or [])
        if normalize_ticket(t)
    }

    # One refund row can cover a booking with several tickets sharing its
    # RRN. Expand each RRN group to one row per distinct ticket and split
    # the group's total refund amount evenly across those tickets.
    expanded_rows = []

    for rrn, refund_group in fr.groupby("_rrn", sort=False, dropna=False):
        source_rows = refund_group.drop(columns=["_rrn"], errors="ignore")

        if not rrn:
            expanded_rows.extend(source_rows.to_dict("records"))
            continue

        s_matches = l_subset[l_subset["_rrn"] == rrn]

        if s_matches.empty or "Ticket No" not in s_matches.columns:
            expanded_rows.extend(source_rows.to_dict("records"))
            continue

        # Count every distinct ticket in the sales lookup, not duplicate
        # sales rows or only the tickets present in this airline refund file.
        s_matches = s_matches.copy()
        s_matches["_normalized_ticket"] = s_matches["Ticket No"].apply(normalize_ticket)
        s_matches = s_matches[s_matches["_normalized_ticket"] != ""]
        s_matches = s_matches.drop_duplicates(subset=["_normalized_ticket"], keep="first")

        if confirmed_set:
            s_matches["_confirmed_first"] = (~s_matches["_normalized_ticket"].isin(confirmed_set)).astype(int)
            s_matches = s_matches.sort_values("_confirmed_first").drop(columns=["_confirmed_first"])

        if s_matches.empty:
            expanded_rows.extend(source_rows.to_dict("records"))
            continue

        base_row = source_rows.iloc[0].to_dict()

        if refund_amount_col in source_rows.columns:
            total_amount = _amount_series(source_rows[refund_amount_col]).sum(min_count=1)
        else:
            total_amount = None

        if pd.notna(total_amount):
            total_cents = int(round(float(total_amount) * 100))
            base_cents, remainder_cents = divmod(total_cents, len(s_matches))
            ticket_amounts = [
                (base_cents + (1 if i < remainder_cents else 0)) / 100
                for i in range(len(s_matches))
            ]
        else:
            ticket_amounts = [None] * len(s_matches)

        for i, (_, sale_row) in enumerate(s_matches.iterrows()):
            refund_row = base_row.copy()
            if ticket_amounts[i] is not None:
                refund_row[refund_amount_col] = ticket_amounts[i]
            for col in cols_to_pull:
                refund_row[col] = sale_row[col]
            expanded_rows.append(refund_row)

    result = pd.DataFrame(expanded_rows)

    matched_cnt = result["Ticket No"].notna().sum() if "Ticket No" in result.columns else 0
    print(f"  [lookup] Fonepay Refund: matched {matched_cnt} of {len(result)} expanded rows with Fonepay sales file by RRN")

    return result


PREP_FUNCS = {
    "ledger_a": prep_ledger_a,
    "ledger_b": prep_ledger_b,
    "airline_1": prep_airline_1,
    "airline_2": prep_airline_2,
    "airline_3": prep_airline_3,
}
