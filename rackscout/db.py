"""PostgreSQL access: connections, versioned migrations and reference data."""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .config import BENCHMARKS_FILE_ENV, benchmarks_file, database_conninfo

log = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
MIGRATIONS_DIR = PACKAGE_DIR / "migrations"
DATA_DIR = PACKAGE_DIR / "data"
MIGRATION_LOCK_KEY = 0x5241434B  # "RACK"
COLLECTOR_LOCK_KEY = 0x53434F55  # "SCOU"


class DatabaseUnavailable(RuntimeError):
    """Database cannot be reached or is not migrated. Message never contains credentials."""


def connect(connect_timeout: int = 10, autocommit: bool = False) -> psycopg.Connection:
    try:
        return psycopg.connect(
            database_conninfo(),
            connect_timeout=connect_timeout,
            application_name="rackscout",
            autocommit=autocommit,
            row_factory=dict_row,
        )
    except psycopg.OperationalError:
        # libpq messages can include host/user details; never propagate them.
        raise DatabaseUnavailable("database connection failed") from None


def migration_files() -> list[tuple[int, Path]]:
    out = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = re.match(r"^(\d{3})_[a-z0-9_]+\.sql$", path.name)
        if not m:
            raise RuntimeError(f"unexpected migration file name {path.name}")
        out.append((int(m.group(1)), path))
    versions = [v for v, _ in out]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError("migration versions must be contiguous from 001")
    return out


def latest_version() -> int:
    files = migration_files()
    return files[-1][0] if files else 0


def wait_for_connection(wait_seconds: int, connect_timeout: int) -> psycopg.Connection:
    """Retry only connection failures until the deadline; raise DatabaseUnavailable afterwards."""
    deadline = time.monotonic() + wait_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            return connect(connect_timeout=connect_timeout)
        except DatabaseUnavailable:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            log.info("database not reachable yet (attempt %d); retrying for up to %.0fs", attempt, remaining)
            time.sleep(min(3.0, max(0.1, remaining)))


def migrate(conn: psycopg.Connection) -> list[int]:
    """Apply pending migrations and sync reference data under an advisory lock."""
    applied: list[int] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
        for version, path in migration_files():
            if version in done:
                continue
            log.info("applying migration %03d", version)
            conn.execute(path.read_text(encoding="utf-8"))
            if version in _DATA_BACKFILLS:
                _DATA_BACKFILLS[version](conn)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            applied.append(version)
        sync_reference_data(conn)
    return applied


def _backfill_memory_types(conn: psycopg.Connection) -> None:
    """Migration 007: explicit memory statements of existing source rows from their stored observations.

    Uses the collector's parser so a later recompute writes identical values (no revision change).
    Manual rows are left NULL/false (their baselines never captured a statement); nothing else is modified.
    """
    from .collector import listing_memory

    ids: list[int] = []
    types: list[str | None] = []
    conflicts: list[bool] = []
    for r in conn.execute(
        """SELECT c.id, c.variant_key, l.provider, l.source_record FROM configurations c
           JOIN listings l ON l.id = c.listing_id WHERE c.origin = 'source'"""
    ).fetchall():
        memory_type, conflict = listing_memory(r["provider"], r["source_record"], r["variant_key"])
        if memory_type is not None or conflict:
            ids.append(r["id"])
            types.append(memory_type)
            conflicts.append(conflict)
    conn.execute(
        """UPDATE configurations c SET memory_type = v.memory_type, memory_type_conflict = v.conflict
           FROM unnest(%s::bigint[], %s::text[], %s::boolean[]) AS v(id, memory_type, conflict) WHERE c.id = v.id""",
        (ids, types, conflicts),
    )


# Data steps that need application code, run right after their migration file in the same transaction.
_DATA_BACKFILLS = {7: _backfill_memory_types}


class ReferenceDataError(RuntimeError):
    """Configured reference data is missing or malformed."""


def _load_json(name: str) -> dict[str, Any]:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


