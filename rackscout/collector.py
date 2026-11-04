"""Scheduled collector: atomic index discovery plus bounded incremental enrichment."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import db, domain
from .config import DESCRIPTION_HOST, EBAY_HOST, Settings
from .sources import HttpFetcher, IndexSnapshot, SourceBlocked, SourceError

log = logging.getLogger(__name__)

PROVIDER = "PerUnitPrice"
INDEX_ORIGIN = "perunitprice.com"
ARCHIVE_AFTER = timedelta(days=7)
DESCRIPTION_REFRESH = timedelta(hours=24)


def _json(value: Any) -> Jsonb:
    return Jsonb(value, dumps=lambda obj: json.dumps(obj, default=_default, separators=(",", ":")))


def _default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"not JSON serializable: {type(obj).__name__}")


def _jsonable(record: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(record, default=_default))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Run bookkeeping
# ---------------------------------------------------------------------------


def start_run(conn: psycopg.Connection, kind: str) -> int:
    with conn.transaction():
        row = conn.execute(
            "INSERT INTO source_runs (provider, kind) VALUES (%s, %s) RETURNING id", (PROVIDER, kind)
        ).fetchone()
    return row["id"]


def finish_run(conn: psycopg.Connection, run_id: int, outcome: str, message: str | None, *,
               upstream: datetime | None = None, discovered: int = 0, enriched: int = 0) -> None:
    conn.execute(
        """UPDATE source_runs SET finished_at = now(), outcome = %s, message = %s, upstream_updated_at = %s,
           discovered_count = %s, enriched_count = %s WHERE id = %s""",
        (outcome, (message or None) and message[:500], upstream, discovered, enriched, run_id),
    )


def backoff_until(conn: psycopg.Connection, origin: str) -> datetime | None:
    row = conn.execute(
        "SELECT blocked_until FROM origin_backoff WHERE origin = %s AND blocked_until > now()", (origin,)
    ).fetchone()
    return row["blocked_until"] if row else None


def set_backoff(conn: psycopg.Connection, origin: str, seconds: float, reason: str) -> None:
    conn.execute(
        """INSERT INTO origin_backoff (origin, blocked_until, reason, updated_at)
           VALUES (%s, now() + make_interval(secs => %s), %s, now())
           ON CONFLICT (origin) DO UPDATE SET blocked_until = EXCLUDED.blocked_until, reason = EXCLUDED.reason,
             updated_at = now()""",
        (origin, float(seconds), reason[:300]),
    )


# ---------------------------------------------------------------------------
# Configuration persistence
# ---------------------------------------------------------------------------


def effective_page(row: dict[str, Any]) -> dict[str, Any] | None:
    """Last successful page verification usable for the current index observation.

    A page check is ignored when the index price changed since the check
    (configuration may have changed). Otherwise the previous verification is
    kept as evidence even when it has aged past its ``fresh_until`` window or
    the latest attempt failed/was blocked; queries treat such evidence as
    stale (never a match) and expose its actual verification time.
    """
    page = row.get("page_claims")
    if not page or page.get("status") != "verified":
        return None
    if page.get("index_price_at_check") != row["source_record"].get("item_price"):
        return None
    return page


def recompute(conn: psycopg.Connection, row: dict[str, Any], catalog: dict[str, dict[str, Any]], source: str) -> None:
    record = row["source_record"]
    page = effective_page(row)
    configs, listing = domain.build_configurations(
        record, catalog.get(record.get("model") or ""), row.get("description_claims"), page
    )
    if page and row.get("page_status") in ("blocked", "failed"):
        # Latest page attempt did not succeed: report it rather than the older verification.
        listing["detail_status"] = row["page_status"]
    existing = {
        r["variant_key"]: r
        for r in conn.execute(
            "SELECT id, variant_key, item_price, shipping_price, price_kind, active FROM configurations "
            "WHERE listing_id = %s AND origin = 'source'",
            (row["id"],),
        ).fetchall()
    }
    keys = []
    for c in configs:
        keys.append(c["variant_key"])
        params = {**c, "listing_id": row["id"], "evidence": _json(c["evidence"]), "warnings": _json(c["warnings"]),
                  "exclusions": _json(c["exclusions"]), "essential_states": _json(c["essential_states"])}
        cid = conn.execute(
            """INSERT INTO configurations (listing_id, variant_key, variant_id, variant_label, active, model, cpu_model, cpu_key,
                 cpu_model_state, cpu_count, cpu_count_state, total_cores, ram_gb, ram_state, drive_bays, bays_state, drive_size,
                 size_state, storage_state, storage_conf, hardware_state, damage, price_kind, item_price, shipping_price, currency,
                 shipping_context, evidence, warnings, exclusions, essential_states, observed_at)
               VALUES (%(listing_id)s, %(variant_key)s, %(variant_id)s, %(variant_label)s, true, %(model)s, %(cpu_model)s, %(cpu_key)s,
                 %(cpu_model_state)s, %(cpu_count)s, %(cpu_count_state)s, %(total_cores)s, %(ram_gb)s, %(ram_state)s, %(drive_bays)s,
                 %(bays_state)s, %(drive_size)s, %(size_state)s, %(storage_state)s, %(storage_conf)s, %(hardware_state)s, %(damage)s,
                 %(price_kind)s, %(item_price)s, %(shipping_price)s, %(currency)s, %(shipping_context)s, %(evidence)s, %(warnings)s,
                 %(exclusions)s, %(essential_states)s, now())
               ON CONFLICT (listing_id, variant_key) DO UPDATE SET variant_id = EXCLUDED.variant_id,
                 variant_label = EXCLUDED.variant_label, active = true, model = EXCLUDED.model, cpu_model = EXCLUDED.cpu_model,
                 cpu_key = EXCLUDED.cpu_key, cpu_model_state = EXCLUDED.cpu_model_state, cpu_count = EXCLUDED.cpu_count,
                 cpu_count_state = EXCLUDED.cpu_count_state, total_cores = EXCLUDED.total_cores, ram_gb = EXCLUDED.ram_gb,
                 ram_state = EXCLUDED.ram_state, drive_bays = EXCLUDED.drive_bays, bays_state = EXCLUDED.bays_state,
                 drive_size = EXCLUDED.drive_size, size_state = EXCLUDED.size_state, storage_state = EXCLUDED.storage_state,
                 storage_conf = EXCLUDED.storage_conf, hardware_state = EXCLUDED.hardware_state, damage = EXCLUDED.damage,
                 price_kind = EXCLUDED.price_kind, item_price = EXCLUDED.item_price, shipping_price = EXCLUDED.shipping_price,
                 currency = EXCLUDED.currency, shipping_context = EXCLUDED.shipping_context, evidence = EXCLUDED.evidence,
                 warnings = EXCLUDED.warnings, exclusions = EXCLUDED.exclusions,
                 essential_states = EXCLUDED.essential_states,
                 revision = configurations.revision + CASE WHEN
                   (configurations.active, configurations.variant_id, configurations.variant_label,
                    configurations.model, configurations.cpu_model, configurations.cpu_key, configurations.cpu_model_state,
                    configurations.cpu_count, configurations.cpu_count_state, configurations.total_cores,
                    configurations.ram_gb, configurations.ram_state, configurations.drive_bays, configurations.bays_state,
                    configurations.drive_size, configurations.size_state, configurations.storage_state,
                    configurations.storage_conf, configurations.hardware_state, configurations.damage,
                    configurations.price_kind, configurations.item_price, configurations.shipping_price,
                    configurations.shipping_context, configurations.evidence, configurations.warnings,
                    configurations.exclusions, configurations.essential_states)
                   IS DISTINCT FROM
                   (EXCLUDED.active, EXCLUDED.variant_id, EXCLUDED.variant_label,
                    EXCLUDED.model, EXCLUDED.cpu_model, EXCLUDED.cpu_key, EXCLUDED.cpu_model_state,
                    EXCLUDED.cpu_count, EXCLUDED.cpu_count_state, EXCLUDED.total_cores,
                    EXCLUDED.ram_gb, EXCLUDED.ram_state, EXCLUDED.drive_bays, EXCLUDED.bays_state,
                    EXCLUDED.drive_size, EXCLUDED.size_state, EXCLUDED.storage_state,
                    EXCLUDED.storage_conf, EXCLUDED.hardware_state, EXCLUDED.damage,
                    EXCLUDED.price_kind, EXCLUDED.item_price, EXCLUDED.shipping_price,
                    EXCLUDED.shipping_context, EXCLUDED.evidence, EXCLUDED.warnings,
                    EXCLUDED.exclusions, EXCLUDED.essential_states)
                   THEN 1 ELSE 0 END, observed_at = now()
               WHERE configurations.origin = 'source'
               RETURNING id""",
            params,
        ).fetchone()["id"]
        prev = existing.get(c["variant_key"])
        if (prev is None or not prev["active"] or prev["item_price"] != c["item_price"]
                or prev["shipping_price"] != c["shipping_price"] or prev["price_kind"] != c["price_kind"]):
            conn.execute(
                """INSERT INTO price_history (configuration_id, price_kind, item_price, shipping_price, source)
                   VALUES (%s, %s, %s, %s, %s)""",
                (cid, c["price_kind"], c["item_price"], c["shipping_price"], source),
            )
    conn.execute(
        "UPDATE configurations SET active = false, revision = revision + 1 "
        "WHERE listing_id = %s AND origin = 'source' AND active AND NOT (variant_key = ANY(%s))",
        (row["id"], keys),
    )
    conn.execute(
        """UPDATE listings SET detail_status = %(detail_status)s, seller = coalesce(%(seller)s, seller),
             condition = coalesce(%(condition)s, condition),
             ended_at = CASE WHEN %(ended)s THEN coalesce(ended_at, now()) ELSE ended_at END,
             ended_evidence = CASE WHEN %(ended)s THEN %(ended_text)s ELSE ended_evidence END
           WHERE id = %(id)s""",
        {**listing, "id": row["id"]},
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@dataclass
class PageResult:
    """Outcome of one vendor index page within a discovery run."""

    vendor: str
    url: str
    snapshot: IndexSnapshot | None = None
    outcome: str | None = None  # success | partial | blocked | failed
    message: str | None = None


def apply_page(conn: psycopg.Connection, page: PageResult, catalog: dict[str, dict[str, Any]], seen: set[str]) -> int:
    """Upsert one vendor page's records and recompute configurations. Caller owns the transaction.

    Listings already applied from an earlier page in this run (``seen``) are
    skipped so each item is attributed to exactly one index page. Returns the
    number of such cross-page duplicates.
    """
    snapshot = page.snapshot
    assert snapshot is not None
    duplicates = 0
    for rec in snapshot.records:
        if rec["item_id"] in seen:
            duplicates += 1
            continue
        seen.add(rec["item_id"])
        record = _jsonable({**rec, "index_url": page.url})
        row = conn.execute(
            """INSERT INTO listings (provider, item_id, canonical_url, original_url, title, condition, model, vendor,
                 index_url, source_record, last_seen_source_at)
               VALUES (%(provider)s, %(item_id)s, %(canonical_url)s, %(original_url)s, %(title)s, %(condition)s, %(model)s,
                 %(vendor)s, %(index_url)s, %(record)s, %(upstream)s)
               ON CONFLICT (item_id) DO UPDATE SET canonical_url = EXCLUDED.canonical_url,
                 original_url = EXCLUDED.original_url, title = EXCLUDED.title, model = EXCLUDED.model,
                 vendor = EXCLUDED.vendor, index_url = EXCLUDED.index_url,
                 condition = coalesce(listings.condition, EXCLUDED.condition), source_record = EXCLUDED.source_record,
                 last_seen_at = now(), last_seen_source_at = greatest(listings.last_seen_source_at, EXCLUDED.last_seen_source_at),
                 archived_at = NULL
               RETURNING id, source_record, description_claims, page_claims, page_status""",
            {
                "provider": PROVIDER, "item_id": rec["item_id"], "canonical_url": rec["source_url"],
                "original_url": rec["original_url"], "title": rec["title"], "condition": rec["condition"],
                "model": rec["model"], "vendor": rec.get("vendor"), "index_url": page.url, "record": _json(record),
                "upstream": snapshot.updated_at,
            },
        ).fetchone()
        recompute(conn, row, catalog, "index")
    return duplicates


def archive_absent(conn: psycopg.Connection, page: PageResult) -> int:
    """Archive listings last attributed to this page and absent for 7 days of its upstream time.

    Runs only for complete page snapshots: when the parser rejected any record,
    an absent listing may simply be one of the rejected rows. Listings of other
    pages (possibly failing or stale) are never archived by this page's absence.
    """
    snapshot = page.snapshot
    assert snapshot is not None
    if snapshot.rejected:
        return 0
    return conn.execute(
        """UPDATE listings SET archived_at = now()
           WHERE archived_at IS NULL AND index_url = %s AND last_seen_source_at < %s""",
        (page.url, snapshot.updated_at - ARCHIVE_AFTER),
    ).rowcount


def record_page(conn: psycopg.Connection, page: PageResult) -> None:
    snapshot = page.snapshot if page.outcome in ("success", "partial") else None
    conn.execute(
        """INSERT INTO source_pages (url, vendor, last_attempt_at, last_outcome, last_message, upstream_updated_at,
             last_success_at, record_count)
           VALUES (%(url)s, %(vendor)s, now(), %(outcome)s, %(message)s, %(upstream)s,
             CASE WHEN %(outcome)s = 'success' THEN now() END, %(count)s)
           ON CONFLICT (url) DO UPDATE SET vendor = EXCLUDED.vendor, last_attempt_at = now(),
             last_outcome = EXCLUDED.last_outcome, last_message = EXCLUDED.last_message,
             upstream_updated_at = coalesce(EXCLUDED.upstream_updated_at, source_pages.upstream_updated_at),
             last_success_at = coalesce(EXCLUDED.last_success_at, source_pages.last_success_at),
             record_count = coalesce(EXCLUDED.record_count, source_pages.record_count)""",
        {"url": page.url, "vendor": page.vendor, "outcome": page.outcome,
         "message": (page.message or None) and page.message[:500],
         "upstream": snapshot.updated_at if snapshot else None,
         "count": len(snapshot.records) if snapshot else None},
    )


def run_discovery(conn: psycopg.Connection, settings: Settings, fetcher: HttpFetcher) -> str:
    """Fetch every fixed vendor index page and apply the valid ones atomically.

    Each page keeps its own freshness, failure state and absence archiving, so
    a fresh page never refreshes or archives another vendor's last-good data. A
    block stops further requests to the shared origin for this run.
    """
    run_id = start_run(conn, "discovery")
    pages = [PageResult(vendor, url) for vendor, url in settings.source_pages]
    until = backoff_until(conn, INDEX_ORIGIN)
    if until:
        message = f"Backing off index origin until {until.isoformat()}"
        with conn.transaction():
            for page in pages:
                page.outcome, page.message = "blocked", message
                record_page(conn, page)
            finish_run(conn, run_id, "blocked", message)
        log.warning("discovery skipped: origin backoff until %s", until.isoformat())
        return "blocked"

    block: SourceBlocked | None = None
    for page in pages:
        if block is not None:
            page.outcome, page.message = "blocked", f"not requested after origin block: {block}"
            continue
        try:
            snapshot = fetcher.fetch_index(page.url, settings.max_index_bytes)
            matching = [record for record in snapshot.records if record.get("vendor") == page.vendor]
            mismatched = len(snapshot.records) - len(matching)
            if mismatched:
                if not matching:
                    raise SourceError(f"{page.vendor} page contains only other or unknown vendors")
                snapshot = replace(snapshot, records=matching, rejected=snapshot.rejected + mismatched)
            page.snapshot = snapshot
        except SourceBlocked as exc:
            block = exc
            page.outcome, page.message = "blocked", str(exc)
        except SourceError as exc:
            page.outcome, page.message = "failed", str(exc)
    if block is not None:
        with conn.transaction():
            set_backoff(conn, INDEX_ORIGIN, block.retry_after or settings.recheck_cooldown_hours * 3600, str(block))

    catalog = db.catalog(conn)
    notes: list[str] = []
    try:
        with conn.transaction():
            stored = {r["url"]: r["upstream_updated_at"]
                      for r in conn.execute("SELECT url, upstream_updated_at FROM source_pages").fetchall()}
            seen: set[str] = set()
            cross_duplicates = 0
            unchanged = True
            for page in pages:
                if page.snapshot is None:
                    continue
                snapshot = page.snapshot
                prev = stored.get(page.url)
                if prev is not None and snapshot.updated_at < prev:
                    page.outcome = "failed"
                    page.message = (f"Upstream snapshot {snapshot.updated_at.isoformat()} older than stored "
                                    f"{prev.isoformat()}; ignored")
                    page.snapshot = None
                    continue
                unchanged = unchanged and prev is not None and snapshot.updated_at == prev
                cross_duplicates += apply_page(conn, page, catalog, seen)
                page_archived = archive_absent(conn, page)
                detail = [f"{len(snapshot.records)} listings"]
                if snapshot.duplicates:
                    detail.append(f"{snapshot.duplicates} duplicate item ids merged")
                if snapshot.rejected:
                    detail.append(f"{snapshot.rejected} invalid records rejected; absence-based archiving skipped")
                if page_archived:
                    detail.append(f"{page_archived} archived after 7 days absent")
                page.outcome = "partial" if snapshot.rejected else "success"
                page.message = "; ".join(detail)
            applied = [p for p in pages if p.snapshot is not None]
            for page in pages:
                record_page(conn, page)
                notes.append(f"{page.vendor}: {page.outcome if page.outcome != 'success' else 'ok'}"
                             + (f" ({page.message})" if page.message else ""))
            if cross_duplicates:
                notes.append(f"{cross_duplicates} listings on several vendor pages merged")
            if applied and unchanged:
                notes.append("upstream timestamp unchanged")
            if not applied:
                outcome = "blocked" if any(p.outcome == "blocked" for p in pages) else "failed"
            elif all(p.outcome == "success" for p in pages):
                outcome = "success"
            else:
                outcome = "partial"
            finish_run(conn, run_id, outcome, "; ".join(notes),
                       upstream=min((p.snapshot.updated_at for p in applied), default=None),
                       discovered=len(seen))
    except psycopg.Error:
        log.exception("discovery snapshot transaction failed; previous inventory retained")
        with conn.transaction():
            finish_run(conn, run_id, "failed", "Database error while applying snapshot; previous inventory retained")
        return "failed"
    log.info("discovery %s: %s", outcome, "; ".join(notes))
    return outcome


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


# Bounded enrichment queue. All lanes share the same eligibility (active and past
# cooldown); target chassis are cataloged 1U models of the modern era, for every vendor.
_CANDIDATE_COLUMNS = """
SELECT l.id, l.item_id, l.source_record, l.description_claims, l.description_status, l.description_checked_at,
       l.page_claims, l.page_status
