"""Server-side inventory filtering, match/lead classification and serialization.

All SQL fragments are constants; user input only flows through bound
parameters. Unknown or conflicting values never satisfy positive constraints:
rows without a known contradiction may appear only as separately grouped
``lead`` rows, never as matches.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import psycopg

from .config import VENDOR_INDEX_URLS
from .domain import SUPPORTED_VENDORS

SORTS = {
    "price": "(quoted_total IS NULL) ASC, quoted_total ASC, item_price ASC NULLS LAST",
    "ram": "ram_gb DESC NULLS LAST, (quoted_total IS NULL) ASC, quoted_total ASC",
    "bays": "drive_bays DESC NULLS LAST, (quoted_total IS NULL) ASC, quoted_total ASC",
    "benchmark": "cpu_mark DESC NULLS LAST, (quoted_total IS NULL) ASC, quoted_total ASC",
}

# A stored eBay page verification applies to the current index observation only
# when the index price is unchanged since the check (same rule as
# collector.effective_page). It is *current* only while its stored verification
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
    f"coalesce(l.page_status = 'verified' AND {PAGE_APPLIES_SQL} AND coalesce("
    "(l.page_claims->>'fresh_until')::timestamptz, "
    f"(l.page_claims->>'checked_at')::timestamptz + interval '{LEGACY_PAGE_FRESH_HOURS} hours') > now(), false)"
)

SELECT_COLUMNS = f"""
c.id, c.variant_id, c.variant_label, c.model, c.cpu_model, c.cpu_count, c.cpu_count_state, c.total_cores,
c.ram_gb, c.ram_state, c.drive_bays, c.bays_state, c.drive_size, c.size_state, c.storage_state, c.storage_conf,
c.hardware_state, c.damage, c.price_kind, c.item_price, c.shipping_price, c.quoted_total, c.currency,
c.shipping_context, c.evidence, c.warnings, c.exclusions,
l.item_id, l.title, l.seller, l.condition, l.canonical_url, l.last_seen_source_at, l.last_seen_at,
l.last_checked_at, l.detail_status, l.page_status, l.page_checked_at, l.vendor,
CASE WHEN l.page_claims->>'status' = 'verified' THEN (l.page_claims->>'checked_at')::timestamptz END AS page_verified_at,
coalesce({PAGE_APPLIES_SQL}, false) AS page_applies, {PAGE_CURRENT_SQL} AS page_current,
m.generation, m.generation_label, m.target_era, m.rack_units, m.socket_capacity,
b.cpu_mark, b.single_thread, b.samples, b.version AS bench_version, b.scope AS bench_scope,
b.source_url AS bench_url, b.retrieved_at AS bench_retrieved_at
"""

FROM_SQL = """
FROM configurations c
JOIN listings l ON l.id = c.listing_id
LEFT JOIN server_models m ON m.model = c.model
LEFT JOIN cpu_benchmarks b ON b.cpu_key = c.cpu_key
WHERE c.active AND l.archived_at IS NULL AND l.ended_at IS NULL AND c.currency = 'USD'
"""


@dataclass(frozen=True)
class Filters:
    vendor: str | None = None
    model: str | None = None
    # Vendor-scoped platform generation (Dell 14G, HPE Gen10, Supermicro X11, ...); requires ``vendor``.
    generation: int | None = None
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
    include_leads: bool = True
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
    sat: list[str] = ["c.price_kind = 'exact'", PAGE_CURRENT_SQL, "c.hardware_state <> 'missing'"]
    bad: list[str] = ["(c.hardware_state = 'missing')"]
    p: dict[str, Any] = {}
    if f.vendor:
        hard.append("l.vendor = %(vendor)s")
        p["vendor"] = f.vendor
    if f.model:
        hard.append("c.model = %(model)s")
        p["model"] = f.model
    if f.generation is not None:
        hard.append("(m.vendor = %(vendor)s AND m.generation = %(generation)s)")
        p["generation"] = f.generation
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


def classified_sql(f: Filters) -> tuple[str, dict[str, Any]]:
    """SQL yielding filtered rows with boolean ``is_match`` (never NULL).

    Chassis scope (model/generation/height) and text search are hard filters:
    unknown catalog metadata never admits a row, not even as a lead.
    """
    hard, sat, bad, params = build_predicates(f)
    is_match = f"({' AND '.join(sat)}) AND NOT ({' OR '.join(bad)})"
    lead_ok = "false" if (f.confirmed_only or not f.include_leads) else f"NOT ({' OR '.join(bad)})"
    sql = (
        f"SELECT * FROM (SELECT {SELECT_COLUMNS}, coalesce(({is_match}), false) AS is_match, "
        f"coalesce(({lead_ok}), false) AS lead_ok {FROM_SQL}{''.join(f' AND {h}' for h in hard)}) AS base "
        "WHERE is_match OR lead_ok"
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
        f"SELECT * FROM ({inner}) AS r ORDER BY is_match DESC, {order}, id ASC LIMIT %(limit)s OFFSET %(offset)s",
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
    essential hardware) stay reachable so their evidence can be inspected.
    """
    row = conn.execute(f"SELECT {SELECT_COLUMNS} {FROM_SQL} AND c.id = %(id)s", {"id": listing_id}).fetchone()
    if row is None:
        return None
    default_inner, default_params = classified_sql(Filters())
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
    if r["page_applies"] and not r["page_current"]:
        when = _iso(r["page_verified_at"]) or "at an unknown time"
        if r["page_status"] != "verified":
            reason = f"latest eBay page check {r['page_status'] or 'not recorded'}"
        else:
            reason = "no successful recheck within the verification window"
        warnings.insert(0, f"eBay page last verified {when}; {reason}; "
                           "current price, configuration and availability not reverified")
    return warnings


