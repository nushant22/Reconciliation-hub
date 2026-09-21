"""Bucketing engine tests.

The contract under test: five mutually-exclusive buckets, total conservation of
rows, and no silent orphan inflation from formatting noise.
"""

from __future__ import annotations

import polars as pl
import pytest

from backend.core.config import KeyPair, MatchConfig, ValuePair
from backend.core.errors import EmptyDataError, SchemaError
from backend.core.matcher import compute_buckets


def cfg(*, epsilon: float = 0.0, values=(ValuePair("amount", "amount"),), keys=None) -> MatchConfig:
    return MatchConfig(
        keys=tuple(keys or (KeyPair("txn_id", "txn_id"),)),
        values=tuple(values),
        epsilon=epsilon,
        profile_name="Test",
    )


def frame(ids: list, amounts: list, col_id: str = "txn_id", col_amt: str = "amount") -> pl.DataFrame:
    return pl.DataFrame({col_id: ids, col_amt: amounts})


# --------------------------------------------------------------------------- #
# Happy path & bucket exclusivity
# --------------------------------------------------------------------------- #
def test_clean_one_to_one_reconciliation():
    a = frame(["T1", "T2", "T3"], ["100.00", "200.00", "300.00"])
    b = frame(["T1", "T2", "T3"], ["100.00", "200.00", "300.00"])
    r = compute_buckets(a, b, cfg())
    assert (r.counts.exact_matches, r.counts.value_mismatches) == (3, 0)
    assert (r.counts.orphans_a, r.counts.orphans_b) == (0, 0)
    assert r.match_rate == 100.0


def test_every_bucket_populated_and_mutually_exclusive():
    a = frame(["T1", "T2", "T3"], ["100.00", "200.00", "300.00"])
    b = frame(["T1", "T2", "T4"], ["100.00", "250.00", "400.00"])
    r = compute_buckets(a, b, cfg())
    assert r.counts.exact_matches == 1  # T1
    assert r.counts.value_mismatches == 1  # T2
    assert r.counts.orphans_a == 1  # T3
    assert r.counts.orphans_b == 1  # T4
    r.counts.assert_conservation()


def test_row_conservation_holds_on_a_messy_mix():
    a = frame([f"T{i}" for i in range(50)], [f"{i}.00" for i in range(50)])
    b = frame([f"T{i}" for i in range(25, 90)], [f"{i}.50" for i in range(25, 90)])
    r = compute_buckets(a, b, cfg())
    assert r.counts.exact_matches + r.counts.value_mismatches + r.counts.orphans_a == 50
    assert r.counts.exact_matches + r.counts.value_mismatches + r.counts.orphans_b == 65


# --------------------------------------------------------------------------- #
# Formatting noise must not create fake breaks
# --------------------------------------------------------------------------- #
def test_whitespace_and_case_noise_still_matches():
    a = frame([" TXN-01 ", "txn-02"], ["100.00", "200.00"])
    b = frame(["txn-01", "TXN-02\t"], ["100.00", "200.00"])
    assert compute_buckets(a, b, cfg()).counts.exact_matches == 2


def test_currency_formatting_difference_is_not_a_break():
    a = frame(["T1", "T2"], ["NPR 1,500.00", "Rs. 99.5"])
    b = frame(["T1", "T2"], ["1500", "99.50"])
    assert compute_buckets(a, b, cfg()).counts.exact_matches == 2


def test_leading_zero_difference_is_not_an_orphan():
    # The Excel round-trip classic: one side lost its leading zeros.
    a = frame(["021203341484"], ["500.00"])
    b = frame(["21203341484"], ["500.00"])
    r = compute_buckets(a, b, cfg())
    assert (r.counts.exact_matches, r.counts.orphans_a, r.counts.orphans_b) == (1, 0, 0)


