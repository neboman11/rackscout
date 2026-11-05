"""Server-side inventory filtering, match/lead classification and serialization.

All SQL fragments are constants; user input only flows through bound
parameters. Unknown or conflicting values never satisfy positive constraints:
rows without a known contradiction may appear only as explicitly badged
``lead`` rows, never as matches. Confidence does not change sort priority.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import psycopg

from . import cpu_specs, hardware_platforms, providers
from .config import VENDOR_INDEX_URLS
from .domain import SUPPORTED_VENDORS
from .retail_sources import RETAILERS

SORTS = {
    "price": "coalesce(quoted_total, item_price) ASC NULLS LAST",
    "ram": "ram_gb DESC NULLS LAST, coalesce(quoted_total, item_price) ASC NULLS LAST",
    "bays": "drive_bays DESC NULLS LAST, coalesce(quoted_total, item_price) ASC NULLS LAST",
    "benchmark": "cpu_mark DESC NULLS LAST, coalesce(quoted_total, item_price) ASC NULLS LAST",
}

# A stored eBay page verification applies to the current index observation only
# when the index price is unchanged since the check (same rule as
# providers.effective_page). It is *current* only while its stored verification
# window (``fresh_until``, written by the collector as check time +
# RACKSCOUT_PAGE_VERIFICATION_HOURS) is open and the latest page attempt
# succeeded. Legacy verifications without a stored window conservatively count
# for the 6h recheck cooldown only. Older or superseded verifications stay
# visible as stale evidence but can never make a row a match.
LEGACY_PAGE_FRESH_HOURS = 6
PAGE_APPLIES_SQL = (
    "(l.page_claims->>'status' = 'verified'"
    " AND (l.page_claims->>'index_price_at_check') IS NOT DISTINCT FROM (l.source_record->>'item_price'))"
)
PAGE_CURRENT_SQL = (
    f"coalesce(l.provider = '{providers.EBAY}' AND l.page_status = 'verified' AND {PAGE_APPLIES_SQL} AND coalesce("
    "(l.page_claims->>'fresh_until')::timestamptz, "
    f"(l.page_claims->>'checked_at')::timestamptz + interval '{LEGACY_PAGE_FRESH_HOURS} hours') > now(), false)"
)
# A retailer quote (never a page verification) is current only while its stored observation window
# (``fresh_until``, minted at acquisition) is open and its latest acquisition attempt succeeded.
QUOTE_CURRENT_SQL = (
    "coalesce(c.source_quote->>'status' = 'success'"
    " AND (c.source_quote->>'fresh_until')::timestamptz > now(), false)"
)

# Manual configurations: a price is paired with the configuration only through an explicit user assertion
# (never browser verification). Page verification and retailer quotes apply to source rows only.
MANUAL_PRICE_PAIRED_SQL = (
    "(c.origin = 'manual' AND c.price_kind = 'exact' AND (c.manual_data->>'price_confirmed') = 'true')"
)
PRICE_PAIRED_SQL = (
    f"({MANUAL_PRICE_PAIRED_SQL} OR (c.origin = 'source' AND c.price_kind = 'exact' AND "
    f"CASE WHEN l.provider = '{providers.EBAY}' THEN {PAGE_CURRENT_SQL} ELSE {QUOTE_CURRENT_SQL} END))"
)
# A source configuration with an active manual child is replaced by its children in inventory,
# whether or not those children pass the filters (applied before filtering, counting and paging).
HIDE_OVERRIDDEN_SQL = (
    "NOT EXISTS (SELECT 1 FROM configurations mc"
    " WHERE mc.source_configuration_id = c.id AND mc.origin = 'manual' AND mc.active)"
)

# A CPU platform reference applies only to a known, non-conflicting CPU model that is not explicitly
# missing; the join then yields no reference row (never a nearby SKU or a chassis guess).
CPU_PLATFORM_JOIN_SQL = (
    "LEFT JOIN cpu_platforms hp ON hp.cpu_key = c.cpu_platform_key"
    " AND c.cpu_model_state IN ('confirmed', 'inferred') AND coalesce(c.essential_states->>'cpu', '') <> 'missing'"
)
# Effective memory platform: the explicit listing memory type, else the CPU reference's single platform;
# an explicit statement naming several generations, or an explicit type contradicting the CPU reference,
# leaves it unknown.
MEMORY_PLATFORM_SQL = (
    "(CASE WHEN c.memory_type_conflict THEN NULL"
    " WHEN c.memory_type IS NOT NULL AND hp.memory_platform IS NOT NULL AND c.memory_type <> hp.memory_platform"
    " THEN NULL ELSE coalesce(c.memory_type, hp.memory_platform) END)"
)

SELECT_COLUMNS = f"""
c.id, c.variant_id, c.variant_label, c.model, c.cpu_model, c.cpu_count, c.cpu_count_state, c.total_cores,
c.ram_gb, c.ram_state, c.drive_bays, c.bays_state, c.drive_size, c.size_state, c.storage_state, c.storage_conf,
c.hardware_state, c.damage, c.price_kind, c.item_price, c.shipping_price, c.quoted_total, c.currency,
c.shipping_context, c.evidence, c.warnings, c.exclusions,
c.origin, c.revision, c.cpu_model_state, c.essential_states, c.manual_data, c.manual_created_at, c.manual_updated_at,
c.source_configuration_id, src.revision AS source_revision, src.active AS source_active,
(c.origin = 'manual' AND (src.active IS NOT TRUE
  OR src.revision IS DISTINCT FROM (c.manual_data->'baseline'->>'source_revision')::integer)) AS source_changed,
