"""Matching & mutually-exclusive bucketing engine.

Design notes
------------
1. **Occurrence-aware keys.** A key that legitimately repeats (two genuine
   transactions sharing a reference) is matched occurrence-by-occurrence: the
   1st A row pairs with the 1st B row, the 2nd with the 2nd. Matching only the
   first occurrence — the naive approach — inflates the orphan queue with
   breaks that are not breaks. Surplus occurrences on one side correctly fall
   out as orphans, which *is* a real quantity break.

2. **Blank keys never match.** An empty reference on both sides is not evidence
   of the same transaction. Blank-key rows are given a per-row sentinel so they
   deterministically land in the orphan bucket and get counted as a warning.

3. **Conservation is asserted, not assumed.** Every A row lands in exactly one
   of {exact, mismatch, orphan_a}; every B row in exactly one of
   {exact, mismatch, orphan_b}. `BucketCounts.assert_conservation()` fails the
   run rather than shipping a workbook that quietly lost rows.
"""

from __future__ import annotations

import logging
import time

import polars as pl

from .config import BucketCounts, MatchConfig, ValuePair
from .errors import EmptyDataError, SchemaError
from .sanitizer import build_key_expr, parse_amount_expr, clean_text_expr

log = logging.getLogger(__name__)

_KEY = "__recon_key"
_OCC = "__recon_occ"
_ROW = "__recon_row"
_EXACT = "__recon_exact"
_SEP = "\u241f"  # symbol-for-unit-separator: cannot occur in real data


class ReconResult:
    """The five buckets plus the metadata needed to audit the run."""

    def __init__(
        self,
        counts: BucketCounts,
        exact_matches: pl.DataFrame,
        value_mismatches: pl.DataFrame,
        orphans_a: pl.DataFrame,
        orphans_b: pl.DataFrame,
        config: MatchConfig,
        duration_s: float,
    ) -> None:
        self.counts = counts
        self.exact_matches = exact_matches
        self.value_mismatches = value_mismatches
        self.orphans_a = orphans_a
        self.orphans_b = orphans_b
        self.config = config
        self.duration_s = duration_s

    @property
    def match_rate(self) -> float:
        denom = max(self.counts.rows_read_a, 1)
        return round(100.0 * self.counts.exact_matches / denom, 2)

    def summary_rows(self) -> list[tuple[str, object]]:
        c = self.counts
        a = max(c.rows_read_a, 1)
        b = max(c.rows_read_b, 1)
        return [
            ("Profile", self.config.profile_name),
            ("Schema hash", self.config.schema_hash()),
            ("Match key", " + ".join(self.config.key_cols_a)),
            ("Compared attributes", " + ".join(v.col_a for v in self.config.values) or "key only"),
            ("Tolerance (epsilon)", f"{self.config.epsilon:.2f}"),
            ("Rows read — File A", c.rows_read_a),
            ("Rows read — File B", c.rows_read_b),
            ("Exact matches", c.exact_matches),
            ("Exact match % (of A)", f"{100.0 * c.exact_matches / a:.2f}%"),
            ("Value mismatches", c.value_mismatches),
            ("Value mismatch % (of A)", f"{100.0 * c.value_mismatches / a:.2f}%"),
            ("Orphans — File A only", c.orphans_a),
            ("Orphan % (of A)", f"{100.0 * c.orphans_a / a:.2f}%"),
            ("Orphans — File B only", c.orphans_b),
            ("Orphan % (of B)", f"{100.0 * c.orphans_b / b:.2f}%"),
            ("Duplicate key occurrences — A", c.duplicate_keys_a),
            ("Duplicate key occurrences — B", c.duplicate_keys_b),
            ("Run duration (s)", f"{self.duration_s:.3f}"),
        ]


# --------------------------------------------------------------------------- #
# Column resolution
# --------------------------------------------------------------------------- #
def resolve_column(df: pl.DataFrame, target: str, side: str) -> str:
    """Case/whitespace-insensitive column lookup with an actionable error."""
    wanted = str(target).strip().lower()
    for col in df.columns:
        if str(col).strip().lower() == wanted:
            return col
    raise SchemaError(
        f"Column '{target}' was not found in File {side}.",
        hint="Available columns: " + ", ".join(map(str, df.columns[:40])),
    )


