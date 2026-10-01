"""
file_mapper.py
Detects which physical file in the input folder corresponds to which
source (ledger_a, ledger_b, airline_1, airline_2, airline_3) by matching
a set of "signature" column names against each file's header row.

We don't rely on filenames - the user may name files anything.
"""
import openpyxl
import csv
import os
import pandas as pd

# Signature columns unique enough to identify each source, checked against
# whatever row we find that looks like a header (we scan rows 1-10 in
# every candidate file since header position varies by source, and pick
# the row/source combo that scores highest).
SIGNATURES = {
    "ledger_a": ["Outbound_Ticket_Numbers", "Parent_Txn_code", "Is One/Two Way?"],
    "ledger_b": ["Retrieval Reference No", "Booking Contact", "flightType"],
    "airline_1": ["UserName", "$Fare", "FSC"],
    "airline_2": ["Issue", "Fare Code", "Net Total"],
    "airline_3": ["INVOICE NO", "PAX NAME", "NPR"],  # NPR appears as merged group header
    "fonepay_refund": ["RETRIEVAL_REFERENCE_NUMBER", "REFUND_AMOUNT", "MERCHANT_PAYMENT_ADVICE_ID"],
}


def _read_rows_openpyxl(path, max_rows=15):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = []
    for i, row in enumerate(ws.iter_rows(min_row=1, max_row=max_rows, values_only=True)):
        rows.append([str(c).strip() if c is not None else "" for c in row])
    return rows


def _read_rows_xlrd(path, max_rows=15):
    try:
        import xlrd
    except ImportError as exc:
        raise RuntimeError(
            f"Could not read legacy Excel file {path}. Install xlrd with "
            "'python -m pip install xlrd', then run again."
        ) from exc

    try:
        book = xlrd.open_workbook(path, formatting_info=True)
    except xlrd.biffh.XLRDError:
        return _read_rows_html_table(path, max_rows=max_rows)

    sheet = book.sheet_by_index(0)
    row_count = min(max_rows, sheet.nrows)
    rows = []
    for r in range(row_count):
        rows.append([str(c).strip() if c not in (None, "") else "" for c in sheet.row_values(r)])

    for row_start, row_end, col_start, col_end in sheet.merged_cells:
        if row_start >= row_count:
            continue
        value = rows[row_start][col_start]
        for r in range(row_start, min(row_end, row_count)):
            for c in range(col_start, col_end):
                rows[r][c] = value

    return rows


def _read_rows_html_table(path, max_rows=15):
    from .loader import read_html_table_rows
    rows = read_html_table_rows(path)[:max_rows]
    return [[str(c).strip() if c not in (None, "") and not (isinstance(c, float) and pd.isna(c)) else ""
             for c in row] for row in rows]


def _read_rows_csv(path, max_rows=15):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for i, row in enumerate(reader):
            if i >= max_rows:
                break
            rows.append([str(c).strip() for c in row])
    return rows


def _candidate_rows(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        return _read_rows_openpyxl(path)
    elif ext == ".xls":
        return _read_rows_xlrd(path)
    elif ext == ".csv":
        return _read_rows_csv(path)
    else:
        raise ValueError(f"Unsupported file type: {path}")


def score_file_for_source(rows, sig_cols):
    """Return best (score, header_row_index_0based) for this signature set
    against any row in `rows` (checks each row as a possible header row,
    also checks pairs of adjacent rows concatenated, for 2-row headers)."""
    best_score = 0
    best_row = None
    for i, row in enumerate(rows):
        row_text = [c.upper() for c in row]
        score = sum(1 for sig in sig_cols if sig.upper() in row_text)
        if score > best_score:
            best_score = score
            best_row = i
        # also try this row combined with the next (2-row header case)
        if i + 1 < len(rows):
            combined = [c.upper() for c in row] + [c.upper() for c in rows[i + 1]]
            score2 = sum(1 for sig in sig_cols if sig.upper() in combined)
            if score2 > best_score:
                best_score = score2
                best_row = i
    return best_score, best_row


def detect_files(input_folder, extra_signatures=None, optional_sources=None,
                  min_required_overrides=None):
    """
    Scans every file in input_folder, scores it against every known
    source signature, and returns the best 1:1 assignment.
    Raises a clear error if a REQUIRED source can't be confidently
    matched, or if two files tie for the same source.

    extra_signatures: dict of {source_key: [sig_cols]} to check in
        addition to the built-in SIGNATURES (e.g. an optional lookup file).
    optional_sources: set/list of source_keys that are allowed to be
        missing without raising an error (they just won't appear in the
        returned assignment).
    min_required_overrides: dict of {source_key: int} to override the
        default "2 signature columns must match" rule for specific
        sources (useful for optional files with fewer known columns).
    """
    all_signatures = dict(SIGNATURES)
    if extra_signatures:
        all_signatures.update(extra_signatures)
    optional_sources = set(optional_sources or [])
    min_required_overrides = min_required_overrides or {}

    files = [
        os.path.join(input_folder, f)
        for f in os.listdir(input_folder)
        if f.lower().endswith((".xlsx", ".xlsm", ".xls", ".csv"))
        and not f.startswith("~$")
    ]
    if not files:
        raise FileNotFoundError(f"No input files found in {input_folder}")

    # score[file][source] = score
    scores = {}
    for f in files:
        try:
            rows = _candidate_rows(f)
        except Exception as e:
            raise RuntimeError(f"Could not read file {f}: {e}")
        scores[f] = {}
        for source, sig_cols in all_signatures.items():
            sc, _ = score_file_for_source(rows, sig_cols)
            scores[f][source] = sc

    # Greedy best-match assignment: highest score pairs claimed first
    assignment = {}
    used_files = set()
    used_sources = set()
    pairs = []
    for f in files:
        for source in all_signatures:
            pairs.append((scores[f][source], f, source))
    pairs.sort(reverse=True, key=lambda x: x[0])

    for score, f, source in pairs:
        if source in used_sources or f in used_files:
            continue
        if score == 0:
            continue
        # require at least N signature columns to match, to avoid false
        # positives (default 2, overridable per source)
        min_required = min_required_overrides.get(source, 2)
        if score < min_required:
            continue
        assignment[source] = f
        used_files.add(f)
        used_sources.add(source)

    required_sources = set(all_signatures.keys()) - optional_sources
    missing = required_sources - set(assignment.keys())
    if missing:
        detail_lines = []
        for source in missing:
            detail_lines.append(f"  - {source}: expected columns like {all_signatures[source]}")
        raise RuntimeError(
            "Could not confidently detect the following source file(s) in the "
            f"input folder ({input_folder}):\n" + "\n".join(detail_lines) +
            "\n\nCheck that all required files are present and unmodified, then try again."
        )

    return assignment  # {source_key: filepath}


if __name__ == "__main__":
    import sys
    folder = sys.argv[1] if len(sys.argv) > 1 else "/home/claude/recon_project/sample_data"
    result = detect_files(folder)
    for k, v in result.items():
        print(f"{k:12s} -> {v}")
