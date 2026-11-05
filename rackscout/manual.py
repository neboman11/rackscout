"""Manual configurations: user-entered configuration/price pairs stored as separate rows.

A manual row is a child of one source configuration of the same listing. It materializes typed
effective values in the ordinary configuration columns (so filtering, sorting and pagination work
unchanged) and keeps, in ``manual_data``, the immutable original baseline of its root source
configuration, the explicit field overrides with server timestamps, the price assertion and notes.
The collector never rewrites manual rows; source rows are never modified here.
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import domain, hardware_platforms, providers

MAX_MANUAL_PER_LISTING = 100
LOCK_TIMEOUT = "5s"
STATEMENT_TIMEOUT = "15s"

ESSENTIAL_KEYS = ("cpu", "ram", "psu", "cooling", "nic", "drive_path")
WRITE_KEYS = ("revision", "label", "notes", "fields", "price_confirmed")
MAX_LABEL = 120
MAX_NOTES = 2000
MAX_CPU_MODEL = 100
MAX_REVISION = 2**31 - 1

_INTEGER_FIELDS = {"cpu_count": (1, 8), "total_cores": (1, 8192), "ram_gb": (0, 1048576), "drive_bays": (1, 128)}
_PRESENCE_FIELDS = {"cpu_included": "cpu", "psu": "psu", "cooling": "cooling", "nic": "nic", "drive_path": "drive_path"}
_MONEY_FIELDS = ("item_price", "shipping_price")
FIELDS = ("model", "cpu_model", "cpu_count", "total_cores", "ram_gb", "drive_bays", "drive_size", "storage_state",
          "cpu_included", "psu", "cooling", "nic", "drive_path", "damage", "item_price", "shipping_price")
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 +\-]{0,59}")
_MONEY_RE = re.compile(r"(?:0|[1-9][0-9]{0,7})(?:\.[0-9]{1,2})?")
# CPU text that states absence or ignorance instead of a model. Such statements belong in the structured
# ``cpu_included`` (missing/unknown) or a null ``cpu_model``; they must never become a confirmed CPU.
_CPU_SENTINEL_RE = re.compile(
    r"(?:no|none|n/?a|na|unknown|unk|missing|not\s+included|not\s+installed|removed|tbd|null|nil|zero|0|[-–—?.\s]+)"
    r"(?:\s+(?:cpus?|processors?))?",
    re.IGNORECASE,
)

# Stored configuration columns captured in a baseline and materialized for manual rows. Baselines captured
# before the memory statement columns existed lack them and never borrow the source's current values.
VALUE_COLUMNS = ("model", "cpu_model", "cpu_key", "cpu_model_state", "cpu_count", "cpu_count_state", "total_cores",
                 "ram_gb", "ram_state", "drive_bays", "bays_state", "drive_size", "size_state", "storage_state",
                 "storage_conf", "hardware_state", "damage", "price_kind", "item_price", "shipping_price",
                 "shipping_context", "memory_type", "memory_type_conflict")

# Evidence ``field`` names written by the domain builder for each editable field.
_EVIDENCE_FIELD = {"model": "model", "cpu_model": "cpu_model", "cpu_count": "cpu_count", "total_cores": "total_cores",
                   "ram_gb": "ram_gb", "drive_bays": "drive_bays", "drive_size": "drive_size",
                   "storage_state": "storage", "cpu_included": "cpu", "psu": "psu", "cooling": "cooling",
                   "nic": "nic", "drive_path": "drive_path", "damage": "damage", "item_price": "price",
                   "shipping_price": "shipping"}
# Domain warning groups (``warning_fields``) describing each essential.
_ESSENTIAL_WARNING_GROUP = {"cpu": "cpu_included", "ram": "ram_gb", "psu": "psu", "cooling": "cooling", "nic": "nic",
                            "drive_path": "drive_path"}
_ESSENTIAL_LABEL = {"cpu": "CPU", "ram": "RAM", "psu": "Power supplies", "cooling": "Cooling (fans/heatsinks)",
                    "nic": "Network adapter", "drive_path": "Drive backplane"}
MANUAL_NOTICE = "Manually edited configuration; entered values are not independently verified"


class ManualError(Exception):
    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class ManualValidationError(ManualError):
    """Invalid write payload or incompatible value (HTTP 422)."""


class ManualNotFound(ManualError):
    """Configuration missing, inactive, or its listing ended/archived (HTTP 404)."""


class ManualConflict(ManualError):
    """Stale revision, busy row, per-listing cap, or a prohibited source write (HTTP 409)."""


@dataclass(frozen=True)
class ManualWrite:
    revision: int
    label: str
    notes: str
    fields: dict[str, Any]
    price_confirmed: bool


# ---------------------------------------------------------------------------
# Payload validation (no database access)
# ---------------------------------------------------------------------------


def _invalid(field: str, message: str) -> ManualValidationError:
    return ManualValidationError(message, field)


def _clean_text(name: str, value: Any, limit: int, *, multiline: bool, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise _invalid(name, f"{name} must be a string")
    for ch in value:
        code = ord(ch)
        if 0xD800 <= code <= 0xDFFF or code == 0x7F or (code < 0x20 and not (multiline and ch in "\n\r\t")):
            raise _invalid(name, f"{name} contains invalid characters")
    value = value.strip() if multiline else " ".join(value.split())
    if not value and not allow_empty:
        raise _invalid(name, f"{name} must not be empty")
    if len(value) > limit:
        raise _invalid(name, f"{name} must be at most {limit} characters")
    return value


def _field(name: str, value: Any) -> Any:
    if name in _INTEGER_FIELDS:
        low, high = _INTEGER_FIELDS[name]
        if value is not None and (type(value) is not int or not low <= value <= high):
            raise _invalid(name, f"{name} must be a whole number from {low} to {high} or null")
        return value
    if name in _MONEY_FIELDS:
        if value is None:
            return None
        if not isinstance(value, str) or not _MONEY_RE.fullmatch(value):
            raise _invalid(name, f"{name} must be a decimal string from 0 to 99999999.99 with at most two "
                                 "decimal places, or null")
        return Decimal(value)
    if name in _PRESENCE_FIELDS:
        if value is None:
            return "unknown"
        if value not in ("present", "missing", "unknown"):
            raise _invalid(name, f"{name} must be present, missing or unknown")
        return value
    if name == "storage_state":
        if value is None:
            return "unknown"
        if value not in ("diskless", "included", "unknown"):
            raise _invalid(name, "storage_state must be diskless, included or unknown")
        return value
    if name == "drive_size":
        if value is not None and value not in ("SFF", "LFF"):
            raise _invalid(name, "drive_size must be SFF, LFF or null")
        return value
    if name == "damage":
        if value is not None and not isinstance(value, bool):
            raise _invalid(name, "damage must be true, false or null")
        return value
    if name == "model":
        if value is not None and (not isinstance(value, str) or not _MODEL_RE.fullmatch(value)):
            raise _invalid(name, "model must be a catalog model key or null")
        return value
    if name == "cpu_model":
        if value is None:
            return None
        text = _clean_text("cpu_model", value, MAX_CPU_MODEL, multiline=False, allow_empty=False)
        if _CPU_SENTINEL_RE.fullmatch(text) or domain.NEG_CPU_RE.search(text):
            raise _invalid(name, "cpu_model must name a CPU model; use cpu_included for a missing or unknown CPU")
        return text
    raise _invalid(name, f"{name} is not an editable field")


_PLAIN_KEY_RE = re.compile(r"[A-Za-z0-9_]{1,64}")


def _safe_key(key: Any) -> str:
    """Client-supplied key fit for error text: plain names verbatim, anything else ASCII-escaped."""
    if isinstance(key, str) and _PLAIN_KEY_RE.fullmatch(key):
        return key
    return ascii(key)[:80]


def parse_write(payload: Any) -> ManualWrite:
    """Validate a complete write body before any database access."""
    if not isinstance(payload, dict):
        raise ManualValidationError("Request body must be a JSON object")
    for key in payload:
        if key not in WRITE_KEYS:
            raise _invalid(_safe_key(key), f"{_safe_key(key)} is not an accepted property")
    for key in WRITE_KEYS:
        if key not in payload:
            raise _invalid(key, f"{key} is required")
    revision = payload["revision"]
    if type(revision) is not int or not 0 < revision <= MAX_REVISION:
        raise _invalid("revision", "revision must be a positive integer")
    label = _clean_text("label", payload["label"], MAX_LABEL, multiline=False, allow_empty=False)
    notes = _clean_text("notes", payload["notes"], MAX_NOTES, multiline=True, allow_empty=True)
    price_confirmed = payload["price_confirmed"]
    if not isinstance(price_confirmed, bool):
        raise _invalid("price_confirmed", "price_confirmed must be true or false")
    raw_fields = payload["fields"]
    if not isinstance(raw_fields, dict):
        raise _invalid("fields", "fields must be an object")
    fields = {}
    for name, value in raw_fields.items():
        if name not in FIELDS:
            raise _invalid(_safe_key(name), f"fields.{_safe_key(name)} is not an editable field")
        fields[name] = _field(name, value)
    if fields.get("cpu_included") == "missing" and fields.get("cpu_model") is not None:
        raise _invalid("cpu_included", "cpu_included cannot be missing while a CPU model is entered")
    if price_confirmed and fields.get("item_price") is None:
        raise _invalid("item_price", "item_price is required to confirm the price of this configuration")
    return ManualWrite(revision, label, notes, fields, price_confirmed)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _money(value: Any) -> str | None:
    return None if value is None else f"{Decimal(value):.2f}"


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(value)


def _json_value(name: str, value: Any) -> Any:
    return _money(value) if name in _MONEY_FIELDS else value


def _presence(state: str | None) -> str:
    return {"confirmed": "present", "present": "present", "missing": "missing"}.get(state or "", "unknown")


def _editable(values: dict[str, Any], states: dict[str, str]) -> dict[str, Any]:
    out = {name: _json_value(name, values.get(name)) for name in FIELDS if name not in _PRESENCE_FIELDS}
    for name, key in _PRESENCE_FIELDS.items():
        out[name] = _presence(states.get(key))
    return {name: out[name] for name in FIELDS}


# ---------------------------------------------------------------------------
# Baseline capture
# ---------------------------------------------------------------------------


def _rebuild(conn: psycopg.Connection, listing_id: int, variant_key: str) -> dict[str, Any] | None:
    """Re-run the collector's pure per-provider builder on stored observations; the configuration with this key."""
    from .collector import RECOMPUTE_COLUMNS

    listing = conn.execute(f"SELECT {RECOMPUTE_COLUMNS} FROM listings WHERE id = %s", (listing_id,)).fetchone()
    record = listing["source_record"]
    catalog = conn.execute("SELECT * FROM server_models WHERE model = %s", (record.get("model") or "",)).fetchone()
    configs, _ = providers.build_listing(listing, catalog)
    return next((c for c in configs if c["variant_key"] == variant_key), None)


