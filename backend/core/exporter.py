"""Multi-sheet audit workbook generator (XlsxWriter).

Sheet contract — fixed, because downstream Ops macros depend on it:
    1. Summary                              Account-wise reconciliation overview table
    2. <Profile name>                       Per-account detail: Volume, Matched Breakdown,
                                            Unmatched Breakdown (three labelled sections)
    3. Unrecon_Summary                      Success-only unreconciled transactions summary
    4. Reconciled_Details                   Successfully reconciled (matched) transactions:
                                            - Exact matches + value mismatches
    5. Unreconciled_Details                 Unreconciled (orphan) transactions:
                                            - Section 1: Unreconciled Success-status transactions
                                            - Section 2: Unreconciled Other-status transactions
    6. Value_Mismatches                     Side-by-side A / B / delta triage view (amount breaks)

`constant_memory` mode is used above a row threshold: XlsxWriter then flushes
each row to disk instead of holding the whole workbook in RAM.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Any

import polars as pl
import xlsxwriter

from .matcher import ReconResult

log = logging.getLogger(__name__)

BRAND_GREEN = "#60BB46"
CHARCOAL = "#1F2937"
SUCCESS_CLR = "#10B981"
WARNING_CLR = "#F59E0B"
DANGER_CLR  = "#EF4444"
LIGHT_GREEN = "#E8F5E1"
LIGHT_RED   = "#FEE2E2"
LIGHT_YELLOW = "#FEF3C7"

#: Above this many rows in a single sheet, switch to streaming writes.
CONSTANT_MEMORY_THRESHOLD = 50_000

# Sheet names in workbook order — the first five are the new reference-style
# sheets; Value_Mismatches is retained for ops triage / downstream macros.
SHEET_ORDER = ("Summary", "Detail", "Unrecon_Summary", "Reconciled_Details", "Unreconciled_Details", "Value_Mismatches")

# Kept for test-suite back-compat: tests that reference the old individual
# data-dump sheets should migrate to the new names above, but the constants
# are preserved so imports don't break.
_LEGACY_DATA_SHEETS = ("Exact_Matches", "Value_Mismatches", "Orphans_FileA", "Orphans_FileB", "Unreconciled_Details")


# --------------------------------------------------------------------------- #
# Format catalogue
# --------------------------------------------------------------------------- #

def _formats(wb: xlsxwriter.Workbook) -> dict[str, Any]:
    base = {"font_name": "Calibri", "font_size": 10}
    return {
        # Banner / title
        "title": wb.add_format({**base, "bold": True, "font_size": 13,
                                 "font_color": "#FFFFFF", "bg_color": BRAND_GREEN,
                                 "align": "left", "valign": "vcenter"}),
        "section": wb.add_format({**base, "bold": True, "font_size": 10,
                                   "font_color": "#FFFFFF", "bg_color": CHARCOAL,
                                   "align": "left", "valign": "vcenter"}),
        # Overview table
        "col_header": wb.add_format({**base, "bold": True, "font_color": "#FFFFFF",
                                      "bg_color": CHARCOAL, "border": 1,
                                      "border_color": "#FFFFFF", "align": "center"}),
        "account_name": wb.add_format({**base, "bold": True, "font_color": CHARCOAL,
                                        "bg_color": "#F8FAFC", "border": 1,
                                        "border_color": "#E2E8F0"}),
        "total_label": wb.add_format({**base, "bold": True, "font_color": "#FFFFFF",
                                       "bg_color": CHARCOAL, "border": 1,
                                       "border_color": "#FFFFFF"}),
        "cell_num": wb.add_format({**base, "num_format": "#,##0", "font_color": CHARCOAL,
                                    "border": 1, "border_color": "#E2E8F0", "align": "right"}),
        "cell_pct": wb.add_format({**base, "num_format": "0.00\"%\"", "font_color": CHARCOAL,
                                    "border": 1, "border_color": "#E2E8F0", "align": "right"}),
        "total_num": wb.add_format({**base, "bold": True, "num_format": "#,##0",
                                     "font_color": "#FFFFFF", "bg_color": CHARCOAL,
                                     "border": 1, "border_color": "#FFFFFF", "align": "right"}),
        "total_pct": wb.add_format({**base, "bold": True, "num_format": "0.00\"%\"",
                                     "font_color": "#FFFFFF", "bg_color": CHARCOAL,
                                     "border": 1, "border_color": "#FFFFFF", "align": "right"}),
        # Detail section labels
        "subsection": wb.add_format({**base, "bold": True, "font_color": CHARCOAL,
                                      "bg_color": "#E2E8F0", "border": 1,
                                      "border_color": "#CBD5E1"}),
        "kpi_label": wb.add_format({**base, "bold": True, "font_color": CHARCOAL,
                                     "bg_color": "#F8FAFC", "border": 1,
                                     "border_color": "#E2E8F0"}),
        "kpi_value": wb.add_format({**base, "num_format": "#,##0", "font_color": CHARCOAL,
                                     "border": 1, "border_color": "#E2E8F0"}),
        "kpi_text": wb.add_format({**base, "font_color": CHARCOAL,
                                    "border": 1, "border_color": "#E2E8F0"}),
        "kpi_money": wb.add_format({**base, "num_format": "#,##0.00", "font_color": CHARCOAL,
                                     "border": 1, "border_color": "#E2E8F0"}),
        # Breakdown table headers
        "hdr_green": wb.add_format({**base, "bold": True, "font_color": "#FFFFFF",
                                     "bg_color": SUCCESS_CLR, "border": 1,
                                     "border_color": "#FFFFFF", "align": "center"}),
        "hdr_orange": wb.add_format({**base, "bold": True, "font_color": "#FFFFFF",
                                      "bg_color": WARNING_CLR, "border": 1,
                                      "border_color": "#FFFFFF", "align": "center"}),
        "hdr_red": wb.add_format({**base, "bold": True, "font_color": "#FFFFFF",
                                   "bg_color": DANGER_CLR, "border": 1,
                                   "border_color": "#FFFFFF", "align": "center"}),
        "hdr_grey": wb.add_format({**base, "bold": True, "font_color": "#FFFFFF",
                                    "bg_color": "#64748B", "border": 1,
                                    "border_color": "#FFFFFF", "align": "center"}),
        # Data rows
        "data": wb.add_format({**base, "font_color": CHARCOAL, "border": 1,
                                "border_color": "#E2E8F0"}),
        "data_num": wb.add_format({**base, "num_format": "#,##0", "font_color": CHARCOAL,
                                    "border": 1, "border_color": "#E2E8F0", "align": "right"}),
        "data_money": wb.add_format({**base, "num_format": "#,##0.00", "font_color": CHARCOAL,
                                      "border": 1, "border_color": "#E2E8F0", "align": "right"}),
        "total_row": wb.add_format({**base, "bold": True, "font_color": "#FFFFFF",
                                     "bg_color": CHARCOAL, "border": 1,
                                     "border_color": "#FFFFFF"}),
        "total_row_num": wb.add_format({**base, "bold": True, "num_format": "#,##0",
                                         "font_color": "#FFFFFF", "bg_color": CHARCOAL,
                                         "border": 1, "border_color": "#FFFFFF",
                                         "align": "right"}),
        "total_row_money": wb.add_format({**base, "bold": True, "num_format": "#,##0.00",
                                           "font_color": "#FFFFFF", "bg_color": CHARCOAL,
                                           "border": 1, "border_color": "#FFFFFF",
                                           "align": "right"}),
        # Misc
        "money": wb.add_format({**base, "num_format": "#,##0.00", "font_color": CHARCOAL}),
        "money_bad": wb.add_format({**base, "num_format": "#,##0.00",
                                     "font_color": "#7F1D1D", "bg_color": LIGHT_RED,
                                     "bold": True}),
        "text": wb.add_format({**base, "font_color": CHARCOAL}),
        "warn": wb.add_format({**base, "font_color": "#92400E", "bg_color": LIGHT_YELLOW}),
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _col_widths(df: pl.DataFrame, sample: int = 200) -> list[int]:
    head = df.head(sample)
    widths: list[int] = []
    for col in df.columns:
        longest = len(str(col))
        if head.height:
            series = head[col].cast(pl.Utf8, strict=False).fill_null("")
            longest = max(longest, int(series.str.len_chars().max() or 0))
        widths.append(min(max(longest + 3, 11), 48))
    return widths


def _parse_amount_col(df: pl.DataFrame, col: str) -> pl.Expr:
    """Return a Polars expression that parses a column as numeric amount.
    
    If the column is already numeric, return it as-is. If it's a string,
    parse it (handles currency symbols, commas, accounting negatives, etc.).
    """
    from .sanitizer import parse_amount_expr
    
    if df.schema[col] in (pl.Float64, pl.Float32, pl.Int64, pl.Int32):
        return pl.col(col)
    else:
        return parse_amount_expr(col)


def _detect_status_col(df: pl.DataFrame, side_prefix: str) -> str | None:
    """Return the first column whose name contains 'status' on the given side.
    
    The matcher prefixes columns as 'A.' or 'B.' (capital), so we match against
    that exact capitalization.
    """
    prefix = side_prefix.upper() + "."
    candidates = [c for c in df.columns
                  if c.startswith(prefix) and "status" in c.lower()]
    return candidates[0] if candidates else None


def _detect_amount_col(df: pl.DataFrame, side_prefix: str) -> str | None:
    """Return the first amount column on the given side.
    
    The matcher prefixes columns as 'A.' or 'B.' (capital), so we match against
    that exact capitalization. Amount columns may be String (original business
    columns preserved as-is) or numeric (delta columns), so we don't filter by dtype.
    """
    prefix = side_prefix.upper() + "."
    for keyword in ("amount", "txnamount", "amt", "settlement_amount", "paid_amt"):
        for c in df.columns:
            if c.startswith(prefix) and keyword in c.lower():
                return c
    return None


def _write_row(ws, row: int, col: int, value: Any, fmt: Any) -> None:
    if value is None:
        ws.write_blank(row, col, None, fmt)
    elif isinstance(value, bool):
        ws.write_string(row, col, str(value), fmt)
    elif isinstance(value, (int, float)):
        ws.write_number(row, col, value, fmt)
    else:
        ws.write_string(row, col, str(value), fmt)


# --------------------------------------------------------------------------- #
# Sheet 1 — Summary (account-wise overview table)
# --------------------------------------------------------------------------- #

def _write_summary(
    wb, ws, result: ReconResult, formats, meta: dict,
    truncated: dict[str, int] | None = None, row_cap: int | None = None,
) -> None:
    c = result.counts
    profile = result.config.profile_name

    ws.set_column(0, 0, 28)
    ws.set_column(1, 1, 20)
    ws.set_column(2, 2, 20)
    ws.set_column(3, 3, 20)
    ws.set_column(4, 4, 14)

    # Banner
    ws.merge_range(0, 0, 0, 4,
                   "ACCOUNT-WISE RECONCILIATION OVERVIEW", formats["title"])
    ws.set_row(0, 26)

    # Column headers
    headers = ["Account", "Our Side Count", "Their Side Count",
               "Reconciled Count", "Recon Rate %"]
    for ci, h in enumerate(headers):
        ws.write(1, ci, h, formats["col_header"])
    ws.set_row(1, 18)

    # Single account row (one run = one account)
    recon_count = c.exact_matches + c.value_mismatches
    rate = 100.0 * recon_count / max(c.rows_read_a, 1)
    ws.write(2, 0, profile, formats["account_name"])
    ws.write_number(2, 1, c.rows_read_a, formats["cell_num"])
    ws.write_number(2, 2, c.rows_read_b, formats["cell_num"])
    ws.write_number(2, 3, recon_count, formats["cell_num"])
    ws.write_number(2, 4, round(rate, 2), formats["cell_pct"])

    # TOTAL row
    ws.write(3, 0, "TOTAL", formats["total_label"])
    ws.write_number(3, 1, c.rows_read_a, formats["total_num"])
    ws.write_number(3, 2, c.rows_read_b, formats["total_num"])
    ws.write_number(3, 3, recon_count, formats["total_num"])
    ws.write_number(3, 4, round(rate, 2), formats["total_pct"])

    # Run metadata below the table
    row = 5
    meta_pairs = [
        ("Generated at", meta.get("generated_at",
                                   datetime.now().isoformat(timespec="seconds"))),
        ("Run ID", meta.get("run_id", "—")),
        ("Operator", meta.get("operator", "—")),
        ("File A (internal)", meta.get("file_a", "—")),
        ("File B (partner)", meta.get("file_b", "—")),
    ]
    for label, value in meta_pairs:
        ws.write(row, 0, label, formats["kpi_label"])
        ws.write(row, 1, value, formats["kpi_text"])
        ws.merge_range(row, 1, row, 4, value, formats["kpi_text"])
        row += 1

    # KPI detail
    row += 1
    for label, value in result.summary_rows():
        ws.write(row, 0, label, formats["kpi_label"])
        fmt = formats["kpi_value"] if isinstance(value, (int, float)) else formats["kpi_text"]
        ws.write(row, 1, value, fmt)
        ws.merge_range(row, 1, row, 4, value, fmt)
        row += 1

    # Truncation notices
    for sheet_name, full in (truncated or {}).items():
        ws.write(row, 0, f"TRUNCATED — {sheet_name}", formats["kpi_label"])
        note = f"showing {row_cap:,} of {full:,} rows"
        ws.merge_range(row, 1, row, 4, note, formats["warn"])
        row += 1

    # Warnings
    if result.counts.warnings:
        row += 1
        ws.write(row, 0, "Warnings", formats["kpi_label"])
        for warning in result.counts.warnings:
            ws.merge_range(row, 1, row, 4, warning, formats["warn"])
            row += 1

    ws.freeze_panes(2, 0)


# --------------------------------------------------------------------------- #
# Sheet 2 — Detail (per-account three-section layout)
# --------------------------------------------------------------------------- #

def _write_detail(wb, ws, result: ReconResult, formats) -> None:
    """Write the per-account detail sheet with three labelled sections."""
    c = result.counts
    profile = result.config.profile_name

    ws.set_column(0, 0, 28)
    ws.set_column(1, 1, 20)
    ws.set_column(2, 2, 20)
    ws.set_column(3, 3, 20)

    # Banner
    ws.merge_range(0, 0, 0, 3, f"ConnectNPay vs {profile}", formats["title"])
    ws.set_row(0, 26)

    row = 2

    # ── Section 1: Volume Comparison ────────────────────────────────────────
    ws.merge_range(row, 0, row, 3, "1. VOLUME COMPARISON", formats["section"])
    ws.set_row(row, 18)
    row += 1

    # Sub-header
    ws.write(row, 1, "Count", formats["subsection"])
    ws.write(row, 2, "Amount", formats["subsection"])
    ws.write(row, 3, "", formats["subsection"])
    row += 1

    # Detect amount columns from the exact_matches + value_mismatches frames
    amount_a_col = _detect_amount_col(result.exact_matches, "A") or \
                   _detect_amount_col(result.value_mismatches, "A")
    amount_b_col = _detect_amount_col(result.exact_matches, "B") or \
                   _detect_amount_col(result.value_mismatches, "B")

    # Compute totals across all buckets
    def _sum_col(frames: list[pl.DataFrame], col: str | None) -> float:
        if col is None:
            return 0.0
        total = 0.0
        for df in frames:
            if col in df.columns:
                # Amount columns may be String (original business cols) or numeric (delta cols)
                # Parse if needed, then sum
                series = df[col]
                if series.dtype in (pl.Float64, pl.Float32, pl.Int64, pl.Int32):
                    s = series.drop_nulls()
                else:
                    # Parse as amount (handles currency symbols, commas, etc.)
                    from .sanitizer import parse_amount_expr
                    s = df.select(parse_amount_expr(col)).to_series().drop_nulls()
                if s.len() > 0:
                    total += float(s.sum())
        return total

    all_a = [result.exact_matches, result.value_mismatches, result.orphans_a]
    all_b = [result.exact_matches, result.value_mismatches, result.orphans_b]

    amt_a = _sum_col(all_a, amount_a_col)
    amt_b = _sum_col(all_b, amount_b_col)

    # DEBUG
    import logging
    log = logging.getLogger(__name__)
    log.info(f"Detail sheet amounts: amount_a_col={amount_a_col}, amt_a={amt_a}, amount_b_col={amount_b_col}, amt_b={amt_b}")

    ws.write(row, 0, "ConnectNPay", formats["kpi_label"])
    ws.write_number(row, 1, c.rows_read_a, formats["kpi_value"])
    ws.write_number(row, 2, amt_a, formats["kpi_money"])
    ws.write_blank(row, 3, None, formats["data"])
    row += 1

    ws.write(row, 0, profile, formats["kpi_label"])
    ws.write_number(row, 1, c.rows_read_b, formats["kpi_value"])
    ws.write_number(row, 2, amt_b, formats["kpi_money"])
    ws.write_blank(row, 3, None, formats["data"])
    row += 1

    diff_count = c.rows_read_a - c.rows_read_b
    diff_amt   = round(amt_a - amt_b, 2)
    ws.write(row, 0, "Difference", formats["kpi_label"])
    ws.write_number(row, 1, diff_count, formats["kpi_value"])
    ws.write_number(row, 2, diff_amt, formats["kpi_money"])
    ws.write_blank(row, 3, None, formats["data"])
    row += 2

    # ── Section 2: Matched Breakdown ────────────────────────────────────────
    ws.merge_range(row, 0, row, 3, "2. MATCHED BREAKDOWN", formats["section"])
    ws.set_row(row, 18)
    row += 1

    # Detect status columns
    matched_df = pl.concat([result.exact_matches, result.value_mismatches],
                           how="diagonal_relaxed") if result.value_mismatches.height > 0 \
                 else result.exact_matches

    status_a_col = _detect_status_col(matched_df, "A")
    status_b_col = _detect_status_col(matched_df, "B")
    has_status_b = status_b_col is not None

    if has_status_b:
        headers = ["CNP Status", "CPT Status", "Count", "CNP_Amount", "CPT_Amount"]
        col_widths_detail = [18, 18, 12, 18, 18]
    else:
        headers = ["CNP Status", "Count", "CNP_Amount", "CPT_Amount"]
        col_widths_detail = [18, 12, 18, 18]

    for ci, (h, w) in enumerate(zip(headers, col_widths_detail)):
        ws.set_column(ci, ci, w)
        ws.write(row, ci, h, formats["hdr_green"])
    row += 1

    # Build status cross-tab from matched frames
    row, total_count, total_amt_a, total_amt_b = _write_matched_breakdown(
        ws, row, result, matched_df, status_a_col, status_b_col,
        amount_a_col, amount_b_col, formats
    )
    row += 2

    # ── Section 3: Unmatched Breakdown ──────────────────────────────────────
    ws.merge_range(row, 0, row, 3, "3. UNMATCHED BREAKDOWN", formats["section"])
    ws.set_row(row, 18)
    row += 1

    # Our side (orphans_a)
    ws.write(row, 0, "ConnectNPay", formats["subsection"])
    ws.merge_range(row, 0, row, 3, "ConnectNPay", formats["subsection"])
    row += 1

    row = _write_unmatched_breakdown(
        ws, row, result.orphans_a, "A", status_a_col, amount_a_col, formats
    )
    row += 1

    # Their side (orphans_b)
    ws.merge_range(row, 0, row, 3, profile, formats["subsection"])
    row += 1

    row = _write_unmatched_breakdown(
        ws, row, result.orphans_b, "B", status_b_col, amount_b_col, formats
    )

    ws.freeze_panes(1, 0)


def _write_matched_breakdown(
    ws, row: int,
    result: ReconResult,
    matched_df: pl.DataFrame,
    status_a_col: str | None,
    status_b_col: str | None,
    amount_a_col: str | None,
    amount_b_col: str | None,
    formats,
) -> tuple[int, int, float, float]:
    """Write grouped matched rows; returns (next_row, total_count, total_amt_a, total_amt_b)."""
    has_status_b = status_b_col is not None
    total_count = 0
    total_amt_a = 0.0
    total_amt_b = 0.0

    if matched_df.height == 0:
        ws.write(row, 0, "No matched transactions.", formats["data"])
        return row + 1, 0, 0.0, 0.0

    # Build grouping key
    group_cols: list[str] = []
    if status_a_col and status_a_col in matched_df.columns:
        group_cols.append(status_a_col)
    if has_status_b and status_b_col and status_b_col in matched_df.columns:
        group_cols.append(status_b_col)

    if group_cols:
        agg_exprs = [pl.len().alias("__count")]
        if amount_a_col and amount_a_col in matched_df.columns:
            agg_exprs.append(_parse_amount_col(matched_df, amount_a_col).sum().alias("__amt_a"))
        if amount_b_col and amount_b_col in matched_df.columns:
            agg_exprs.append(_parse_amount_col(matched_df, amount_b_col).sum().alias("__amt_b"))
        grouped = matched_df.group_by(group_cols).agg(agg_exprs).sort(group_cols)
        rows_iter = grouped.iter_rows(named=True)
    else:
        # No status columns — single aggregate row
        count = matched_df.height
        amt_a = 0.0
        amt_b = 0.0
        if amount_a_col and amount_a_col in matched_df.columns:
            amt_a = float(matched_df.select(_parse_amount_col(matched_df, amount_a_col).sum()).item() or 0.0)
        if amount_b_col and amount_b_col in matched_df.columns:
            amt_b = float(matched_df.select(_parse_amount_col(matched_df, amount_b_col).sum()).item() or 0.0)
        rows_iter = [{"__count": count, "__amt_a": amt_a, "__amt_b": amt_b}]

    for grp in rows_iter:
        cnt   = grp.get("__count", 0)
        amt_a = round(grp.get("__amt_a", 0.0) or 0.0, 2)
        amt_b = round(grp.get("__amt_b", 0.0) or 0.0, 2)
        total_count += cnt
        total_amt_a += amt_a
        total_amt_b += amt_b

        ci = 0
        if group_cols:
            for gc in group_cols:
                ws.write(row, ci, str(grp.get(gc, "")), formats["data"])
                ci += 1
        ws.write_number(row, ci, cnt, formats["data_num"]); ci += 1
        ws.write_number(row, ci, amt_a, formats["data_money"]); ci += 1
        ws.write_number(row, ci, amt_b, formats["data_money"])
        row += 1

    # TOTAL row
    ci = 0
    if group_cols:
        ws.write(row, ci, "TOTAL", formats["total_row"])
        ci += 1
        for _ in group_cols[1:]:
            ws.write_blank(row, ci, None, formats["total_row"]); ci += 1
    else:
        ws.write(row, ci, "TOTAL", formats["total_row"]); ci += 1
    ws.write_number(row, ci, total_count, formats["total_row_num"]); ci += 1
    ws.write_number(row, ci, round(total_amt_a, 2), formats["total_row_money"]); ci += 1
    ws.write_number(row, ci, round(total_amt_b, 2), formats["total_row_money"])
    row += 1

    return row, total_count, total_amt_a, total_amt_b


def _write_unmatched_breakdown(
    ws, row: int,
    orphan_df: pl.DataFrame,
    side: str,
    status_col: str | None,
    amount_col: str | None,
    formats,
) -> int:
    """Write Status / Count / Amount breakdown for one orphan side."""
    # Headers
    ws.write(row, 0, "Status", formats["hdr_red"])
    ws.write(row, 1, "Count", formats["hdr_red"])
    ws.write(row, 2, "Amount", formats["hdr_red"])
    ws.write_blank(row, 3, None, formats["hdr_red"])
    row += 1

    total_count = 0
    total_amount = 0.0

    if orphan_df.height == 0:
        ws.write(row, 0, "TOTAL", formats["total_row"])
        ws.write_number(row, 1, 0, formats["total_row_num"])
        ws.write_number(row, 2, 0.0, formats["total_row_money"])
        ws.write_blank(row, 3, None, formats["total_row"])
        return row + 1

    if status_col and status_col in orphan_df.columns:
        agg_exprs = [pl.len().alias("__count")]
        if amount_col and amount_col in orphan_df.columns:
            agg_exprs.append(_parse_amount_col(orphan_df, amount_col).sum().alias("__amt"))
        grouped = orphan_df.group_by(status_col).agg(agg_exprs).sort(status_col)
        for grp in grouped.iter_rows(named=True):
            status = str(grp.get(status_col, ""))
            cnt    = grp.get("__count", 0)
            amt    = round(grp.get("__amt", 0.0) or 0.0, 2)
            total_count  += cnt
            total_amount += amt
            ws.write(row, 0, status, formats["data"])
            ws.write_number(row, 1, cnt, formats["data_num"])
            ws.write_number(row, 2, amt, formats["data_money"])
            ws.write_blank(row, 3, None, formats["data"])
            row += 1
    else:
        # No status column — use a generic label
        label = "All Transactions"
        amt = 0.0
        if amount_col and amount_col in orphan_df.columns:
            amt = round(float(orphan_df.select(_parse_amount_col(orphan_df, amount_col).sum()).item() or 0.0), 2)
        total_count  = orphan_df.height
        total_amount = amt
        ws.write(row, 0, label, formats["data"])
        ws.write_number(row, 1, total_count, formats["data_num"])
        ws.write_number(row, 2, total_amount, formats["data_money"])
        ws.write_blank(row, 3, None, formats["data"])
        row += 1

    # TOTAL
    ws.write(row, 0, "TOTAL", formats["total_row"])
    ws.write_number(row, 1, total_count, formats["total_row_num"])
    ws.write_number(row, 2, round(total_amount, 2), formats["total_row_money"])
    ws.write_blank(row, 3, None, formats["total_row"])
    row += 1

    return row


# --------------------------------------------------------------------------- #
# Sheet 3 — Unrecon_Summary
# --------------------------------------------------------------------------- #

def _write_unrecon_summary(wb, ws, result: ReconResult, formats) -> None:
    """Success-only unreconciled transactions summary table."""
    ws.set_column(0, 0, 20)
    ws.set_column(1, 1, 16)
    ws.set_column(2, 2, 18)
    ws.set_column(3, 3, 16)
    ws.set_column(4, 4, 18)
    ws.set_column(5, 5, 36)

    ws.merge_range(0, 0, 0, 5,
                   "Unreconciled Transactions Summary", formats["title"])
    ws.set_row(0, 26)

    headers = ["Account", "Our Count", "Our Amount",
               "Their Count", "Their Amount", "Remarks"]
    for ci, h in enumerate(headers):
        ws.write(1, ci, h, formats["col_header"])

    c = result.counts
    profile = result.config.profile_name

    # Filter success-only orphans where possible
    status_a = _detect_status_col(result.orphans_a, "A")
    status_b = _detect_status_col(result.orphans_b, "B")
    amount_a = _detect_amount_col(result.orphans_a, "A")
    amount_b = _detect_amount_col(result.orphans_b, "B")

    def _success_filter(df: pl.DataFrame, status_col: str | None) -> pl.DataFrame:
        if status_col and status_col in df.columns:
            return df.filter(pl.col(status_col).cast(pl.Utf8).str.to_lowercase() == "success")
        return df

    succ_a = _success_filter(result.orphans_a, status_a)
    succ_b = _success_filter(result.orphans_b, status_b)

    our_count   = succ_a.height
    their_count = succ_b.height

    our_amt = 0.0
    if amount_a and amount_a in succ_a.columns and succ_a.height > 0:
        our_amt = round(float(succ_a.select(_parse_amount_col(succ_a, amount_a).sum()).item() or 0.0), 2)

    their_amt = 0.0
    if amount_b and amount_b in succ_b.columns and succ_b.height > 0:
        their_amt = round(float(succ_b.select(_parse_amount_col(succ_b, amount_b).sum()).item() or 0.0), 2)

    row = 2
    if our_count > 0 or their_count > 0:
        ws.write(row, 0, profile, formats["account_name"])
        ws.write_number(row, 1, our_count, formats["cell_num"])
        ws.write_number(row, 2, our_amt, formats["kpi_money"])
        ws.write_number(row, 3, their_count, formats["cell_num"])
        ws.write_number(row, 4, their_amt, formats["kpi_money"])
        ws.write(row, 5, "Previous / Next Day Settlement.", formats["data"])
        row += 1

    # TOTAL
    ws.write(row, 0, "TOTAL", formats["total_label"])
    ws.write_number(row, 1, our_count, formats["total_num"])
    ws.write_number(row, 2, round(our_amt, 2), formats["total_row_money"])
    ws.write_number(row, 3, their_count, formats["total_num"])
    ws.write_number(row, 4, round(their_amt, 2), formats["total_row_money"])
    ws.write_blank(row, 5, None, formats["total_label"])

    ws.freeze_panes(2, 0)


# --------------------------------------------------------------------------- #
# Sheet 4 — Reconciled_Details
# --------------------------------------------------------------------------- #

def _write_reconciled_details(wb, ws, result: ReconResult, formats,
                               row_cap: int | None = None) -> None:
    """Raw transaction rows for successfully reconciled (matched) entries."""
    ws.merge_range(0, 0, 0, 5,
                   "Reconciled Transaction Details", formats["title"])
    ws.set_row(0, 26)

    row = 2

    # ── Successfully Reconciled (Matched Transactions) ────────────────────────
    ws.write(row, 0, "SUCCESSFULLY RECONCILED TRANSACTIONS", formats["section"])
    ws.merge_range(row, 0, row, 5, "SUCCESSFULLY RECONCILED TRANSACTIONS", formats["section"])
    row += 1

    # Combine exact matches and value mismatches into single matched dataset
    matched_df = pl.concat([result.exact_matches, result.value_mismatches],
                           how="diagonal_relaxed") if result.value_mismatches.height > 0 \
                 else result.exact_matches

    if matched_df.height > 0:
        ws.write(row, 0, "Exact Matches & Value Mismatches (Reconciled)", formats["subsection"])
        row += 1
        row = _write_raw_frame(ws, row, matched_df, formats, row_cap, "hdr_green")
        ws.write(row, 0,
                 f"Total Reconciled: {matched_df.height} txn(s)",
                 formats["data"])
    else:
        ws.write(row, 0, "No reconciled transactions.", formats["data"])

    ws.freeze_panes(1, 0)


# --------------------------------------------------------------------------- #
# Sheet 5 — Unreconciled_Details
# --------------------------------------------------------------------------- #

def _write_unreconciled_details(wb, ws, result: ReconResult, formats,
                                 row_cap: int | None = None) -> None:
    """Raw transaction rows for unreconciled (orphan) entries."""
    profile = result.config.profile_name

    ws.merge_range(0, 0, 0, 5,
                   "Unreconciled Transaction Details", formats["title"])
    ws.set_row(0, 26)

    row = 2

    status_a = _detect_status_col(result.orphans_a, "A")
    status_b = _detect_status_col(result.orphans_b, "B")

    def _success_filter(df: pl.DataFrame, status_col: str | None) -> pl.DataFrame:
        if status_col and status_col in df.columns:
            return df.filter(pl.col(status_col).cast(pl.Utf8).str.to_lowercase() == "success")
        return df

    def _non_success_filter(df: pl.DataFrame, status_col: str | None) -> pl.DataFrame:
        if status_col and status_col in df.columns:
            return df.filter(pl.col(status_col).cast(pl.Utf8).str.to_lowercase() != "success")
        return pl.DataFrame()  # If no status column, return empty

    succ_a = _success_filter(result.orphans_a, status_a)
    succ_b = _success_filter(result.orphans_b, status_b)
    other_a = _non_success_filter(result.orphans_a, status_a)
    other_b = _non_success_filter(result.orphans_b, status_b)

    # ── Section 1: Unreconciled - Success Only ───────────────────────────────
    ws.write(row, 0, "UNRECONCILED TRANSACTIONS - SUCCESS STATUS", formats["section"])
    ws.merge_range(row, 0, row, 5, "UNRECONCILED TRANSACTIONS - SUCCESS STATUS", formats["section"])
    row += 1

    # ── CNP Exclusive (our success orphans) ──────────────────────────────────
    ws.write(row, 0, "CNP Exclusive - Success", formats["subsection"])
    row += 1

    row = _write_raw_frame(ws, row, succ_a, formats, row_cap, "hdr_orange")

    ws.write(row, 0,
             f"CNP Success Unreconciled: {succ_a.height} txn(s)",
             formats["data"])
    row += 2

    # ── Their Exclusive (partner success orphans) ────────────────────────────
    ws.write(row, 0, f"{profile} Exclusive - Success", formats["subsection"])
    row += 1

    row = _write_raw_frame(ws, row, succ_b, formats, row_cap, "hdr_orange")

    ws.write(row, 0,
             f"{profile} Success Unreconciled: {succ_b.height} txn(s)",
             formats["data"])
    row += 2

    # ── Section 2: Unreconciled - All Other Statuses ─────────────────────────
    ws.write(row, 0, "UNRECONCILED TRANSACTIONS - OTHER STATUSES", formats["section"])
    ws.merge_range(row, 0, row, 5, "UNRECONCILED TRANSACTIONS - OTHER STATUSES", formats["section"])
    row += 1

    # ── CNP Exclusive (our non-success orphans) ──────────────────────────────
    ws.write(row, 0, "CNP Exclusive - Failed/Pending/Other", formats["subsection"])
    row += 1

    row = _write_raw_frame(ws, row, other_a, formats, row_cap, "hdr_red")

    ws.write(row, 0,
             f"CNP Other Status Unreconciled: {other_a.height} txn(s)",
             formats["data"])
    row += 2

    # ── Their Exclusive (partner non-success orphans) ────────────────────────
    ws.write(row, 0, f"{profile} Exclusive - Failed/Pending/Other", formats["subsection"])
    row += 1

    row = _write_raw_frame(ws, row, other_b, formats, row_cap, "hdr_red")

    ws.write(row, 0,
             f"{profile} Other Status Unreconciled: {other_b.height} txn(s)",
             formats["data"])

    ws.freeze_panes(1, 0)


def _write_raw_frame(
    ws, row: int, df: pl.DataFrame, formats, row_cap: int | None, hdr_key: str
) -> int:
    """Write column headers + all data rows for a raw frame."""
    if df.width == 0:
        ws.write(row, 0, "No rows.", formats["data"])
        return row + 1

    hdr_fmt = formats[hdr_key]
    widths = _col_widths(df)
    for ci, (col, w) in enumerate(zip(df.columns, widths)):
        ws.set_column(ci, ci, w)
        ws.write(row, ci, str(col), hdr_fmt)
    row += 1

    capped = df.head(row_cap) if row_cap else df
    for record in capped.iter_rows():
        for ci, value in enumerate(record):
            fmt = formats["data_money"] if isinstance(value, float) else formats["data"]
            _write_row(ws, row, ci, value, fmt)
        row += 1

    if row_cap and df.height > row_cap:
        ws.write(row, 0,
                 f"⚠ Truncated: {row_cap:,} of {df.height:,} rows shown.",
                 formats["warn"])
        row += 1

    return row


# --------------------------------------------------------------------------- #
# Sheet 6 — Value_Mismatches (retained for triage / downstream macros)
# --------------------------------------------------------------------------- #

def _write_value_mismatches(wb, ws, df: pl.DataFrame, formats,
                             row_cap: int | None = None,
                             full_height: int | None = None) -> None:
    hdr_fmt = formats["hdr_orange"]
    if df.width == 0:
        ws.write(0, 0, "No value mismatches.", formats["text"])
        return

    widths = _col_widths(df)
    delta_cols: list[int] = []
    for ci, (col, w) in enumerate(zip(df.columns, widths)):
        is_money = df.schema[col] in (pl.Float64, pl.Float32)
        ws.set_column(ci, ci, w, formats["money"] if is_money else formats["text"])
        if str(col).startswith("delta."):
            delta_cols.append(ci)
        ws.write(0, ci, str(col), hdr_fmt)

    for r, record in enumerate(df.iter_rows(), start=1):
        for ci, value in enumerate(record):
            if value is None:
                ws.write_blank(r, ci, None)
            elif isinstance(value, bool):
                ws.write_string(r, ci, str(value))
            elif isinstance(value, (int, float)):
                ws.write_number(r, ci, value)
            else:
                ws.write_string(r, ci, str(value))

    if full_height is not None and full_height > df.height:
        ws.write(df.height + 1, 0,
                 f"⚠ Truncated: {df.height:,} of {full_height:,} rows shown (row cap applied).",
                 formats["warn"])

    last_row = max(df.height, 1)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, last_row, df.width - 1)

    for ci in delta_cols:
        ws.conditional_format(
            1, ci, last_row, ci,
            {"type": "cell", "criteria": "!=", "value": 0, "format": formats["money_bad"]},
        )


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def build_workbook(
    result: ReconResult, meta: dict | None = None, row_cap: int | None = None
) -> bytes:
    """Render the workbook and return it as bytes.

    Sheets produced (in order):
        1. Summary                              — account-wise overview + run metadata
        2. Detail                               — per-account three-section layout
        3. Unrecon_Summary                      — success-only orphan counts
        4. Reconciled_Details                   — successfully reconciled (matched) transactions
        5. Unreconciled_Details                 — unreconciled (orphan) transactions:
                                                   * Unreconciled Success-status transactions
                                                   * Unreconciled Other-status transactions
        6. Value_Mismatches                     — amount-break triage sheet

    `row_cap` bounds the rows written per raw-data sheet.
    """
    meta = meta or {}
    buffer = io.BytesIO()

    biggest = max(
        result.exact_matches.height,
        result.value_mismatches.height,
        result.orphans_a.height,
        result.orphans_b.height,
        1,
    )
    options = {"in_memory": True, "constant_memory": biggest > CONSTANT_MEMORY_THRESHOLD}
    wb = xlsxwriter.Workbook(buffer, options)
    fmts = _formats(wb)

    vm_full = result.value_mismatches.height
    vm_df   = result.value_mismatches.head(row_cap) if row_cap else result.value_mismatches
    truncated: dict[str, int] = {}
    if row_cap and vm_full > row_cap:
        truncated["Value_Mismatches"] = vm_full

    # 1. Summary
    try:
        ws = wb.add_worksheet("Summary")
        ws.set_tab_color(BRAND_GREEN)
        _write_summary(wb, ws, result, fmts, meta, truncated, row_cap)
    except Exception as e:
        log.error(f"Failed to create 'Summary' worksheet: {e}")
        raise

    # 2. Detail — sheet name = sanitised profile name (max 31 chars, Excel limit)
    detail_name = _excel_sheet_name(result.config.profile_name)
    log.info(f"Creating Detail worksheet with name: '{detail_name}' (length: {len(detail_name)})")
    try:
        ws = wb.add_worksheet(detail_name)
        ws.set_tab_color(SUCCESS_CLR)
        _write_detail(wb, ws, result, fmts)
    except Exception as e:
        log.error(f"Failed to create Detail worksheet '{detail_name}': {e}")
        raise

    # 3. Unrecon_Summary
    try:
        ws = wb.add_worksheet("Unrecon_Summary")
        ws.set_tab_color(WARNING_CLR)
        _write_unrecon_summary(wb, ws, result, fmts)
    except Exception as e:
        log.error(f"Failed to create 'Unrecon_Summary' worksheet: {e}")
        raise

    # 4. Reconciled_Details
    try:
        ws = wb.add_worksheet("Reconciled_Details")
        ws.set_tab_color(SUCCESS_CLR)
        _write_reconciled_details(wb, ws, result, fmts, row_cap)
    except Exception as e:
        log.error(f"Failed to create 'Reconciled_Details' worksheet: {e}")
        raise

    # 5. Unreconciled_Details
    try:
        ws = wb.add_worksheet("Unreconciled_Details")
        ws.set_tab_color(DANGER_CLR)
        _write_unreconciled_details(wb, ws, result, fmts, row_cap)
    except Exception as e:
        log.error(f"Failed to create 'Unreconciled_Details' worksheet: {e}")
        raise

    # 6. Value_Mismatches
    try:
        ws = wb.add_worksheet("Value_Mismatches")
        ws.set_tab_color(WARNING_CLR)
        _write_value_mismatches(wb, ws, vm_df, fmts, row_cap, full_height=vm_full)
    except Exception as e:
        log.error(f"Failed to create 'Value_Mismatches' worksheet: {e}")
        raise

    wb.close()
    payload = buffer.getvalue()
    log.info("workbook built: %d bytes, %d sheets", len(payload), 6)
    return payload


def write_workbook(result: ReconResult, path: str, meta: dict | None = None) -> str:
    with open(path, "wb") as fh:
        fh.write(build_workbook(result, meta))
    return path


def _excel_sheet_name(name: str, max_len: int = 31) -> str:
    """Sanitise a string into a valid Excel sheet name.
    
    Excel sheet names must:
    - Be 1-31 characters long
    - Not contain: \\ / : * ? [ ]
    - Not start or end with an apostrophe
    
    Args:
        name: The proposed worksheet name
        max_len: Maximum length (default 31, Excel's limit)
    
    Returns:
        A valid Excel worksheet name
    """
    if not name or not isinstance(name, str):
        return "Detail"
    
    name = str(name).strip()
    if not name:
        return "Detail"
    
    # Remove invalid characters: \ / : * ? [ ]
    invalid_chars = ['\\', '/', ':', '*', '?', '[', ']']
    cleaned = "".join(ch if ch not in invalid_chars else "_" for ch in name)
    
    # Remove leading/trailing apostrophes (Excel restriction)
    cleaned = cleaned.strip("'").strip()
    
    # Ensure not empty after cleaning
    if not cleaned:
        return "Detail"
    
    # Truncate to max length
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    
    # Final validation: ensure no apostrophes at edges after truncation
    cleaned = cleaned.strip("'").strip()
    
    # Ultimate fallback
    return cleaned if cleaned else "Detail"
