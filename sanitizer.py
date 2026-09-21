"""Defensive pre-processing.

Everything in here is a pure function returning a Polars expression or frame —
no I/O, no globals — so each rule is unit-testable in isolation (see
`tests/test_sanitizer.py`).

The single most important invariant: **sanitisation is symmetric**. Whatever is
applied to File A's key must be applied byte-for-byte to File B's key, or the
match rate silently collapses.
"""

from __future__ import annotations

import polars as pl

from .config import DateMode

#: Nepal Standard Time is UTC+05:45. Partner gateways frequently stamp rows in
#: UTC while the internal ledger stamps NPT, which shifts ~22% of a day's rows
#: across the date boundary if left uncorrected.
NPT_OFFSET_MINUTES = 5 * 60 + 45

#: Matched case-insensitively and anchored on word boundaries so a reference
#: like "NPRTXN99" is never mangled. Rust's regex engine (Polars) has no
#: look-around, so boundaries are expressed with \b and literal symbols.
CURRENCY_TOKEN_PATTERN = r"(?i)(\bnpr\b|\bnrs\b|\brs\.?|\binr\b|₨|रू)"

#: Zero-width space, non-breaking space, BOM, tabs — all routinely present in
#: CSVs exported from legacy banking systems.
INVISIBLE_PATTERN = r"[\u200b\u200c\u200e\u200f\ufeff\u00a0\t\r\n]"

_AD_DATETIME_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S%.f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%.f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y",
    "%d-%b-%Y %H:%M:%S",
    "%d-%b-%Y",
    "%d %b %Y",
)


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #
def clean_text_expr(col: str, *, lowercase: bool = True) -> pl.Expr:
    """Trim, de-invisible, collapse internal runs of whitespace, optionally fold case.

    Nulls become empty strings so downstream string ops never produce nulls —
    blank keys are handled explicitly by the matcher, not by null propagation.
    """
    expr = (
        pl.col(col)
        .cast(pl.Utf8, strict=False)
        .fill_null("")
        .str.replace_all(INVISIBLE_PATTERN, " ")
        .str.replace_all(r"\s+", " ")
        .str.strip_chars()
    )
    return expr.str.to_lowercase() if lowercase else expr


def strip_currency_expr(expr: pl.Expr) -> pl.Expr:
    """Remove `NPR` / `Rs.` / `₨` tokens and thousands separators."""
    return (
        expr.str.replace_all(CURRENCY_TOKEN_PATTERN, " ")
        .str.replace_all(r",", "")
        .str.replace_all(r"\s+", " ")
        .str.strip_chars()
    )


def strip_leading_zeros_expr(expr: pl.Expr) -> pl.Expr:
    """Drop leading zeros from *purely numeric* values only.

    A reference that round-tripped through a numeric Excel cell loses its
    leading zeros ("021203341484" -> "21203341484"). Alphanumeric references are
    left untouched so unrelated formats can never be collapsed together, and an
    all-zero value degrades to a single "0" rather than to an empty key.
    """
    numeric = expr.str.contains(r"^0*\d+$")
    # No look-ahead in Rust regex: capture the first surviving digit instead, so
    # "000" collapses to "0" rather than to an empty key.
    stripped = expr.str.replace(r"^0+(\d)", "${1}")
    return pl.when(numeric).then(stripped).otherwise(expr)


