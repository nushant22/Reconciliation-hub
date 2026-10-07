"""Synthetic input files for the Airlines reconciliation (tests + demo).

``build_sample_files(folder)`` writes the 8 files the Airlines page expects,
laid out exactly the way config.yaml describes them (header rows, sheet
names, two-row NPR/USD header, comma-joined cancellation tickets ...), with
a small, fully known scenario:

  Shree  S1 in eSewa            -> matched (eSewa)
         S2 in Fonepay          -> matched (Fonepay)
         S3 in eSewa, amt off   -> matched, Difference = 50
         S4 only REVERSED       -> Unrecon, Reverted? = Yes
         S5 nowhere             -> Unrecon, Reverted? = No
  Yeti   Y1 in eSewa            -> matched
         Y2 (listed twice)      -> de-duplicated, matched (Fonepay)
         Y3 nowhere             -> Unrecon
  Buddha B1 eSewa, B2 Fonepay   -> matched
         B3 nowhere             -> Unrecon
         refunds: B4 (eSewa Refund + Agent cashback), B5 (Fonepay Refund
         via RRN), B6 (nobody refunded it, 10 days old -> Overdue)
"""
from __future__ import annotations

import os
from datetime import date, timedelta

import openpyxl


def _wb(rows_by_sheet):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in rows_by_sheet.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    return wb