l.item_id, l.provider, l.title, l.seller, l.condition, l.canonical_url, l.last_seen_source_at, l.last_seen_at,
l.last_checked_at, l.next_check_after, l.detail_status, l.page_status, l.vendor,
c.source_quote, (c.origin = 'source' AND c.source_quote IS NOT NULL AND {QUOTE_CURRENT_SQL}) AS quote_current,
CASE WHEN c.origin = 'source' AND l.provider <> '{providers.EBAY}' THEN
  (SELECT count(*) FROM configurations o WHERE o.listing_id = c.listing_id AND o.origin = 'source' AND o.active)
END AS retail_options,
CASE WHEN c.origin = 'source' AND l.provider = '{providers.EBAY}' THEN l.page_checked_at END AS page_checked_at,
CASE WHEN c.origin = 'source' AND l.provider = '{providers.EBAY}' AND l.page_claims->>'status' = 'verified'
     THEN (l.page_claims->>'checked_at')::timestamptz END AS page_verified_at,
(c.origin = 'source' AND l.provider = '{providers.EBAY}' AND coalesce({PAGE_APPLIES_SQL}, false)) AS page_applies,
(c.origin = 'source' AND {PAGE_CURRENT_SQL}) AS page_current,
coalesce(c.origin = 'source' AND l.provider = '{providers.EBAY}' AND (l.page_claims->>'status' = 'verified')
         AND (l.page_claims->>'has_variations') = 'true', false) AS page_has_variations,
CASE WHEN l.provider = '{providers.EBAY}' AND l.description_claims->>'status' = 'verified'
       THEN l.description_claims->>'text_excerpt'
     WHEN l.provider <> '{providers.EBAY}' THEN left(l.source_record->'retail'->>'description_text', 1500)
