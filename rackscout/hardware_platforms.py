"""CPU manufacturer, architecture family and DDR memory-platform references from the bundled catalog.

Matching reuses ``cpu_specs.canonical_key`` exactly: no prefix, suffix or neighbouring-SKU guesses.
A model whose text names a different vendor than the matched row stays unmatched. References describe
the CPU model's platform, not hardware verified as installed in a listing. ``memory_platform`` is None
when the platform supports more than one DDR generation or no primary memory source was verified.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .cpu_specs import _AMD_RE, _INTEL_RE, canonical_key

DATA_FILE = Path(__file__).parent / "data" / "cpu_platforms.json"
MANUFACTURERS = ("Intel", "AMD")
MEMORY_PLATFORMS = ("DDR3", "DDR4", "DDR5")


def _valid(entry: dict[str, Any], source: dict[str, Any] | None) -> bool:
    architecture = entry.get("architecture")
    return (
        isinstance(entry.get("cpu_model"), str)
        and entry.get("manufacturer") in MANUFACTURERS
        and isinstance(architecture, str) and architecture.strip() == architecture != ""
        and entry.get("memory_platform") in (*MEMORY_PLATFORMS, None)
        and source is not None
        and all(isinstance(source.get(k), str) and source[k] for k in ("url", "provider", "retrieved_at"))
    )


@lru_cache(maxsize=1)
def _catalog() -> dict[str, tuple[tuple[str, Any], ...]]:
    raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    sources = raw.get("sources") or {}
    out: dict[str, tuple[tuple[str, Any], ...]] = {}
    conflicted: set[str] = set()
    for entry in raw.get("cpus") or []:
        source = sources.get(entry.get("source"))
        if not _valid(entry, source):
            continue
        key = canonical_key(entry["cpu_model"])
        if key is None or key in conflicted:
            continue
        if key in out:
            # Two rows for one canonical SKU: refuse to answer rather than pick one.
            del out[key]
            conflicted.add(key)
            continue
        out[key] = (
            ("cpu_key", key),
            ("cpu_model", entry["cpu_model"]),
            ("manufacturer", entry["manufacturer"]),
            ("architecture", entry["architecture"]),
            ("memory_platform", entry["memory_platform"]),
            ("source_url", source["url"]),
            ("provider", source["provider"]),
            ("retrieved_at", source["retrieved_at"]),
        )
    return out


def references() -> list[dict[str, Any]]:
    """Every exact reference row, ordered by canonical key."""
    catalog = _catalog()
    return [dict(catalog[key]) for key in sorted(catalog)]


def architectures() -> tuple[str, ...]:
    """Supported architecture labels, sorted."""
    return tuple(sorted({dict(row)["architecture"] for row in _catalog().values()}))


def _find(cpu_model: str | None) -> tuple[tuple[str, Any], ...] | None:
    key = canonical_key(cpu_model)
    found = _catalog().get(key) if key else None
    if found is None:
        return None
    text = cpu_model.lower()
    manufacturer = dict(found)["manufacturer"]
    # Vendor words are stripped by canonical_key; a contradicting vendor must not borrow the other's SKU.
    if (manufacturer == "Intel" and _AMD_RE.search(text)) or (manufacturer == "AMD" and _INTEL_RE.search(text)):
        return None
    return found


def lookup(cpu_model: str | None) -> dict[str, Any] | None:
    """The exact reference row for ``cpu_model``, or None when unknown, ambiguous or contradictory."""
    found = _find(cpu_model)
    return dict(found) if found is not None else None


def reference_key(cpu_model: str | None) -> str | None:
    """The matched row's ``cpu_key`` (its canonical key), or None."""
    found = _find(cpu_model)
    return found[0][1] if found is not None else None
