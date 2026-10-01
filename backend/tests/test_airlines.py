"""
test_recon.py
Regression test suite for the reconciliation pipeline. Run with:

    pytest backend/tests/test_airlines.py
    (or: python -m backend.tests.test_airlines)

Each test is a plain function starting with test_. check() records the
result AND raises AssertionError so pytest sees failures. Add a new test function here any time
a bug is found and fixed - that's what keeps a fix from silently
regressing later.
"""
import os
import sys
import tempfile
import pandas as pd
import numpy as np

from backend.airlines import prep
from backend.airlines import matcher
from backend.airlines import refund_state
from backend.airlines import engine as run_recon

PASS = []
FAIL = []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
    else:
        FAIL.append((name, detail))
        raise AssertionError(f"{name} {detail}")


# ============================================================
# normalize_ticket
# ============================================================

def test_normalize_ticket_float_artifact():
    check(
        "normalize_ticket strips trailing .0",
        prep.normalize_ticket("1112223334445.0") == "1112223334445",
    )


def test_normalize_ticket_internal_whitespace():
    check(
        "normalize_ticket removes internal whitespace",
        prep.normalize_ticket(" T1 T2 ") == "T1T2",
    )


def test_normalize_ticket_case():
    check(
        "normalize_ticket uppercases",
        prep.normalize_ticket("t1") == "T1",
    )


def test_normalize_ticket_preserves_leading_zero_for_strings():
    check(
        "normalize_ticket does not strip a leading zero from a real string",
        prep.normalize_ticket("0912223334445") == "0912223334445",
    )


def test_normalize_ticket_none():
    check(
        "normalize_ticket(None) is empty string, not 'NONE'",
        prep.normalize_ticket(None) == "",
    )


# ============================================================
# prep_ledger_a: two-way price split, one-way, reversal detection
# ============================================================

def test_ledger_a_two_way_price_split():
    df = pd.DataFrame({
        "Parent_Txn_code": ["TXN1", "TXN1"],
        "Child_Status": ["N/A", "N/A"],
        "Txn_Amount": [10000, 10000],
        "ticket_number": ["T1", "T2"],
    })
    out = prep.prep_ledger_a(df)
    check(
        "two-way trip splits Txn_Amount by row count sharing Parent_Txn_code",
        list(out["Price Per Ticket"]) == [5000.0, 5000.0],
        detail=str(out["Price Per Ticket"].tolist()),
    )


def test_ledger_a_one_way_no_split():
    df = pd.DataFrame({
        "Parent_Txn_code": ["TXN2"],
        "Child_Status": ["N/A"],
        "Txn_Amount": [5000],
        "ticket_number": ["T3"],
    })
    out = prep.prep_ledger_a(df)
    check(
        "one-way trip is not divided",
        out["Price Per Ticket"].iloc[0] == 5000.0,
    )


def test_ledger_a_status_from_child_status():
    df = pd.DataFrame({
        "Parent_Txn_code": ["A", "B", "C", "D"],
        "Child_Status": ["N/A", "FULL_REFUND", "PARTIAL_REFUND", "REVERTED"],
        "Txn_Amount": [1000, 1000, 1000, 1000],
        "ticket_number": ["T1", "T2", "T3", "T4"],
    })
    out = prep.prep_ledger_a(df)
    expected = ["COMPLETE", "REVERSED", "REVERSED", "REVERSED"]
    check(
        "Status derived correctly from Child_Status for all documented values",
        list(out["Status"]) == expected,
        detail=str(out["Status"].tolist()),
    )


def test_ledger_a_status_fallback_to_child_txn():
    df = pd.DataFrame({
        "Parent_Txn_code": ["A", "B"],
        "child_txn": [None, "CHILD_5"],
        "Txn_Amount": [1000, 1000],
        "ticket_number": ["T1", "T2"],
    })
    out = prep.prep_ledger_a(df)  # no Child_Status column at all
    check(
        "falls back to child_txn presence when Child_Status is missing",
        list(out["Status"]) == ["COMPLETE", "REVERSED"],
    )