END AS description_text,
m.generation, m.generation_label, m.target_era, m.rack_units, m.socket_capacity,
b.cpu_mark, b.single_thread, b.samples, b.version AS bench_version, b.scope AS bench_scope,
b.source_url AS bench_url, b.retrieved_at AS bench_retrieved_at,
c.memory_type, c.memory_type_conflict, hp.manufacturer AS cpu_manufacturer, hp.architecture AS cpu_architecture,
hp.memory_platform AS cpu_memory_platform, {MEMORY_PLATFORM_SQL} AS memory_platform,
hp.source_url AS hardware_source_url, hp.provider AS hardware_provider, hp.retrieved_at AS hardware_retrieved_at
"""

FROM_SQL = f"""
FROM configurations c
JOIN listings l ON l.id = c.listing_id
LEFT JOIN configurations src ON src.id = c.source_configuration_id
LEFT JOIN server_models m ON m.model = c.model
LEFT JOIN cpu_benchmarks b ON b.cpu_key = c.cpu_key
{CPU_PLATFORM_JOIN_SQL}
WHERE c.active AND l.archived_at IS NULL AND l.ended_at IS NULL AND c.currency = 'USD'
"""


@dataclass(frozen=True)
class Filters:
    # Selected provider keys (``providers.names()``), OR-combined; empty means every provider. Manual rows
    # belong to their listing's provider. Validated and deduplicated by the API layer.
    providers: tuple[str, ...] = ()
    vendor: str | None = None
    # Selected chassis models, OR-combined; empty means unconstrained. Deduplicated by the API layer.
    models: tuple[str, ...] = ()
    # Vendor-scoped platform generation (Dell 14G, HPE Gen10, Supermicro X11, ...); requires ``vendor``.
    generation: int | None = None
    # Hardware reference labels (``hardware_platforms``), OR-combined within each field and AND-combined
    # across fields; empty means unconstrained. Validated and deduplicated by the API layer. Hard filters:
    # an unknown or contradicted platform never admits a row, not even as a lead.
    cpu_manufacturers: tuple[str, ...] = ()
    cpu_architectures: tuple[str, ...] = ()
    memory_platforms: tuple[str, ...] = ()
    modern_only: bool = True
    height: int | None = 1
    min_ram: int | None = 16
    min_bays: int | None = 4
    drive_size: str | None = None
    cpu_count: int | None = None
    max_total: Decimal | None = None
    diskless: bool = True
    hide_damaged: bool = True
    confirmed_only: bool = False
    match_status: str = "all"
    q: str | None = None
    sort: str = "price"
    page: int = 1
    page_size: int = 50


def build_predicates(f: Filters) -> tuple[list[str], list[str], list[str], dict[str, Any]]:
    """Return (hard, satisfied, contradicted) SQL conditions plus bound params.

    ``hard`` conditions exclude rows from both matches and leads. A match needs
    every ``satisfied`` condition and no ``contradicted`` one; a lead needs only
    the absence of known contradictions. Explicitly missing essential hardware
    (CPU, RAM, PSU, cooling, NIC, drive backplane, ...) is always a known
    contradiction of the required operating hardware.
    """
    if f.generation is not None and f.vendor is None:
        raise ValueError("generation numbers are vendor-specific; a vendor is required")
    hard: list[str] = []
    sat: list[str] = [PRICE_PAIRED_SQL, "c.hardware_state <> 'missing'"]
    bad: list[str] = ["(c.hardware_state = 'missing')"]
    p: dict[str, Any] = {}
    if f.vendor:
        hard.append("l.vendor = %(vendor)s")
        p["vendor"] = f.vendor
    if f.providers:
        hard.append("l.provider = ANY(%(providers)s)")
        p["providers"] = list(f.providers)
    if f.models:
        hard.append("c.model = ANY(%(models)s)")
        p["models"] = list(f.models)
    if f.generation is not None:
        hard.append("(m.vendor = %(vendor)s AND m.generation = %(generation)s)")
        p["generation"] = f.generation
    if f.cpu_manufacturers:
        hard.append("hp.manufacturer = ANY(%(cpu_manufacturers)s)")
        p["cpu_manufacturers"] = list(f.cpu_manufacturers)
    if f.cpu_architectures:
        hard.append("hp.architecture = ANY(%(cpu_architectures)s)")
        p["cpu_architectures"] = list(f.cpu_architectures)
    if f.memory_platforms:
        hard.append(f"{MEMORY_PLATFORM_SQL} = ANY(%(memory_platforms)s)")
        p["memory_platforms"] = list(f.memory_platforms)
    if f.modern_only:
        hard.append("m.target_era")
    if f.height is not None:
        hard.append("m.rack_units = %(height)s")
        p["height"] = f.height
    if f.q:
        hard.append(
            "(l.title ILIKE %(q)s ESCAPE '\\' OR c.cpu_model ILIKE %(q)s ESCAPE '\\' OR l.seller ILIKE %(q)s ESCAPE '\\'"
            " OR l.item_id = %(q_exact)s)"
        )
        escaped = f.q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        p["q"] = f"%{escaped}%"
        p["q_exact"] = f.q
    if f.min_ram is not None:
        sat.append("(c.ram_state IN ('confirmed','inferred') AND c.ram_gb >= %(min_ram)s)")
        bad.append("(c.ram_gb IS NOT NULL AND c.ram_gb < %(min_ram)s)")
        p["min_ram"] = f.min_ram
    if f.min_bays is not None:
        sat.append("(c.bays_state IN ('confirmed','inferred') AND c.drive_bays >= %(min_bays)s)")
        bad.append("(c.drive_bays IS NOT NULL AND c.drive_bays < %(min_bays)s)")
        p["min_bays"] = f.min_bays
    if f.drive_size:
        sat.append("(c.size_state IN ('confirmed','inferred') AND c.drive_size = %(drive_size)s)")
        bad.append("(c.drive_size IS NOT NULL AND c.drive_size <> %(drive_size)s)")
        p["drive_size"] = f.drive_size
    if f.cpu_count is not None:
        sat.append("(c.cpu_count_state IN ('confirmed','inferred') AND c.cpu_count = %(cpu_count)s)")
        bad.append("(c.cpu_count IS NOT NULL AND c.cpu_count <> %(cpu_count)s)")
        p["cpu_count"] = f.cpu_count
    if f.max_total is not None:
        sat.append("(c.quoted_total IS NOT NULL AND c.quoted_total <= %(max_total)s)")
        bad.append("(c.item_price IS NULL OR c.item_price > %(max_total)s"
                   " OR (c.quoted_total IS NOT NULL AND c.quoted_total > %(max_total)s))")
        p["max_total"] = f.max_total
    if f.diskless:
        sat.append("(c.storage_state = 'diskless' AND c.storage_conf IN ('confirmed','inferred'))")
        bad.append("(c.storage_state = 'included')")
    if f.hide_damaged:
        hard.append("(c.damage IS NOT TRUE AND c.hardware_state <> 'damaged')")
    if f.confirmed_only:
        sat.append("c.hardware_state = 'confirmed'")
    return hard, sat, bad, p


def classified_sql(f: Filters, *, hide_overridden: bool = True) -> tuple[str, dict[str, Any]]:
    """SQL yielding filtered rows with boolean ``is_match`` (never NULL).

    Chassis scope (model/generation/height) and text search are hard filters:
    unknown catalog metadata never admits a row, not even as a lead. Inventory
    hides source rows replaced by manual children; detail reads do not.
    """
    hard, sat, bad, params = build_predicates(f)
    if hide_overridden:
        hard.insert(0, HIDE_OVERRIDDEN_SQL)
    is_match = f"({' AND '.join(sat)}) AND NOT ({' OR '.join(bad)})"
    lead_ok = "false" if f.confirmed_only else f"NOT ({' OR '.join(bad)})"
    eligibility = {
        "all": "is_match OR lead_ok",
        "match": "is_match",
        "lead": "NOT is_match AND lead_ok",
    }[f.match_status]
    sql = (
        f"SELECT * FROM (SELECT {SELECT_COLUMNS}, coalesce(({is_match}), false) AS is_match, "
        f"coalesce(({lead_ok}), false) AS lead_ok {FROM_SQL}{''.join(f' AND {h}' for h in hard)}) AS base "
        f"WHERE {eligibility}"
    )
    return sql, params


def query_listings(conn: psycopg.Connection, f: Filters) -> dict[str, Any]:
    inner, params = classified_sql(f)
    counts = conn.execute(
        f"SELECT count(*) AS total, count(*) FILTER (WHERE is_match) AS matches FROM ({inner}) AS r", params
    ).fetchone()
    total = counts["total"]
    matches = counts["matches"]
    pages = max(1, -(-total // f.page_size))
    order = SORTS[f.sort]
    rows = conn.execute(
        f"SELECT * FROM ({inner}) AS r ORDER BY {order}, id ASC LIMIT %(limit)s OFFSET %(offset)s",
        {**params, "limit": f.page_size, "offset": (f.page - 1) * f.page_size},
    ).fetchall()
    return {
        "items": [serialize(r, "match" if r["is_match"] else "lead") for r in rows],
        "total": total,
        "total_matches": matches,
        "total_leads": total - matches,
        "page": f.page,
        "page_size": f.page_size,
        "pages": pages,
    }


def get_listing(conn: psycopg.Connection, listing_id: int) -> dict[str, Any] | None:
    """Detail of one active configuration; match_state evaluated against default filters.

    Rows that are neither matches nor leads (for example explicitly missing
    essential hardware) stay reachable so their evidence can be inspected, as
    do source rows replaced in inventory by manual children.
    """
    row = conn.execute(f"SELECT {SELECT_COLUMNS} {FROM_SQL} AND c.id = %(id)s", {"id": listing_id}).fetchone()
    if row is None:
        return None
    default_inner, default_params = classified_sql(Filters(), hide_overridden=False)
    default = conn.execute(
        f"SELECT is_match FROM ({default_inner}) AS r WHERE id = %(id)s", {**default_params, "id": listing_id}
    ).fetchone()
    return serialize(row, "match" if default and default["is_match"] else "lead")


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def _money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


def _warnings(r: dict[str, Any]) -> list[str]:
    """Stored warnings plus verification staleness evaluated at read time."""
    warnings = list(r["warnings"] or [])
    if r["memory_type_conflict"]:
        warnings.append("Listing states conflicting memory generations; memory platform unknown")
    if r["memory_type"] and r["cpu_memory_platform"] and r["memory_type"] != r["cpu_memory_platform"]:
        warnings.append(f"Listing memory type {r['memory_type']} contradicts the {r['cpu_memory_platform']} platform "
                        f"of the referenced {r['cpu_model']} CPU; memory platform unknown")
    if r["page_applies"] and not r["page_current"]:
        when = _iso(r["page_verified_at"]) or "at an unknown time"
        if r["page_status"] != "verified":
            reason = f"latest eBay page check {r['page_status'] or 'not recorded'}"
        else:
            reason = "no successful recheck within the verification window"
        warnings.insert(0, f"eBay page last verified {when}; {reason}; "
                           "current price, configuration and availability not reverified")
    quote = r["source_quote"] if r["origin"] == "source" else None
    if quote and r["price_kind"] == "exact" and not r["quote_current"]:
        if quote.get("status") != "success":
            reason = f"latest retailer check {quote.get('status')}"
        else:
            reason = "observation window ended without a newer observation"
        warnings.insert(0, f"Retailer quote last observed {quote.get('observed_at')}; {reason}; "
                           "current price, configuration and availability not reconfirmed")
    if r["origin"] == "manual" and r["source_changed"]:
        if not r["source_active"]:
            warnings.insert(0, "Original source configuration is no longer listed; manual values kept unchanged")
        else:
            warnings.insert(0, "Source listing data changed after this manual configuration was saved; "
                               "manual values kept unchanged")
    return warnings


# Explicit orderable hardware choices only. Generic payment/shipping/upgrade/marketing
# wording must never match: every alternative names a hardware dimension.
_CHOICE_RE = re.compile(
    r"\b(?:select|choose|pick)\s*(?:your\s+|the\s+|from\s+)?(?:ram|memory|cpus?|processors?|configuration|config|hdd|ssd|drives?|storage)\b"
    r"|\b(?:ram|memory|cpu|processor|configuration)s?\s+(?:options|choices)\b",
    re.IGNORECASE,
)


# A sentence that negates the choice or only invites the buyer to ask/request one is not
# an orderable option; missing unusual wording is preferred over a false affirmation.
_VETO_RE = re.compile(
    r"\b(?:no|without)\s+(?:(?:other|additional|extra|alternative|different|further|any|more|available)\s+)*"
    r"(?:(?:(?:ram|memory|cpus?|processors?|configuration|config|hdd|ssd|drives?|storage)\s+)?"
    r"(?:options?|choices?|alternatives?)|select|choose|pick)\b|"
    r"\b(?:not|none|unavailable|cannot|contact|ask|message|email|inquire|enquire|request|"
    r"quote|upgrades?|custom)\b|\b[a-z]+n['’]t\b",
    re.IGNORECASE,
)
_SENTENCE_RE = re.compile(r"[.!?|\n;]+")


def _choice_evidence(source: str, text: str | None) -> list[str]:
    for sentence in _SENTENCE_RE.split(text or ""):
        m = _CHOICE_RE.search(sentence)
        if m and not _VETO_RE.search(sentence):
            return [f"{source} offers hardware choice: \"{sentence.strip()[:100]}\""]
    return []


_MANUAL_PRICE_EVIDENCE = "Price manually entered for this configuration; not independently verified"


def _offer(r: dict[str, Any]) -> dict[str, Any]:
    """Classify how far the shown price and specification are known to belong together."""
    price_kind = r["price_kind"]
    if r["origin"] == "manual" and price_kind == "exact" and (r["manual_data"] or {}).get("price_confirmed") is True:
        return {"kind": "manual_configuration", "price_is_starting": False, "specification_price_paired": True,
                "evidence": [_MANUAL_PRICE_EVIDENCE]}
    if r["origin"] == "manual" and r["item_price"] is not None \
            and (r["manual_data"] or {}).get("fields", {}).get("item_price") is not None:
        return {"kind": "manual_unconfirmed", "price_is_starting": False, "specification_price_paired": False,
                "evidence": ["Price manually entered; pairing with this configuration not confirmed"]}
    if price_kind == "exact":
        current = bool(r["page_current"] if r["provider"] == providers.EBAY else r["quote_current"])
        return {"kind": "configuration" if current else "historical_configuration",
                "price_is_starting": False, "specification_price_paired": current, "evidence": []}
    evidence: list[str] = []
    if r["page_has_variations"] and r["page_current"]:
        evidence.append("eBay page lists selectable variations")
    if (r["retail_options"] or 0) >= 2:
        evidence.append(f"Retailer lists {r['retail_options']} purchasable native options for this product")
    evidence += _choice_evidence("Title", r["title"])
    evidence += _choice_evidence("Description", r["description_text"])
    return {"kind": "multiple_options" if evidence else "unverified",
            "price_is_starting": price_kind in ("base", "range") and r["item_price"] is not None,
            "specification_price_paired": False, "evidence": evidence}


def _manual(r: dict[str, Any]) -> dict[str, Any] | None:
    if r["origin"] != "manual":
        return None
    data = r["manual_data"] or {}
    return {
        "source_configuration_id": r["source_configuration_id"],
        "revision": r["revision"],
        "created_at": _iso(r["manual_created_at"]),
        "updated_at": _iso(r["manual_updated_at"]),
        "fields": sorted(data.get("fields") or {}),
        "notes": data.get("notes") or "",
        "price_confirmed": data.get("price_confirmed") is True,
        "source_changed": bool(r["source_changed"]),
    }


_REFERENCE_STATES = ("confirmed", "inferred")


def _cpu_reference(r: dict[str, Any]) -> tuple[dict[str, Any] | None, int | None]:
    """(per-CPU reference match, reference total) without touching reported ``total_cores``.

    A total needs a known, non-conflicting model and a known populated count; chassis socket capacity is
    never a population. A CPU explicitly missing or a conflicting model shows no reference at all.
    """
    states = r["essential_states"] or {}
    model_state = r.get("cpu_model_state")
    if states.get("cpu") == "missing" or model_state in ("conflicting", "unknown"):
        return None, None
    match = cpu_specs.lookup(r["cpu_model"])["match"]
    # Legacy NULL essentials cannot establish CPU presence when some hardware is explicitly missing.
    if r["essential_states"] is None and r["hardware_state"] == "missing":
        return match, None
    count = r["cpu_count"]
    if (match is None or r["cpu_count_state"] not in _REFERENCE_STATES
            or isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= cpu_specs.MAX_CPU_COUNT):
        return match, None
    return match, match["cores_per_cpu"] * count


_VARIANT_ID_RE = re.compile(r"[0-9]{1,20}")


def _source_url(r: dict[str, Any]) -> str:
    """eBay item link, or the retailer's canonical product link selecting the native variant when known."""
    url = r["canonical_url"]
    if r["provider"] != providers.EBAY and r["origin"] == "source" and _VARIANT_ID_RE.fullmatch(r["variant_id"] or ""):
        return f"{url}?variant={r['variant_id']}"
    return url


