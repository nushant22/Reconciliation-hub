"""Account-configuration loader and per-side pre-processor.

This module reads `backend/profiles/accounts.json` and exposes two things:

1. **`AccountConfig`** — a typed wrapper around one account's JSON entry.
2. **`prepare_side(df, side_cfg)`** — applies all filters, transforms and
   status-normalisation defined for a side, then returns a clean frame that
   the existing `compute_buckets` engine can match against without any further
   column mapping.

Design constraints
------------------
* Pure Polars — no row-by-row Python loops on data.
* All string comparisons are case-insensitive (`.str.to_lowercase()` once, then
  compare against lowercase literals).
* Columns are resolved case-insensitively so `"AMOUNT"` in the config matches
  a real column `"amount"` or `"Amount"` without crashing.
* A missing optional column (e.g. `status_col`) is silently skipped — the
  matcher can still run key-only with no status cross-tab.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import polars as pl

from .errors import SchemaError

log = logging.getLogger(__name__)

ACCOUNTS_PATH = Path(__file__).resolve().parent.parent / "profiles" / "accounts.json"

# Sentinel column written by status normalisation so the exporter can cross-tab
# without knowing the original status column name.
NORM_STATUS_COL = "__recon_status"


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #

@dataclass
class SideConfig:
    label: str                          # "CNP", "DishHome", …
    file: str                           # expected filename fragment
    header_row: int                     # 1-based
    key_col: str                        # primary match key column
    status_col: str | None              # raw status column (may be absent)
    amount_col: str | None              # raw amount column (may be absent)
    filters: list[dict[str, Any]]       # ordered filter rules
    transforms: list[dict[str, Any]]    # ordered transform rules
    status_rules: dict[str, Any]        # normalisation map


@dataclass
class AccountConfig:
    name: str
    folder: str
    output_prefix: str
    left: SideConfig
    right: SideConfig


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=1)
def _raw_accounts(path: str | None = None) -> list[dict]:
    target = Path(path) if path else ACCOUNTS_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SchemaError(f"accounts.json not found at {target}") from exc
    except json.JSONDecodeError as exc:
        raise SchemaError(f"accounts.json is not valid JSON: {exc}") from exc
    return payload.get("accounts", [])


def account_names(path: str | None = None) -> list[str]:
    return [a["name"] for a in _raw_accounts(path)]


def get_account(name: str, path: str | None = None) -> AccountConfig:
    for entry in _raw_accounts(path):
        if entry["name"] == name:
            return _parse_account(entry)
    known = ", ".join(a["name"] for a in _raw_accounts(path))
    raise SchemaError(
        f"Unknown account '{name}'.",
        hint=f"Known accounts: {known}",
    )


def _parse_side(raw: dict) -> SideConfig:
    return SideConfig(
        label=raw.get("label", ""),
        file=raw.get("file", ""),
        header_row=int(raw.get("header_row", 1)),
        key_col=raw.get("key_col", ""),
        status_col=raw.get("status_col"),
        amount_col=raw.get("amount_col"),
        filters=raw.get("filters", []),
        transforms=raw.get("transforms", []),
        status_rules=raw.get("status_rules", {}),
    )


def _parse_account(raw: dict) -> AccountConfig:
    return AccountConfig(
        name=raw["name"],
        folder=raw.get("folder", raw["name"]),
        output_prefix=raw.get("output_prefix", raw["name"]),
        left=_parse_side(raw["left"]),
        right=_parse_side(raw["right"]),
    )


# --------------------------------------------------------------------------- #
# Column resolver (case-insensitive)
# --------------------------------------------------------------------------- #

def _resolve_col(df: pl.DataFrame, wanted: str, label: str) -> str | None:
    """Return the actual column name that matches `wanted` case-insensitively.

    Returns None (never raises) so optional columns can be skipped gracefully.
    """
    target = wanted.strip().lower()
    for col in df.columns:
        if col.strip().lower() == target:
            return col
    log.debug("column '%s' not found in %s (available: %s)", wanted, label, df.columns[:20])
    return None


def _require_col(df: pl.DataFrame, wanted: str, label: str) -> str:
    col = _resolve_col(df, wanted, label)
    if col is None:
        raise SchemaError(
            f"Required column '{wanted}' was not found in the {label} file.",
            hint="Available columns: " + ", ".join(df.columns[:40]),
        )
    return col


# --------------------------------------------------------------------------- #
# Filter engine
# --------------------------------------------------------------------------- #

def _apply_filters(df: pl.DataFrame, filters: list[dict], label: str) -> pl.DataFrame:
    """Apply every filter rule in order.  Unknown columns are skipped with a warning."""
    for rule in filters:
        action = rule.get("action", "")
        raw_col = rule.get("column", "")
        col = _resolve_col(df, raw_col, label)
        if col is None:
            log.warning("filter skipped — column '%s' not found in %s", raw_col, label)
            continue

        if action == "equals":
            val = str(rule["value"]).lower()
            df = df.filter(pl.col(col).cast(pl.Utf8, strict=False)
                             .fill_null("").str.to_lowercase() == val)

        elif action == "not_in":
            bad = [str(v).lower() for v in rule.get("values", [])]
            df = df.filter(
                ~pl.col(col).cast(pl.Utf8, strict=False)
                  .fill_null("").str.to_lowercase().is_in(bad)
            )

        elif action == "not_prefix":
            prefix = str(rule["prefix"]).lower()
            df = df.filter(
                ~pl.col(col).cast(pl.Utf8, strict=False)
                  .fill_null("").str.to_lowercase().str.starts_with(prefix)
            )

        elif action == "non_empty":
            df = df.filter(
                pl.col(col).cast(pl.Utf8, strict=False).fill_null("").str.strip_chars() != ""
            )

        elif action == "regex":
            pattern = rule["pattern"]
            df = df.filter(
                pl.col(col).cast(pl.Utf8, strict=False)
                  .fill_null("").str.contains(pattern)
            )

        elif action == "amount_lt":
            threshold = float(rule["value"])
            # Parse the column as numeric then filter
            parsed = pl.col(col).cast(pl.Utf8, strict=False).str.replace_all(r",", "") \
                                 .cast(pl.Float64, strict=False)
            df = df.filter(parsed < threshold)

        else:
            log.warning("unknown filter action '%s' — skipped", action)

    return df


# --------------------------------------------------------------------------- #
# Transform engine
# --------------------------------------------------------------------------- #

def _apply_transforms(df: pl.DataFrame, transforms: list[dict], label: str) -> pl.DataFrame:
    """Apply every transform rule in order, mutating/creating columns."""
    for rule in transforms:
        action = rule.get("action", "")

        # ── strip_prefix ────────────────────────────────────────────────────
        # Strip a fixed prefix from `source` and write to `target`.
        # If `source` and `target` are the same, the column is updated in place.
        if action == "strip_prefix":
            src_name = rule.get("source", "")
            tgt_name = rule.get("target", src_name)
            prefix   = str(rule.get("prefix", "")).lower()
            src_col  = _resolve_col(df, src_name, label)
            if src_col is None:
                log.warning("transform strip_prefix skipped — '%s' not found in %s", src_name, label)
                continue

            base = pl.col(src_col).cast(pl.Utf8, strict=False).fill_null("")
            # strip prefix case-insensitively: compare lowercased head
            stripped = pl.when(
                base.str.to_lowercase().str.starts_with(prefix)
            ).then(
                base.str.slice(len(prefix))
            ).otherwise(base)

            tgt_actual = _resolve_col(df, tgt_name, label) or tgt_name
            df = df.with_columns(stripped.alias(tgt_actual))

        # ── extract_after_marker ─────────────────────────────────────────────
        # Find the last occurrence of `marker` in `source` (case-insensitive)
        # and write everything after it to `target`.
        # DishHome / WLink / eSewa* use this to turn
        # "ESEWA1789575041147:17557494" -> "1789575041147:17557494"
        elif action == "extract_after_marker":
            src_name = rule.get("source", "")
            tgt_name = rule.get("target", src_name)
            marker   = str(rule.get("marker", "")).upper()
            src_col  = _resolve_col(df, src_name, label)
            if src_col is None:
                log.warning("transform extract_after_marker skipped — '%s' not found in %s", src_name, label)
                continue

            base = pl.col(src_col).cast(pl.Utf8, strict=False).fill_null("")
            # Polars str.split + list.last gives us everything after the last
            # occurrence of the marker (case-insensitive via uppercase cast).
            upper = base.str.to_uppercase()
            extracted = (
                pl.when(upper.str.contains(marker))
                .then(
                    # split on the uppercase marker, take the last piece
                    upper.str.split(marker).list.last()
                )
                .otherwise(base)
            )
            # The split was on the uppercased string; we need the original-case
            # tail.  Compute byte offset instead.
            # Polars (as of 1.x) lacks str.find, so we use a regex extract:
            # capture everything after the last marker occurrence.
            pattern = f"(?i).*{re.escape(marker)}(.*)"
            extracted_orig = (
                pl.when(base.str.to_uppercase().str.contains(marker))
                .then(base.str.extract(pattern, group_index=1).fill_null(base))
                .otherwise(base)
            )

            tgt_actual = _resolve_col(df, tgt_name, label) or tgt_name
            df = df.with_columns(extracted_orig.alias(tgt_actual))

        # ── clean_spaces ─────────────────────────────────────────────────────
        # Collapse all internal whitespace to single spaces and strip ends.
        elif action == "clean_spaces":
            col_name = rule.get("column", "")
            col = _resolve_col(df, col_name, label)
            if col is None:
                log.warning("transform clean_spaces skipped — '%s' not found in %s", col_name, label)
                continue
            df = df.with_columns(
                pl.col(col).cast(pl.Utf8, strict=False).fill_null("")
                  .str.replace_all(r"\s+", " ").str.strip_chars()
                  .alias(col)
            )

        else:
            log.warning("unknown transform action '%s' — skipped", action)

    return df


# --------------------------------------------------------------------------- #
# Status normalisation
# --------------------------------------------------------------------------- #

def _build_status_normaliser(rules: dict[str, Any], raw_col: str) -> pl.Expr:
    """Return a Polars expression that maps raw status values to normalised labels.

    Two rule formats are supported:

    Format A — `groups` dict (used by most CNP sides):
        {"groups": {"Success": ["success"], "Failure": [...]}, "default": "Failure"}

    Format B — flat lists (used by some partner sides):
        {"success_values": ["success"], "timeout_values": ["903"], "default": "Failure"}
    """
    default = str(rules.get("default", "Unknown"))
    base = pl.col(raw_col).cast(pl.Utf8, strict=False).fill_null("").str.to_lowercase()

    # Build (condition, label) pairs in deterministic order
    pairs: list[tuple[pl.Expr, str]] = []

    if "groups" in rules:
        for label, values in rules["groups"].items():
            lc = [str(v).lower() for v in values]
            pairs.append((base.is_in(lc), label))
    else:
        # Format B: known keys are success_values, timeout_values, failure_values
        key_map = {
            "success_values": "Success",
            "timeout_values": "Timeout",
            "failure_values": "Failure",
        }
        for key, label in key_map.items():
            vals = rules.get(key, [])
            if vals:
                lc = [str(v).lower() for v in vals]
                pairs.append((base.is_in(lc), label))

    if not pairs:
        return pl.lit(default).alias(NORM_STATUS_COL)

    # Build nested when/then/otherwise chain
    expr = pl.when(pairs[0][0]).then(pl.lit(pairs[0][1]))
    for cond, label in pairs[1:]:
        expr = expr.when(cond).then(pl.lit(label))
    return expr.otherwise(pl.lit(default)).alias(NORM_STATUS_COL)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def prepare_side(df: pl.DataFrame, side_cfg: SideConfig) -> pl.DataFrame:
    """Apply filters, transforms and status normalisation for one side.

    Returns a frame that:
    - Has had all unwanted rows removed (filters)
    - Has computed/cleaned columns (transforms)
    - Has a `__recon_status` column with normalised labels (if status_col exists)
    - Preserves all original columns so the workbook shows full row detail
    """
    label = side_cfg.label

    # 1. Filters
    before = df.height
    df = _apply_filters(df, side_cfg.filters, label)
    after = df.height
    if before != after:
        log.info("%s: filters removed %d rows (%d -> %d)", label, before - after, before, after)

    # 2. Transforms
    df = _apply_transforms(df, side_cfg.transforms, label)

    # 3. Status normalisation
    if side_cfg.status_col and side_cfg.status_rules:
        raw_col = _resolve_col(df, side_cfg.status_col, label)
        if raw_col is not None:
            norm_expr = _build_status_normaliser(side_cfg.status_rules, raw_col)
            df = df.with_columns(norm_expr)
        else:
            log.warning("%s: status_col '%s' not found — skipping normalisation",
                        label, side_cfg.status_col)

    return df


def make_match_config(account: AccountConfig) -> "MatchConfig":  # type: ignore[name-defined]
    """Build a `MatchConfig` from an `AccountConfig`.

    The amount columns are compared as numeric values (delta computed).
    If the two sides share the same amount column name, `col_a` and `col_b`
    are set to the same string — the matcher namespaces them as `A.<col>` and
    `B.<col>` so they never collide in the output frame.
    """
    from .config import KeyPair, MatchConfig, ValuePair

    key_pair = KeyPair(
        col_a=account.left.key_col,
        col_b=account.right.key_col,
        strip_leading_zeros=True,
    )

    value_pairs: list[ValuePair] = []
    if account.left.amount_col and account.right.amount_col:
        value_pairs.append(
            ValuePair(
                col_a=account.left.amount_col,
                col_b=account.right.amount_col,
                numeric=True,
            )
        )

    return MatchConfig(
        keys=(key_pair,),
        values=tuple(value_pairs),
        epsilon=0.0,
        profile_name=account.name,
    )