# --------------------------------------------------------------------------- #
# Deltas & tolerance
# --------------------------------------------------------------------------- #
def test_delta_direction_is_a_minus_b():
    a = frame(["T1"], ["1000.00"])
    b = frame(["T1"], ["999.25"])
    r = compute_buckets(a, b, cfg())
    assert r.value_mismatches["delta.amount"].to_list() == [0.75]


def test_negative_delta_reported_as_negative():
    r = compute_buckets(frame(["T1"], ["900.00"]), frame(["T1"], ["1000.00"]), cfg())
    assert r.value_mismatches["delta.amount"].to_list() == [-100.0]


def test_epsilon_absorbs_partner_rounding_drift():
    a = frame(["T1", "T2"], ["100.00", "100.00"])
    b = frame(["T1", "T2"], ["100.01", "100.05"])
    r = compute_buckets(a, b, cfg(epsilon=0.01))
    assert (r.counts.exact_matches, r.counts.value_mismatches) == (1, 1)


def test_zero_epsilon_is_the_default_and_is_strict():
    r = compute_buckets(frame(["T1"], ["100.00"]), frame(["T1"], ["100.01"]), cfg())
    assert r.counts.value_mismatches == 1


def test_float_noise_does_not_manufacture_a_break():
    # 0.1 + 0.2 style artefacts must be rounded away before comparison.
    a = frame(["T1"], ["0.30000000000000004"])
    b = frame(["T1"], ["0.30"])
    assert compute_buckets(a, b, cfg()).counts.exact_matches == 1


def test_negative_amounts_reconcile_and_delta_correctly():
    a = frame(["T1", "T2"], ["-500.00", "(250.00)"])
    b = frame(["T1", "T2"], ["-500.00", "-250.00"])
    assert compute_buckets(a, b, cfg()).counts.exact_matches == 2


def test_unparseable_amount_on_one_side_is_a_mismatch_not_a_match():
    r = compute_buckets(frame(["T1"], ["N/A"]), frame(["T1"], ["500.00"]), cfg())
    assert r.counts.value_mismatches == 1


def test_both_sides_unparseable_counts_as_matching_absence():
    r = compute_buckets(frame(["T1"], ["N/A"]), frame(["T1"], [""]), cfg())
    assert r.counts.exact_matches == 1


def test_break_reason_names_the_offending_attribute():
    a = pl.DataFrame({"txn_id": ["T1"], "amount": ["100.00"], "status": ["success"]})
    b = pl.DataFrame({"txn_id": ["T1"], "amount": ["100.00"], "status": ["failed"]})
    config = cfg(values=(ValuePair("amount", "amount"), ValuePair("status", "status", numeric=False)))
    r = compute_buckets(a, b, config)
    assert r.value_mismatches["break_reason"].to_list() == ["status"]


# --------------------------------------------------------------------------- #
# Duplicates, blanks, composite keys
# --------------------------------------------------------------------------- #
def test_duplicate_keys_match_occurrence_by_occurrence():
    a = frame(["T1", "T1", "T1"], ["100.00", "100.00", "100.00"])
    b = frame(["T1", "T1"], ["100.00", "100.00"])
    r = compute_buckets(a, b, cfg())
    # Two genuine pairs; only the surplus third A row is a real orphan.
    assert (r.counts.exact_matches, r.counts.orphans_a, r.counts.orphans_b) == (2, 1, 0)
    assert r.counts.duplicate_keys_a == 2


def test_blank_keys_never_match_each_other():
    a = frame(["", "T1"], ["10.00", "10.00"])
    b = frame(["", "T1"], ["10.00", "10.00"])
    r = compute_buckets(a, b, cfg())
    assert r.counts.exact_matches == 1
    assert (r.counts.orphans_a, r.counts.orphans_b) == (1, 1)
    assert any("blank key" in w for w in r.counts.warnings)