def _quote(r: dict[str, Any]) -> dict[str, Any] | None:
    """Retailer quote provenance of a source row (never an eBay page verification); None otherwise."""
    quote = r["source_quote"] if r["origin"] == "source" else None
    if not quote:
        return None
    return {"method": quote.get("method"), "observed_at": quote.get("observed_at"),
            "fresh_until": quote.get("fresh_until"), "current": bool(r["quote_current"]),
            "status": quote.get("status")}


def _hardware(r: dict[str, Any]) -> dict[str, Any]:
    """CPU platform reference facts (model references, not installed-hardware verification) and memory platform."""
    reference = None
    if r["cpu_manufacturer"] is not None:
        reference = {"source_url": r["hardware_source_url"], "provider": r["hardware_provider"],
                     "retrieved_at": _iso(r["hardware_retrieved_at"])}
    memory_source = None
    if r["memory_platform"] is not None:
        memory_source = "listing" if r["memory_type"] is not None else "cpu_reference"
    return {
        "cpu_manufacturer": r["cpu_manufacturer"],
        "cpu_architecture": r["cpu_architecture"],
        "memory_platform": r["memory_platform"],
        "memory_platform_source": memory_source,
        "hardware_reference": reference,
    }


def serialize(r: dict[str, Any], match_state: str) -> dict[str, Any]:
    benchmark = None
    cpu_spec, inferred_total_cores = _cpu_reference(r)
    if r.get("cpu_mark") is not None:
        benchmark = {
            "cpu_mark": r["cpu_mark"],
            "single_thread": r["single_thread"],
            "samples": r["samples"],
            "version": r["bench_version"],
            "scope": r["bench_scope"],
            "source_url": r["bench_url"],
            "retrieved_at": _iso(r["bench_retrieved_at"]),
        }
    return {
        "id": r["id"],
        "item_id": r["item_id"],
        "provider": r["provider"],
        "platform": providers.names().get(r["provider"], r["provider"]),
        "variant_id": r["variant_id"],
        "variant_label": r["variant_label"],
        "title": r["title"],
        "vendor": r["vendor"],
        "model": r["model"],
        "generation": r["generation"],
        "generation_label": r["generation_label"],
        "target_era": r["target_era"],
        "rack_units": r["rack_units"],
        "seller": r["seller"],
        "condition": r["condition"],
        "cpu_model": r["cpu_model"],
        "cpu_count": r["cpu_count"],
        "cpu_count_state": r["cpu_count_state"],
        "socket_capacity": r["socket_capacity"],
        "total_cores": r["total_cores"],
        "ram_gb": r["ram_gb"],
        "ram_state": r["ram_state"],
        "drive_bays": r["drive_bays"],
        "drive_size": r["drive_size"],
        "storage_state": r["storage_state"],
        "storage_conf": r["storage_conf"],
        "hardware_state": r["hardware_state"],
        "damage": r["damage"],
        "price_kind": r["price_kind"],
        "item_price": _money(r["item_price"]),
        "shipping_price": _money(r["shipping_price"]),
        "quoted_total": _money(r["quoted_total"]),
        "currency": r["currency"],
        "shipping_context": r["shipping_context"],
        "match_state": match_state,
        "source_url": _source_url(r),
        "source_updated_at": _iso(r["last_seen_source_at"]),
        "last_seen_at": _iso(r["last_seen_at"]),
        "last_checked_at": _iso(r["last_checked_at"]),
        "next_check_after": _iso(r["next_check_after"]),
        "detail_status": r["detail_status"],
        "page_verified_at": _iso(r["page_verified_at"]),
        "page_checked_at": _iso(r["page_checked_at"]),
        "page_verification_current": bool(r["page_current"]),
        "quote": _quote(r),
        "evidence": r["evidence"] or [],
        "warnings": _warnings(r),
        "exclusions": r["exclusions"] or [],
        "benchmark": benchmark,
        "offer": _offer(r),
        "origin": r["origin"],
        "revision": r["revision"],
        "essential_states": r["essential_states"],
        "cpu_spec": cpu_spec,
        "inferred_total_cores": inferred_total_cores,
        **_hardware(r),
        "manual": _manual(r),
    }


