"""Physical-core reference lookup from the bundled, pinned CPU catalog.

Matching is exact on a canonical SKU key. Only benign decoration (brand words, trademark marks, clock
speed and ``v4`` spacing) is removed; anything else (unknown suffix or version, alternatives,
contradictory manufacturers) leaves the model unmatched. Nothing here guesses or does I/O
beyond reading the packaged JSON once.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .domain import normalize_cpu_key

DATA_FILE = Path(__file__).parent / "data" / "cpu_specs.json"
MAX_CPU_COUNT = 8
_CHOICE_RE = re.compile(r"\b(?:or|and|and/or|vs|versus)\b|[/,;&|]|\s\+\s|\+(?=\w)")
_AMD_RE = re.compile(r"\b(?:amd|epyc|opteron|ryzen|threadripper)\b")
_INTEL_RE = re.compile(r"\b(?:intel|xeon|platinum|gold|silver|bronze)\b|\be[3578]\s*-?\s*\d{4}")
_MARK_RE = re.compile(r"\((?:r|tm)\)|[®™℠]")
_CLOCK_RE = re.compile(r"(?:@\s*)?\b\d+(?:\.\d+)?\s*ghz\b")
_VERSION_RE = re.compile(r"\b(\d{3,4})\s*v(\d)\b")
_NOISE_TOKENS = frozenset({"intel", "amd", "xeon", "core", "cpu", "processor", "@"})


def canonical_key(cpu_model: str | None) -> str | None:
    """Canonical SKU key, or None when the text is empty, ambiguous or names contradictory vendors."""
    if not isinstance(cpu_model, str):
        return None
    text = cpu_model.lower()
    if _CHOICE_RE.search(text):
        return None
    if _AMD_RE.search(text) and _INTEL_RE.search(text):
        return None
    text = _MARK_RE.sub(" ", text)
    text = _CLOCK_RE.sub(" ", text)
    text = _VERSION_RE.sub(r"\1 v\2", text)
    key = normalize_cpu_key(text)
    if not key:
        return None
    tokens = [t for t in key.split() if t not in _NOISE_TOKENS]
    return " ".join(tokens) or None


def _valid_cores(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


@lru_cache(maxsize=1)
def _catalog() -> dict[str, dict[str, Any]]:
    raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    defaults = {k: raw.get(k) for k in ("provider", "source_url", "retrieved_at")}
    out: dict[str, dict[str, Any]] = {}
    conflicted: set[str] = set()
    for entry in raw.get("cpus") or []:
        model = entry.get("cpu_model")
        cores = entry.get("cores_per_cpu")
        if not isinstance(model, str) or not _valid_cores(cores):
            continue
        key = canonical_key(model)
        if key is None or key in conflicted:
            continue
        match = {
            "cpu_model": model,
            "cores_per_cpu": cores,
            **{k: entry.get(k) or defaults[k] for k in defaults},
        }
        previous = out.get(key)
        if previous is not None and previous["cores_per_cpu"] != cores:
            # Two different core counts for one canonical SKU: refuse to answer rather than pick one.
            del out[key]
            conflicted.add(key)
        elif previous is None:
            out[key] = match
    return out


def lookup(cpu_model: str | None, cpu_count: int | None = None) -> dict[str, Any]:
    """``{match, total_cores}``; the total needs a known populated count (never chassis socket capacity)."""
    key = canonical_key(cpu_model)
    found = _catalog().get(key) if key else None
    if found is None:
        return {"match": None, "total_cores": None}
    # Concise legacy Intel SKUs (X5650, W-2125, etc.) lack the Xeon tier markers above.
    if _AMD_RE.search(cpu_model.lower()) and not _AMD_RE.search(found["cpu_model"].lower()):
        return {"match": None, "total_cores": None}
    match = dict(found)
    total = None
    if isinstance(cpu_count, int) and not isinstance(cpu_count, bool) and 1 <= cpu_count <= MAX_CPU_COUNT:
        total = match["cores_per_cpu"] * cpu_count
    return {"match": match, "total_cores": total}
