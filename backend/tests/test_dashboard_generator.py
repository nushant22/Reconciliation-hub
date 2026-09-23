"""Tests for the dashboard generator module."""

import io
from pathlib import Path

import pytest
import xlsxwriter

from backend.core.dashboard_generator import (
    AccountSnapshot,
    DashboardData,
    generate_dashboard,
    parse_reconciliation_file,
)


def create_mock_excel_summary(account_name: str, our_count: int, their_count: int,
                               matched: int, recon_rate: float) -> bytes:
    """Create a minimal mock Excel file for testing."""
    output = io.BytesIO()
    wb = xlsxwriter.Workbook(output, {"constant_memory": False})
    
    # Summary sheet
    ws_summary = wb.add_worksheet("Summary")
    
    # Banner row
    ws_summary.write(0, 0, "ACCOUNT-WISE RECONCILIATION OVERVIEW")
    
    # Headers
    ws_summary.write(1, 0, "Account")
    ws_summary.write(1, 1, "Our Side Count")
    ws_summary.write(1, 2, "Their Side Count")
    ws_summary.write(1, 3, "Reconciled Count")
    ws_summary.write(1, 4, "Recon Rate %")
    
    # Data row
    ws_summary.write(2, 0, account_name)
    ws_summary.write_number(2, 1, our_count)
    ws_summary.write_number(2, 2, their_count)
    ws_summary.write_number(2, 3, matched)
    ws_summary.write_number(2, 4, recon_rate)
    
    # TOTAL row
    ws_summary.write(3, 0, "TOTAL")
    ws_summary.write_number(3, 1, our_count)
    ws_summary.write_number(3, 2, their_count)
    ws_summary.write_number(3, 3, matched)
    ws_summary.write_number(3, 4, recon_rate)
    
    # Metadata
    ws_summary.write(5, 0, "Generated at")
    ws_summary.write(5, 1, "2026-09-22T10:30:00")
    
    # Detail sheet (minimal)
    ws_detail = wb.add_worksheet("Detail")
    ws_detail.write(0, 0, "ConnectNPay vs " + account_name)
    
    wb.close()
    output.seek(0)
    return output.getvalue()


class TestDashboardParsing:
    """Test parsing of reconciliation files."""
    
    def test_parse_excel_summary_basic(self):
        """Test basic Excel summary parsing."""
        excel_bytes = create_mock_excel_summary(
            account_name="TestAccount",
            our_count=1000,
            their_count=995,
            matched=990,
            recon_rate=99.0
        )
        
        snapshot = parse_reconciliation_file(excel_bytes, "TestAccount")
        
        assert snapshot.account_name == "TestAccount"
        assert snapshot.our_count == 1000
        assert snapshot.their_count == 995
        assert snapshot.matched == 990
        assert snapshot.recon_rate == 99.0
        assert snapshot.cnp_only == 10  # our_count - matched
        assert snapshot.cpt_only == 5   # their_count - matched
    
    def test_parse_excel_with_override_name(self):
        """Test account name override."""
        excel_bytes = create_mock_excel_summary(
            account_name="FileAccount",
            our_count=100,
            their_count=100,
            matched=100,
            recon_rate=100.0
        )
        
        snapshot = parse_reconciliation_file(excel_bytes, "OverrideName")
        
        assert snapshot.account_name == "OverrideName"
    
    def test_excel_file_detection_from_bytes(self):
        """Test that Excel files are correctly detected from bytes."""
        excel_bytes = create_mock_excel_summary(
            account_name="Test",
            our_count=100,
            their_count=100,
            matched=100,
            recon_rate=100.0
        )
        
        # Excel files start with PK (ZIP signature) but should be detected as Excel
        assert excel_bytes[:4] == b'PK\x03\x04'
        
        # Should parse as Excel, not CSV archive
        snapshot = parse_reconciliation_file(excel_bytes, "Test")
        assert snapshot.account_name == "Test"
    
    def test_account_snapshot_calculations(self):
        """Test AccountSnapshot derived fields."""
        snapshot = AccountSnapshot(
            account_name="Test",
            run_date="2026-09-22",
            our_count=1000,
            their_count=980,
            matched=970,
            cnp_only=30,
            cpt_only=10,
            recon_rate=97.0,
            cnp_unmatched_amt=15000.0,
            cpt_unmatched_amt=5000.0,
            amount_diff=10000.0
        )
        
        assert snapshot.cnp_only == 30
        assert snapshot.cpt_only == 10
        assert snapshot.amount_diff == 10000.0