def facets(conn: psycopg.Connection) -> dict[str, Any]:
    model_vendors: dict[str, str] = {}
    for r in conn.execute(
        """SELECT DISTINCT c.model, l.vendor FROM configurations c JOIN listings l ON l.id = c.listing_id
           WHERE c.active AND c.model IS NOT NULL AND l.vendor IS NOT NULL
             AND l.archived_at IS NULL AND l.ended_at IS NULL"""
    ).fetchall():
        model_vendors[r["model"]] = r["vendor"]
    catalog = conn.execute(
        "SELECT model, vendor, generation, generation_label FROM server_models ORDER BY vendor, generation, model"
    ).fetchall()
    generations_by_vendor: dict[str, list[int]] = {}
    labels: dict[str, dict[str, list[str]]] = {}
    for r in catalog:
        model_vendors[r["model"]] = r["vendor"]
        gens = generations_by_vendor.setdefault(r["vendor"], [])
        if r["generation"] not in gens:
            gens.append(r["generation"])
        names = labels.setdefault(r["vendor"], {}).setdefault(str(r["generation"]), [])
        if r["generation_label"] not in names:
            names.append(r["generation_label"])
    return {
        "models": sorted(model_vendors),
        "generations": sorted({g for gens in generations_by_vendor.values() for g in gens}),
        "drive_sizes": ["SFF", "LFF"],
        "vendors": list(SUPPORTED_VENDORS),
        "providers": [{"id": key, "name": name} for key, name in providers.names().items()],
        "model_vendors": dict(sorted(model_vendors.items())),
        "generations_by_vendor": generations_by_vendor,
        "generation_labels": {v: {g: " / ".join(sorted(n)) for g, n in by_gen.items()} for v, by_gen in labels.items()},
        "cpu_manufacturers": list(hardware_platforms.MANUFACTURERS),
        "cpu_architectures": list(hardware_platforms.architectures()),
        "memory_platforms": list(hardware_platforms.MEMORY_PLATFORMS),
    }


