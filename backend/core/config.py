"""Run configuration objects.

Flat dataclasses only — these are serialised into the audit log and hashed to
produce the schema fingerprint, so they must stay plain and deterministic.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Literal

DateMode = Literal["off", "date_only", "shift_npt", "bs_text"]
"""How a flagged date column is normalised before it participates in a key.

off        -- leave the raw (whitespace-trimmed) string alone.
date_only  -- parse AD datetime, drop the time-of-day component entirely.
shift_npt  -- parse AD datetime, add +05:45 (UTC -> Nepal Time), then drop time.
bs_text    -- Bikram Sambat string: do not attempt AD parsing, just normalise
              separators so `2081/05/12` and `2081-5-12` collapse to one form.
"""


@dataclass(frozen=True)
class KeyPair:
    """One component of the (possibly composite) matching key."""

    col_a: str
    col_b: str
    date_mode: DateMode = "off"
    #: Numeric keys often lose their leading zeros when a file passes through
    #: Excel ("021203341484" -> "21203341484"). Stripping them on both sides
    #: prevents a silent, artificial orphan.
    strip_leading_zeros: bool = True


@dataclass(frozen=True)
class ValuePair:
    """One attribute compared once the key has matched."""

    col_a: str
    col_b: str
    #: True  -> compare as money (Float64, rounded to 2dp, delta computed)
    #: False -> compare as normalised text (no delta)
    numeric: bool = True


@dataclass(frozen=True)
class MatchConfig:
    keys: tuple[KeyPair, ...]
    values: tuple[ValuePair, ...] = ()
    #: Absolute tolerance for numeric comparison. Default 0.00 => byte-exact
    #: after 2dp rounding. Set to e.g. 0.01 to absorb partner rounding drift.
    epsilon: float = 0.00
    lowercase_keys: bool = True
    strip_currency_from_keys: bool = True
    profile_name: str = "Custom"

    def __post_init__(self) -> None:
        if not self.keys:
            raise ValueError("At least one key pair is required to reconcile.")
        if self.epsilon < 0:
            raise ValueError("epsilon must be >= 0.")

    @property
    def key_cols_a(self) -> list[str]:
        return [k.col_a for k in self.keys]

    @property
    def key_cols_b(self) -> list[str]:
        return [k.col_b for k in self.keys]

    def schema_hash(self) -> str:
        """Stable fingerprint of the mapping — two runs with the same hash
        applied to the same inputs must produce the same buckets."""
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class LoadSpec:
    """How to read one side of the reconciliation off disk / out of a buffer."""

    #: 1-based row number that holds the header. Partner statements routinely
    #: carry 1-3 banner rows above the real header (see `header_row` in the
    #: legacy accounts.yaml).
    header_row: int = 1
    sheet: str | int = 0
    encoding: str | None = None  # None => autodetect with fallback chain


@dataclass
class BucketCounts:
    rows_read_a: int = 0
    rows_read_b: int = 0
    exact_matches: int = 0
    value_mismatches: int = 0
    orphans_a: int = 0
    orphans_b: int = 0
    duplicate_keys_a: int = 0
    duplicate_keys_b: int = 0
    warnings: list[str] = field(default_factory=list)

    def assert_conservation(self) -> None:
        """Every input row must land in exactly one bucket. If this trips, the
        engine has silently dropped or duplicated a row and the workbook cannot
        be trusted for audit."""
        left = self.exact_matches + self.value_mismatches + self.orphans_a
        right = self.exact_matches + self.value_mismatches + self.orphans_b
        if left != self.rows_read_a or right != self.rows_read_b:
            raise AssertionError(
                "Bucket conservation failed: "
                f"A {left}/{self.rows_read_a}, B {right}/{self.rows_read_b}"
            )

    def as_dict(self) -> dict[str, int | list[str]]:
        return asdict(self)
