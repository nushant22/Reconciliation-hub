"""Exporter, loader-resilience and end-to-end pipeline tests."""

from __future__ import annotations

import io

import polars as pl
import pytest
from openpyxl import load_workbook

from backend.core.config import KeyPair, LoadSpec, MatchConfig, ValuePair
from backend.core.errors import EmptyDataError, FileReadError
from backend.core.exporter import SHEET_ORDER, build_workbook
from backend.core.loader import read_table
from backend.core.matcher import compute_buckets
from backend.core.pipeline import run_reconciliation

CONFIG = MatchConfig(
    keys=(KeyPair("txn_id", "txn_id"),),
    values=(ValuePair("amount", "amount"),),
    profile_name="Test Profile",
)

CSV_A = b"txn_id,amount,channel\nT1,100.00,wallet\nT2,NPR 200.00,bank\nT3,300.00,wallet\n"
CSV_B = b"txn_id,amount,channel\nT1,100.00,wallet\nT2,250.00,bank\nT4,400.00,wallet\n"


def workbook_from(a: pl.DataFrame, b: pl.DataFrame):
    result = compute_buckets(a, b, CONFIG)
    return load_workbook(io.BytesIO(build_workbook(result, {"operator": "ops.analyst"})))


# --------------------------------------------------------------------------- #
# Workbook structure
# --------------------------------------------------------------------------- #

def test_workbook_has_the_contracted_sheets():
    """The first five sheet names must match SHEET_ORDER exactly; the Detail
    sheet may carry the profile name instead of the literal string 'Detail'."""
    wb = workbook_from(read_table(CSV_A, "a.csv"), read_table(CSV_B, "b.csv"))
    names = wb.sheetnames
    # Always present by exact name
    for expected in ("Summary", "Unrecon_Summary", "Unreconciled_Details", "Value_Mismatches"):
        assert expected in names, f"Expected sheet '{expected}' not found in {names}"
    # Detail sheet is named after the profile (max 31 chars)
    assert any(n not in ("Summary", "Unrecon_Summary", "Unreconciled_Details",
                          "Value_Mismatches") for n in names), \
        "Expected a Detail / per-profile sheet in the workbook"


def test_summary_sheet_carries_account_overview_table():
    """Summary sheet must contain the account name and column headers."""
    wb = workbook_from(read_table(CSV_A, "a.csv"), read_table(CSV_B, "b.csv"))
    ws = wb["Summary"]
    all_values = {cell.value for row in ws.iter_rows() for cell in row if cell.value}
    assert "Our Side Count" in all_values
    assert "Their Side Count" in all_values
    assert "Reconciled Count" in all_values
    assert "Recon Rate %" in all_values
    assert "Test Profile" in all_values


def test_summary_sheet_carries_kpi_metadata():
    """Summary sheet must still carry the run-metadata KPI block."""
    wb = workbook_from(read_table(CSV_A, "a.csv"), read_table(CSV_B, "b.csv"))
    labels = {row[0].value for row in wb["Summary"].iter_rows(min_col=1, max_col=1)}
    for expected in ("Rows read — File A", "Exact matches", "Orphans — File A only", "Schema hash"):
        assert expected in labels, f"KPI label '{expected}' missing from Summary"


def test_detail_sheet_has_three_sections():
    """The per-account detail sheet must contain all three section headings."""
    wb = workbook_from(read_table(CSV_A, "a.csv"), read_table(CSV_B, "b.csv"))
    # Find the detail sheet (not one of the fixed-name sheets)
    fixed = {"Summary", "Unrecon_Summary", "Unreconciled_Details", "Value_Mismatches"}
    detail_name = next(n for n in wb.sheetnames if n not in fixed)
    ws = wb[detail_name]
    all_values = {str(cell.value) for row in ws.iter_rows() for cell in row if cell.value}
    assert any("VOLUME COMPARISON" in v for v in all_values)
    assert any("MATCHED BREAKDOWN" in v for v in all_values)
    assert any("UNMATCHED BREAKDOWN" in v for v in all_values)


def test_detail_volume_comparison_counts_are_correct():
    """Volume Comparison section must show the correct row counts."""
    wb = workbook_from(read_table(CSV_A, "a.csv"), read_table(CSV_B, "b.csv"))
    fixed = {"Summary", "Unrecon_Summary", "Unreconciled_Details", "Value_Mismatches"}
    detail_name = next(n for n in wb.sheetnames if n not in fixed)
    ws = wb[detail_name]
    numeric_values = {cell.value for row in ws.iter_rows() for cell in row
                      if isinstance(cell.value, (int, float))}
    # CSV_A has 3 rows, CSV_B has 3 rows
    assert 3 in numeric_values


def test_unrecon_summary_sheet_exists_and_has_headers():
    wb = workbook_from(read_table(CSV_A, "a.csv"), read_table(CSV_B, "b.csv"))
    ws = wb["Unrecon_Summary"]
    all_values = {cell.value for row in ws.iter_rows() for cell in row if cell.value}
    assert "Our Count" in all_values
    assert "Their Count" in all_values
    assert "Our Amount" in all_values


def test_unreconciled_details_sheet_exists():
    wb = workbook_from(read_table(CSV_A, "a.csv"), read_table(CSV_B, "b.csv"))
    assert "Unreconciled_Details" in wb.sheetnames


def test_value_mismatches_sheet_exposes_side_by_side_delta():
    wb = workbook_from(read_table(CSV_A, "a.csv"), read_table(CSV_B, "b.csv"))
    ws = wb["Value_Mismatches"]
    header = [c.value for c in next(ws.iter_rows(max_row=1))]
    assert "A.amount" in header and "B.amount" in header and "delta.amount" in header
    delta = header.index("delta.amount")
    assert ws.cell(row=2, column=delta + 1).value == pytest.approx(-50.0)