FROM listings l
LEFT JOIN server_models m ON m.model = l.model
WHERE l.archived_at IS NULL AND l.ended_at IS NULL
  AND (l.next_check_after IS NULL OR l.next_check_after <= now())
"""
_TARGET = "(m.target_era AND m.rack_units = 1)"
_PRICE = "(l.source_record->>'item_price')::numeric"
# Lane 1: cheapest previously verified targets first; the oldest lane prevents starvation.
VERIFIED_TARGETS_SQL = f"""{_CANDIDATE_COLUMNS}
  AND {_TARGET} AND l.page_claims->>'status' = 'verified'
ORDER BY {_PRICE} ASC NULLS LAST,
  coalesce((l.page_claims->>'fresh_until')::timestamptz, (l.page_claims->>'checked_at')::timestamptz) ASC NULLS FIRST,
  l.id ASC
LIMIT %s
"""
# Lane 2: never-checked targets, cheapest first.
NEW_TARGETS_SQL = f"""{_CANDIDATE_COLUMNS}
  AND {_TARGET} AND l.last_checked_at IS NULL
ORDER BY {_PRICE} ASC NULLS LAST, l.first_seen_at ASC, l.id ASC
LIMIT %s
"""
# Lane 3: every candidate by age of its last check (or first sighting when never checked),
# so a stream of new cheap listings cannot starve older ones.
OLDEST_SQL = f"""{_CANDIDATE_COLUMNS}
ORDER BY coalesce(l.last_checked_at, l.first_seen_at) ASC, l.id ASC
LIMIT %s
"""


def select_candidates(conn: psycopg.Connection, budget: int) -> list[dict[str, Any]]:
    """Pick at most ``budget`` due listings for detail checks, deterministically.

    Budgets of 3+ reserve up to (budget-1)//2 slots each for due rechecks of
    verified target pages and for never-checked targets, and the rest (at least
    one) for the globally oldest candidates; quota a lane cannot use is filled
    from the oldest lane. Short budgets trade fairness for keeping verified
    matches current: 1 checks the cheapest due verified target (else the oldest
    candidate) and 2 adds one never-checked target (else the oldest), so the
    oldest lane only progresses through unused quota there. Starvation-free
    progress for every lane is guaranteed from a budget of 3.
    """
    if budget <= 0:
        return []
    lane_quota = (budget - 1) // 2 if budget >= 3 else 1
    lanes = [
        (conn.execute(VERIFIED_TARGETS_SQL, (budget,)).fetchall(), lane_quota),
        (conn.execute(NEW_TARGETS_SQL, (budget,)).fetchall(), lane_quota if budget >= 2 else 0),
    ]
    oldest = conn.execute(OLDEST_SQL, (budget,)).fetchall()
    lanes.append((oldest, budget))
    chosen: dict[int, dict[str, Any]] = {}
    for rows, quota in lanes:
        for row in rows:
            if quota <= 0 or len(chosen) >= budget:
                break
            if row["id"] not in chosen:
                chosen[row["id"]] = row
                quota -= 1
    # Unused lane quota (budget 1 with no verified target, or overlapping lanes).
    for rows, _ in (lanes[2], lanes[0], lanes[1]):
        for row in rows:
            if len(chosen) >= budget:
                break
            chosen.setdefault(row["id"], row)
    return list(chosen.values())


def run_enrichment(conn: psycopg.Connection, settings: Settings, fetcher: HttpFetcher, max_details: int,
                   browser_enabled: bool, *, target_listing_id: int | None = None) -> str:
    if max_details <= 0:
        return "skipped"
    run_id = start_run(conn, "enrichment")
    if target_listing_id is None:
        candidates = select_candidates(conn, max_details)
    else:
        candidates = conn.execute(_CANDIDATE_COLUMNS + " AND l.id = %s LIMIT 1", (target_listing_id,)).fetchall()
    conn.commit()
    catalog = db.catalog(conn)
    cooldown = timedelta(hours=settings.recheck_cooldown_hours)
    verification_window = timedelta(hours=settings.page_verification_hours)
    desc_blocked = backoff_until(conn, DESCRIPTION_HOST) is not None
    page_blocked = backoff_until(conn, EBAY_HOST) is not None
    browser_note = None if browser_enabled else "browser checks disabled"
    checker = None
    if browser_enabled and not page_blocked:
        from .browser import BrowserChecker

        checker = BrowserChecker(settings.browser_timeout, settings.max_variants)
    attempted = enriched = blocked = failed = 0
    try:
        for cand in candidates:
            # Quiet gap after each listing, including cached descriptions and browser backoffs.
            if attempted:
                time.sleep(settings.request_delay)
            attempted += 1
            item_id = cand["item_id"]
            messages: list[str] = []
            outcomes: list[str] = []
            desc_claims = cand["description_claims"]
            desc_status = cand["description_status"]
            desc_checked = cand["description_checked_at"]
            desc_due = desc_status not in ("verified", "absent") or desc_checked is None or desc_checked < _now() - DESCRIPTION_REFRESH
            if desc_due and not desc_blocked:
                try:
                    parsed = fetcher.fetch_description(item_id, settings.max_description_bytes)
                    desc_checked = _now()
                    if parsed is None:
                        desc_status, desc_claims = "absent", None
                        outcomes.append("absent")
                        messages.append("no seller description")
                    else:
                        desc_status = "verified"
                        desc_claims = {**parsed, "status": "verified", "checked_at": desc_checked.isoformat()}
                        outcomes.append("verified")
                except SourceBlocked as exc:
                    desc_blocked = True
                    desc_status, desc_checked = "blocked", _now()
                    outcomes.append("blocked")
                    messages.append(str(exc))
                    with conn.transaction():
                        set_backoff(conn, DESCRIPTION_HOST, exc.retry_after or cooldown.total_seconds(), str(exc))
                except SourceError as exc:
                    desc_status, desc_checked = "failed", _now()
                    outcomes.append("failed")
                    messages.append(str(exc))
            elif desc_blocked and desc_due:
                outcomes.append("blocked")
                messages.append("description origin backing off")

            page_claims = cand["page_claims"]
            page_status = cand["page_status"]
            page_checked = None
            if checker is not None and not page_blocked:
                page_checked = _now()
                try:
                    result = checker.check(item_id)
                    # checked_at is the successful verification time; it is never refreshed by later
                    # failed/blocked/skipped attempts (page_checked_at records the latest attempt).
                    page_claims = {**result, "checked_at": page_checked.isoformat(),
                                   "fresh_until": (page_checked + verification_window).isoformat(),
                                   "index_price_at_check": cand["source_record"].get("item_price")}
                    page_status = "verified"
                    outcomes.append("verified")
                except SourceBlocked as exc:
                    page_blocked = True
                    page_status = "blocked"
                    outcomes.append("blocked")
                    messages.append(str(exc))
                    with conn.transaction():
                        set_backoff(conn, EBAY_HOST, exc.retry_after or cooldown.total_seconds(), str(exc))
                except SourceError as exc:
                    page_status = "failed"
                    outcomes.append("failed")
                    messages.append(str(exc))
                except Exception as exc:  # noqa: BLE001 - Playwright errors (launch/sandbox/browser crash)
                    if not _is_playwright_error(exc):
                        raise
                    page_status = "failed"
                    outcomes.append("failed")
                    messages.append(f"browser error: {type(exc).__name__}")
                    log.warning("browser check failed for %s: %s", item_id, type(exc).__name__)
                    if not checker.started:
                        browser_note = "browser unavailable; checks disabled for this run"
                        checker.close()
                        checker = None
            elif browser_enabled and page_blocked:
                messages.append("eBay page origin backing off")

            if "verified" in outcomes:
                enriched += 1
            if outcomes and all(o == "blocked" for o in outcomes):
                blocked += 1
            elif "failed" in outcomes or "blocked" in outcomes:
                failed += 1

            with conn.transaction():
                conn.execute(
                    """UPDATE listings SET description_claims = %(dc)s, description_status = %(ds)s,
                         description_checked_at = %(dt)s, page_claims = %(pc)s, page_status = %(ps)s,
                         page_checked_at = coalesce(%(pt)s, page_checked_at), last_checked_at = now(),
                         next_check_after = now() + %(cooldown)s, check_message = %(msg)s
                       WHERE id = %(id)s""",
                    {"dc": _json(desc_claims) if desc_claims else None, "ds": desc_status, "dt": desc_checked,
                     "pc": _json(page_claims) if page_claims else None, "ps": page_status, "pt": page_checked,
                     "cooldown": cooldown, "msg": "; ".join(messages)[:500] or None, "id": cand["id"]},
                )
                row = conn.execute(
                    "SELECT id, source_record, description_claims, page_claims, page_status FROM listings WHERE id = %s",
                    (cand["id"],),
                ).fetchone()
                recompute(conn, row, catalog, "enrichment")
    finally:
        if checker is not None:
            checker.close()

    if attempted == 0:
        outcome, message = "success", "no listings due for detail checks"
    elif blocked == attempted:
        outcome, message = "blocked", f"{attempted} checks blocked"
    elif blocked or failed:
        outcome, message = "partial", f"{enriched} of {attempted} enriched; {blocked} blocked; {failed} with failures"
    elif checker is None and attempted:
        outcome = "partial"
        message = (f"{enriched} of {attempted} seller descriptions checked; eBay page/variation verification "
                   f"not performed ({browser_note or 'eBay page origin backing off'}); prices remain base/unverified")
    else:
        outcome, message = "success", f"{enriched} of {attempted} enriched"
    if browser_note and browser_note not in message:
        message = f"{message}; {browser_note}"
    if page_blocked and browser_enabled and "backing off" not in message:
        message = f"{message}; eBay page checks backing off"
    with conn.transaction():
        finish_run(conn, run_id, outcome, message, enriched=enriched)
    log.info("enrichment %s: %s", outcome, message)
    return outcome


def _is_playwright_error(exc: Exception) -> bool:
    try:
        from playwright.sync_api import Error as PlaywrightError
    except ImportError:
        return False
    return isinstance(exc, PlaywrightError)


def prune_history(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute("DELETE FROM price_history WHERE observed_at < now() - interval '90 days'")


class SchemaNotMigrated(RuntimeError):
    """The database schema is older than this release; ``migrate`` must run first."""


@dataclass(frozen=True)
class CollectionResult:
    """Outcomes this collection recorded in ``source_runs`` (enrichment may also be ``skipped``)."""

    discovery: str
    enrichment: str


def try_collector_lock(lock_conn: psycopg.Connection) -> bool:
    """Take the global collector session lock on an autocommit connection without waiting."""
    return bool(lock_conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (db.COLLECTOR_LOCK_KEY,)).fetchone()["ok"])


def run_collection(settings: Settings, max_details: int | None = None, browser: bool | None = None,
                   fetcher: HttpFetcher | None = None, *, target_listing_id: int | None = None) -> CollectionResult:
    """Run full collection, or only one selected listing, while caller holds the collector lock.

    Opens and closes its own work connection. A fetcher created here is closed
    here; a caller-supplied fetcher stays the caller's.
    """
    # Autocommit: every `conn.transaction()` block is a real, atomic transaction.
    conn = db.connect(settings.db_connect_timeout, autocommit=True)
    owned = fetcher is None
    try:
        if fetcher is None:
            fetcher = HttpFetcher(settings.request_timeout, settings.request_delay)
        if not db.schema_ready(conn):
            raise SchemaNotMigrated("database schema not migrated")
        budget = settings.max_details if max_details is None else max_details
        if target_listing_id is None:
            discovery = run_discovery(conn, settings, fetcher)
        else:
            discovery = "skipped"
            budget = min(settings.max_details, budget, 1)
        enrichment_options = {} if target_listing_id is None else {"target_listing_id": target_listing_id}
        enrichment = run_enrichment(
            conn, settings, fetcher, budget,
            settings.browser_enabled if browser is None else (browser and settings.browser_enabled),
            **enrichment_options,
        )
        prune_history(conn)
        return CollectionResult(discovery, enrichment)
    finally:
        if owned and fetcher is not None:
            fetcher.close()
        conn.close()


def collect(settings: Settings, max_details: int | None = None, browser: bool | None = None) -> int:
    """Run one scheduled collection unless another collector holds the lock. Returns process exit code."""
    lock_conn = db.connect(settings.db_connect_timeout, autocommit=True)
    try:
        if not try_collector_lock(lock_conn):
            log.warning("another collector holds the lock; exiting")
            return 0
        try:
            result = run_collection(settings, max_details=max_details, browser=browser)
        except SchemaNotMigrated:
            log.error("database schema not migrated; run `python -m rackscout migrate`")
            return 1
        # A partial snapshot applied its valid records; its degraded state is visible in /api/status.
        return 0 if result.discovery in ("success", "partial") else 1
    finally:
        # Closing the session releases the advisory lock.
        lock_conn.close()