def _baseline(conn: psycopg.Connection, row: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Immutable original snapshot of a source configuration."""
    rebuilt = _rebuild(conn, row["listing_id"], row["variant_key"])
    states = row["essential_states"] or (rebuilt or {}).get("essential_states")
    states = dict(states) if states else {k: "unknown" for k in ESSENTIAL_KEYS}
    # Warning attribution is used only when it describes exactly the stored warnings.
    attributed = rebuilt is not None and rebuilt["warnings"] == (row["warnings"] or [])
    return {
        "source_configuration_id": row["id"],
        "source_revision": row["revision"],
        "captured_at": _iso(now),
        "variant_key": row["variant_key"],
        "variant_label": row["variant_label"],
        "values": {c: _money(row[c]) if c in _MONEY_FIELDS else row[c] for c in VALUE_COLUMNS},
        "essential_states": states,
        "evidence": row["evidence"] or [],
        "warnings": row["warnings"] or [],
        "exclusions": row["exclusions"] or [],
        "warning_fields": rebuilt["warning_fields"] if attributed else None,
        # A stored blocker that reconstruction cannot attribute is never erased by unknown states.
        "missing_blocker": row["hardware_state"] == "missing" and "missing" not in states.values(),
        "review_blocker": row["hardware_state"] == "review" and all(states[k] == "confirmed" for k in ESSENTIAL_KEYS),
    }


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------


def _manual_evidence(name: str, value: Any, stamp: str) -> dict[str, Any]:
    if name in _MONEY_FIELDS:
        shown = _money(value) if value is not None else "not entered"
    elif value is None:
        shown = "cleared"
    else:
        shown = str(value).lower() if isinstance(value, bool) else str(value)
    state = "unknown" if value is None or value == "unknown" else "confirmed"
    return {"field": _EVIDENCE_FIELD[name], "state": state,
            "text": domain.clean_text(f"Manually entered {name}: {shown} ({stamp})"), "source_url": None}


def _same_cpu(entered: str | None, base: str | None) -> bool:
    """Whether an entered CPU model names the base option's CPU: the same exact reference SKU (which
    rejects contradictory manufacturers) when both resolve, otherwise unchanged normalized text."""
    a, b = hardware_platforms.reference_key(entered), hardware_platforms.reference_key(base)
    if a is not None and b is not None:
        return a == b
    return domain.normalize_cpu_key(entered) == domain.normalize_cpu_key(base)


def _materialize(baseline: dict[str, Any], write: ManualWrite, stamps: dict[str, str],
                 price_stamp: str | None) -> dict[str, Any]:
    """Effective column values for a manual row from its baseline and explicit overrides."""
    base = baseline["values"]
    fields = write.fields
    v = dict(base)
    v["item_price"], v["shipping_price"] = _decimal(base["item_price"]), _decimal(base["shipping_price"])
    states = dict(baseline["essential_states"])
    base_states = dict(states)

    def known(name: str) -> str:
        return "unknown" if fields[name] is None else "confirmed"

    if "model" in fields:
        v["model"] = fields["model"]
    if "cpu_model" in fields:
        v["cpu_model"] = fields["cpu_model"]
        v["cpu_key"] = domain.normalize_cpu_key(fields["cpu_model"])
        v["cpu_model_state"] = known("cpu_model")
    if "cpu_count" in fields:
        v["cpu_count"], v["cpu_count_state"] = fields["cpu_count"], known("cpu_count")
    cpu_changed = (
        ("cpu_model" in fields and domain.normalize_cpu_key(fields["cpu_model"]) != domain.normalize_cpu_key(base["cpu_model"]))
        or ("cpu_count" in fields and fields["cpu_count"] != base["cpu_count"])
    )
    # The base option's explicit memory type, as frozen in the baseline, describes its own CPU's platform
    # only: a different (or cleared) CPU never inherits it and falls back to that CPU's reference platform.
    same_cpu = "cpu_model" not in fields or _same_cpu(fields["cpu_model"], base["cpu_model"])
    v["memory_type"] = base.get("memory_type") if same_cpu else None
    v["memory_type_conflict"] = base.get("memory_type_conflict") is True and same_cpu
    v["cpu_platform_key"] = hardware_platforms.reference_key(v["cpu_model"])
    if "total_cores" in fields:
        v["total_cores"] = fields["total_cores"]
    elif cpu_changed:
        v["total_cores"] = None  # core totals belong to the original CPU/count only
    if "ram_gb" in fields:
        ram = fields["ram_gb"]
        v["ram_gb"], v["ram_state"] = ram, known("ram_gb")
        states["ram"] = "unknown" if ram is None else ("missing" if ram == 0 else "confirmed")
    if "drive_bays" in fields:
        v["drive_bays"], v["bays_state"] = fields["drive_bays"], known("drive_bays")
    if "drive_size" in fields:
        v["drive_size"], v["size_state"] = fields["drive_size"], known("drive_size")
    if "storage_state" in fields:
        v["storage_state"] = fields["storage_state"]
        v["storage_conf"] = "unknown" if fields["storage_state"] == "unknown" else "confirmed"
    if "cpu_included" in fields:
        states["cpu"] = {"present": "confirmed", "missing": "missing", "unknown": "unknown"}[fields["cpu_included"]]
    elif "cpu_model" in fields:
        states["cpu"] = "confirmed" if fields["cpu_model"] else ("missing" if base_states["cpu"] == "missing" else "unknown")
    for name in ("psu", "cooling", "nic", "drive_path"):
        if name in fields:
            states[name] = {"present": "confirmed", "missing": "missing", "unknown": "unknown"}[fields[name]]
    if "damage" in fields:
        v["damage"] = fields["damage"]

    missing_blocker = baseline["missing_blocker"] and not all(
        k in fields for k in ("cpu_included", "ram_gb", "psu", "cooling", "nic", "drive_path"))
    review_blocker = baseline["review_blocker"] and not all(k in fields for k in ("psu", "cooling", "nic", "drive_path"))
    if missing_blocker or "missing" in states.values():
        v["hardware_state"] = "missing"
    elif v["damage"]:
        v["hardware_state"] = "damaged"
    elif all(states[k] == "confirmed" for k in ESSENTIAL_KEYS) and not review_blocker:
        v["hardware_state"] = "confirmed"
    else:
        v["hardware_state"] = "review"

    # Price: only an explicit assertion pairs a price with this configuration.
    price_touched = True
    if write.price_confirmed:
        v["item_price"], v["shipping_price"], v["price_kind"] = fields["item_price"], fields.get("shipping_price"), "exact"
        v["shipping_context"] = ("Manually entered shipping quote; destination not confirmed; tax excluded"
                                 if v["shipping_price"] is not None else
                                 "Shipping not entered; total unknown (not treated as free)")
    elif not any(k in fields for k in _MONEY_FIELDS) and base["price_kind"] in ("base", "range") \
            and base["item_price"] is not None:
        price_touched = False  # the unpaired source starting quote stays a starting quote
    else:
        v["item_price"], v["shipping_price"] = fields.get("item_price"), fields.get("shipping_price")
        v["price_kind"] = "unknown"  # Amounts are known independently of configuration pairing.
        v["shipping_context"] = ("Manually entered shipping amount; destination not confirmed; tax excluded"
                                 if v["shipping_price"] is not None else
                                 "Shipping not entered; total unknown (not treated as free)")

    # Warnings: replace exactly the domain-attributed warnings of asserted facts.
    replaced = {f for f in fields if f not in _MONEY_FIELDS}
    replaced |= {_ESSENTIAL_WARNING_GROUP[k] for k in ESSENTIAL_KEYS if states[k] != base_states[k]}
    if price_touched:
        replaced |= {"price", "shipping"}
    attribution = baseline["warning_fields"]
    if attribution is None:
        kept = list(baseline["warnings"])
    else:
        drop = {w for group in replaced for w in attribution.get(group, [])}
        kept = [w for w in baseline["warnings"] if w not in drop]
    added = [MANUAL_NOTICE]
    for key in ESSENTIAL_KEYS:
        if _ESSENTIAL_WARNING_GROUP[key] in replaced:
            if states[key] == "missing":
                added.append(f"{_ESSENTIAL_LABEL[key]} not included (manual entry)")
            elif states[key] != "confirmed" and key != "ram":
                added.append(f"{_ESSENTIAL_LABEL[key]} not confirmed")
    if "ram_gb" in fields and fields["ram_gb"] is None:
        added.append("RAM amount unknown")
    if "cpu_count" in fields and fields["cpu_count"] is None:
        added.append("Installed CPU count unknown")
    if "storage_state" in fields and fields["storage_state"] == "unknown":
        added.append("Included storage not stated")
    if "drive_bays" in fields and fields["drive_bays"] is None:
        added.append("Drive bay count not stated")
    if "damage" in fields and fields["damage"]:
        added.append("Damage reported (manual entry)")
    if missing_blocker:
        added.append("Earlier source data recorded missing essential hardware; confirm every component to clear it")
    if price_touched and not write.price_confirmed:
        added.append("Price not confirmed for this manual configuration")
        if base["price_kind"] == "exact":
            added.append("The original option price is not paired with this manual configuration")
    elif write.price_confirmed and v["shipping_price"] is None:
        added.append("Shipping unknown; quoted total unavailable (not treated as free)")

    # Evidence: copied facts keep their source evidence and confidence; asserted facts get manual evidence.
    touched = {_EVIDENCE_FIELD[f] for f in fields}
    if price_touched:
        touched |= {"price", "shipping"}
    evidence = [
        {**e, "text": domain.clean_text(f"Original source (superseded by manual entry): {e['text']}")}
        if e.get("field") in touched else e
        for e in baseline["evidence"]
    ]
    evidence += [_manual_evidence(name, fields[name], stamps[name]) for name in FIELDS if name in fields]
    if write.price_confirmed:
        evidence.append({"field": "price", "state": "confirmed", "source_url": None,
                         "text": f"Price manually confirmed for this configuration ({price_stamp}); not independently verified"})

    v["essential_states"] = states
    v["evidence"] = evidence
    v["warnings"] = list(dict.fromkeys(kept + added))
    v["exclusions"] = baseline["exclusions"]
    return v


# ---------------------------------------------------------------------------
# Transactions and locking
# ---------------------------------------------------------------------------


@contextmanager
def _write_transaction(conn: psycopg.Connection) -> Iterator[None]:
    try:
        with conn.transaction():
            conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
            conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            yield
    except psycopg.errors.LockNotAvailable:
        raise ManualConflict("Configuration is being changed elsewhere; reload and try again") from None


def _lock(conn: psycopg.Connection, configuration_id: int, *, live: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """Lock the listing, then the configuration (the collector's order)."""
    head = conn.execute("SELECT listing_id FROM configurations WHERE id = %s", (configuration_id,)).fetchone()
    if head is None:
        raise ManualNotFound("Configuration not found")
    listing = conn.execute(
        "SELECT id, vendor, title, archived_at, ended_at FROM listings WHERE id = %s FOR UPDATE", (head["listing_id"],)
    ).fetchone()
    row = conn.execute("SELECT * FROM configurations WHERE id = %s FOR UPDATE", (configuration_id,)).fetchone()
    if row is None or listing is None or row["listing_id"] != listing["id"]:
        raise ManualNotFound("Configuration not found")
    if live and (not row["active"] or listing["archived_at"] is not None or listing["ended_at"] is not None):
        raise ManualNotFound("Configuration not found")
    return listing, row


def _check_revision(row: dict[str, Any], revision: int) -> None:
    if row["revision"] != revision:
        raise ManualConflict("Configuration changed; reload before saving")


def _validate_catalog(conn: psycopg.Connection, listing: dict[str, Any], baseline: dict[str, Any],
                      write: ManualWrite) -> None:
    model = write.fields.get("model", baseline["values"]["model"])
    catalog = None
    if model is not None:
        catalog = conn.execute("SELECT vendor, socket_capacity FROM server_models WHERE model = %s", (model,)).fetchone()
    if "model" in write.fields and model is not None and (catalog is None or catalog["vendor"] != listing["vendor"]):
        raise _invalid("model", "model must be a cataloged model from the listing's vendor")
    # Validate the effective count (explicit value, explicit null, or the inherited original) against the
    # effective chassis, so a model change cannot keep an impossible inherited count.
    count = write.fields["cpu_count"] if "cpu_count" in write.fields else baseline["values"]["cpu_count"]
    if count is not None and catalog is not None and count > catalog["socket_capacity"]:
        raise _invalid("cpu_count", f"cpu_count exceeds the {catalog['socket_capacity']}-socket chassis; "
                                    "enter the installed CPU count or clear it")


def _manual_data(baseline: dict[str, Any], write: ManualWrite, previous: dict[str, Any] | None,
                 now: datetime) -> tuple[dict[str, Any], dict[str, str], str | None]:
    stamp = _iso(now)
    old_fields = (previous or {}).get("fields") or {}
    old_stamps = (previous or {}).get("field_updated_at") or {}
    fields_json = {name: _json_value(name, write.fields[name]) for name in FIELDS if name in write.fields}
    stamps = {name: old_stamps[name] if name in old_fields and old_fields[name] == value and name in old_stamps
              else stamp for name, value in fields_json.items()}
    price_stamp = None
    if write.price_confirmed:
        # The assertion pairs a price with one exact configuration: any change to the explicit fields
        # (hardware or money) needs a fresh confirmation time; label/notes edits keep it.
        unchanged = (previous is not None and previous.get("price_confirmed") is True
                     and old_fields == fields_json)
        price_stamp = previous.get("price_confirmed_at") if unchanged and previous else stamp
    data = {"baseline": baseline, "label": write.label, "notes": write.notes, "fields": fields_json,
            "field_updated_at": stamps, "price_confirmed": write.price_confirmed, "price_confirmed_at": price_stamp}
    return data, stamps, price_stamp


_ROW_COLUMNS = VALUE_COLUMNS + ("cpu_platform_key", "evidence", "warnings", "exclusions", "essential_states")


def _row_params(values: dict[str, Any]) -> dict[str, Any]:
    params = {c: values[c] for c in VALUE_COLUMNS + ("cpu_platform_key",)}
    for c in ("evidence", "warnings", "exclusions", "essential_states"):
        params[c] = Jsonb(values[c])
    return params


def _record_price(conn: psycopg.Connection, configuration_id: int, values: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO price_history (configuration_id, price_kind, item_price, shipping_price, source)
           VALUES (%s, %s, %s, %s, 'manual')""",
        (configuration_id, values["price_kind"], values["item_price"], values["shipping_price"]),
    )


# ---------------------------------------------------------------------------
# Public service
# ---------------------------------------------------------------------------


def editor(conn: psycopg.Connection, configuration_id: int) -> dict[str, Any]:
    """Read-only editor state for a live source or manual configuration (bypasses inventory hiding)."""
    row = conn.execute(
        """SELECT c.*, l.title FROM configurations c JOIN listings l ON l.id = c.listing_id
           WHERE c.id = %s AND c.active AND l.archived_at IS NULL AND l.ended_at IS NULL""",
        (configuration_id,),
    ).fetchone()
    if row is None:
        raise ManualNotFound("Configuration not found")
    if row["origin"] == "manual":
        data = row["manual_data"]
        baseline = data["baseline"]
        label, notes, fields = data["label"], data["notes"], data["fields"]
        price_confirmed = data["price_confirmed"] is True
        states = row["essential_states"] or baseline["essential_states"]
    else:
        baseline = _baseline(conn, row, _now())
        label = " ".join((row["variant_label"] or row["title"]).split())[:MAX_LABEL].strip() or "Manual configuration"
        notes, fields, price_confirmed = "", {}, False
        states = baseline["essential_states"]
    siblings = conn.execute(
        """SELECT id, revision, ram_gb, item_price, shipping_price, manual_data, source_configuration_id
           FROM configurations WHERE listing_id = %s AND origin = 'manual' AND active ORDER BY id""",
        (row["listing_id"],),
    ).fetchall()
    return {
        "configuration_id": row["id"],
        "source_configuration_id": row["source_configuration_id"],
        "revision": row["revision"],
        "label": label,
        "notes": notes,
        "fields": fields,
        "values": _editable(row, states),
        "price_confirmed": price_confirmed,
        "original": {
            "configuration_id": baseline["source_configuration_id"],
            "revision": baseline["source_revision"],
            "captured_at": baseline["captured_at"],
            "variant_label": baseline["variant_label"],
            "values": _editable(baseline["values"], baseline["essential_states"]),
            "essential_states": baseline["essential_states"],
            "evidence": baseline["evidence"],
            "warnings": baseline["warnings"],
            "exclusions": baseline["exclusions"],
        },
        "configurations": [
            {"id": s["id"], "label": s["manual_data"]["label"], "revision": s["revision"],
             "source_configuration_id": s["source_configuration_id"], "ram_gb": s["ram_gb"],
             "item_price": _money(s["item_price"]), "shipping_price": _money(s["shipping_price"]),
             "price_confirmed": s["manual_data"]["price_confirmed"] is True}
            for s in siblings
        ],
    }


def create(conn: psycopg.Connection, configuration_id: int, parsed: ManualWrite) -> int:
    """Create one manual child of the selected row's root source configuration; returns its id."""
    with _write_transaction(conn):
        listing, row = _lock(conn, configuration_id, live=True)
        _check_revision(row, parsed.revision)
        now = _now()
        if row["origin"] == "manual":
            root_id, baseline = row["source_configuration_id"], row["manual_data"]["baseline"]
        else:
            root_id, baseline = row["id"], _baseline(conn, row, now)
        count = conn.execute(
            "SELECT count(*) AS n FROM configurations WHERE listing_id = %s AND origin = 'manual' AND active",
            (listing["id"],),
        ).fetchone()["n"]
        if count >= MAX_MANUAL_PER_LISTING:
            raise ManualConflict(f"A listing can have at most {MAX_MANUAL_PER_LISTING} manual configurations")
        _validate_catalog(conn, listing, baseline, parsed)
        data, stamps, price_stamp = _manual_data(baseline, parsed, None, now)
        values = _materialize(baseline, parsed, stamps, price_stamp)
        columns = ", ".join(_ROW_COLUMNS)
        placeholders = ", ".join(f"%({c})s" for c in _ROW_COLUMNS)
        new_id = conn.execute(
            f"""INSERT INTO configurations (listing_id, variant_key, variant_id, variant_label, active, {columns},
                  origin, source_configuration_id, manual_data, revision, manual_created_at, manual_updated_at,
                  observed_at)
                VALUES (%(listing_id)s, %(variant_key)s, NULL, %(label)s, true, {placeholders},
                  'manual', %(root_id)s, %(manual_data)s, 1, %(now)s, %(now)s, %(now)s)
                RETURNING id""",
            {**_row_params(values), "listing_id": listing["id"], "variant_key": f"manual:{uuid.uuid4()}",
             "label": parsed.label, "root_id": root_id, "manual_data": Jsonb(data), "now": now},
        ).fetchone()["id"]
        _record_price(conn, new_id, values)
        return new_id


def update(conn: psycopg.Connection, configuration_id: int, parsed: ManualWrite) -> int:
    """Replace a manual row's explicit fields, label, notes and price assertion; returns its id."""
    with _write_transaction(conn):
        listing, row = _lock(conn, configuration_id, live=True)
        if row["origin"] != "manual":
            raise ManualConflict("Source configurations cannot be edited; create a manual configuration instead")
        _check_revision(row, parsed.revision)
        baseline = row["manual_data"]["baseline"]
        _validate_catalog(conn, listing, baseline, parsed)
        now = _now()
        data, stamps, price_stamp = _manual_data(baseline, parsed, row["manual_data"], now)
        values = _materialize(baseline, parsed, stamps, price_stamp)
        assignments = ", ".join(f"{c} = %({c})s" for c in _ROW_COLUMNS)
        conn.execute(
            f"""UPDATE configurations SET {assignments}, variant_label = %(label)s, manual_data = %(manual_data)s,
                  revision = revision + 1, manual_updated_at = %(now)s, observed_at = %(now)s
                WHERE id = %(id)s""",
            {**_row_params(values), "label": parsed.label, "manual_data": Jsonb(data), "now": now,
             "id": row["id"]},
        )
        if (row["price_kind"], row["item_price"], row["shipping_price"]) != (
                values["price_kind"], values["item_price"], values["shipping_price"]):
            _record_price(conn, row["id"], values)
        return row["id"]


def delete(conn: psycopg.Connection, configuration_id: int, revision: int) -> dict[str, int]:
    """Remove a manual row (returning inventory to the source row when no sibling remains)."""
    with _write_transaction(conn):
        _, row = _lock(conn, configuration_id, live=False)
        if row["origin"] != "manual":
            raise ManualConflict("Source configurations cannot be deleted")
        _check_revision(row, revision)
        conn.execute("DELETE FROM configurations WHERE id = %s", (row["id"],))
        return {"deleted_id": row["id"], "source_id": row["source_configuration_id"]}
