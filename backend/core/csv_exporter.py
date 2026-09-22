"""Multi-file CSV export for reconciliation results.

Exports the reconciliation result as multiple CSV files (one per sheet)
which are then packaged into a ZIP archive for download.

This is a lightweight alternative to the Excel workbook exporter.
"""

from __future__ import annotations

import io
import logging
import zipfile
from datetime import datetime
from typing import Any

import polars as pl

from .matcher import ReconResult

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

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
    """Return the first column whose name contains 'status' on the given side."""
    prefix = side_prefix.upper() + "."
    candidates = [c for c in df.columns
                  if c.startswith(prefix) and "status" in c.lower()]
    return candidates[0] if candidates else None


def _detect_amount_col(df: pl.DataFrame, side_prefix: str) -> str | None:
    """Return the first amount column on the given side."""
    prefix = side_prefix.upper() + "."
    for keyword in ("amount", "txnamount", "amt", "settlement_amount", "paid_amt"):
        for c in df.columns:
            if c.startswith(prefix) and keyword in c.lower():
                return c
    return None


# --------------------------------------------------------------------------- #
# CSV Generators
# --------------------------------------------------------------------------- #

def _generate_summary_csv(result: ReconResult, meta: dict) -> str:
    """Generate Summary CSV content."""
    c = result.counts
    profile = result.config.profile_name
    
    # Account-wise overview
    recon_count = c.exact_matches + c.value_mismatches
    rate = 100.0 * recon_count / max(c.rows_read_a, 1)
    
    rows = []
    rows.append(["ACCOUNT-WISE RECONCILIATION OVERVIEW"])
    rows.append([])
    rows.append(["Account", "Our Side Count", "Their Side Count", "Reconciled Count", "Recon Rate %"])
    rows.append([profile, c.rows_read_a, c.rows_read_b, recon_count, round(rate, 2)])
    rows.append(["TOTAL", c.rows_read_a, c.rows_read_b, recon_count, round(rate, 2)])
    rows.append([])
    
    # Metadata
    rows.append(["Run Metadata"])
    rows.append(["Generated at", meta.get("generated_at", datetime.now().isoformat(timespec="seconds"))])
    rows.append(["Run ID", meta.get("run_id", "—")])
    rows.append(["Operator", meta.get("operator", "—")])
    rows.append(["File A (internal)", meta.get("file_a", "—")])
    rows.append(["File B (partner)", meta.get("file_b", "—")])
    rows.append([])
    
    # KPI detail
    rows.append(["Detailed Metrics"])
    for label, value in result.summary_rows():
        rows.append([label, value])
    
    # Warnings
    if result.counts.warnings:
        rows.append([])
        rows.append(["Warnings"])
        for warning in result.counts.warnings:
            rows.append([warning])
    
    # Convert to CSV
    output = io.StringIO()
    for row in rows:
        output.write(",".join(str(v) for v in row) + "\n")
    return output.getvalue()


