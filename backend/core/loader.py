"""Ingestion layer.

Partner files arrive in whatever shape the partner's ERP felt like emitting:
ISO-8859 bytes, a three-row marketing banner above the header, semicolon
delimiters, ragged trailing columns. Every one of those is a *caught* condition
here that surfaces as a readable message, never a traceback in the UI.

Everything is read as text first and typed later by the sanitizer — letting an
inference engine guess that a reference column is an Int64 is how leading zeros
disappear and orphans appear from nowhere.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import polars as pl

from .config import LoadSpec
from .errors import EmptyDataError, FileReadError
from .sanitizer import clean_dataframe

log = logging.getLogger(__name__)

CSV_EXTENSIONS = {".csv", ".txt", ".tsv"}
EXCEL_EXTENSIONS = {".xlsx", ".xlsm", ".xls"}

#: Tried in order. utf-8-sig first strips the BOM Excel loves to prepend;
#: cp1252/latin-1 never fail, so the chain always terminates.
ENCODING_CHAIN = ("utf-8-sig", "utf-8", "cp1252", "iso-8859-1")

_NULL_TOKENS = ["", "NA", "N/A", "n/a", "NULL", "null", "None", "nan", "NaN", "-"]


def decode_bytes(raw: bytes, encoding: str | None = None) -> tuple[str, str]:
    """Decode with an explicit encoding or walk the fallback chain."""
    chain = (encoding,) if encoding else ENCODING_CHAIN
    last: Exception | None = None
    for enc in chain:
        try:
            return raw.decode(enc), enc  # type: ignore[arg-type]
        except (UnicodeDecodeError, LookupError) as exc:
            last = exc
            continue
    raise FileReadError(
        "The file could not be decoded as text.",
        hint=f"Tried {', '.join(str(c) for c in chain)}. Last error: {last}",
    )


def sniff_separator(header_line: str) -> str:
    """Pick the delimiter that splits the header into the most fields."""
    candidates = {",": 0, ";": 0, "\t": 0, "|": 0}
    for sep in candidates:
        candidates[sep] = header_line.count(sep)
    best = max(candidates, key=lambda s: candidates[s])
    return best if candidates[best] > 0 else ","


def _read_csv(raw: bytes, spec: LoadSpec) -> pl.DataFrame:
    text, encoding = decode_bytes(raw, spec.encoding)
    if encoding != "utf-8-sig":
        log.info("decoded input as %s", encoding)

    skip = max(int(spec.header_row) - 1, 0)
    lines = text.splitlines()
    if len(lines) <= skip:
        raise EmptyDataError(
            f"The file has {len(lines)} line(s) but the header row is set to {spec.header_row}.",
            hint="Lower the header row setting.",
        )
    separator = sniff_separator(lines[skip])

    try:
        return pl.read_csv(
            io.BytesIO(text.encode("utf-8")),
            separator=separator,
            skip_rows=skip,
            has_header=True,
            infer_schema_length=0,  # everything as Utf8 — typing happens later
            truncate_ragged_lines=True,
            null_values=_NULL_TOKENS,
            quote_char='"',
        )
    except Exception as exc:  # noqa: BLE001 — surfaced to the operator verbatim
        raise FileReadError(
            "The CSV could not be parsed.",
            hint=f"Delimiter guessed as '{separator}'. Parser said: {exc}",
        ) from exc


def _read_excel(raw: bytes, spec: LoadSpec, suffix: str) -> pl.DataFrame:
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise FileReadError("pandas is required to read Excel files.") from exc

    engine = "xlrd" if suffix == ".xls" else "openpyxl"
    try:
        frame = pd.read_excel(
            io.BytesIO(raw),
            sheet_name=spec.sheet,
            header=max(int(spec.header_row) - 1, 0),
            dtype=str,
            engine=engine,
        )
    except ImportError as exc:
        raise FileReadError(
            f"Reading {suffix} needs the '{engine}' package installed.",
            hint="pip install xlrd  (legacy .xls) — or re-save the file as .xlsx.",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise FileReadError(
            "The Excel workbook could not be read.",
            hint=f"Sheet '{spec.sheet}', header row {spec.header_row}. Reader said: {exc}",
        ) from exc

    if isinstance(frame, dict):  # sheet_name=None returned every sheet
        frame = next(iter(frame.values()))
    return pl.from_pandas(frame.astype(str).where(frame.notna(), None))


def read_table(raw: bytes, filename: str, spec: LoadSpec | None = None) -> pl.DataFrame:
    """Read an uploaded CSV/Excel buffer into a clean, all-text Polars frame."""
    spec = spec or LoadSpec()
    suffix = Path(filename).suffix.lower()

    if not raw:
        raise EmptyDataError(f"'{filename}' is empty (0 bytes).")

    if suffix in CSV_EXTENSIONS:
        df = _read_csv(raw, spec)
    elif suffix in EXCEL_EXTENSIONS:
        df = _read_excel(raw, spec, suffix)
    else:
        raise FileReadError(
            f"Unsupported file type '{suffix or filename}'.",
            hint="Upload a .csv, .tsv, .xlsx or .xls file.",
        )

    df = clean_dataframe(df)
    if df.width == 0:
        raise EmptyDataError(
            f"No usable header row was found in '{filename}'.",
            hint=f"Header row is currently set to {spec.header_row}.",
        )
    if df.height == 0:
        raise EmptyDataError(f"'{filename}' parsed to zero data rows.")
    log.info("loaded %s: %d rows x %d cols", filename, df.height, df.width)
    return df


def read_path(path: str | Path, spec: LoadSpec | None = None) -> pl.DataFrame:
    """Convenience wrapper for CLI / test usage."""
    p = Path(path)
    if not p.exists():
        raise FileReadError(f"File not found: {p}")
    return read_table(p.read_bytes(), p.name, spec)