def build_sample_files(folder: str, today: date | None = None, late_refund: bool = False) -> dict[str, str]:
    """late_refund=True: B5's Fonepay refund arrives under RRN 'R77', and the sale
    that created R77 is NOT in today's Fonepay ledger (it was on an earlier day) -
    so the ticket can only be found in the saved RRN history."""
    today = today or date.today()
    os.makedirs(folder, exist_ok=True)
    paths: dict[str, str] = {}
    d = today.isoformat()
    old = (today - timedelta(days=10)).isoformat()

    # ---- eSewa (ledger_a): header on row 2 ------------------------------
    a_hdr = ["Txn_Date", "Parent_Txn_code", "child_txn", "Sector", "ticket_number",
             "Outbound_PNR_No", "Outbound_Ticket_Numbers", "Is One/Two Way?",
             "Outbound_Departure_Date", "Txn_Amount", "Child_Status", "Profile", "Total_Cashback"]

    def a_row(txn, ticket, amt, status="N/A", profile="Customer", cash=0):
        return [d, txn, "", "KTM-PKR", ticket, "PNR" + ticket[-3:], ticket, "One Way", d,
                amt, status, profile, cash]

    esewa = [["eSewa export"], a_hdr,
             a_row("T1", "1110000000001", 1000),   # S1
             a_row("T3", "1110000000003", 1050),   # S3 (airline says 1000)
             a_row("T4", "1110000000004", 900, status="FULL_REFUND"),   # S4 reversed
             a_row("T7", "2220000000001", 1200),   # Y1
             a_row("T8", "3330000000001", 2000),   # B1
             # B4: refunded by an Agent -> cashback 100 on the refund transaction
             a_row("P4", "3330000000004", 2000, status="full_refund", profile="Agent", cash=100)]
    paths["ledger_a"] = os.path.join(folder, "esewa_ledger.xlsx")
    _wb({"Sheet1": esewa}).save(paths["ledger_a"])

    # ---- Fonepay (ledger_b): header row 1 ---------------------------------
    b_hdr = ["Recorded Date", "Retrieval Reference No", "Booking Contact", "flightType", "Sector",
             "Flight Date", "Flight No", "PNR No", "Ticket No", "Passenger", "Total Fare"]

    def b_row(rrn, ticket, fare):
        return [d, rrn, "9800000000", "OW", "KTM-BIR", d, "U4-101", "PNRB", ticket, "Test Pax", fare]

    fonepay = [b_hdr,
               b_row("R2", "1110000000002", 1100),   # S2
               b_row("R6", "2220000000002", 1300),   # Y2
               b_row("R9", "3330000000002", 2500)]   # B2
    if not late_refund:
        fonepay.append(b_row("R5", "3330000000005", 1500))   # B5 (refunded via Fonepay)
    paths["ledger_b"] = os.path.join(folder, "fonepay_ledger.xlsx")
    _wb({"Sheet1": fonepay}).save(paths["ledger_b"])

    # ---- Shree (airline_1): header row 1 ------------------------------------
    s_hdr = ["UserName", "Issue Date", "Sector", "Flight Date", "Flight No", "Ticket", "PAX NAME",
             "$Fare", "FSC", "Net", "Comm"]

    def s_row(ticket, net, comm):
        return ["agent1", d, "KTM-PKR", d, "SH-1", ticket, "Pax", 10, 1, net, comm]

    shree = [s_hdr,
             s_row("1110000000001", 900, 100),    # S1 total 1000
             s_row("1110000000002", 1000, 100),   # S2 total 1100
             s_row("1110000000003", 900, 100),    # S3 total 1000 (eSewa 1050)
             s_row("1110000000004", 800, 100),    # S4 (reversed on eSewa)
             s_row("1110000000005", 700, 100)]    # S5 nowhere
    paths["airline_1"] = os.path.join(folder, "shree.xlsx")
    _wb({"Sheet1": shree}).save(paths["airline_1"])

    # ---- Yeti (airline_2): header row 2, has duplicate ticket ----------------
    y_hdr = ["Issue", "From", "To", "Flight", "Date", "Ticket", "Passenger", "Fare Code", "Net Total"]

    def y_row(ticket, net):
        return [d, "KTM", "BIR", "YT-2", d, ticket, "Pax", "Y", net]

    yeti = [["Yeti sales"], y_hdr,
            y_row("2220000000001", 1200),
            y_row("2220000000002", 1300),
            y_row("2220000000002", 1300),   # duplicate -> dedup
            y_row("2220000000003", 999)]    # Y3 nowhere
    paths["airline_2"] = os.path.join(folder, "yeti.xlsx")
    _wb({"Sheet1": yeti}).save(paths["airline_2"])

    # ---- Buddha (airline_3): 2-row header on rows 6-7; sheet names ------------
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Agent Sales Report"
    ws["A1"] = "Buddha Air - Agent Sales Report"
    simple = ["INVOICE NO", "ISSUE DATE", "SECTOR", "CLASS", "TICKET", "PAX NAME"]
    for i, h in enumerate(simple, start=1):
        ws.cell(row=6, column=i, value=h)
        ws.merge_cells(start_row=6, start_column=i, end_row=7, end_column=i)
    ws.cell(row=6, column=7, value="NPR")
    ws.merge_cells(start_row=6, start_column=7, end_row=6, end_column=9)
    ws.cell(row=6, column=10, value="USD")
    ws.merge_cells(start_row=6, start_column=10, end_row=6, end_column=12)
    for i, h in enumerate(["FARE", "FSC", "NET", "FARE", "FSC", "NET"], start=7):
        ws.cell(row=7, column=i, value=h)

    def buddha_row(inv, ticket, net):
        return [inv, d, "KTM-PKR", "Y", ticket, "Pax", net + 100, 0, net, 0, 0, 0]

    for r in [buddha_row("I1", "3330000000001", 2000),   # B1
              buddha_row("I2", "3330000000002", 2500),   # B2
              buddha_row("I3", "3330000000003", 777)]:   # B3 nowhere
        ws.append(r)

    rf = wb.create_sheet("Refunds")
    rf["A1"] = "Refunds"
    for i, h in enumerate(["TICKET NO", "PAX NAME", "ISSUE/RF", "REF"], start=1):
        rf.cell(row=2, column=i, value=h)
        rf.merge_cells(start_row=2, start_column=i, end_row=3, end_column=i)
    refunds = [["3330000000004", "Pax", f"{d}/{d}T10:00:00", 1900],   # B4 eSewa refund
               ["3330000000005", "Pax", f"{d}/{d}T10:00:00", 1450],   # B5 Fonepay refund
               ["3330000000006", "Pax", f"{old}/{old}T10:00:00", 500]]   # B6 old, unmatched
    for r_i, r in enumerate(refunds, start=4):   # data starts under the 2-row header
        for c_i, v in enumerate(r, start=1):
            rf.cell(row=r_i, column=c_i, value=v)
    paths["airline_3"] = os.path.join(folder, "buddha.xlsx")
    wb.save(paths["airline_3"])

    # ---- eSewa Refund / Cancellation report: header row 2 --------------------
    c_hdr = ["Txn_date", "Parent_txn_Code", "Services", "Status", "Ticket Number",
             "Child_Amount", "Parent_Amount"]
    canc = [["Cancellation report"], c_hdr,
            [f"{today.month}/{today.day}/{today.year} 7:08:12 AM", "P4", "Flight", "Success",
             "3330000000004,3330000000099", 1900, 1900]]   # 099 = phantom, not airline-confirmed
    paths["cancellation_report"] = os.path.join(folder, "esewa_refund.xlsx")
    _wb({"Sheet1": canc}).save(paths["cancellation_report"])

    # ---- Fonepay Refund: header row 1 -----------------------------------------
    fr = [["RETRIEVAL_REFERENCE_NUMBER", "REFUND_AMOUNT", "MERCHANT_PAYMENT_ADVICE_ID"],
          ["R77" if late_refund else "R5", 1450, "ADV1"]]
    paths["fonepay_refund"] = os.path.join(folder, "fonepay_refund.xlsx")
    _wb({"Sheet1": fr}).save(paths["fonepay_refund"])

    return paths


def build_history_file(path: str, rows: list[tuple[str, str, str]]) -> str:
    """A Fonepay transaction report (same layout as the daily one) holding
    (RRN, Ticket No, 'YYYY-MM-DD') rows - e.g. an old sale day for the history upload."""
    hdr = ["Recorded Date", "Retrieval Reference No", "Booking Contact", "flightType", "Sector",
           "Flight Date", "Flight No", "PNR No", "Ticket No", "Passenger", "Total Fare"]
    data = [hdr] + [[d, rrn, "9800000000", "OW", "KTM-BIR", d, "U4-101", "PNRB", t, "Test Pax", 1500]
                    for rrn, t, d in rows]
    _wb({"Sheet1": data}).save(path)
    return path


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "sample_airlines_data"
    for k, v in build_sample_files(out).items():
        print(f"{k:20s} {v}")