_BENCHMARK_FIELDS = {"cpu_model": str, "cpu_mark": int, "single_thread": int, "samples": int, "version": str,
                     "scope": str, "source_url": str, "retrieved_at": str}


def load_benchmarks() -> dict[str, Any] | None:
    """Read the optional external per-CPU benchmark dataset.

    Returns None when ``RACKSCOUT_BENCHMARKS_FILE`` is unset (benchmarks simply
    unavailable). A configured file that is missing, unreadable or malformed
    raises ``ReferenceDataError``; it never falls back to bundled scores.
    """
    path = benchmarks_file()
    if path is None:
        return None
    where = f"{BENCHMARKS_FILE_ENV} ({path})"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReferenceDataError(f"{where} cannot be read: {exc.strerror or type(exc).__name__}") from None
    except (ValueError, UnicodeDecodeError):
        raise ReferenceDataError(f"{where} is not valid JSON") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("provider"), str) or not doc["provider"].strip():
        raise ReferenceDataError(f"{where} must be an object with a non-empty 'provider'")
    rows = doc.get("benchmarks")
    if not isinstance(rows, list) or not rows:
        raise ReferenceDataError(f"{where} must contain a non-empty 'benchmarks' list")
    from .domain import normalize_cpu_key

    keys: set[str] = set()
    for i, b in enumerate(rows):
        if not isinstance(b, dict):
            raise ReferenceDataError(f"{where}: benchmarks[{i}] is not an object")
        for name, kind in _BENCHMARK_FIELDS.items():
            value = b.get(name)
            if not isinstance(value, kind) or isinstance(value, bool) or (kind is str and not value.strip()):
                raise ReferenceDataError(f"{where}: benchmarks[{i}].{name} must be a non-empty {kind.__name__}")
            if kind is int and value <= 0:
                raise ReferenceDataError(f"{where}: benchmarks[{i}].{name} must be positive")
        if b["scope"] != "single_cpu":
            raise ReferenceDataError(f"{where}: benchmarks[{i}].scope must be single_cpu (per-CPU values)")
        if not b["source_url"].startswith("https://"):
            raise ReferenceDataError(f"{where}: benchmarks[{i}].source_url must be https")
        key = normalize_cpu_key(b["cpu_model"])
        if key in keys:
            raise ReferenceDataError(f"{where}: duplicate CPU {b['cpu_model']!r}")
        keys.add(key)
    return doc


def sync_reference_data(conn: psycopg.Connection) -> None:
    """Upsert cited model metadata, CPU platform references and, when configured, external benchmark data.

    Inventory is never revised: only the reference-derived ``configurations.cpu_platform_key`` is
    re-derived. Without a configured benchmark file, stored benchmark rows are left untouched.
    """
    from .domain import normalize_cpu_key

    models = _load_json("models.json")["models"]
    for m in models:
        conn.execute(
            """INSERT INTO server_models (model, vendor, generation, generation_label, target_era, rack_units,
                 socket_capacity, cpu_family, source_url, source_text, retrieved_at)
               VALUES (%(model)s, %(vendor)s, %(generation)s, %(generation_label)s, %(target_era)s, %(rack_units)s,
                 %(socket_capacity)s, %(cpu_family)s, %(source_url)s, %(source_text)s, %(retrieved_at)s)
               ON CONFLICT (model) DO UPDATE SET vendor = EXCLUDED.vendor, generation = EXCLUDED.generation,
                 generation_label = EXCLUDED.generation_label, target_era = EXCLUDED.target_era,
                 rack_units = EXCLUDED.rack_units, socket_capacity = EXCLUDED.socket_capacity,
                 cpu_family = EXCLUDED.cpu_family, source_url = EXCLUDED.source_url,
                 source_text = EXCLUDED.source_text, retrieved_at = EXCLUDED.retrieved_at""",
            m,
        )
    conn.execute("DELETE FROM server_models WHERE NOT (model = ANY(%s))", ([m["model"] for m in models],))
    _sync_cpu_platforms(conn)
    doc = load_benchmarks()
    if doc is None:
        log.info("%s not set; CPU benchmark data not synchronized", BENCHMARKS_FILE_ENV)
        return
    keys = []
    for b in doc["benchmarks"]:
        key = normalize_cpu_key(b["cpu_model"])
        keys.append(key)
        conn.execute(
            """INSERT INTO cpu_benchmarks (cpu_key, cpu_model, provider, cpu_mark, single_thread, samples, version, scope, source_url, retrieved_at)
               VALUES (%(key)s, %(cpu_model)s, %(provider)s, %(cpu_mark)s, %(single_thread)s, %(samples)s, %(version)s, %(scope)s, %(source_url)s, %(retrieved_at)s)
               ON CONFLICT (cpu_key) DO UPDATE SET cpu_model = EXCLUDED.cpu_model, provider = EXCLUDED.provider,
                 cpu_mark = EXCLUDED.cpu_mark, single_thread = EXCLUDED.single_thread, samples = EXCLUDED.samples,
                 version = EXCLUDED.version, scope = EXCLUDED.scope, source_url = EXCLUDED.source_url, retrieved_at = EXCLUDED.retrieved_at""",
            {**b, "key": key, "provider": doc["provider"]},
        )
    conn.execute("DELETE FROM cpu_benchmarks WHERE NOT (cpu_key = ANY(%s))", (keys,))


