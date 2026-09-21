"""Partner profile presets.

A profile is a *suggestion*, not a contract: it pre-fills the mapper so an
analyst reconciling NIC Asia daily does not re-map six dropdowns every morning,
but every field stays overridable because partners change their export format
without telling anyone.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .config import KeyPair, LoadSpec, MatchConfig, ValuePair
from .errors import SchemaError

PROFILE_PATH = Path(__file__).resolve().parent.parent / "profiles" / "company_schemas.json"


@lru_cache(maxsize=1)
def load_profiles(path: str | None = None) -> dict[str, dict]:
    target = Path(path) if path else PROFILE_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SchemaError(f"Profile file missing: {target}") from exc
    except json.JSONDecodeError as exc:
        raise SchemaError(f"Profile file is not valid JSON: {exc}") from exc
    return {p["name"]: p for p in payload.get("profiles", [])}


def profile_names(path: str | None = None) -> list[str]:
    return list(load_profiles(path).keys())


def get_profile(name: str, path: str | None = None) -> dict:
    profiles = load_profiles(path)
    if name not in profiles:
        raise SchemaError(
            f"Unknown company profile '{name}'.",
            hint="Known profiles: " + ", ".join(profiles),
        )
    return profiles[name]


def load_specs(profile: dict) -> tuple[LoadSpec, LoadSpec]:
    a = profile.get("file_a", {}) or {}
    b = profile.get("file_b", {}) or {}
    return (
        LoadSpec(header_row=int(a.get("header_row", 1)), sheet=a.get("sheet", 0)),
        LoadSpec(header_row=int(b.get("header_row", 1)), sheet=b.get("sheet", 0)),
    )


def to_match_config(
    profile: dict,
    *,
    keys: list[dict] | None = None,
    values: list[dict] | None = None,
    epsilon: float | None = None,
) -> MatchConfig:
    """Build a MatchConfig from a profile, with UI overrides taking precedence."""
    raw_keys = keys if keys is not None else profile.get("keys", [])
    raw_values = values if values is not None else profile.get("values", [])
    if not raw_keys:
        raise SchemaError(
            "No matching key selected.",
            hint="Pick at least one Primary Key attribute for File A and File B.",
        )
    return MatchConfig(
        keys=tuple(
            KeyPair(
                col_a=k["col_a"],
                col_b=k["col_b"],
                date_mode=k.get("date_mode", "off"),
                strip_leading_zeros=bool(k.get("strip_leading_zeros", True)),
            )
            for k in raw_keys
        ),
        values=tuple(
            ValuePair(col_a=v["col_a"], col_b=v["col_b"], numeric=bool(v.get("numeric", True)))
            for v in raw_values
        ),
        epsilon=float(profile.get("epsilon", 0.0) if epsilon is None else epsilon),
        profile_name=profile.get("name", "Custom"),
    )
