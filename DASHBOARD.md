# Multi-Account Reconciliation Dashboard

## Overview

The Dashboard feature aggregates reconciliation results from multiple accounts into a consolidated Excel workbook showing overall performance, per-account metrics, and detailed breakdowns.

## Quick Start

### 1. Via Web UI

```bash
streamlit run app.py
# Navigate to "Dashboard" page in sidebar
# Upload reconciliation files (.xlsx or .zip)
# Click "Generate Dashboard"
# Download the result
```

### 2. Programmatically

```python
from backend.core.dashboard_generator import generate_dashboard

snapshot_files = [
    ("recon_fonepay_20260922.xlsx", "Fonepay"),
    ("recon_ncell_20260922.xlsx", "Ncell"),
    # ... more accounts
]

dashboard = generate_dashboard(
    snapshot_files=snapshot_files,
    snapshot_date="2026-09-22"
)

with open("dashboard_output.xlsx", "wb") as f:
    f.write(dashboard)
```

## Input Files

**Supported Formats:**
- **Excel workbooks** (.xlsx, .xls) — from "Excel (Legacy)" output format
- **CSV archives** (.zip) — from "CSV (Lightweight)" output format

**Filename Convention:** `recon_<account>_<date>.<ext>`
- Example: `recon_fonepay_20260922.xlsx` → Account name: "Fonepay"

## Dashboard Output

The generated Excel workbook contains:

### Sheet 1: Overview
- Snapshot date and key metrics
- Total accounts processed
- Overall reconciliation rate
- Total transaction volume
- Summary counts (matched, CNP-only, CPT-only)
- Accounts flagged for review

### Sheet 2: Recon_Rate_By_Account
Per-account performance table with:
- Our Count, Their Count, Matched
- CNP-only, CPT-only breakdowns
- Recon Rate %
- Flag (OK if ≥99.9%, Review if <99.9%)

**Sorted by:** Recon rate (lowest first) — problem accounts at top

### Sheet 3: Account_Matrix
Detailed breakdown with all metrics from Sheet 2 plus:
- CNP Unmatched Amount
- CPT Unmatched Amount
- Amount Difference
- TOTAL row with aggregates

## Key Features

✅ Auto-detects account names from filenames  
✅ Supports both Excel and CSV formats  
✅ Color-coded status flags (OK/Review)  
✅ Professional Excel formatting  
✅ Aggregate calculations across accounts  
✅ Clear error messages  

## Common Use Cases

### Daily Operations
```bash
# Generate dashboard after overnight reconciliations
streamlit run app.py → Dashboard → Upload files → Generate
```

### Automated Reports
```python
from pathlib import Path
from backend.core.dashboard_generator import generate_dashboard

# Find all reconciliation files
recon_dir = Path("./reconciliations/20260922")
files = [(str(f), f.stem.split("_")[1].title()) 
         for f in recon_dir.glob("recon_*.xlsx")]

# Generate
dashboard = generate_dashboard(files, "2026-09-22")
Path("dashboard_20260922.xlsx").write_bytes(dashboard)
```

### Batch Processing
```python
from backend.core.dashboard_generator import parse_reconciliation_file

# Parse and filter
accounts = [parse_reconciliation_file(f) for f in file_list]
flagged = [a for a in accounts if a.recon_rate < 99.9]

print(f"Accounts needing review: {len(flagged)}")
for account in flagged:
    print(f"  {account.account_name}: {account.recon_rate:.2f}%")
```

## Troubleshooting

### Parse Errors

**"Sheet 'Summary' not found"**
→ File is not a valid reconciliation output. Re-run reconciliation.

**"No valid reconciliation files could be parsed"**
→ Check file formats. Must be .xlsx or .zip from reconciliation engine.

### Performance

For 20+ accounts or large files:
- Use Excel format (faster than CSV parsing)
- Batch into multiple dashboards
- Run offline via Python script

## Testing

```bash
# Run test suite
pytest backend/tests/test_dashboard_generator.py -v

# Test with your files via UI
streamlit run app.py
```

## API Reference

### `generate_dashboard()`

```python
def generate_dashboard(
    snapshot_files: list[tuple[str | bytes, str]],
    snapshot_date: str | None = None,
    historical_files: list[tuple[str, list[tuple[str | bytes, str]]]] = None,
) -> bytes
```

**Parameters:**
- `snapshot_files`: List of (file_path_or_bytes, account_name) tuples
- `snapshot_date`: Date in YYYY-MM-DD format (defaults to today)
- `historical_files`: Optional, for future trend analysis

**Returns:** Excel workbook as bytes

### `parse_reconciliation_file()`

```python
def parse_reconciliation_file(
    file_path: str | bytes,
    account_name: str = None
) -> AccountSnapshot
```

**Parameters:**
- `file_path`: Path to file or raw bytes
- `account_name`: Optional override (auto-detected from filename)

**Returns:** AccountSnapshot with metrics

## Future Enhancements

Planned features:
- Multi-day trend analysis
- Status breakdown (CNP vs CPT by status)
- Unmatched transaction details aggregation
- Embedded charts and visualizations

---

**Version:** 1.0  
**Status:** Production-ready  
**Dependencies:** Already in requirements.txt (polars, openpyxl, xlsxwriter, streamlit)