def _generate_detail_csv(result: ReconResult) -> str:
    """Generate Detail CSV content."""
    c = result.counts
    profile = result.config.profile_name
    
    rows = []
    rows.append([f"ConnectNPay vs {profile} - Detailed Reconciliation Report"])
    rows.append([])
    
    # ── Section 1: Volume Comparison ────────────────────────────────────────
    rows.append(["1. VOLUME COMPARISON"])
    rows.append(["", "Count", "Amount"])
    
    # Detect amount columns
    amount_a_col = _detect_amount_col(result.exact_matches, "A") or \
                   _detect_amount_col(result.value_mismatches, "A")
    amount_b_col = _detect_amount_col(result.exact_matches, "B") or \
                   _detect_amount_col(result.value_mismatches, "B")
    
    # Compute totals
    def _sum_col(frames: list[pl.DataFrame], col: str | None) -> float:
        if col is None:
            return 0.0
        total = 0.0
        for df in frames:
            if col in df.columns:
                series = df[col]
                if series.dtype in (pl.Float64, pl.Float32, pl.Int64, pl.Int32):
                    s = series.drop_nulls()
                else:
                    from .sanitizer import parse_amount_expr
                    s = df.select(parse_amount_expr(col)).to_series().drop_nulls()
                if s.len() > 0:
                    total += float(s.sum())
        return total
    
    all_a = [result.exact_matches, result.value_mismatches, result.orphans_a]
    all_b = [result.exact_matches, result.value_mismatches, result.orphans_b]
    
    amt_a = _sum_col(all_a, amount_a_col)
    amt_b = _sum_col(all_b, amount_b_col)
    
    rows.append(["ConnectNPay", c.rows_read_a, round(amt_a, 2)])
    rows.append([profile, c.rows_read_b, round(amt_b, 2)])
    rows.append(["Difference", c.rows_read_a - c.rows_read_b, round(amt_a - amt_b, 2)])
    rows.append([])
    
    # ── Section 2: Matched Breakdown ────────────────────────────────────────
    rows.append(["2. MATCHED BREAKDOWN"])
    
    matched_df = pl.concat([result.exact_matches, result.value_mismatches],
                           how="diagonal_relaxed") if result.value_mismatches.height > 0 \
                 else result.exact_matches
    
    status_a_col = _detect_status_col(matched_df, "A")
    status_b_col = _detect_status_col(matched_df, "B")
    has_status_b = status_b_col is not None
    
    if has_status_b:
        rows.append(["CNP Status", "CPT Status", "Count", "CNP_Amount", "CPT_Amount"])
    else:
        rows.append(["CNP Status", "Count", "CNP_Amount", "CPT_Amount"])
    
    # Build status cross-tab
    if matched_df.height > 0:
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
            
            total_count = 0
            total_amt_a = 0.0
            total_amt_b = 0.0
            
            for grp in grouped.iter_rows(named=True):
                cnt = grp.get("__count", 0)
                amt_a = round(grp.get("__amt_a", 0.0) or 0.0, 2)
                amt_b = round(grp.get("__amt_b", 0.0) or 0.0, 2)
                total_count += cnt
                total_amt_a += amt_a
                total_amt_b += amt_b
                
                row_data = []
                for gc in group_cols:
                    row_data.append(str(grp.get(gc, "")))
                row_data.extend([cnt, amt_a, amt_b])
                rows.append(row_data)
            
            # TOTAL row
            total_row = ["TOTAL"] + ([""] * (len(group_cols) - 1)) if len(group_cols) > 1 else ["TOTAL"]
            total_row.extend([total_count, round(total_amt_a, 2), round(total_amt_b, 2)])
            rows.append(total_row)
        else:
            # No status columns
            count = matched_df.height
            amt_a = 0.0
            amt_b = 0.0
            if amount_a_col and amount_a_col in matched_df.columns:
                amt_a = float(matched_df.select(_parse_amount_col(matched_df, amount_a_col).sum()).item() or 0.0)
            if amount_b_col and amount_b_col in matched_df.columns:
                amt_b = float(matched_df.select(_parse_amount_col(matched_df, amount_b_col).sum()).item() or 0.0)
            rows.append(["All", count, round(amt_a, 2), round(amt_b, 2)])
    
    rows.append([])
    
    # ── Section 3: Unmatched Breakdown ──────────────────────────────────────
    rows.append(["3. UNMATCHED BREAKDOWN"])
    rows.append([])
    rows.append(["ConnectNPay Unmatched"])
    rows.append(["Status", "Count", "Amount"])
    rows.extend(_generate_unmatched_rows(result.orphans_a, "A", status_a_col, amount_a_col))
    rows.append([])
    
    rows.append([f"{profile} Unmatched"])
    rows.append(["Status", "Count", "Amount"])
    rows.extend(_generate_unmatched_rows(result.orphans_b, "B", status_b_col, amount_b_col))
    
    # Convert to CSV
    output = io.StringIO()
    for row in rows:
        output.write(",".join(str(v) for v in row) + "\n")
    return output.getvalue()


def _generate_unmatched_rows(
    orphan_df: pl.DataFrame,
    side: str,
    status_col: str | None,
    amount_col: str | None
) -> list[list]:
    """Generate unmatched breakdown rows."""
    rows = []
    total_count = 0
    total_amount = 0.0
    
    if orphan_df.height == 0:
        rows.append(["TOTAL", 0, 0.0])
        return rows
    
    if status_col and status_col in orphan_df.columns:
        agg_exprs = [pl.len().alias("__count")]
        if amount_col and amount_col in orphan_df.columns:
            agg_exprs.append(_parse_amount_col(orphan_df, amount_col).sum().alias("__amt"))
        grouped = orphan_df.group_by(status_col).agg(agg_exprs).sort(status_col)
        for grp in grouped.iter_rows(named=True):
            status = str(grp.get(status_col, ""))
            cnt = grp.get("__count", 0)
            amt = round(grp.get("__amt", 0.0) or 0.0, 2)
            total_count += cnt
            total_amount += amt
            rows.append([status, cnt, amt])
    else:
        label = "All Transactions"
        amt = 0.0
        if amount_col and amount_col in orphan_df.columns:
            amt = round(float(orphan_df.select(_parse_amount_col(orphan_df, amount_col).sum()).item() or 0.0), 2)
        total_count = orphan_df.height
        total_amount = amt
        rows.append([label, total_count, total_amount])
    
    rows.append(["TOTAL", total_count, round(total_amount, 2)])
    return rows


