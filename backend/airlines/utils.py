"""
utils.py
"""
from openpyxl.utils import column_index_from_string


def select_fields_by_letters(df, fields_str):
    """fields_str like 'A,C,G' or 'Txn_Date, Sector, Price Per Ticket' ->
    returns df subset with those columns based on column names or Excel letters."""
    if not fields_str or not str(fields_str).strip():
        return df.copy()

    items = [x.strip() for x in str(fields_str).split(",") if x.strip()]
    cols = []
    col_lookup = {str(c).strip().lower(): c for c in df.columns}

    for item in items:
        # Check if item is a direct column name
        if item.lower() in col_lookup:
            cols.append(col_lookup[item.lower()])
        else:
            # Try as Excel column letter
            try:
                idx = column_index_from_string(item.upper()) - 1
                if 0 <= idx < len(df.columns):
                    cols.append(df.columns[idx])
            except Exception:
                pass

    # dedupe while preserving order
    seen = set()
    final_cols = []
    for c in cols:
        if c not in seen:
            final_cols.append(c)
            seen.add(c)
    return df[final_cols].copy() if final_cols else df.copy()
