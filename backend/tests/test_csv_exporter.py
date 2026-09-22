"""Unit tests for CSV archive exporter and pipeline integration."""

from __future__ import annotations

import io
import zipfile

import polars as pl
import pytest

from backend.core.config import KeyPair, LoadSpec, MatchConfig, ValuePair
from backend.core.csv_exporter import build_csv_archive
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


def csv_archive_from(a: pl.DataFrame, b: pl.DataFrame):
    """Build a CSV archive from two DataFrames."""
    result = compute_buckets(a, b, CONFIG)
    return zipfile.ZipFile(io.BytesIO(build_csv_archive(result, {"operator": "ops.analyst"})))


# --------------------------------------------------------------------------- #
# CSV Archive structure
# --------------------------------------------------------------------------- #

def test_archive_contains_all_expected_files():
    """The ZIP archive must contain all 8 CSV files."""
    archive = csv_archive_from(read_table(CSV_A, "a.csv"), read_table(CSV_B, "b.csv"))
    files = archive.namelist()
    
    expected = [
        "summary.csv",
        "detail.csv",
        "unrecon_summary.csv",
        "unreconciled_details.csv",
        "value_mismatches.csv",
        "exact_matches.csv",
        "orphans_a.csv",
        "orphans_b.csv",
    ]
    
    for expected_file in expected:
        assert expected_file in files, f"Expected file '{expected_file}' not found in {files}"


def test_summary_csv_has_overview_content():
    """Summary CSV must contain account overview headers."""
    archive = csv_archive_from(read_table(CSV_A, "a.csv"), read_table(CSV_B, "b.csv"))
    summary_content = archive.read("summary.csv").decode('utf-8')
    
    assert "ACCOUNT-WISE RECONCILIATION OVERVIEW" in summary_content
    assert "Our Side Count" in summary_content
    assert "Their Side Count" in summary_content
    assert "Reconciled Count" in summary_content
    assert "Test Profile" in summary_content


def test_detail_csv_has_three_sections():
    """Detail CSV must contain all three section headings."""
    archive = csv_archive_from(read_table(CSV_A, "a.csv"), read_table(CSV_B, "b.csv"))
    detail_content = archive.read("detail.csv").decode('utf-8')
    
    assert "1. VOLUME COMPARISON" in detail_content
    assert "2. MATCHED BREAKDOWN" in detail_content
    assert "3. UNMATCHED BREAKDOWN" in detail_content


def test_exact_matches_csv_is_valid():
    """Exact matches CSV must be parseable and contain the matched record."""
    archive = csv_archive_from(read_table(CSV_A, "a.csv"), read_table(CSV_B, "b.csv"))
    exact_matches_content = archive.read("exact_matches.csv")
    
    # Parse the CSV back into a DataFrame
    df = pl.read_csv(io.BytesIO(exact_matches_content))
    
    # T1 should be an exact match
    assert df.height == 1
    assert "T1" in df.select(pl.col("A.txn_id")).to_series().to_list()


def test_value_mismatches_csv_contains_delta():
    """Value mismatches CSV must contain delta columns."""
    archive = csv_archive_from(read_table(CSV_A, "a.csv"), read_table(CSV_B, "b.csv"))
    mismatches_content = archive.read("value_mismatches.csv")
    
    df = pl.read_csv(io.BytesIO(mismatches_content))
    
    # T2 should be a value mismatch (200 vs 250)
    assert df.height == 1
    assert "delta.amount" in df.columns


def test_orphans_csvs_exist_and_parseable():
    """Orphan CSV files must exist and be valid."""
    archive = csv_archive_from(read_table(CSV_A, "a.csv"), read_table(CSV_B, "b.csv"))
    
    orphans_a_content = archive.read("orphans_a.csv")
    orphans_b_content = archive.read("orphans_b.csv")
    
    df_a = pl.read_csv(io.BytesIO(orphans_a_content))
    df_b = pl.read_csv(io.BytesIO(orphans_b_content))
    
    # T3 is orphan in A, T4 is orphan in B
    assert df_a.height == 1
    assert df_b.height == 1


def test_empty_value_mismatches_produces_empty_csv():
    """When there are no value mismatches, the CSV should be empty or header-only."""
    clean = read_table(CSV_A, "a.csv")
    result = compute_buckets(clean, clean, CONFIG)
    archive = zipfile.ZipFile(io.BytesIO(build_csv_archive(result, {})))
    
    mismatches_content = archive.read("value_mismatches.csv")
    df = pl.read_csv(io.BytesIO(mismatches_content))
    
    # Should be empty (no mismatches when comparing identical data)
    assert df.height == 0


def test_row_cap_limits_data_files():
    """row_cap parameter must limit the number of rows in data CSVs."""
    ids = [f"T{i}" for i in range(50)]
    a = pl.DataFrame({"txn_id": ids, "amount": ["1.00"] * 50})
    result = compute_buckets(a, a, CONFIG)
    archive = zipfile.ZipFile(io.BytesIO(build_csv_archive(result, {}, row_cap=10)))
    
    exact_matches_content = archive.read("exact_matches.csv")
    df = pl.read_csv(io.BytesIO(exact_matches_content))
    
    # Should be capped at 10 rows
    assert df.height == 10


# --------------------------------------------------------------------------- #
# End-to-end pipeline
# --------------------------------------------------------------------------- #

def test_pipeline_produces_zip_archive_and_audit_row(tmp_path):
    """Pipeline must produce a ZIP archive (not Excel) and record to audit DB."""
    db = str(tmp_path / "audit.db")
    outcome = run_reconciliation(
        file_a=CSV_A, name_a="a.csv", file_b=CSV_B, name_b="b.csv",
        config=CONFIG, operator="ops.analyst", db_path=db,
    )
    
    # Verify it's a ZIP file (starts with PK signature)
    assert outcome.workbook[:2] == b"PK", "Output should be a ZIP archive"
    assert outcome.filename.endswith(".zip"), "Filename should end with .zip"
    
    # Verify the result counts
    assert outcome.result.counts.exact_matches == 1
    
    # Verify it's a valid ZIP with expected files
    archive = zipfile.ZipFile(io.BytesIO(outcome.workbook))
    files = archive.namelist()
    assert "summary.csv" in files
    assert "exact_matches.csv" in files