def serialize(r: dict[str, Any], match_state: str) -> dict[str, Any]:
    benchmark = None
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
        "hardware_state": r["hardware_state"],
        "damage": r["damage"],
        "price_kind": r["price_kind"],
        "item_price": _money(r["item_price"]),
        "shipping_price": _money(r["shipping_price"]),
        "quoted_total": _money(r["quoted_total"]),
        "currency": r["currency"],
        "shipping_context": r["shipping_context"],
        "match_state": match_state,
        "source_url": r["canonical_url"],
        "source_updated_at": _iso(r["last_seen_source_at"]),
        "last_seen_at": _iso(r["last_seen_at"]),
        "last_checked_at": _iso(r["last_checked_at"]),
        "detail_status": r["detail_status"],
        "page_verified_at": _iso(r["page_verified_at"]),
        "page_checked_at": _iso(r["page_checked_at"]),
        "page_verification_current": bool(r["page_current"]),
        "evidence": r["evidence"] or [],
        "warnings": _warnings(r),
        "exclusions": r["exclusions"] or [],
        "benchmark": benchmark,
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
        "model_vendors": dict(sorted(model_vendors.items())),
        "generations_by_vendor": generations_by_vendor,
        "generation_labels": {v: {g: " / ".join(sorted(n)) for g, n in by_gen.items()} for v, by_gen in labels.items()},
    }


STALE_AFTER_HOURS = 8
COVERAGE = ("US eBay Buy It Now listings indexed by PerUnitPrice for Dell, HPE, Supermicro, Lenovo and Cisco "
            "rack servers; not exhaustive")


def status(conn: psycopg.Connection) -> dict[str, Any]:
    def last(kind: str) -> dict[str, Any]:
        row = conn.execute(
            """SELECT outcome, started_at, message FROM source_runs WHERE kind = %s AND finished_at IS NOT NULL
               ORDER BY finished_at DESC, id DESC LIMIT 1""",
            (kind,),
        ).fetchone()
        if not row:
            return {"state": "never", "last_attempt_at": None, "message": None}
        return {"state": row["outcome"], "last_attempt_at": _iso(row["started_at"]), "message": row["message"]}

    # Freshness is per vendor index page: the aggregate is only as fresh as the
    # stalest configured page, so one fresh OEM never hides another stale one.
    pages = {
        r["url"]: r
        for r in conn.execute(
            """SELECT url, last_attempt_at, last_outcome, last_message, upstream_updated_at, last_success_at,
                      coalesce(upstream_updated_at < now() - make_interval(hours => %s), true) AS stale
               FROM source_pages""",
            (STALE_AFTER_HOURS,),
        ).fetchall()
    }
    counts = {
        r["vendor"]: r["n"]
        for r in conn.execute(
            """SELECT l.vendor, count(*) AS n FROM configurations c JOIN listings l ON l.id = c.listing_id
               WHERE c.active AND l.archived_at IS NULL AND l.ended_at IS NULL GROUP BY l.vendor"""
        ).fetchall()
    }
    sources = []
    for vendor, url in VENDOR_INDEX_URLS:
        page = pages.get(url)
        sources.append({
            "vendor": vendor,
            "url": url,
            "state": page["last_outcome"] if page and page["last_outcome"] else "never",
            "last_attempt_at": _iso(page["last_attempt_at"]) if page else None,
            "last_success_at": _iso(page["last_success_at"]) if page else None,
            "source_updated_at": _iso(page["upstream_updated_at"]) if page else None,
            "stale": bool(page["stale"]) if page else True,
            "inventory_count": counts.get(vendor, 0),
            "message": page["last_message"] if page else None,
        })
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
        "provider": "PerUnitPrice",
        "coverage": COVERAGE,
        "source_urls": [url for _, url in VENDOR_INDEX_URLS],
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