STALE_AFTER_HOURS = 8
COVERAGE = ("US eBay Buy It Now listings indexed by PerUnitPrice for Dell, HPE, Supermicro, Lenovo and Cisco "
            "rack servers, plus the public server catalogs of the US retailers TechMikeNY, SaveMyServer and "
            "Cloud Ninjas when enabled; not exhaustive")


def status(conn: psycopg.Connection, *, retailers: bool = True) -> dict[str, Any]:
    """Collection status: aggregate runs plus one entry per source (eBay vendor page or retailer catalog).

    ``retailers`` lists the retailer catalogs (collection enabled). Every entry carries its own
    provider-local state, freshness and inventory count, so a failing source never hides another.
    """
    def last(kind: str) -> dict[str, Any]:
        row = conn.execute(
            """SELECT outcome, started_at, message FROM source_runs WHERE kind = %s AND finished_at IS NOT NULL
               ORDER BY finished_at DESC, id DESC LIMIT 1""",
            (kind,),
        ).fetchone()
        if not row:
            return {"state": "never", "last_attempt_at": None, "message": None}
        return {"state": row["outcome"], "last_attempt_at": _iso(row["started_at"]), "message": row["message"]}

    # Freshness is per source: an eBay vendor page by its upstream snapshot time, a retailer catalog
    # (which states no upstream time) by its last successful local observation. The aggregate is only
    # as fresh as the stalest listed source, so one fresh source never hides another stale one.
    pages = {
        r["url"]: r
        for r in conn.execute(
            """SELECT url, provider, last_attempt_at, last_outcome, last_message, upstream_updated_at, last_success_at,
                      coalesce(CASE WHEN provider = %s THEN upstream_updated_at ELSE last_success_at END
                               < now() - make_interval(hours => %s), true) AS stale
               FROM source_pages""",
            (providers.EBAY, STALE_AFTER_HOURS),
        ).fetchall()
    }
    # Active inventory per eBay vendor page and per retailer (a retailer catalog mixes OEMs).
    counts: dict[tuple[str, str | None], int] = {}
    for r in conn.execute(
        f"""SELECT l.provider, CASE WHEN l.provider = %s THEN l.vendor END AS vendor, count(*) AS n
            FROM configurations c JOIN listings l ON l.id = c.listing_id
            WHERE c.active AND l.archived_at IS NULL AND l.ended_at IS NULL AND {HIDE_OVERRIDDEN_SQL}
            GROUP BY 1, 2""",
        (providers.EBAY,),
    ).fetchall():
        counts[(r["provider"], r["vendor"])] = r["n"]
    names = providers.names()

    def entry(provider: str, vendor: str | None, url: str, count: int) -> dict[str, Any]:
        page = pages.get(url)
        return {
            "provider": provider,
            "platform": names[provider],
            "vendor": vendor,
            "url": url,
            "state": page["last_outcome"] if page and page["last_outcome"] else "never",
            "last_attempt_at": _iso(page["last_attempt_at"]) if page else None,
            "last_success_at": _iso(page["last_success_at"]) if page else None,
            "source_updated_at": _iso(page["upstream_updated_at"]) if page else None,
            "stale": bool(page["stale"]) if page else True,
            "inventory_count": count,
            "message": page["last_message"] if page else None,
        }

    sources = [entry(providers.EBAY, vendor, url, counts.get((providers.EBAY, vendor), 0))
               for vendor, url in VENDOR_INDEX_URLS]
    if retailers:
        sources += [entry(key, None, r.catalog_url, counts.get((key, None), 0)) for key, r in RETAILERS.items()]
    upstreams = [pages[url]["upstream_updated_at"] if url in pages else None for _, url in VENDOR_INDEX_URLS]
    last_success = conn.execute(
        "SELECT max(finished_at) AS ts FROM source_runs WHERE kind = 'discovery' AND outcome = 'success'"
    ).fetchone()["ts"]
    backoffs = [
        {"origin": r["origin"], "blocked_until": _iso(r["blocked_until"]), "reason": r["reason"]}
        for r in conn.execute(
            "SELECT origin, blocked_until, reason FROM origin_backoff WHERE blocked_until > now() ORDER BY origin"
        ).fetchall()
    ]
    return {
        "provider": providers.EBAY,
        "coverage": COVERAGE,
        "source_urls": [url for _, url in VENDOR_INDEX_URLS],
        # eBay index upstream snapshot time (stalest vendor page); retailer catalogs state none.
        "source_updated_at": None if None in upstreams else _iso(min(upstreams)),
        "last_success_at": _iso(last_success),
        "stale": any(s["stale"] for s in sources),
        "stale_after_hours": STALE_AFTER_HOURS,
        "sources": sources,
        "discovery": last("discovery"),
        "enrichment": last("enrichment"),
        "backoff": backoffs,
        "inventory_count": sum(counts.values()),
    }