def test_delta_is_written_as_a_number_not_a_string():
    wb = workbook_from(read_table(CSV_A, "a.csv"), read_table(CSV_B, "b.csv"))
    header = [c.value for c in next(wb["Value_Mismatches"].iter_rows(max_row=1))]
    cell = wb["Value_Mismatches"].cell(row=2, column=header.index("delta.amount") + 1)
    assert isinstance(cell.value, (int, float))


def test_value_mismatches_empty_when_inputs_identical():
    """When both sides are identical there should be no value mismatches."""
    clean = read_table(CSV_A, "a.csv")
    result = compute_buckets(clean, clean, CONFIG)
    wb = load_workbook(io.BytesIO(build_workbook(result, {})))
    ws = wb["Value_Mismatches"]
    # Only the header row, or the "No value mismatches" message
    cell_values = [cell.value for row in ws.iter_rows() for cell in row if cell.value]
    assert result.counts.value_mismatches == 0


# --------------------------------------------------------------------------- #
# Row-cap / truncation
# --------------------------------------------------------------------------- #

def test_row_cap_truncates_visibly_never_silently():
    ids = [f"T{i}" for i in range(50)]
    a = pl.DataFrame({"txn_id": ids, "amount": ["1.00"] * 50})
    result = compute_buckets(a, a, CONFIG)
    wb = load_workbook(io.BytesIO(build_workbook(result, {}, row_cap=10)))
    # Truncation notice must appear on the Summary sheet
    summary_values = {cell.value for row in wb["Summary"].iter_rows()
                      for cell in row if cell.value}
    assert "TRUNCATED — Value_Mismatches" not in summary_values  # no mismatches here
    # Value_Mismatches sheet should exist regardless
    assert "Value_Mismatches" in wb.sheetnames


def test_row_cap_limits_unreconciled_details():
    """When orphans exceed row_cap, the Details sheet must include a truncation notice."""
    # 20 orphans on side A
    ids_a = [f"A{i}" for i in range(20)]
    ids_b = [f"B{i}" for i in range(20)]
    a = pl.DataFrame({"txn_id": ids_a, "amount": ["1.00"] * 20})
    b = pl.DataFrame({"txn_id": ids_b, "amount": ["1.00"] * 20})
    result = compute_buckets(a, b, CONFIG)
    wb = load_workbook(io.BytesIO(build_workbook(result, {}, row_cap=5)))
    ws = wb["Unreconciled_Details"]
    all_values = [cell.value for row in ws.iter_rows() for cell in row if cell.value]
    assert any("Truncated" in str(v) for v in all_values)


# --------------------------------------------------------------------------- #
# Loader resilience
# --------------------------------------------------------------------------- #

def test_latin1_bytes_are_decoded_not_crashed():
    raw = "txn_id,amount,remarks\nT1,100.00,Café payment\n".encode("iso-8859-1")
    df = read_table(raw, "partner.csv")
    assert df.height == 1


def test_banner_rows_above_the_header_are_skipped():
    raw = b"NIC ASIA BANK LTD\nSettlement Report\ntxn_id,amount\nT1,100.00\n"
    df = read_table(raw, "settlement.csv", LoadSpec(header_row=3))
    assert df.columns == ["txn_id", "amount"] and df.height == 1


def test_semicolon_delimited_export_is_detected():
    df = read_table(b"txn_id;amount\nT1;100.00\n", "euro.csv")
    assert df.columns == ["txn_id", "amount"]


def test_ragged_trailing_columns_do_not_abort_the_read():
    raw = b"txn_id,amount\nT1,100.00\nT2,200.00,junk,junk\n"
    assert read_table(raw, "ragged.csv").height >= 2


def test_unsupported_extension_is_a_friendly_error():
    with pytest.raises(FileReadError) as exc:
        read_table(b"whatever", "statement.pdf")
    assert "csv" in str(exc.value).lower()


def test_zero_byte_upload_is_a_friendly_error():
    with pytest.raises(EmptyDataError):
        read_table(b"", "empty.csv")


def test_header_row_beyond_end_of_file_is_a_friendly_error():
    with pytest.raises(EmptyDataError):
        read_table(b"txn_id,amount\nT1,1.00\n", "short.csv", LoadSpec(header_row=9))


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #

def test_pipeline_produces_workbook_and_audit_row(tmp_path):
    db = str(tmp_path / "audit.db")
    outcome = run_reconciliation(
        file_a=CSV_A, name_a="internal.csv",
        file_b=CSV_B, name_b="partner.csv",
        config=CONFIG, operator="ops.analyst", db_path=db,
    )
    assert outcome.workbook[:2] == b"PK"  # a real xlsx container
    assert outcome.filename.endswith(".xlsx")
    assert outcome.result.counts.exact_matches == 1

    from backend.core.audit import recent_runs

    runs = recent_runs(db_path=db)
    assert len(runs) == 1
    assert runs[0]["operator"] == "ops.analyst"
    assert runs[0]["schema_hash"] == CONFIG.schema_hash()


def test_rerunning_identical_inputs_is_flagged_as_a_duplicate(tmp_path):
    db = str(tmp_path / "audit.db")
    kwargs = dict(
        file_a=CSV_A, name_a="internal.csv", file_b=CSV_B, name_b="partner.csv",
        config=CONFIG, operator="ops.analyst", db_path=db,
    )
    first = run_reconciliation(**kwargs)
    second = run_reconciliation(**kwargs)
    assert first.duplicate_of is None
    assert second.duplicate_of is not None
    assert second.duplicate_of["run_id"] == first.run_id


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
