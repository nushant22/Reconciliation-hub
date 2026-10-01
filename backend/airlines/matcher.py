"""
matcher.py
For a given airline dataframe and a ledger dataframe, split the airline
rows into MATCHED (found in ledger) and produce a side-by-side merged
dataframe, plus return the unmatched-by-this-ledger subset separately
(caller combines "not found in A AND not found in B" for Unrecon).
"""
import pandas as pd
from .prep import normalize_ticket


def _normalize_key(series):
    """Uses the SAME normalize_ticket() as every other ticket comparison
    in this pipeline (strip+upper, strips Excel float artifacts, removes
    internal whitespace). This used to be a plain strip+upper here, which
    is the same mismatch class that caused a real bug elsewhere (a ticket
    with a float artifact or hidden whitespace silently failing to
    match) - now consistent everywhere a ticket number gets compared."""
    return series.apply(normalize_ticket)


def match_airline_to_ledger(airline_df, airline_key, ledger_df, ledger_key,
                             ledger_prefix, airline_prefix,
                             ledger_amount_col=None, airline_amount_col=None):
    """
    Returns:
      recon_df: side-by-side matched rows, columns prefixed to avoid clashes
      matched_airline_keys: set of airline ticket numbers that matched
    """
    a = airline_df.copy()
    l = ledger_df.copy()
    a["_key"] = _normalize_key(a[airline_key])
    l["_key"] = _normalize_key(l[ledger_key])

    merged = a.merge(l, on="_key", how="inner", suffixes=("", "_ledger_dup"))

    # build side-by-side output: ledger fields first, then airline fields
    ledger_cols = [c for c in ledger_df.columns]
    airline_cols = [c for c in airline_df.columns]

    out = pd.DataFrame()
    for c in ledger_cols:
        colname = c if c in merged.columns else f"{c}_ledger_dup"
        out[f"{ledger_prefix}: {c}"] = merged[colname] if colname in merged.columns else merged[c]
    for c in airline_cols:
        out[f"{airline_prefix}: {c}"] = merged[c]

    if ledger_amount_col and airline_amount_col:
        l_col = f"{ledger_prefix}: {ledger_amount_col}"
        a_col = f"{airline_prefix}: {airline_amount_col}"
        if l_col in out.columns and a_col in out.columns:
            from .prep import _amount_series
            l_series = _amount_series(out[l_col])
            a_series = _amount_series(out[a_col])
            out["Difference"] = (l_series - a_series).round(2)

    matched_keys = set(merged["_key"].unique())
    return out, matched_keys


def build_unrecon(airline_df, airline_key, matched_keys_a, matched_keys_b):
    a = airline_df.copy()
    a["_key"] = _normalize_key(a[airline_key])
    unrecon = a[~a["_key"].isin(matched_keys_a) & ~a["_key"].isin(matched_keys_b)]
    unrecon = unrecon.drop(columns=["_key"])
    return unrecon.reset_index(drop=True)