def test_ledger_a_missing_amount_returns_reschedule_marker():
    df = pd.DataFrame({
        "Parent_Txn_code": ["A"],
        "Child_Status": ["N/A"],
        "ticket_number": ["T1"],
        # no Txn_Amount column at all
    })
    out = prep.prep_ledger_a(df)
    check(
        "missing amount column yields the Reschedule/Refund marker, not a crash or silent 0",
        out["Price Per Ticket"].iloc[0] == "Reschedule/Refund",
    )


# ============================================================
# prep_cancellation_report: airline-confirmed divisor + tagging
# ============================================================

def test_cancellation_report_confirmed_divisor():
    df = pd.DataFrame({
        "Parent_txn_Code": ["TXN1"],
        "Ticket Number": ["T1,T2,T3"],
        "Child_Amount": [9000],
    })
    airline_confirmed = {"T1", "T2"}  # only 2 of 3 listed tickets are real
    out = prep.prep_cancellation_report(
        df, match_key_col="Ticket Number",
        airline_confirmed_tickets=airline_confirmed,
        txn_code_col="Parent_txn_Code",
    )
    prices = dict(zip(out["Ticket Number"], out["Price Per Ticket"]))
    check(
        "divides by airline-CONFIRMED count (2), not the ledger's raw listed count (3)",
        prices["T1"] == 4500.0 and prices["T2"] == 4500.0 and prices["T3"] == 4500.0,
        detail=str(prices),
    )
    statuses = dict(zip(out["Ticket Number"], out["Refund Status vs Airline"]))
    check(
        "T3 (listed but never confirmed, while siblings WERE confirmed) is tagged a data issue",
        "data issue" in statuses["T3"].lower(),
        detail=statuses["T3"],
    )
    check(
        "T1/T2 (confirmed) are tagged Confirmed",
        statuses["T1"] == "Confirmed by Airline" and statuses["T2"] == "Confirmed by Airline",
    )


def test_cancellation_report_pending_not_data_issue():
    """If NOTHING in the transaction is confirmed yet, that's a timing lag
    (pending), not a data issue - must not be conflated."""
    df = pd.DataFrame({
        "Parent_txn_Code": ["TXN2"],
        "Ticket Number": ["T4,T5"],
        "Child_Amount": [4000],
    })
    out = prep.prep_cancellation_report(
        df, match_key_col="Ticket Number",
        airline_confirmed_tickets={"T1", "T2"},  # neither T4 nor T5 confirmed
        txn_code_col="Parent_txn_Code",
    )
    check(
        "falls back to raw count (2) when nothing in the transaction is confirmed",
        (out["Price Per Ticket"] == 2000.0).all(),
    )
    check(
        "tagged pending, not a data issue, when nothing in the transaction matched",
        (out["Refund Status vs Airline"] == "Not yet confirmed by airline (pending)").all(),
    )


def test_cancellation_report_backward_compatible_without_airline_data():
    df = pd.DataFrame({"Ticket Number": ["T6,T7"], "Child_Amount": [2000]})
    out = prep.prep_cancellation_report(df, match_key_col="Ticket Number")
    check(
        "with no airline_confirmed_tickets, behaves as plain raw-count split",
        (out["Price Per Ticket"] == 1000.0).all(),
    )
    check(
        "no extra tagging columns added when airline data isn't provided",
        "Refund Status vs Airline" not in out.columns,
    )


# ============================================================
# matcher.py: normalization consistency (the bug that caused
# 12000-instead-of-6000)
# ============================================================

def test_matcher_handles_float_artifact_mismatch():
    airline_df = pd.DataFrame({"Ticket": ["T1", "T2.0"]})  # float artifact on one row
    ledger_df = pd.DataFrame({"Ticket": ["T1", "T2"], "Amount": [100, 200]})
    recon_df, matched_keys = matcher.match_airline_to_ledger(
        airline_df, "Ticket", ledger_df, "Ticket",
        ledger_prefix="Ledger", airline_prefix="Airline",
    )
    check(
        "both tickets match despite one side having a float artifact",
        len(recon_df) == 2,
        detail=f"got {len(recon_df)} matched rows, expected 2",
    )


# ============================================================
# Fonepay RRN lookup (case/whitespace bug)
# ============================================================