def test_composite_key_disambiguates_recycled_references():
    a = pl.DataFrame(
        {"ref": ["R1", "R1"], "txn_date": ["2026-05-13", "2026-05-14"], "amount": ["10.00", "20.00"]}
    )
    b = pl.DataFrame(
        {"ref": ["R1", "R1"], "txn_date": ["2026-05-14", "2026-05-13"], "amount": ["20.00", "10.00"]}
    )
    config = MatchConfig(
        keys=(KeyPair("ref", "ref"), KeyPair("txn_date", "txn_date", date_mode="date_only")),
        values=(ValuePair("amount", "amount"),),
    )
    r = compute_buckets(a, b, config)
    assert r.counts.exact_matches == 2


def test_utc_to_npt_skew_is_absorbed_by_the_date_toggle():
    a = pl.DataFrame({"ref": ["R1"], "txn_date": ["2026-05-13 18:30:00"], "amount": ["10.00"]})
    b = pl.DataFrame({"ref": ["R1"], "txn_date": ["2026-05-14"], "amount": ["10.00"]})
    keys = (KeyPair("ref", "ref"), KeyPair("txn_date", "txn_date", date_mode="shift_npt"))
    without = MatchConfig(keys=(KeyPair("ref", "ref"), KeyPair("txn_date", "txn_date", date_mode="date_only")))
    assert compute_buckets(a, b, without).counts.orphans_a == 1
    assert compute_buckets(a, b, MatchConfig(keys=keys)).counts.exact_matches == 1


def test_key_only_reconciliation_treats_key_match_as_exact():
    a = frame(["T1", "T2"], ["1.00", "2.00"])
    b = frame(["T1", "T3"], ["9.99", "2.00"])
    r = compute_buckets(a, b, MatchConfig(keys=(KeyPair("txn_id", "txn_id"),)))
    assert (r.counts.exact_matches, r.counts.orphans_a, r.counts.orphans_b) == (1, 1, 1)


def test_differently_named_columns_across_files():
    a = pl.DataFrame({"vendor trans id": ["T1"], "amount": ["50.00"]})
    b = pl.DataFrame({"unique_id": ["T1"], "txnamount": ["50.00"]})
    config = MatchConfig(
        keys=(KeyPair("vendor trans id", "unique_id"),),
        values=(ValuePair("amount", "txnamount"),),
    )
    assert compute_buckets(a, b, config).counts.exact_matches == 1


def test_column_lookup_is_case_insensitive():
    a = pl.DataFrame({"TXN_ID": ["T1"], "Amount": ["5.00"]})
    b = pl.DataFrame({"txn_id": ["T1"], "amount": ["5.00"]})
    assert compute_buckets(a, b, cfg()).counts.exact_matches == 1


# --------------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------------- #
def test_missing_mapped_column_raises_actionable_error():
    a = frame(["T1"], ["1.00"])
    b = pl.DataFrame({"other_id": ["T1"], "amount": ["1.00"]})
    with pytest.raises(SchemaError) as exc:
        compute_buckets(a, b, cfg())
    assert "other_id" in str(exc.value)


def test_empty_side_raises_empty_data_error():
    empty = pl.DataFrame({"txn_id": [], "amount": []}, schema={"txn_id": pl.Utf8, "amount": pl.Utf8})
    with pytest.raises(EmptyDataError):
        compute_buckets(empty, frame(["T1"], ["1.00"]), cfg())


def test_config_rejects_missing_keys():
    with pytest.raises(ValueError):
        MatchConfig(keys=())


def test_schema_hash_is_stable_and_mapping_sensitive():
    one = cfg().schema_hash()
    assert one == cfg().schema_hash()
    assert one != cfg(epsilon=0.05).schema_hash()


# --------------------------------------------------------------------------- #
# Throughput guard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("size", [200_000])
def test_large_batch_completes_within_budget(size: int):
    ids = [f"T{i}" for i in range(size)]
    a = frame(ids, ["100.00"] * size)
    b = frame(ids[: size - 1000] + [f"X{i}" for i in range(1000)], ["100.00"] * size)
    r = compute_buckets(a, b, cfg())
    assert r.counts.exact_matches == size - 1000
    assert r.duration_s < 10.0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
