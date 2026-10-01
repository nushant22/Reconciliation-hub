"""
report.py
Builds the final formatted Excel workbook: 6 Recon sheets, 3 Unrecon
sheets, 1 Summary sheet.
"""
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
HEADER_FONT = Font(color="FFFFFF", bold=True)
RECON_FILL = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
UNRECON_FILL = PatternFill(start_color="FCE4E4", end_color="FCE4E4", fill_type="solid")

# Refund-related summary columns get their own header/fill so they read as
# a distinct block next to the ledger-vs-airline summary columns.
REFUND_HEADER_FILL = PatternFill(start_color="7F6000", end_color="7F6000", fill_type="solid")
REFUND_DATA_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")


def _is_refund_col(col_name):
    name = str(col_name).strip().lower()
    return (name.startswith("refund") or name.startswith("cashback")
            or ("esewa 2 profile" in name) or name.startswith("reverted"))


def _is_amount_col(col_name):
    name = str(col_name).strip().lower()
    return any(k in name for k in ("amount", "fare", "net", "comm", "price", "ref", "total", "cashback", "difference", "vat", "fsc", "psc"))


def _write_df_to_sheet(ws, df, row_fill=None):
    ws.append(list(df.columns))
    for c in range(1, len(df.columns) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = HEADER_FONT
        cell.fill = REFUND_HEADER_FILL if _is_refund_col(df.columns[c - 1]) else HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")

    diff_col_indices = [c for c, col in enumerate(df.columns, 1) if "diff" in str(col).lower()]

    for _, row in df.iterrows():
        ws.append(list(row))
        curr_row = ws.max_row
        for c in range(1, len(df.columns) + 1):
            cell = ws.cell(row=curr_row, column=c)
            val = cell.value
            col_name = df.columns[c - 1]

            # Formatting numbers
            if isinstance(val, (int, float)) and not pd.isna(val):
                if _is_amount_col(col_name) and not any(x in str(col_name).lower() for x in ("count", "sn", "s.no", "pnr", "ticket", "code", "reference", "advice")):
                    cell.number_format = "#,##0.00"
                    cell.alignment = Alignment(horizontal="right")
                elif "count" in str(col_name).lower() or "outstanding" in str(col_name).lower():
                    cell.number_format = "#,##0"
                    cell.alignment = Alignment(horizontal="right")

            # Fill colors
            if _is_refund_col(col_name):
                cell.fill = REFUND_DATA_FILL
            elif row_fill:
                cell.fill = row_fill

            # Highlight non-zero differences
            if c in diff_col_indices and isinstance(val, (int, float)) and abs(val) > 0.01:
                cell.font = Font(color="9C0006", bold=True)
                cell.fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")

    for c in range(1, len(df.columns) + 1):
        col_letter = get_column_letter(c)
        max_len = max(
            [len(str(df.columns[c - 1]))] +
            [len(str(v)) for v in df.iloc[:, c - 1].astype(str).tolist()[:200]]
        )
        ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 45)

    ws.freeze_panes = "A2"


def build_report(recon_sheets, unrecon_sheets, summary_df, output_path):
    wb = Workbook()
    wb.remove(wb.active)

    if isinstance(summary_df, dict):
        for sheet_name, df in summary_df.items():
            ws_summary = wb.create_sheet(sheet_name[:31])
            _write_df_to_sheet(ws_summary, df)
    else:
        ws_summary = wb.create_sheet("Summary")
        _write_df_to_sheet(ws_summary, summary_df)

    for name, df in recon_sheets.items():
        ws = wb.create_sheet(name[:31])
        if df.empty:
            ws.append(["No matched rows"])
        else:
            _write_df_to_sheet(ws, df, row_fill=RECON_FILL)

    for name, df in unrecon_sheets.items():
        ws = wb.create_sheet(name[:31])
        if df.empty:
            ws.append(["No unmatched rows"])
        else:
            _write_df_to_sheet(ws, df, row_fill=UNRECON_FILL)

    wb.save(output_path)
