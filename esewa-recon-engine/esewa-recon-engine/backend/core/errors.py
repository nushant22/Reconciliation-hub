"""User-facing error types.

The UI catches `ReconError` and renders `.message` as a toast. Anything that is
*not* a `ReconError` is a genuine bug and is allowed to propagate to the logs —
we never want a real defect silently rendered as a friendly warning.
"""

from __future__ import annotations


class ReconError(Exception):
    """Base class for every expected, explainable failure."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return f"{self.message} {self.hint}".strip() if self.hint else self.message


class FileReadError(ReconError):
    """Unreadable/corrupt upload, unsupported extension, undecodable bytes."""


class SchemaError(ReconError):
    """Missing header, missing mapped column, ambiguous column name."""


class EmptyDataError(ReconError):
    """File parsed cleanly but contains zero usable rows."""