# --------------------------------------------------------------------------- #
# Side preparation
# --------------------------------------------------------------------------- #
def _prepare_side(
    df: pl.DataFrame, config: MatchConfig, side: str
) -> tuple[pl.DataFrame, int, int]:
    """Attach key / occurrence / parsed-value helper columns to one side.

    Returns (frame, blank_key_rows, duplicate_key_occurrences).
    """
    if df.height == 0:
        raise EmptyDataError(
            f"File {side} contains no data rows after parsing.",
            hint="Check the header row setting — banner rows above the real header shift it.",
        )

    key_cols = config.key_cols_a if side == "A" else config.key_cols_b
    resolved_keys = [resolve_column(df, c, side) for c in key_cols]

    parts: list[pl.Expr] = []
    for pair, col in zip(config.keys, resolved_keys):
        parts.append(
            build_key_expr(
                col,
                lowercase=config.lowercase_keys,
                strip_currency=config.strip_currency_from_keys,
                strip_zeros=pair.strip_leading_zeros,
                date_mode=pair.date_mode,
            ).alias(f"__kp_{len(parts)}")
        )

    out = df.with_row_index(_ROW).with_columns(parts)
    part_names = [f"__kp_{i}" for i in range(len(parts))]

    out = out.with_columns(pl.concat_str(part_names, separator=_SEP).alias(_KEY))

    # Blank composite key (every component empty) -> per-row sentinel so the
    # row is structurally incapable of matching and surfaces as an orphan.
    blank = pl.all_horizontal([pl.col(p) == "" for p in part_names])
    blank_rows = int(out.select(blank.sum()).item() or 0)
    out = out.with_columns(
        pl.when(blank)
        .then(pl.format("__blank__{}__{}", pl.lit(side), pl.col(_ROW)))
        .otherwise(pl.col(_KEY))
        .alias(_KEY)
    )

    # Occurrence index within each key, ordered by original row position.
    out = out.with_columns(pl.int_range(pl.len()).over(_KEY).alias(_OCC))
    dupes = int(out.select((pl.col(_OCC) > 0).sum()).item() or 0)

    # Parsed comparison values.
    value_exprs: list[pl.Expr] = []
    for i, vp in enumerate(config.values):
        raw = resolve_column(df, vp.col_a if side == "A" else vp.col_b, side)
        if vp.numeric:
            value_exprs.append(parse_amount_expr(raw).alias(f"__v{i}_{side}"))
        else:
            value_exprs.append(clean_text_expr(raw, lowercase=True).alias(f"__v{i}_{side}"))
    if value_exprs:
        out = out.with_columns(value_exprs)

    out = out.drop(part_names)

    # Namespace the business columns so an inner join can never collide and the
    # workbook reader always knows which file a column came from.
    business = [c for c in out.columns if not c.startswith("__recon") and not c.startswith("__v")]
    out = out.rename({c: f"{side}.{c}" for c in business})
    return out, blank_rows, dupes


def _comparison_exprs(values: tuple[ValuePair, ...], epsilon: float) -> tuple[list[pl.Expr], list[str]]:
    """Per-attribute match flags (+ deltas for numeric attributes)."""
    exprs: list[pl.Expr] = []
    flag_names: list[str] = []
    for i, vp in enumerate(values):
        a, b = pl.col(f"__v{i}_A"), pl.col(f"__v{i}_B")
        if vp.numeric:
            both_null = a.is_null() & b.is_null()
            within = ((a - b).abs().round(2) <= epsilon).fill_null(False)
            exprs.append((a - b).round(2).alias(f"delta.{vp.col_a}"))
        else:
            both_null = a.is_null() & b.is_null()
            within = (a == b).fill_null(False)
        exprs.append((both_null | within).alias(f"__flag_{i}"))
        flag_names.append(f"__flag_{i}")
    return exprs, flag_names