def _sync_cpu_platforms(conn: psycopg.Connection) -> None:
    """Upsert exact-model CPU platform references and re-derive every configuration's reference key.

    Keys come from each row's own stored CPU model (source and manual alike); revisions, values, manual
    metadata and history are untouched. Older schemas (before migration 007) have nothing to sync.
    """
    if conn.execute("SELECT to_regclass('cpu_platforms') IS NOT NULL AS present").fetchone()["present"] is not True:
        return
    from . import hardware_platforms

    refs = hardware_platforms.references()
    for r in refs:
        conn.execute(
            """INSERT INTO cpu_platforms (cpu_key, cpu_model, manufacturer, architecture, memory_platform, source_url,
                 provider, retrieved_at)
               VALUES (%(cpu_key)s, %(cpu_model)s, %(manufacturer)s, %(architecture)s, %(memory_platform)s,
                 %(source_url)s, %(provider)s, %(retrieved_at)s)
               ON CONFLICT (cpu_key) DO UPDATE SET cpu_model = EXCLUDED.cpu_model,
                 manufacturer = EXCLUDED.manufacturer, architecture = EXCLUDED.architecture,
                 memory_platform = EXCLUDED.memory_platform, source_url = EXCLUDED.source_url,
                 provider = EXCLUDED.provider, retrieved_at = EXCLUDED.retrieved_at""",
            r,
        )
    conn.execute("DELETE FROM cpu_platforms WHERE NOT (cpu_key = ANY(%s))", ([r["cpu_key"] for r in refs],))
    models = [r["cpu_model"] for r in conn.execute(
        "SELECT DISTINCT cpu_model FROM configurations WHERE cpu_model IS NOT NULL").fetchall()]
    conn.execute(
        """UPDATE configurations c SET cpu_platform_key = k.key
           FROM unnest(%s::text[], %s::text[]) AS k(model, key)
           WHERE c.cpu_model = k.model AND c.cpu_platform_key IS DISTINCT FROM k.key""",
        (models, [hardware_platforms.reference_key(m) for m in models]),
    )
    conn.execute("UPDATE configurations SET cpu_platform_key = NULL WHERE cpu_model IS NULL AND cpu_platform_key IS NOT NULL")


def schema_ready(conn: psycopg.Connection) -> bool:
    row = conn.execute(
        "SELECT to_regclass('schema_migrations') IS NOT NULL AS present"
    ).fetchone()
    if not row or not row["present"]:
        return False
    row = conn.execute("SELECT coalesce(max(version), 0) AS v FROM schema_migrations").fetchone()
    return bool(row) and row["v"] == latest_version()


def catalog(conn: psycopg.Connection) -> dict[str, dict[str, Any]]:
    return {r["model"]: r for r in conn.execute("SELECT * FROM server_models").fetchall()}