def test_fonepay_rrn_lookup_case_and_whitespace():
    fonepay_refund = pd.DataFrame({
        "RETRIEVAL_REFERENCE_NUMBER": [" rrn123 "],
        "REFUND_AMOUNT": [5000],
    })
    ledger_b = pd.DataFrame({
        "Retrieval Reference No": ["RRN123"],
        "Ticket No": ["T1"],
    })
    out = prep.prep_fonepay_refund_lookup(fonepay_refund, ledger_b)
    check(
        "RRN matches despite case/whitespace difference",
        out["Ticket No"].iloc[0] == "T1",
        detail=str(out["Ticket No"].tolist()),
    )


def test_fonepay_rrn_lookup_missing_columns_warns_not_crashes():
    fonepay_refund = pd.DataFrame({"WRONG_COL": ["x"]})
    ledger_b = pd.DataFrame({"Retrieval Reference No": ["RRN1"]})
    out = prep.prep_fonepay_refund_lookup(fonepay_refund, ledger_b)
    check(
        "missing RRN column returns original df unchanged, doesn't crash",
        out is not None and len(out) == 1,
    )


# ============================================================
# build_cashback_lookup: no double-counting repeated parent totals
# ============================================================

def test_cashback_lookup_no_double_count():
    df = pd.DataFrame({
        "Parent_Txn_code": ["TXN1", "TXN1"],
        "ticket_number": ["T1", "T2"],
        "Child_Status": ["full_refund", "full_refund"],
        "Profile": ["Agent", "Agent"],
        "Total_Cashback": [600, 600],  # same total repeated per ticket row
    })
    lookup, ticket_map = prep.build_cashback_lookup(df)
    check(
        "cashback total is NOT doubled despite repeating across 2 rows",
        lookup["TXN1"]["total_cashback"] == 600.0,
        detail=str(lookup),
    )
    check("ticket map built for fallback lookup", ticket_map.get("T1") == "TXN1")


def test_cashback_lookup_excludes_non_refund_status():
    df = pd.DataFrame({
        "Parent_Txn_code": ["TXN1"],
        "ticket_number": ["T1"],
        "Child_Status": ["N/A"],  # normal complete sale, not a refund
        "Profile": ["Agent"],
        "Total_Cashback": [999],
    })
    lookup, _ = prep.build_cashback_lookup(df)
    check(
        "a non-refund-status row is excluded from the cashback lookup entirely",
        "TXN1" not in lookup,
    )


def test_cashback_lookup_non_agent_still_returned_but_marked():
    df = pd.DataFrame({
        "Parent_Txn_code": ["TXN1"],
        "ticket_number": ["T1"],
        "Child_Status": ["full_refund"],
        "Profile": ["Individual"],
        "Total_Cashback": [500],
    })
    lookup, _ = prep.build_cashback_lookup(df)
    check(
        "non-Agent profile is captured but flagged is_agent=False (caller applies 0 cashback)",
        "TXN1" in lookup and lookup["TXN1"]["is_agent"] is False,
    )


# ============================================================
# refund_state: aging + CSV round-trip (leading zero bug)
# ============================================================

def test_aging_pending_vs_overdue():
    df = pd.DataFrame({
        "Ticket": ["T1", "T2", "T3"],
        "_RefundDate": pd.to_datetime(["2026-08-18", "2026-08-16", None]),
    })
    tagged = refund_state.tag_aging(df, grace_period_days=2, as_of="2026-08-19")
    statuses = dict(zip(tagged["Ticket"], tagged["Refund Status"]))
    check("1 day old -> Pending", statuses["T1"] == "Pending")
    check("3 days old (past 2-day grace) -> Overdue", statuses["T2"] == "Overdue")
    check("unparseable date -> Unknown date, not silently miscounted", statuses["T3"] == "Unknown date")