def _ordered_output(df: pl.DataFrame, config: MatchConfig, *, with_delta: bool) -> pl.DataFrame:
    """Key columns first, then the side-by-side A/B/Δ triplets, then the rest."""
    lead: list[str] = []
    for pair in config.keys:
        for cand in (f"A.{pair.col_a.lower()}", f"B.{pair.col_b.lower()}"):
            if cand in df.columns and cand not in lead:
                lead.append(cand)
    if with_delta:
        for vp in config.values:
            for cand in (f"A.{vp.col_a.lower()}", f"B.{vp.col_b.lower()}", f"delta.{vp.col_a}"):
                if cand in df.columns and cand not in lead:
                    lead.append(cand)
        if "break_reason" in df.columns:
            lead.insert(0, "break_reason")
    rest = [c for c in df.columns if c not in lead and not c.startswith("__")]
    return df.select(lead + rest)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def compute_buckets(df_a: pl.DataFrame, df_b: pl.DataFrame, config: MatchConfig) -> ReconResult:
    """Split two frames into the five mutually-exclusive reconciliation buckets."""
    started = time.perf_counter()

    left, blank_a, dupes_a = _prepare_side(df_a, config, "A")
    right, blank_b, dupes_b = _prepare_side(df_b, config, "B")

    counts = BucketCounts(
        rows_read_a=left.height,
        rows_read_b=right.height,
        duplicate_keys_a=dupes_a,
        duplicate_keys_b=dupes_b,
    )
    if blank_a:
        counts.warnings.append(f"{blank_a} row(s) in File A have a blank key — forced to orphans.")
    if blank_b:
        counts.warnings.append(f"{blank_b} row(s) in File B have a blank key — forced to orphans.")
    if dupes_a or dupes_b:
        counts.warnings.append(
            f"Duplicate key occurrences detected (A={dupes_a}, B={dupes_b}); "
            "matched occurrence-by-occurrence."
        )

    join_on = [_KEY, _OCC]
    matched = left.join(right, on=join_on, how="inner")
    orphans_a = left.join(right.select(join_on), on=join_on, how="anti")
    orphans_b = right.join(left.select(join_on), on=join_on, how="anti")

    if config.values:
        cmp_exprs, flags = _comparison_exprs(config.values, config.epsilon)
        matched = matched.with_columns(cmp_exprs)
        matched = matched.with_columns(pl.all_horizontal(flags).alias(_EXACT))
        reason = pl.concat_str(
            [
                pl.when(pl.col(f).not_()).then(pl.lit(f"{vp.col_a}; ")).otherwise(pl.lit(""))
                for f, vp in zip(flags, config.values)
            ]
        ).str.strip_chars(" ;")
        matched = matched.with_columns(reason.alias("break_reason"))
    else:
        # Key-only reconciliation: a key match is by definition an exact match.
        matched = matched.with_columns(
            pl.lit(True).alias(_EXACT), pl.lit("").alias("break_reason")
        )

    exact = matched.filter(pl.col(_EXACT))
    mismatch = matched.filter(pl.col(_EXACT).not_())

    counts.exact_matches = exact.height
    counts.value_mismatches = mismatch.height
    counts.orphans_a = orphans_a.height
    counts.orphans_b = orphans_b.height
    counts.assert_conservation()

    result = ReconResult(
        counts=counts,
        exact_matches=_ordered_output(exact.drop("break_reason"), config, with_delta=False),
        value_mismatches=_ordered_output(mismatch, config, with_delta=True),
        orphans_a=_ordered_output(orphans_a, config, with_delta=False),
        orphans_b=_ordered_output(orphans_b, config, with_delta=False),
        config=config,
        duration_s=time.perf_counter() - started,
    )
    log.info(
        "recon complete schema=%s rows_a=%d rows_b=%d exact=%d mismatch=%d orphan_a=%d orphan_b=%d in %.3fs",
        config.schema_hash(),
        counts.rows_read_a,
        counts.rows_read_b,
        counts.exact_matches,
        counts.value_mismatches,
        counts.orphans_a,
        counts.orphans_b,
        result.duration_s,
    )
    return result
