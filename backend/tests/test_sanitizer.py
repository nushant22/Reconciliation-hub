"""Sanitizer edge cases — every one of these is a break we have actually seen
in a Nepali settlement file."""

from __future__ import annotations

import polars as pl
import pytest

from backend.core.sanitizer import (
    build_key_expr,
    clean_dataframe,
    clean_text_expr,
    normalize_date_expr,
    parse_amount_expr,
    strip_leading_zeros_expr,
)


def amounts(values: list[str | None]) -> list[float | None]:
    return pl.DataFrame({"amount": values}).select(parse_amount_expr("amount"))["amount"].to_list()


def keys(values: list[str | None], **kwargs) -> list[str]:
    return pl.DataFrame({"k": values}).select(build_key_expr("k", **kwargs))["k"].to_list()


# --------------------------------------------------------------------------- #
# Money
# --------------------------------------------------------------------------- #
def test_equivalent_amount_spellings_collapse():
    assert amounts(["500", "500.0", "500.00", " 500.000 "]) == [500.0, 500.0, 500.0, 500.0]


def test_currency_tokens_and_separators_stripped():
    assert amounts(["NPR 1,250.50", "Rs. 1,250.50", "Rs 1250.5", "₨1,250.50", "1,250.50"]) == [
        1250.5
    ] * 5


def test_negative_notations():
    assert amounts(["-99.99", "(99.99)", "99.99-", "NPR (99.99)"]) == [-99.99] * 4


def test_large_amount_with_multiple_separators():
    assert amounts(["1,234,567.89"]) == [1234567.89]


def test_rounding_is_half_even_to_two_places():
    # 64-bit float noise must not survive into a comparison.
    assert amounts(["100.005", "0.1", "99.999"]) == [100.0, 0.1, 100.0]


def test_junk_becomes_null_not_zero():
    # A zero is a financial claim; a null is a data defect. Never conflate them.
    assert amounts(["N/A", "", "pending", None, "--"]) == [None] * 5


def test_zero_is_preserved():
    assert amounts(["0", "0.00", "NPR 0.00"]) == [0.0, 0.0, 0.0]


def test_whitespace_and_invisible_characters():
    assert amounts(["\u00a0500.00\u200b", "\t500.00\n"]) == [500.0, 500.0]


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #
def test_key_whitespace_and_case_folding():
    assert keys(["  TXN-001 ", "txn-001", "Txn-001\t"]) == ["txn-001"] * 3


def test_internal_whitespace_collapsed():
    assert keys(["TXN  001"]) == ["txn 001"]


def test_leading_zeros_stripped_only_for_numeric_keys():
    assert keys(["021203341484", "21203341484"]) == ["21203341484", "21203341484"]
    # Alphanumeric references must never be touched.
    assert keys(["0A1203341484"]) == ["0a1203341484"]


def test_all_zero_key_degrades_to_single_zero():
    assert keys(["000", "0"]) == ["0", "0"]


def test_leading_zero_stripping_can_be_disabled():
    assert keys(["007"], strip_zeros=False) == ["007"]


def test_nulls_become_empty_keys():
    assert keys([None, ""]) == ["", ""]


def test_strip_leading_zeros_expr_direct():
    out = pl.DataFrame({"k": ["0012", "12", "abc", "0x12"]}).select(
        strip_leading_zeros_expr(pl.col("k"))
    )["k"].to_list()
    assert out == ["12", "12", "abc", "0x12"]


def test_clean_text_expr_preserves_case_when_asked():
    out = pl.DataFrame({"k": [" Ram Bahadur "]}).select(clean_text_expr("k", lowercase=False))[
        "k"
    ].to_list()
    assert out == ["Ram Bahadur"]


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
def dates(values: list[str], mode: str) -> list[str]:
    return pl.DataFrame({"d": values}).select(normalize_date_expr("d", mode))["d"].to_list()


def test_time_of_day_is_dropped():
    assert dates(["2026-05-14 14:32:07", "2026-05-14 00:00:00", "2026-05-14"], "date_only") == [
        "2026-05-14"
    ] * 3


def test_mixed_ad_formats_normalise():
    assert dates(["14/05/2026", "14-05-2026", "2026/05/14", "14-May-2026"], "date_only") == [
        "2026-05-14"
    ] * 4


def test_npt_shift_moves_late_utc_rows_to_the_next_day():
    # 18:30 UTC on the 13th is 00:15 NPT on the 14th — the classic off-by-one-day break.
    assert dates(["2026-05-13 18:30:00"], "shift_npt") == ["2026-05-14"]
    assert dates(["2026-05-13 10:00:00"], "shift_npt") == ["2026-05-13"]


def test_unparseable_date_falls_back_to_cleaned_original():
    assert dates(["not a date"], "date_only") == ["not a date"]


def test_bikram_sambat_strings_are_normalised_not_parsed():
    assert dates(["2081/5/2", "2081-05-02", "2081.05.02"], "bs_text") == ["2081-05-02"] * 3


def test_date_mode_off_is_passthrough():
    assert dates([" 2026-05-14 14:00 "], "off") == ["2026-05-14 14:00"]


# --------------------------------------------------------------------------- #
# Frame hygiene
# --------------------------------------------------------------------------- #
def test_clean_dataframe_normalises_headers_and_drops_placeholders():
    df = pl.DataFrame({" Txn ID ": ["1"], "Unnamed: 3": ["x"], "": ["y"], "AMOUNT": ["5"]})
    out = clean_dataframe(df)
    assert out.columns == ["txn id", "amount"]


def test_clean_dataframe_drops_fully_empty_rows():
    df = pl.DataFrame({"a": ["1", "", " "], "b": ["x", "", None]})
    assert clean_dataframe(df).height == 1


def test_clean_dataframe_deduplicates_repeated_headers():
    df = pl.DataFrame({"amount": ["1"], "Amount": ["2"]})
    assert clean_dataframe(df).columns == ["amount", "amount_1"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