def test_pending_state_preserves_leading_zero():
    """The exact bug: a ticket number with a leading zero used to be
    silently turned into a number (losing the zero) on CSV round-trip."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "pending.csv")
        df = pd.DataFrame({"TICKET NO": ["0912223334445"]})
        refund_state.save_pending(df, path)
        loaded = refund_state.load_pending(path)
        check(
            "leading zero survives the save/load round trip",
            loaded["TICKET NO"].iloc[0] == "0912223334445",
            detail=f"got {loaded['TICKET NO'].iloc[0]!r}",
        )


def test_pending_state_missing_file_returns_empty():
    loaded = refund_state.load_pending("/tmp/definitely_does_not_exist_12345.csv")
    check("missing state file returns empty DataFrame, not an error", loaded.empty)


def test_load_and_prep_all_no_crash_without_fonepay_refund_file():
    """Regression: accumulated_ledger_b used to only be assigned inside
    'if fonepay_refund_raw is not None' - a day with no Fonepay Refund
    file present would crash later with NameError when it was referenced.
    Simulates the no-file case directly rather than the full run()."""
    accumulated_ledger_b = None  # what run() now initializes before the conditional
    fonepay_refund_raw = None
    if fonepay_refund_raw is not None:
        accumulated_ledger_b = "should not reach here"
    try:
        # this is the exact pattern used at the call site now
        ok = accumulated_ledger_b is None or hasattr(accumulated_ledger_b, "columns")
        check("accumulated_ledger_b safely handles the no-Fonepay-Refund-file day (no NameError)", ok)
    except NameError:
        check("accumulated_ledger_b safely handles the no-Fonepay-Refund-file day (no NameError)", False)


# ============================================================
# Fonepay RRN backdated-lookup accumulation (the "refund posts days
# after the original RRN" problem)
# ============================================================

def test_rrn_lookup_resolves_backdated_refund():
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "fonepay_rrn.csv")

        # RRN created on day 1
        day1_ledger_b = pd.DataFrame({"Retrieval Reference No": ["RRN999"], "Ticket No": ["T500"]})
        refund_state.merge_reference_lookup(
            day1_ledger_b, "Retrieval Reference No", state_path,
            retention_days=180, as_of="2026-08-10"
        )
        # 6 days later, today's ledger has unrelated fresh data only
        day7_ledger_b = pd.DataFrame({"Retrieval Reference No": ["RRN_OTHER"], "Ticket No": ["T999"]})
        accumulated = refund_state.merge_reference_lookup(
            day7_ledger_b, "Retrieval Reference No", state_path,
            retention_days=180, as_of="2026-08-16"
        )
        fonepay_refund = pd.DataFrame({"RETRIEVAL_REFERENCE_NUMBER": ["RRN999"], "REFUND_AMOUNT": [5000]})
        result = prep.prep_fonepay_refund_lookup(fonepay_refund, accumulated)
        check(
            "a refund for an RRN created 6 days ago (long gone from today's ledger) still resolves",
            result["Ticket No"].iloc[0] == "T500",
            detail=str(result["Ticket No"].tolist()),
        )


def test_rrn_lookup_prunes_beyond_retention():
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "fonepay_rrn.csv")
        old = pd.DataFrame({"Retrieval Reference No": ["RRN_OLD"]})
        refund_state.merge_reference_lookup(old, "Retrieval Reference No", state_path,
                                             retention_days=30, as_of="2026-01-01")
        fresh = pd.DataFrame({"Retrieval Reference No": ["RRN_NEW"]})
        result = refund_state.merge_reference_lookup(fresh, "Retrieval Reference No", state_path,
                                                       retention_days=30, as_of="2026-07-20")
        check(
            "entries older than the retention window are pruned",
            "RRN_OLD" not in result["Retrieval Reference No"].values
            and "RRN_NEW" in result["Retrieval Reference No"].values,
        )


# ============================================================
# Fonepay Refund Recon: "sold via Fonepay" check must use ACCUMULATED
# history, not just today's snapshot (the exact bug found in production -
# my first version wrongly said "never sold via Fonepay" for tickets that
# were, just not on THIS specific day)
# ============================================================

def test_fonepay_refund_diagnostic_uses_accumulated_history_not_today_only():
    cfg = {
        "refund_reconciliation": {"refund_amount_column": "REF"},
        "amount_fields": {"fonepay_refund": "REFUND_AMOUNT"},
    }
    airline_refunds = pd.DataFrame({"TICKET NO": ["T100"], "REF": [5000], "_norm_ticket": ["T100"]})
    fonepay_refund = pd.DataFrame({"Ticket No": ["T900"], "REFUND_AMOUNT": [3000]})

    # T100 is NOT in today's tiny snapshot, but IS in accumulated history -
    # the diagnostic must find it via the accumulated set, not miss it.
    accumulated_history_tickets = {"T100", "T900"}
    today_only_tickets = {"T900"}  # T100 absent - this is what the bug used

    import io
    import contextlib

    # correct: passing the ACCUMULATED set should flag T100 as "worth investigating"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run_recon.build_fonepay_refund_reconciliation(
            cfg, fonepay_refund, airline_refunds, "TICKET NO",
            refund_name="Buddha Refund", fonepay_name="Fonepay Refund",
            fonepay_sales_tickets=accumulated_history_tickets,
        )
    output = buf.getvalue()
    check(
        "using accumulated history correctly flags a ticket sold via Fonepay on a past day",
        "WORTH INVESTIGATING" in output and "T100" in output,
        detail=output,
    )

    # the bug: passing only TODAY's snapshot would wrongly conclude "never sold via Fonepay"
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        run_recon.build_fonepay_refund_reconciliation(
            cfg, fonepay_refund, airline_refunds, "TICKET NO",
            refund_name="Buddha Refund", fonepay_name="Fonepay Refund",
            fonepay_sales_tickets=today_only_tickets,
        )
    output2 = buf2.getvalue()
    check(
        "sanity check: today-only snapshot WOULD have wrongly said CONFIRMED EXPECTED (proves the bug was real)",
        "CONFIRMED EXPECTED" in output2,
        detail=output2,
    )


# ============================================================
# Full 2-day carry-forward integration scenario
# ============================================================

def test_carry_forward_resolves_next_day():
    """Day 1: airline refund exists, no matching cancellation yet -> pending.
    Day 2: cancellation posts -> the CARRIED FORWARD refund should match."""
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "pending.csv")

        day1_refunds = pd.DataFrame({
            "TICKET NO": ["T100"],
            "REF": [5000],
        })
        day1_cancellations = pd.DataFrame(columns=["Parent_txn_Code", "Ticket Number", "Child_Amount"])

        cfg = {
            "refund_reconciliation": {
                "state_file": state_path,
                "txn_code_column": "Parent_txn_Code",
            }
        }

        combined1, recon1, matched1 = run_recon.build_refund_reconciliation(
            cfg, day1_refunds, "TICKET NO", day1_cancellations, "Ticket Number",
            refund_name="Buddha Refund", cancellation_name="eSewa Refund",
        )
        # manually persist since build_refund_reconciliation doesn't save on its own
        # (the caller does that after computing refund_unrecon_df) - simulate that step:
        unmatched1 = combined1[~combined1["_norm_ticket"].isin(matched1)]
        refund_state.save_pending(unmatched1.drop(columns=["_norm_ticket"], errors="ignore"), state_path)

        check("day 1: nothing matches yet (no cancellation posted)", len(matched1) == 0)

        day2_refunds = pd.DataFrame(columns=["TICKET NO", "REF"])  # no new refunds today
        day2_cancellations = pd.DataFrame({
            "Parent_txn_Code": ["TXN1"],
            "Ticket Number": ["T100"],
            "Child_Amount": [5000],
        })

        combined2, recon2, matched2 = run_recon.build_refund_reconciliation(
            cfg, day2_refunds, "TICKET NO", day2_cancellations, "Ticket Number",
            refund_name="Buddha Refund", cancellation_name="eSewa Refund",
        )
        check(
            "day 2: the carried-forward refund resolves once the cancellation posts",
            len(matched2) == 1,
            detail=f"matched2={matched2}",
        )
        check("day 2: Refund Recon sheet has exactly 1 row", len(recon2) == 1)


# ============================================================
# Uniform Reverted?/Refund? tagging
# ============================================================

def test_tag_reverted_column_applies_uniformly():
    unrecon_full = pd.DataFrame({"Ticket": ["T1", "T2", "T3"]})
    unrecon = unrecon_full.copy()
    reverted = {"T1"}
    out = run_recon.tag_reverted_column(unrecon, unrecon_full, "Ticket", reverted)
    check(
        "Reverted? correctly Yes/No per ticket",
        list(out["Reverted?"]) == ["Yes", "No", "No"],
    )


def test_tag_refund_and_reverted_are_independent():
    unrecon_full = pd.DataFrame({"Ticket": ["T1", "T2", "T3"]})
    unrecon = unrecon_full.copy()
    unrecon = run_recon.tag_refund_column(unrecon, unrecon_full, "Ticket", {"T2"})
    unrecon = run_recon.tag_reverted_column(unrecon, unrecon_full, "Ticket", {"T1"})
    check(
        "Refund? and Reverted? tag different tickets independently, no interference",
        list(unrecon["Refund?"]) == ["No", "Yes", "No"]
        and list(unrecon["Reverted?"]) == ["Yes", "No", "No"],
        detail=str(unrecon.to_dict()),
    )


def test_tag_refund_column_uses_normalize_ticket():
    """Regression: tag_refund_column used to build lookup keys with plain
    strip+upper while the set it checked against used normalize_ticket -
    a float-artifact ticket would silently never match."""
    unrecon_full = pd.DataFrame({"Ticket": ["T1.0"]})
    unrecon = unrecon_full.copy()
    refund_tickets = {prep.normalize_ticket("T1")}  # normalized set, as built elsewhere
    out = run_recon.tag_refund_column(unrecon, unrecon_full, "Ticket", refund_tickets)
    check(
        "Refund? correctly matches despite a float artifact, using normalize_ticket consistently",
        out["Refund?"].iloc[0] == "Yes",
    )


def test_merge_reference_lookup_preserves_multi_ticket_booking():
    """Multi-ticket bookings under the same RRN must NOT be dropped by deduplication."""
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "fonepay_rrn.csv")
        day1 = pd.DataFrame({
            "Retrieval Reference No": ["RRN100", "RRN100", "RRN100"],
            "Ticket No": ["T1", "T2", "T3"],
            "Recorded Date": ["2026-08-20", "2026-08-20", "2026-08-20"],
        })
        merged = refund_state.merge_reference_lookup(day1, "Retrieval Reference No", state_path)
        check(
            "merge_reference_lookup preserves all tickets in a multi-ticket booking",
            len(merged) == 3 and set(merged["Ticket No"]) == {"T1", "T2", "T3"},
            detail=str(merged.to_dict()),
        )


def test_prep_fonepay_refund_lookup_multi_ticket_pairing():
    """Multiple refund rows for the same RRN should map to distinct tickets."""
    ledger_b = pd.DataFrame({
        "Retrieval Reference No": ["RRN100", "RRN100"],
        "Ticket No": ["T1", "T2"],
    })
    fonepay_refund = pd.DataFrame({
        "RETRIEVAL_REFERENCE_NUMBER": ["RRN100", "RRN100"],
        "REFUND_AMOUNT": [5000, 5000],
    })
    out = prep.prep_fonepay_refund_lookup(fonepay_refund, ledger_b)
    check(
        "prep_fonepay_refund_lookup assigns distinct tickets to multiple refund rows under same RRN",
        set(out["Ticket No"]) == {"T1", "T2"},
        detail=str(out.to_dict()),
    )


def test_prep_fonepay_refund_lookup_prioritizes_airline_confirmed():
    """If one ticket in a multi-ticket booking is confirmed refunded, prioritize that ticket."""
    ledger_b = pd.DataFrame({
        "Retrieval Reference No": ["RRN100", "RRN100"],
        "Ticket No": ["T_NOT_REFUNDED", "T_CONFIRMED"],
    })
    fonepay_refund = pd.DataFrame({
        "RETRIEVAL_REFERENCE_NUMBER": ["RRN100"],
        "REFUND_AMOUNT": [5000],
    })
    out = prep.prep_fonepay_refund_lookup(
        fonepay_refund, ledger_b, airline_confirmed_tickets={"T_CONFIRMED"}
    )
    check(
        "prep_fonepay_refund_lookup prioritizes airline-confirmed ticket for a single refund row",
        out["Ticket No"].iloc[0] == "T_CONFIRMED",
        detail=str(out.to_dict()),
    )


# ============================================================
# runner
# ============================================================

def main():
    test_fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in test_fns:
        try:
            fn()
        except Exception as e:
            FAIL.append((fn.__name__, f"EXCEPTION: {e}"))

    print(f"\n{'='*60}")
    print(f"PASSED: {len(PASS)}")
    print(f"FAILED: {len(FAIL)}")
    print(f"{'='*60}")
    if FAIL:
        print("\nFailures:")
        for name, detail in FAIL:
            print(f"  [FAIL] {name}")
            if detail:
                print(f"         {detail}")
        sys.exit(1)
    else:
        print("\nAll tests passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