# --------------------------------------------------------------------------- #
# Money
# --------------------------------------------------------------------------- #
def parse_amount_expr(col: str) -> pl.Expr:
    """Parse a money column into Float64 rounded to 2 decimal places.

    Handles: currency tokens, thousands separators, accounting negatives
    `(500.00)`, trailing-minus `500.00-`, stray spaces, and unparseable junk
    (-> null, never 0.0 — a null is a data problem, a zero is a claim).

    `500` and `500.00` and `NPR 500.0 ` all collapse to 500.0 exactly.
    """
    cleaned = (
        pl.col(col)
        .cast(pl.Utf8, strict=False)
        .fill_null("")
        .str.replace_all(INVISIBLE_PATTERN, " ")
        .str.strip_chars()
    )
    cleaned = strip_currency_expr(cleaned)
    cleaned = (
        cleaned.str.replace_all(r",", "")
        .str.replace_all(r"\s", "")
        # accounting-style negative: (1234.50) -> -1234.50
        .str.replace_all(r"^\((.*)\)$", "-${1}")
        # trailing minus: 1234.50- -> -1234.50
        .str.replace_all(r"^(.*)-$", "-${1}")
        .str.replace_all(r"^\+", "")
    )
    cleaned = pl.when(cleaned.str.contains(r"^-?\d*\.?\d+$")).then(cleaned).otherwise(None)
    return cleaned.cast(pl.Float64, strict=False).round(2)


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
def normalize_date_expr(col: str, mode: DateMode = "date_only") -> pl.Expr:
    """Normalise a date column to a canonical `YYYY-MM-DD` string.

    A string (not a Datetime) is returned deliberately: date columns are only
    ever used as *key material* here, and a string key joins identically across
    engines and survives the Excel round-trip without timezone re-interpretation.

    Values that match no known AD format fall back to their cleaned original, so
    a mixed-format column degrades gracefully instead of nulling out the key.
    """
    base = clean_text_expr(col, lowercase=False)

    if mode == "off":
        return base

    if mode == "bs_text":
        # Bikram Sambat: no AD parsing is possible. Unify separators and
        # zero-pad components so 2081/5/2 == 2081-05-02.
        unified = base.str.replace_all(r"[/.]", "-").str.replace_all(r"\s.*$", "")
        return (
            unified.str.split("-")
            .list.eval(pl.element().str.strip_chars().str.zfill(2))
            .list.join("-")
        )

    parsed = pl.coalesce(
        [base.str.strptime(pl.Datetime, fmt, strict=False) for fmt in _AD_DATETIME_FORMATS]
    )
    if mode == "shift_npt":
        parsed = parsed.dt.offset_by(f"{NPT_OFFSET_MINUTES}m")

    # date_only and shift_npt both discard time-of-day: an internal ledger
    # stamped 14:32:07 and a settlement file stamped 00:00:00 are the same day.
    return pl.coalesce([parsed.dt.strftime("%Y-%m-%d"), base])


# --------------------------------------------------------------------------- #
# Frame-level helpers
# --------------------------------------------------------------------------- #
def clean_dataframe(df: pl.DataFrame) -> pl.DataFrame:
    """Normalise column *names* and drop structurally empty columns/rows.

    Column names are trimmed, de-invisibled and lower-cased; `Unnamed: 4` style
    placeholder columns produced by ragged Excel exports are dropped; fully
    empty rows (common as trailing padding) are removed.
    """
    import re

    renamed: dict[str, str] = {}
    seen: dict[str, int] = {}
    for name in df.columns:
        clean = re.sub(INVISIBLE_PATTERN, " ", str(name))
        clean = re.sub(r"\s+", " ", clean).strip().lower()
        if not clean or clean.startswith("unnamed"):
            clean = ""
        if clean:
            if clean in seen:
                seen[clean] += 1
                clean = f"{clean}_{seen[clean]}"
            else:
                seen[clean] = 0
        renamed[name] = clean

    keep = [old for old, new in renamed.items() if new]
    df = df.select(keep).rename({old: renamed[old] for old in keep})

    if df.height == 0 or df.width == 0:
        return df

    as_text = [
        pl.col(c).cast(pl.Utf8, strict=False).fill_null("").str.strip_chars() for c in df.columns
    ]
    non_empty = pl.any_horizontal([e != "" for e in as_text])
    return df.filter(non_empty)


def build_key_expr(
    col: str,
    *,
    lowercase: bool = True,
    strip_currency: bool = True,
    strip_zeros: bool = True,
    date_mode: DateMode = "off",
) -> pl.Expr:
    """Compose the full key-normalisation chain for one key component."""
    if date_mode != "off":
        expr = normalize_date_expr(col, date_mode)
        if lowercase:
            expr = expr.str.to_lowercase()
        return expr

    expr = clean_text_expr(col, lowercase=lowercase)
    if strip_currency:
        expr = strip_currency_expr(expr)
    if strip_zeros:
        expr = strip_leading_zeros_expr(expr)
    return expr