class TestDashboardGeneration:
    """Test dashboard workbook generation."""
    
    def test_generate_dashboard_single_account(self):
        """Test dashboard generation with one account."""
        excel_bytes = create_mock_excel_summary(
            account_name="SingleAccount",
            our_count=500,
            their_count=500,
            matched=495,
            recon_rate=99.0
        )
        
        dashboard = generate_dashboard(
            snapshot_files=[(excel_bytes, "SingleAccount")],
            snapshot_date="2026-09-22"
        )
        
        assert isinstance(dashboard, bytes)
        assert len(dashboard) > 1000  # Should be a valid Excel file
        
        # Verify it starts with Excel magic bytes
        assert dashboard[:4] == b'PK\x03\x04'  # ZIP signature (xlsx is ZIP)
    
    def test_generate_dashboard_multiple_accounts(self):
        """Test dashboard generation with multiple accounts."""
        accounts = [
            ("Account1", 1000, 995, 990, 99.0),
            ("Account2", 500, 498, 495, 99.0),
            ("Account3", 2000, 2000, 1980, 99.0),
        ]
        
        snapshot_files = []
        for name, our, their, matched, rate in accounts:
            excel_bytes = create_mock_excel_summary(name, our, their, matched, rate)
            snapshot_files.append((excel_bytes, name))
        
        dashboard = generate_dashboard(
            snapshot_files=snapshot_files,
            snapshot_date="2026-09-22"
        )
        
        assert isinstance(dashboard, bytes)
        assert len(dashboard) > 2000  # Should be larger with multiple accounts
    
    def test_dashboard_data_aggregation(self):
        """Test DashboardData aggregation logic."""
        accounts = [
            AccountSnapshot(
                account_name="Account1",
                run_date="2026-09-22",
                our_count=1000,
                their_count=995,
                matched=990,
                cnp_only=10,
                cpt_only=5,
                recon_rate=99.0,
                cnp_unmatched_amt=5000.0,
                cpt_unmatched_amt=2500.0,
                amount_diff=2500.0
            ),
            AccountSnapshot(
                account_name="Account2",
                run_date="2026-09-22",
                our_count=500,
                their_count=498,
                matched=495,
                cnp_only=5,
                cpt_only=3,
                recon_rate=99.0,
                cnp_unmatched_amt=2500.0,
                cpt_unmatched_amt=1500.0,
                amount_diff=1000.0
            ),
        ]
        
        data = DashboardData(
            snapshot_date="2026-09-22",
            accounts=accounts
        )
        
        # Test aggregate calculations
        total_our = sum(a.our_count for a in data.accounts)
        total_matched = sum(a.matched for a in data.accounts)
        overall_rate = 100.0 * total_matched / total_our
        
        assert total_our == 1500
        assert total_matched == 1485
        assert abs(overall_rate - 99.0) < 0.1


class TestErrorHandling:
    """Test error handling and edge cases."""
    
    def test_empty_snapshot_list(self):
        """Test dashboard generation with no files."""
        with pytest.raises(ValueError, match="No valid reconciliation files"):
            generate_dashboard(
                snapshot_files=[],
                snapshot_date="2026-09-22"
            )
    
    def test_invalid_file_format(self):
        """Test parsing invalid file format."""
        invalid_bytes = b"This is not an Excel file"
        
        with pytest.raises(Exception):
            parse_reconciliation_file(invalid_bytes, "TestAccount")
    
    def test_excel_missing_summary_sheet(self):
        """Test Excel file without Summary sheet."""
        # Create Excel with wrong sheet name
        output = io.BytesIO()
        wb = xlsxwriter.Workbook(output, {"constant_memory": False})
        ws = wb.add_worksheet("WrongSheetName")
        ws.write(0, 0, "Data")
        wb.close()
        output.seek(0)
        
        excel_bytes = output.getvalue()
        
        with pytest.raises(ValueError, match="Summary.*not found"):
            parse_reconciliation_file(excel_bytes, "Test")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