def _generate_unrecon_summary_csv(result: ReconResult) -> str:
    """Generate Unrecon_Summary CSV content."""
    c = result.counts
    profile = result.config.profile_name
    
    rows = []
    rows.append(["Unreconciled Transactions Summary"])
    rows.append([])
    rows.append(["Account", "Our Count", "Our Amount", "Their Count", "Their Amount", "Remarks"])
    
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
    
    our_count = succ_a.height
    their_count = succ_b.height
    
    our_amt = 0.0
    if amount_a and amount_a in succ_a.columns and succ_a.height > 0:
        our_amt = round(float(succ_a.select(_parse_amount_col(succ_a, amount_a).sum()).item() or 0.0), 2)
    
    their_amt = 0.0
    if amount_b and amount_b in succ_b.columns and succ_b.height > 0:
        their_amt = round(float(succ_b.select(_parse_amount_col(succ_b, amount_b).sum()).item() or 0.0), 2)
    
    if our_count > 0 or their_count > 0:
        rows.append([profile, our_count, our_amt, their_count, their_amt, "Previous / Next Day Settlement."])
    
    rows.append(["TOTAL", our_count, round(our_amt, 2), their_count, round(their_amt, 2), ""])
    
    # Convert to CSV
    output = io.StringIO()
    for row in rows:
        output.write(",".join(str(v) for v in row) + "\n")
    return output.getvalue()


def _dataframe_to_csv(df: pl.DataFrame, row_cap: int | None = None) -> str:
    """Convert a Polars DataFrame to CSV string."""
    if row_cap:
        df = df.head(row_cap)
    return df.write_csv()


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def build_csv_archive(
    result: ReconResult, meta: dict | None = None, row_cap: int | None = None
) -> bytes:
    """Build a ZIP archive containing multiple CSV files.
    
    Files produced:
        1. summary.csv              — account-wise overview + run metadata
        2. detail.csv               — per-account three-section layout
        3. unrecon_summary.csv      — success-only orphan counts
        4. unreconciled_details.csv — raw unreconciled rows (both sides)
        5. value_mismatches.csv     — amount-break triage data
        6. exact_matches.csv        — all exact matches
        7. orphans_a.csv            — orphans from side A
        8. orphans_b.csv            — orphans from side B
    
    `row_cap` bounds the rows per data file.
    """
    meta = meta or {}
    buffer = io.BytesIO()
    
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. Summary
        zf.writestr("summary.csv", _generate_summary_csv(result, meta))
        
        # 2. Detail
        zf.writestr("detail.csv", _generate_detail_csv(result))
        
        # 3. Unrecon Summary
        zf.writestr("unrecon_summary.csv", _generate_unrecon_summary_csv(result))
        
        # 4. Unreconciled Details - combine success-only orphans
        status_a = _detect_status_col(result.orphans_a, "A")
        status_b = _detect_status_col(result.orphans_b, "B")
        
        def _success_filter(df: pl.DataFrame, status_col: str | None) -> pl.DataFrame:
            if status_col and status_col in df.columns:
                return df.filter(pl.col(status_col).cast(pl.Utf8).str.to_lowercase() == "success")
            return df
        
        succ_a = _success_filter(result.orphans_a, status_a)
        succ_b = _success_filter(result.orphans_b, status_b)
        
        # Combine with a side indicator
        if succ_a.height > 0:
            succ_a = succ_a.with_columns(pl.lit("CNP Exclusive").alias("_Side"))
        if succ_b.height > 0:
            succ_b = succ_b.with_columns(pl.lit("Partner Exclusive").alias("_Side"))
        
        if succ_a.height > 0 and succ_b.height > 0:
            unrecon_details = pl.concat([succ_a, succ_b], how="diagonal_relaxed")
        elif succ_a.height > 0:
            unrecon_details = succ_a
        elif succ_b.height > 0:
            unrecon_details = succ_b
        else:
            unrecon_details = pl.DataFrame()
        
        zf.writestr("unreconciled_details.csv", _dataframe_to_csv(unrecon_details, row_cap))
        
        # 5. Value Mismatches
        zf.writestr("value_mismatches.csv", _dataframe_to_csv(result.value_mismatches, row_cap))
        
        # 6. Exact Matches
        zf.writestr("exact_matches.csv", _dataframe_to_csv(result.exact_matches, row_cap))
        
        # 7. Orphans A
        zf.writestr("orphans_a.csv", _dataframe_to_csv(result.orphans_a, row_cap))
        
        # 8. Orphans B
        zf.writestr("orphans_b.csv", _dataframe_to_csv(result.orphans_b, row_cap))
    
    payload = buffer.getvalue()
    log.info("CSV archive built: %d bytes, 8 files", len(payload))
    return payload
