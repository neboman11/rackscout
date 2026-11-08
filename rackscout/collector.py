"""Scheduled collector: atomic index/catalog discovery plus bounded incremental enrichment."""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import partial
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import db, domain, hardware_platforms, providers, retail_sources
from .collection_session import CollectionSession
from .config import DESCRIPTION_HOST, EBAY_HOST, Settings
from .sources import HttpFetcher, IndexSnapshot, SourceBlocked, SourceError

log = logging.getLogger(__name__)

PROVIDER = providers.EBAY
# Discovery/enrichment runs cover every enabled provider; per-source state lives in source_pages.
RUN_PROVIDER = "all"
INDEX_ORIGIN = "perunitprice.com"
ARCHIVE_AFTER = timedelta(days=7)
DESCRIPTION_REFRESH = timedelta(hours=24)
# Listing columns ``recompute`` (and manual baseline reconstruction) build configurations from.
RECOMPUTE_COLUMNS = "id, provider, source_record, description_claims, page_claims, page_status, quote_status"
# Retailer native product ids: ASCII digits only (str.isdigit also accepts other Unicode digits).
NATIVE_ID_RE = re.compile(r"[0-9]{1,20}")
# Explicit memory statements of one source configuration. eBay index field: exactly DDR3/DDR4/DDR5 (any
# case) apart from surrounding spaces/tabs. Retailers: the native variant's own memory-named labels
# (domain.RAM_LABEL_RE: Memory, RAM, Memory Size, DIMMs, ...) or, without one, the installed text's
# memory-named fields; capacity/maximum statements, descriptions and marketing text never count.
# Module generations: DDRn, DDR3L, PCn, PC3L. Several distinct generations are a conflict.
_INDEX_MEMORY_TYPE_RE = re.compile(r"[ \t]*(ddr[345])[ \t]*", re.IGNORECASE)
_MEMORY_GENERATION_RE = re.compile(r"(?<![a-z0-9])(?:ddr|pc)([345])l?(?![a-z0-9])", re.IGNORECASE)
_LABELLED_FIELD_RE = re.compile(r"\s*([^:]{1,40}?)\s*:\s*(.+)", re.DOTALL)


def _memory_generation(texts: list[str]) -> tuple[str | None, bool]:
    found = {m.group(1) for text in texts for m in _MEMORY_GENERATION_RE.finditer(text)}
    if len(found) > 1:
        return None, True
    return (f"DDR{found.pop()}" if found else None), False


def listing_memory(provider: str, record: dict[str, Any], variant_key: str) -> tuple[str | None, bool]:
    """(memory type, conflicting) explicitly stated for one source configuration.

    The type is set only for exactly one stated generation; ``conflicting`` when the configuration's own
    memory statement names several. Neither is ever inferred from CPU, chassis or capacity text.
    """
    if provider == providers.EBAY:
        raw = record.get("memory_type")
        if not isinstance(raw, str):
            return None, False
        if m := _INDEX_MEMORY_TYPE_RE.fullmatch(raw):
            return m.group(1).upper(), False
        return None, _memory_generation([raw])[1]
    retail = record.get("retail")
    if not isinstance(retail, dict):
        return None, False
    for v in retail.get("variants") or []:
        if isinstance(v, dict) and variant_key and v.get("variant_id") == variant_key:
            named = [text for name, text in (v.get("labels") or {}).items()
                     if isinstance(name, str) and isinstance(text, str) and domain.RAM_LABEL_RE.search(name)]
            if named:
                # The option's own memory replaces the product's installed memory, generation named or not.
                return _memory_generation(named)
            break
    installed = retail.get("installed_text")
    if not isinstance(installed, str):
        return None, False
    fields = []
    for segment in domain.RETAIL_SEGMENT_RE.split(installed):
        m = _LABELLED_FIELD_RE.fullmatch(segment) if segment else None
        if m and domain.RAM_LABEL_RE.search(m.group(1)) and not domain.CAPACITY_RE.search(segment):
            fields.append(m.group(2))
    return _memory_generation(fields)


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


# Message of a run a stopped collector left open, finalized by the next collection under the lock. It also
# fences the stopped collector: a run finalized this way is never resumed (``Checkpoints.owns``).
ORPHAN_RUN_MESSAGE = "Collector stopped before finishing this run; finalized by a later collection"


def finalize_interrupted_runs(conn: psycopg.Connection) -> None:
    """Mark every unfinished source run failed. DB-only; caller holds the global collector lock and the transaction.

    With the lock held no collector is running, so an open run belongs to a stopped one.
    """
    conn.execute(
        "UPDATE source_runs SET finished_at = now(), outcome = 'failed', message = %s WHERE finished_at IS NULL",
        (ORPHAN_RUN_MESSAGE,))


class Checkpoints:
    """Database checkpoints of one collection.

    Wraps a ``CollectionSession`` (collector lock verified before every checkpoint, transient
    connection failures reconnected and the lock reacquired, then only the DB callback retried) or a
    raw connection (one plain transaction per checkpoint, no recovery). ``run`` callbacks are DB-only
    and atomic and may execute more than once: they derive everything from in-memory acquisition
    results and reset whatever state they mutate. Source and browser requests happen only between
    checkpoints, each after ``check``, and are never repeated by a retry.

    Run identity is stable under an ambiguous commit: the id is reserved first, then inserted with
    ``ON CONFLICT DO NOTHING``. With a session, ``begin`` arms a persisted ownership fence: after a
    reconnect the session calls ``owns`` under the reacquired lock, and a newer collection that
    created runs, or finalized this collection's open run as orphaned, stops this one from resuming.
    """

    def __init__(self, target: CollectionSession | psycopg.Connection) -> None:
        self._conn = target if isinstance(target, psycopg.Connection) else None
        self.session = None if self._conn is not None else target
        self._high_water = 0  # highest source_runs id this collection has seen or reserved
        self._run_id: int | None = None  # this collection's latest run
        self._complete = False

    def run(self, callback: Callable[[psycopg.Connection], Any]) -> Any:
        if self.session is not None:
            return self.session.run(callback)
        with self._conn.transaction():
            return callback(self._conn)

    def check(self) -> None:
        """Network boundary: confirm (or recover) collector ownership before a source/browser request."""
        if self.session is not None:
            self.session.check()

    def begin(self) -> None:
        """Finalize runs a stopped collector left open and arm the run fence. Caller holds the collector lock."""
        def begin(conn: psycopg.Connection) -> int:
            finalize_interrupted_runs(conn)
            return conn.execute("SELECT coalesce(max(id), 0) AS id FROM source_runs").fetchone()["id"]

        self._high_water = self.run(begin)
        if self.session is not None:
            self.session.add_resume_guard(self.owns)

    def owns(self, conn: psycopg.Connection) -> bool:
        """Resume guard: no newer collection ran while this one was disconnected (DB-only, under the lock)."""
        if self._complete:
            return True  # every source phase finished; the caller's own bookkeeping may continue
        row = conn.execute(
            """SELECT EXISTS (SELECT 1 FROM source_runs WHERE id > %(seen)s) AS superseded,
                 EXISTS (SELECT 1 FROM source_runs WHERE id = %(run)s AND finished_at IS NOT NULL
                         AND message = %(orphan)s) AS orphaned""",
            {"seen": self._high_water, "run": self._run_id, "orphan": ORPHAN_RUN_MESSAGE},
        ).fetchone()
        if row["superseded"] or row["orphaned"]:
            log.error("a newer collection ran while this collector was disconnected; not resuming")
            return False
        return True

    def start_run(self, kind: str) -> int:
        run_id = self.run(
            lambda conn: conn.execute("SELECT nextval(pg_get_serial_sequence('source_runs', 'id')) AS id").fetchone()["id"])
        # Fence first: a retried INSERT is only resumed while no newer collection holds a later id.
        self._run_id, self._high_water = run_id, max(self._high_water, run_id)
        self.run(lambda conn: conn.execute(
            "INSERT INTO source_runs (id, provider, kind) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (run_id, RUN_PROVIDER, kind)))
        return run_id

    def complete(self) -> None:
        """Every source phase finished: later checkpoints (caller bookkeeping) no longer need the run fence."""
        self._complete = True

    def abandon(self, message: str) -> None:
        """Best effort: mark this collection's still-open run failed after an unexpected error."""
        run_id = self._run_id
        if run_id is None:
            return
        try:
            self.run(lambda conn: conn.execute(
                """UPDATE source_runs SET finished_at = now(), outcome = 'failed', message = %s
                   WHERE id = %s AND finished_at IS NULL""", (message[:500], run_id)))
        except (psycopg.Error, db.DatabaseUnavailable) as exc:
            log.warning("could not mark run %s failed (%s); the next collection finalizes it", run_id,
                        type(exc).__name__)


def _checkpoints(target: psycopg.Connection | Checkpoints) -> Checkpoints:
    return target if isinstance(target, Checkpoints) else Checkpoints(target)


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


def set_backoff(conn: psycopg.Connection, origin: str, seconds: float, reason: str, *,
                observed_at: datetime | None = None) -> None:
    """Back off ``origin`` until ``observed_at`` (default: now) plus ``seconds``; never shortens an active backoff.

    A fixed observation time keeps a retried checkpoint from moving the expiry.
    """
    conn.execute(
        """INSERT INTO origin_backoff (origin, blocked_until, reason, updated_at)
           VALUES (%s, coalesce(%s::timestamptz, now()) + make_interval(secs => %s), %s, now())
           ON CONFLICT (origin) DO UPDATE SET
             blocked_until = greatest(origin_backoff.blocked_until, EXCLUDED.blocked_until),
             reason = EXCLUDED.reason, updated_at = now()""",
        (origin, observed_at, float(seconds), reason[:300]),
    )


# ---------------------------------------------------------------------------
# Configuration persistence
# ---------------------------------------------------------------------------


def recompute(conn: psycopg.Connection, row: dict[str, Any], catalog: dict[str, dict[str, Any]], source: str) -> None:
    """Rebuild the listing's source configurations from its stored observations. Caller owns the transaction.

    ``row`` has ``RECOMPUTE_COLUMNS``. The stored observation is authoritative: native variants an
    incomplete retailer acquisition did not see are retained in it (``providers.merge_incomplete``),
    so every rebuild keeps them. A retailer quote's freshness and attempt status (``source_quote``)
    never count as a revision.
    """
    record = row["source_record"]
    configs, listing = providers.build_listing(row, catalog.get(record.get("model") or ""))
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
                  "exclusions": _json(c["exclusions"]), "essential_states": _json(c["essential_states"]),
                  "source_quote": _json(c["source_quote"]) if c["source_quote"] is not None else None,
                  "cpu_platform_key": hardware_platforms.reference_key(c["cpu_model"])}
        params["memory_type"], params["memory_type_conflict"] = listing_memory(row["provider"], record, c["variant_key"])
        cid = conn.execute(
            """INSERT INTO configurations (listing_id, variant_key, variant_id, variant_label, active, model, cpu_model, cpu_key,
                 cpu_model_state, cpu_count, cpu_count_state, total_cores, ram_gb, ram_state, drive_bays, bays_state, drive_size,
                 size_state, storage_state, storage_conf, hardware_state, damage, price_kind, item_price, shipping_price, currency,
                 shipping_context, evidence, warnings, exclusions, essential_states, source_quote, cpu_platform_key, memory_type,
                 memory_type_conflict, observed_at)
               VALUES (%(listing_id)s, %(variant_key)s, %(variant_id)s, %(variant_label)s, true, %(model)s, %(cpu_model)s, %(cpu_key)s,
                 %(cpu_model_state)s, %(cpu_count)s, %(cpu_count_state)s, %(total_cores)s, %(ram_gb)s, %(ram_state)s, %(drive_bays)s,
                 %(bays_state)s, %(drive_size)s, %(size_state)s, %(storage_state)s, %(storage_conf)s, %(hardware_state)s, %(damage)s,
                 %(price_kind)s, %(item_price)s, %(shipping_price)s, %(currency)s, %(shipping_context)s, %(evidence)s, %(warnings)s,
                 %(exclusions)s, %(essential_states)s, %(source_quote)s, %(cpu_platform_key)s, %(memory_type)s,
                 %(memory_type_conflict)s, now())
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
                 essential_states = EXCLUDED.essential_states, source_quote = EXCLUDED.source_quote,
                 cpu_platform_key = EXCLUDED.cpu_platform_key, memory_type = EXCLUDED.memory_type,
                 memory_type_conflict = EXCLUDED.memory_type_conflict,
                 revision = configurations.revision + CASE WHEN
                   (configurations.active, configurations.variant_id, configurations.variant_label,
                    configurations.model, configurations.cpu_model, configurations.cpu_key, configurations.cpu_model_state,
                    configurations.cpu_count, configurations.cpu_count_state, configurations.total_cores,
                    configurations.ram_gb, configurations.ram_state, configurations.drive_bays, configurations.bays_state,
                    configurations.drive_size, configurations.size_state, configurations.storage_state,
                    configurations.storage_conf, configurations.hardware_state, configurations.damage,
                    configurations.price_kind, configurations.item_price, configurations.shipping_price,
                    configurations.shipping_context, configurations.evidence, configurations.warnings,
                    configurations.exclusions, configurations.essential_states, configurations.memory_type,
                    configurations.memory_type_conflict)
                   IS DISTINCT FROM
                   (EXCLUDED.active, EXCLUDED.variant_id, EXCLUDED.variant_label,
                    EXCLUDED.model, EXCLUDED.cpu_model, EXCLUDED.cpu_key, EXCLUDED.cpu_model_state,
                    EXCLUDED.cpu_count, EXCLUDED.cpu_count_state, EXCLUDED.total_cores,
                    EXCLUDED.ram_gb, EXCLUDED.ram_state, EXCLUDED.drive_bays, EXCLUDED.bays_state,
                    EXCLUDED.drive_size, EXCLUDED.size_state, EXCLUDED.storage_state,
                    EXCLUDED.storage_conf, EXCLUDED.hardware_state, EXCLUDED.damage,
                    EXCLUDED.price_kind, EXCLUDED.item_price, EXCLUDED.shipping_price,
                    EXCLUDED.shipping_context, EXCLUDED.evidence, EXCLUDED.warnings,
                    EXCLUDED.exclusions, EXCLUDED.essential_states, EXCLUDED.memory_type,
                    EXCLUDED.memory_type_conflict)
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
    """Outcome of one discovery source within a run: an eBay vendor index page or a retailer catalog."""

    vendor: str | None
    url: str
    provider: str = PROVIDER
    snapshot: IndexSnapshot | retail_sources.CatalogSnapshot | None = None
    outcome: str | None = None  # success | partial | blocked | failed
    message: str | None = None
    invalid: int = 0  # retailer records refused by the collector's own identity checks
    retained: int = 0  # retailer products whose last-good observation an incomplete snapshot could not replace

    @property
    def label(self) -> str:
        return self.vendor if self.provider == PROVIDER else providers.names()[self.provider]

    @property
    def origin(self) -> str:
        return INDEX_ORIGIN if self.provider == PROVIDER else retail_sources.RETAILERS[self.provider].origin

    @property
    def complete(self) -> bool:
        """Whether absence from this snapshot may archive listings or deactivate unseen variants."""
        snapshot = self.snapshot
        assert snapshot is not None
        if isinstance(snapshot, IndexSnapshot):
            return not snapshot.rejected
        return snapshot.complete and not snapshot.rejected and not self.invalid


def retail_record_problem(provider: str, rec: Any) -> str | None:
    """Why a connector record cannot be stored as this provider's listing, else None.

    The collector never trusts a record to name another provider, a non-canonical or foreign URL,
    a non-USD price or an observation without its quote window.
    """
    if not isinstance(rec, dict) or rec.get("provider") != provider:
        return "record names a different provider"
    item_id, url = rec.get("item_id"), rec.get("source_url")
    if not isinstance(item_id, str) or not NATIVE_ID_RE.fullmatch(item_id):
        return "invalid native product id"
    retail = rec.get("retail")
    if not isinstance(retail, dict) or not isinstance(retail.get("handle"), str):
        return "missing retail observation"
    origin = retail_sources.RETAILERS[provider].origin
    if (not isinstance(url, str) or url != f"https://{origin}/products/{retail['handle']}"
            or not retail_sources.allowed_url(provider, url)):
        return "source URL is not this retailer's canonical product URL"
    if rec.get("currency") != "USD":
        return "price currency is not USD"
    if retail.get("method") not in ("retailer_catalog", "retailer_product"):
        return "unknown acquisition method"
    for key in ("observed_at", "fresh_until"):
        try:
            stamp = datetime.fromisoformat(str(retail.get(key)))
        except ValueError:
            return f"invalid {key}"
        if stamp.tzinfo is None:
            return f"{key} lacks a timezone"
    if not isinstance(retail.get("variants"), list) or not isinstance(rec.get("title"), str):
        return "missing variants or title"
    return None


def _retail_listing_params(page: PageResult, record: dict[str, Any]) -> dict[str, Any]:
    """Listing columns for one JSON-safe retailer observation (``source_record``)."""
    return {
        "provider": page.provider, "item_id": record["item_id"], "canonical_url": record["source_url"],
        "original_url": (record.get("original_url") or record["source_url"])[:2000], "title": record["title"],
        "condition": record.get("condition"), "model": record.get("model"), "vendor": record.get("vendor"),
        "index_url": page.url, "record": _json(record),
        "upstream": datetime.fromisoformat(record["retail"]["observed_at"]),
    }


def apply_page(conn: psycopg.Connection, page: PageResult, catalog: dict[str, dict[str, Any]],
               seen: set[tuple[str, str]]) -> int:
    """Upsert one source's records and recompute configurations. Caller owns the transaction.

    Identities are (provider, native id). Listings already applied from an earlier eBay page in this
    run (``seen``) are skipped so each item is attributed to exactly one index page. Returns the
    number of such cross-page duplicates. A retailer listing's acquisition status becomes
    ``success``. For an incomplete retailer snapshot, ``providers.merge_incomplete`` retains stored
    native variants the snapshot did not include, or keeps the whole last-good observation when the
    new one changed the facts those variants were built from (counted in ``page.retained``).
    """
    snapshot = page.snapshot
    assert snapshot is not None
    retail = page.provider != PROVIDER
    complete = page.complete
    duplicates = 0
    for rec in snapshot.records:
        key = (page.provider, rec["item_id"])
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        if retail:
            record = _jsonable({**rec, "index_url": page.url})
            if not complete:
                previous = conn.execute(
                    "SELECT source_record, quote_status FROM listings WHERE provider = %s AND item_id = %s", key,
                ).fetchone()
                record, kept_last_good = providers.merge_incomplete(
                    record, previous["source_record"] if previous else None,
                    previous["quote_status"] if previous else None)
                if kept_last_good:
                    # Seen, but its last-good observation stays authoritative: nothing renewed, the latest
                    # attempt status unchanged; only the new observation's explicit negatives applied.
                    row = conn.execute(
                        f"""UPDATE listings SET source_record = %s, last_seen_at = now(), archived_at = NULL,
                              last_seen_source_at = greatest(last_seen_source_at, %s)
                            WHERE provider = %s AND item_id = %s RETURNING {RECOMPUTE_COLUMNS}""",
                        (_json(record), datetime.fromisoformat(rec["retail"]["observed_at"]), *key),
                    ).fetchone()
                    page.retained += 1
                    recompute(conn, row, catalog, "index")
                    continue
            params = _retail_listing_params(page, record)
        else:
            params = {
                "provider": PROVIDER, "item_id": rec["item_id"], "canonical_url": rec["source_url"],
                "original_url": rec["original_url"], "title": rec["title"], "condition": rec["condition"],
                "model": rec["model"], "vendor": rec.get("vendor"), "index_url": page.url,
                "record": _json(_jsonable({**rec, "index_url": page.url})), "upstream": snapshot.updated_at,
            }
        row = conn.execute(
            f"""INSERT INTO listings (provider, item_id, canonical_url, original_url, title, condition, model, vendor,
                 index_url, source_record, last_seen_source_at, quote_status)
               VALUES (%(provider)s, %(item_id)s, %(canonical_url)s, %(original_url)s, %(title)s, %(condition)s, %(model)s,
                 %(vendor)s, %(index_url)s, %(record)s, %(upstream)s, %(quote_status)s)
               ON CONFLICT (provider, item_id) DO UPDATE SET canonical_url = EXCLUDED.canonical_url,
                 original_url = EXCLUDED.original_url, title = EXCLUDED.title, model = EXCLUDED.model,
                 vendor = EXCLUDED.vendor, index_url = EXCLUDED.index_url,
                 condition = coalesce(listings.condition, EXCLUDED.condition), source_record = EXCLUDED.source_record,
                 last_seen_at = now(), last_seen_source_at = greatest(listings.last_seen_source_at, EXCLUDED.last_seen_source_at),
                 archived_at = NULL, quote_status = EXCLUDED.quote_status
               RETURNING {RECOMPUTE_COLUMNS}""",
            {**params, "quote_status": "success" if retail else None},
        ).fetchone()
        recompute(conn, row, catalog, "index")
    return duplicates


def archive_absent(conn: psycopg.Connection, page: PageResult) -> int:
    """Archive this source's listings absent for 7 days of its snapshot time.

    Runs only for complete snapshots: when the parser rejected any record or a retailer catalog was
    truncated, an absent listing may simply be unseen. Listings of other pages or providers
    (possibly failing or stale) are never archived by this source's absence.
    """
    snapshot = page.snapshot
    assert snapshot is not None
    if not page.complete:
        return 0
    when = snapshot.updated_at if isinstance(snapshot, IndexSnapshot) else snapshot.observed_at
    return conn.execute(
        """UPDATE listings SET archived_at = now()
           WHERE archived_at IS NULL AND provider = %s AND index_url = %s AND last_seen_source_at < %s""",
        (page.provider, page.url, when - ARCHIVE_AFTER),
    ).rowcount


def record_page(conn: psycopg.Connection, page: PageResult) -> None:
    snapshot = page.snapshot if page.outcome in ("success", "partial") else None
    if snapshot is None:
        upstream = None
    elif isinstance(snapshot, IndexSnapshot):
        upstream = snapshot.updated_at
    else:
        upstream = snapshot.upstream_updated_at  # retailer catalogs rarely state one; never the local time
    conn.execute(
        """INSERT INTO source_pages (url, provider, vendor, last_attempt_at, last_outcome, last_message,
             upstream_updated_at, last_success_at, record_count)
           VALUES (%(url)s, %(provider)s, %(vendor)s, now(), %(outcome)s, %(message)s, %(upstream)s,
             CASE WHEN %(outcome)s = 'success' THEN now() END, %(count)s)
           ON CONFLICT (url) DO UPDATE SET provider = EXCLUDED.provider, vendor = EXCLUDED.vendor,
             last_attempt_at = now(), last_outcome = EXCLUDED.last_outcome, last_message = EXCLUDED.last_message,
             upstream_updated_at = coalesce(EXCLUDED.upstream_updated_at, source_pages.upstream_updated_at),
             last_success_at = coalesce(EXCLUDED.last_success_at, source_pages.last_success_at),
             record_count = coalesce(EXCLUDED.record_count, source_pages.record_count)""",
        {"url": page.url, "provider": page.provider, "vendor": page.vendor, "outcome": page.outcome,
         "message": (page.message or None) and page.message[:500], "upstream": upstream,
         "count": len(snapshot.records) if snapshot else None},
    )


def discovery_pages(settings: Settings) -> list[PageResult]:
    """Every enabled discovery source in collection order: eBay vendor pages, then retailer catalogs."""
    pages = [PageResult(vendor, url) for vendor, url in settings.source_pages]
    if settings.retailers_enabled:
        pages += [PageResult(None, r.catalog_url, key) for key, r in retail_sources.RETAILERS.items()]
    return pages


def _fetch_page(page: PageResult, settings: Settings, fetcher: HttpFetcher, observed: datetime) -> None:
    """Acquire one source into ``page.snapshot``; raises SourceBlocked/SourceError."""
    if page.provider == PROVIDER:
        snapshot = fetcher.fetch_index(page.url, settings.max_index_bytes)
        matching = [record for record in snapshot.records if record.get("vendor") == page.vendor]
        mismatched = len(snapshot.records) - len(matching)
        if mismatched:
            if not matching:
                raise SourceError(f"{page.vendor} page contains only other or unknown vendors")
            snapshot = replace(snapshot, records=matching, rejected=snapshot.rejected + mismatched)
        page.snapshot = snapshot
        return
    catalog = retail_sources.fetch_catalog(
        page.provider, fetcher, observed_at=observed,
        fresh_until=observed + timedelta(hours=settings.page_verification_hours), max_bytes=settings.max_index_bytes)
    valid = [rec for rec in catalog.records if retail_record_problem(page.provider, rec) is None]
    page.invalid = len(catalog.records) - len(valid)
    page.snapshot = replace(catalog, records=valid)


def run_discovery(conn: psycopg.Connection | Checkpoints, settings: Settings, fetcher: HttpFetcher) -> str:
    """Fetch every enabled discovery source and apply the valid ones atomically.

    Each eBay vendor page and retailer catalog keeps its own freshness, failure state and absence
    archiving, so a fresh source never refreshes or archives another source's last-good data. A
    block (or an active backoff) stops requests to that origin only; other providers continue.
    ``conn`` is a raw connection or the collection's ``Checkpoints``; acquired snapshots stay in
    memory while their database application is retried, so a reconnect never refetches a source.
    """
    checkpoints = _checkpoints(conn)
    run_id = checkpoints.start_run("discovery")
    pages = discovery_pages(settings)
    observed = _now()
    stopped: dict[str, str] = {}  # origin -> reason no (further) request is made this run
    blocks: dict[str, tuple[SourceBlocked, datetime]] = {}  # origin -> block and when it was observed
    for page in pages:
        origin = page.origin
        if origin not in stopped and (until := checkpoints.run(partial(backoff_until, origin=origin))) is not None:
            stopped[origin] = f"Backing off {origin} until {until.isoformat()}"
            log.warning("discovery skipped %s: origin backoff until %s", origin, until.isoformat())
        if origin in stopped:
            page.outcome, page.message = "blocked", stopped[origin]
            continue
        checkpoints.check()
        try:
            _fetch_page(page, settings, fetcher, observed)
        except SourceBlocked as exc:
            blocks[origin] = (exc, _now())
            stopped[origin] = f"not requested after origin block: {exc}"
            page.outcome, page.message = "blocked", str(exc)
        except SourceError as exc:
            page.outcome, page.message = "failed", str(exc)
    if blocks:
        def store_backoffs(c: psycopg.Connection) -> None:
            for origin, (block, blocked_at) in blocks.items():
                set_backoff(c, origin, block.retry_after or settings.recheck_cooldown_hours * 3600, str(block),
                            observed_at=blocked_at)

        checkpoints.run(store_backoffs)

    catalog = checkpoints.run(db.catalog)
    acquired = [(page, page.snapshot, page.outcome, page.message) for page in pages]

    def apply(c: psycopg.Connection) -> tuple[str, list[str]]:
        # A retry starts again from the acquisition results, never from a rolled-back attempt's state.
        for page, snapshot, outcome, message in acquired:
            page.snapshot, page.outcome, page.message, page.retained = snapshot, outcome, message, 0
        notes: list[str] = []
        stored = {r["url"]: r["upstream_updated_at"]
                  for r in c.execute("SELECT url, upstream_updated_at FROM source_pages").fetchall()}
        seen: set[tuple[str, str]] = set()
        cross_duplicates = 0
        unchanged = True
        for page in pages:
            if page.snapshot is None:
                continue
            snapshot = page.snapshot
            if isinstance(snapshot, IndexSnapshot):
                prev = stored.get(page.url)
                if prev is not None and snapshot.updated_at < prev:
                    page.outcome = "failed"
                    page.message = (f"Upstream snapshot {snapshot.updated_at.isoformat()} older than stored "
                                    f"{prev.isoformat()}; ignored")
                    page.snapshot = None
                    continue
                unchanged = unchanged and prev is not None and snapshot.updated_at == prev
            cross_duplicates += apply_page(c, page, catalog, seen)
            page_archived = archive_absent(c, page)
            detail = [f"{len(snapshot.records)} listings"]
            if snapshot.duplicates:
                detail.append(f"{snapshot.duplicates} duplicate item ids merged")
            rejected = snapshot.rejected + page.invalid
            if rejected:
                detail.append(f"{rejected} invalid records rejected; absence-based archiving skipped")
            if not isinstance(snapshot, IndexSnapshot) and not snapshot.complete:
                detail.append("catalog incomplete; absence-based archiving and removal of unseen variants skipped")
            if page.retained:
                detail.append(f"{page.retained} products kept their last complete observation")
            if page_archived:
                detail.append(f"{page_archived} archived after 7 days absent")
            page.outcome = "success" if page.complete else "partial"
            page.message = "; ".join(detail)
        applied = [p for p in pages if p.snapshot is not None]
        for page in pages:
            record_page(c, page)
            notes.append(f"{page.label}: {page.outcome if page.outcome != 'success' else 'ok'}"
                         + (f" ({page.message})" if page.message else ""))
        if cross_duplicates:
            notes.append(f"{cross_duplicates} listings on several vendor pages merged")
        ebay_applied = [p for p in applied if p.provider == PROVIDER]
        if ebay_applied and unchanged:
            notes.append("upstream timestamp unchanged")
        if not applied:
            outcome = "blocked" if any(p.outcome == "blocked" for p in pages) else "failed"
        elif all(p.outcome == "success" for p in pages):
            outcome = "success"
        else:
            outcome = "partial"
        finish_run(c, run_id, outcome, "; ".join(notes),
                   upstream=min((p.snapshot.updated_at for p in ebay_applied), default=None),
                   discovered=len(seen))
        return outcome, notes

    try:
        outcome, notes = checkpoints.run(apply)
    except psycopg.Error:
        # Not a lost connection (a session recovers those and retries ``apply``): the snapshot itself failed.
        log.exception("discovery snapshot transaction failed; previous inventory retained")
        checkpoints.run(partial(finish_run, run_id=run_id, outcome="failed",
                                message="Database error while applying snapshot; previous inventory retained"))
        return "failed"
    log.info("discovery %s: %s", outcome, "; ".join(notes))
    return outcome


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


# Bounded enrichment queue. All lanes share the same eligibility (active and past
# cooldown); target chassis are cataloged 1U models of the modern era, for every vendor.
_CANDIDATE_COLUMNS = """
SELECT l.id, l.provider, l.item_id, l.source_record, l.description_claims, l.description_status,
       l.description_checked_at, l.page_claims, l.page_status, l.quote_status
FROM listings l
LEFT JOIN server_models m ON m.model = l.model
WHERE l.archived_at IS NULL AND l.ended_at IS NULL AND l.manual_sold_at IS NULL
  AND (l.next_check_after IS NULL OR l.next_check_after <= now())
  AND NOT (l.provider = ANY(%(skip)s))
"""
_TARGET = "(m.target_era AND m.rack_units = 1)"
_PRICE = "(l.source_record->>'item_price')::numeric"
# Lane 1: cheapest previously verified targets first; the oldest lane prevents starvation.
VERIFIED_TARGETS_SQL = f"""{_CANDIDATE_COLUMNS}
  AND {_TARGET} AND l.page_claims->>'status' = 'verified'
ORDER BY {_PRICE} ASC NULLS LAST,
  coalesce((l.page_claims->>'fresh_until')::timestamptz, (l.page_claims->>'checked_at')::timestamptz) ASC NULLS FIRST,
  l.id ASC
LIMIT %(limit)s
"""
# Lane 2: never-checked targets, cheapest first.
NEW_TARGETS_SQL = f"""{_CANDIDATE_COLUMNS}
  AND {_TARGET} AND l.last_checked_at IS NULL
ORDER BY {_PRICE} ASC NULLS LAST, l.first_seen_at ASC, l.id ASC
LIMIT %(limit)s
"""
# Lane 3: every candidate by age of its last check (or first sighting when never checked),
# so a stream of new cheap listings cannot starve older ones.
OLDEST_SQL = f"""{_CANDIDATE_COLUMNS}
ORDER BY coalesce(l.last_checked_at, l.first_seen_at) ASC, l.id ASC
LIMIT %(limit)s
"""


def select_candidates(conn: psycopg.Connection, budget: int, *,
                      skip_providers: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Pick at most ``budget`` due listings of every provider for detail checks, deterministically.

    One budget is shared by eBay listings and retailer products; ``skip_providers`` (disabled or
    backing-off retailers) are never selected, so they cannot consume it.

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
    params = {"limit": budget, "skip": list(skip_providers)}
    lane_quota = (budget - 1) // 2 if budget >= 3 else 1
    lanes = [
        (conn.execute(VERIFIED_TARGETS_SQL, params).fetchall(), lane_quota),
        (conn.execute(NEW_TARGETS_SQL, params).fetchall(), lane_quota if budget >= 2 else 0),
    ]
    oldest = conn.execute(OLDEST_SQL, params).fetchall()
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


def _store_check(conn: psycopg.Connection, listing_id: int, update: str, params: Any,
                 catalog: dict[str, dict[str, Any]]) -> None:
    """Persist one acquired detail check and rebuild the listing's configurations (a retryable checkpoint)."""
    conn.execute(update, params)
    row = conn.execute(f"SELECT {RECOMPUTE_COLUMNS} FROM listings WHERE id = %s", (listing_id,)).fetchone()
    recompute(conn, row, catalog, "enrichment")


def enrich_retailer(conn: psycopg.Connection | Checkpoints, settings: Settings, fetcher: HttpFetcher,
                    cand: dict[str, Any], catalog: dict[str, dict[str, Any]],
                    backing_off: set[str]) -> tuple[str, str | None]:
    """Re-quote one retailer product through its own product endpoint only. Returns (outcome, message).

    Outcome is ``verified`` (fresh observation applied), ``blocked`` or ``failed``. A blocked or
    failed attempt (including unknown stock) keeps the last observed record, price, stock and quote
    times and only marks the quote's latest attempt; a provider already backing off
    (``backing_off``) is not requested at all and its listing is left untouched.
    """
    checkpoints = _checkpoints(conn)
    provider = cand["provider"]
    origin = retail_sources.RETAILERS[provider].origin
    if not settings.retailers_enabled:
        return "failed", "retailer collection disabled"
    if provider in backing_off or checkpoints.run(partial(backoff_until, origin=origin)) is not None:
        backing_off.add(provider)
        return "blocked", f"{origin} backing off; not requested"
    checkpoints.check()
    observed = _now()
    record: dict[str, Any] | None = None
    message: str | None = None
    try:
        rec = retail_sources.fetch_product(
            provider, cand["source_record"], fetcher, observed_at=observed,
            fresh_until=observed + timedelta(hours=settings.page_verification_hours),
            max_bytes=settings.max_description_bytes)
        problem = retail_record_problem(provider, rec)
        if problem is None and rec["item_id"] != cand["item_id"]:
            problem = "product identity changed"
        if problem:
            raise SourceError(f"product record refused: {problem}")
        status = "success"
        record = _jsonable({**rec, "index_url": cand["source_record"].get("index_url")})
    except SourceBlocked as exc:
        status, message = "blocked", str(exc)
        backing_off.add(provider)
        checkpoints.run(partial(set_backoff, origin=origin, seconds=exc.retry_after or settings.recheck_cooldown_hours * 3600,
                                reason=str(exc), observed_at=_now()))
    except SourceError as exc:
        status, message = "failed", str(exc)
    cooldown = timedelta(hours=settings.recheck_cooldown_hours)
    if record is not None:
        update = """UPDATE listings SET quote_status = 'success', source_record = %(record)s, title = %(title)s,
                      model = %(model)s, vendor = %(vendor)s, condition = coalesce(%(condition)s, condition),
                      last_checked_at = now(), next_check_after = now() + %(cooldown)s, check_message = NULL
                    WHERE id = %(id)s"""
        params: Any = {"record": _json(record), "title": record["title"], "model": record.get("model"),
                       "vendor": record.get("vendor"), "condition": record.get("condition"), "cooldown": cooldown,
                       "id": cand["id"]}
    else:
        update = """UPDATE listings SET quote_status = %s, last_checked_at = now(), next_check_after = now() + %s,
                      check_message = %s WHERE id = %s"""
        params = (status, cooldown, (message or "")[:500] or None, cand["id"])
    checkpoints.run(partial(_store_check, listing_id=cand["id"], update=update, params=params, catalog=catalog))
    return ("verified" if status == "success" else status), message


def _still_eligible(conn: psycopg.Connection, listing_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM listings WHERE id = %s AND archived_at IS NULL AND ended_at IS NULL"
        " AND manual_sold_at IS NULL", (listing_id,)).fetchone() is not None


def _enrichment_plan(conn: psycopg.Connection, settings: Settings, max_details: int,
                     target_listing_id: int | None) -> tuple[set[str], list[dict[str, Any]], dict[str, dict[str, Any]], bool, bool]:
    """(retailers backing off, candidates, catalog, description origin blocked, eBay page origin blocked)."""
    retail_backing_off = {key for key, r in retail_sources.RETAILERS.items() if backoff_until(conn, r.origin)}
    if target_listing_id is None:
        skip = set(retail_sources.RETAILERS) if not settings.retailers_enabled else retail_backing_off
        candidates = select_candidates(conn, max_details, skip_providers=tuple(sorted(skip)))
    else:
        candidates = conn.execute(_CANDIDATE_COLUMNS + " AND l.id = %(id)s LIMIT 1",
                                  {"skip": [], "id": target_listing_id}).fetchall()
    return (retail_backing_off, candidates, db.catalog(conn), backoff_until(conn, DESCRIPTION_HOST) is not None,
            backoff_until(conn, EBAY_HOST) is not None)


def run_enrichment(conn: psycopg.Connection | Checkpoints, settings: Settings, fetcher: HttpFetcher, max_details: int,
                   browser_enabled: bool, *, target_listing_id: int | None = None) -> str:
    """Bounded detail checks. ``conn`` is a raw connection or the collection's ``Checkpoints``.

    Every acquired description, browser result and retailer quote stays in memory, with the loop's
    counters, while its database checkpoint is retried, so a reconnect never repeats a request.
    """
    if max_details <= 0:
        return "skipped"
    checkpoints = _checkpoints(conn)
    run_id = checkpoints.start_run("enrichment")
    retail_backing_off, candidates, catalog, desc_blocked, page_blocked = checkpoints.run(partial(
        _enrichment_plan, settings=settings, max_details=max_details, target_listing_id=target_listing_id))
    cooldown = timedelta(hours=settings.recheck_cooldown_hours)
    verification_window = timedelta(hours=settings.page_verification_hours)
    browser_note = None if browser_enabled else "browser checks disabled"
    checker = None
    if browser_enabled and not page_blocked and any(c["provider"] == PROVIDER for c in candidates):
        from .browser import BrowserChecker

        checker = BrowserChecker(settings.browser_timeout, settings.max_variants)
    attempted = ebay_attempted = enriched = blocked = failed = 0
    try:
        for cand in candidates:
            # Quiet gap after each listing of any provider, including cached descriptions and backoffs.
            if attempted:
                time.sleep(settings.request_delay)
            # The candidate list predates the quiet gap: re-read live eligibility (no lock kept across network)
            # so a listing marked sold, ended or archived meanwhile is never requested or touched.
            if not checkpoints.run(partial(_still_eligible, listing_id=cand["id"])):
                log.info("listing %s no longer eligible for a detail check; skipped", cand["item_id"])
                continue
            attempted += 1
            if cand["provider"] != PROVIDER:
                result, note = enrich_retailer(checkpoints, settings, fetcher, cand, catalog, retail_backing_off)
                enriched += result == "verified"
                blocked += result == "blocked"
                failed += result == "failed"
                if note:
                    log.info("retailer check %s %s: %s", cand["provider"], cand["item_id"], note)
                continue
            ebay_attempted += 1
            item_id = cand["item_id"]
            messages: list[str] = []
            outcomes: list[str] = []
            desc_claims = cand["description_claims"]
            desc_status = cand["description_status"]
            desc_checked = cand["description_checked_at"]
            desc_due = desc_status not in ("verified", "absent") or desc_checked is None or desc_checked < _now() - DESCRIPTION_REFRESH
            if desc_due and not desc_blocked:
                checkpoints.check()
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
                    checkpoints.run(partial(set_backoff, origin=DESCRIPTION_HOST,
                                            seconds=exc.retry_after or cooldown.total_seconds(), reason=str(exc),
                                            observed_at=desc_checked))
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
                checkpoints.check()
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
                    checkpoints.run(partial(set_backoff, origin=EBAY_HOST,
                                            seconds=exc.retry_after or cooldown.total_seconds(), reason=str(exc),
                                            observed_at=_now()))
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

            checkpoints.run(partial(
                _store_check, listing_id=cand["id"],
                update="""UPDATE listings SET description_claims = %(dc)s, description_status = %(ds)s,
                     description_checked_at = %(dt)s, page_claims = %(pc)s, page_status = %(ps)s,
                     page_checked_at = coalesce(%(pt)s, page_checked_at), last_checked_at = now(),
                     next_check_after = now() + %(cooldown)s, check_message = %(msg)s
                   WHERE id = %(id)s""",
                params={"dc": _json(desc_claims) if desc_claims else None, "ds": desc_status, "dt": desc_checked,
                        "pc": _json(page_claims) if page_claims else None, "ps": page_status, "pt": page_checked,
                        "cooldown": cooldown, "msg": "; ".join(messages)[:500] or None, "id": cand["id"]},
                catalog=catalog))
    finally:
        if checker is not None:
            checker.close()

    if attempted == 0:
        outcome, message = "success", "no listings due for detail checks"
    elif blocked == attempted:
        outcome, message = "blocked", f"{attempted} checks blocked"
    elif blocked or failed:
        outcome, message = "partial", f"{enriched} of {attempted} enriched; {blocked} blocked; {failed} with failures"
    elif checker is None and ebay_attempted:
        outcome = "partial"
        message = (f"{enriched} of {attempted} seller descriptions checked; eBay page/variation verification "
                   f"not performed ({browser_note or 'eBay page origin backing off'}); prices remain base/unverified")
    else:
        outcome, message = "success", f"{enriched} of {attempted} enriched"
    if browser_note and ebay_attempted and browser_note not in message:
        message = f"{message}; {browser_note}"
    if page_blocked and browser_enabled and ebay_attempted and "backing off" not in message:
        message = f"{message}; eBay page checks backing off"
    checkpoints.run(partial(finish_run, run_id=run_id, outcome=outcome, message=message, enriched=enriched))
    log.info("enrichment %s: %s", outcome, message)
    return outcome


def _is_playwright_error(exc: Exception) -> bool:
    try:
        from playwright.sync_api import Error as PlaywrightError
    except ImportError:
        return False
    return isinstance(exc, PlaywrightError)


def prune_history(conn: psycopg.Connection) -> None:
    """Drop price history older than 90 days. Caller owns the transaction (a checkpoint)."""
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


def _guard_requests(fetcher: HttpFetcher, check: Callable[[], None]) -> Callable[[], None]:
    """Chain ``check`` into the fetcher's ``before_request`` hook; return a callable restoring the caller's hook.

    The hook runs after the fetcher's origin pacing, right before each request. Test doubles
    without the hook are left untouched.
    """
    if not hasattr(fetcher, "before_request"):
        return lambda: None
    previous = fetcher.before_request

    def before_request() -> None:
        if previous is not None:
            previous()
        check()

    fetcher.before_request = before_request

    def restore() -> None:
        fetcher.before_request = previous

    return restore


def run_collection(settings: Settings, max_details: int | None = None, browser: bool | None = None,
                   fetcher: HttpFetcher | None = None, *, target_listing_id: int | None = None,
                   session: CollectionSession) -> CollectionResult:
    """Run full collection, or only one selected listing, while caller holds the collector lock.

    Every database step is a recoverable checkpoint on the caller's locked session;
    every source request and browser check first verifies ownership. Runs left open by a
    stopped collector are finalized failed first. A fetcher created here is closed here;
    a caller-supplied fetcher stays caller's, with its previous ``before_request`` hook restored.
    """
    checkpoints = Checkpoints(session)
    owned = fetcher is None
    restore_hook: Callable[[], None] | None = None
    try:
        if fetcher is None:
            fetcher = HttpFetcher(settings.request_timeout, settings.request_delay)
        if not checkpoints.run(db.schema_ready):
            raise SchemaNotMigrated("database schema not migrated")
        checkpoints.begin()
        restore_hook = _guard_requests(fetcher, checkpoints.check)
        budget = settings.max_details if max_details is None else max_details
        try:
            if target_listing_id is None:
                discovery = run_discovery(checkpoints, settings, fetcher)
            else:
                discovery = "skipped"
                budget = min(settings.max_details, budget, 1)
            enrichment_options = {} if target_listing_id is None else {"target_listing_id": target_listing_id}
            enrichment = run_enrichment(
                checkpoints, settings, fetcher, budget,
                settings.browser_enabled if browser is None else (browser and settings.browser_enabled),
                **enrichment_options,
            )
            checkpoints.run(prune_history)
        except db.DatabaseUnavailable:
            raise  # lost ownership or database: nothing may be written; the next collection finalizes the run
        except Exception as exc:
            checkpoints.abandon(f"Collection aborted ({type(exc).__name__}); remaining checks not performed")
            raise
        checkpoints.complete()
        return CollectionResult(discovery, enrichment)
    finally:
        if restore_hook is not None:
            restore_hook()
        if owned and fetcher is not None:
            fetcher.close()


def collect(settings: Settings, max_details: int | None = None, browser: bool | None = None) -> int:
    """Run one scheduled collection unless another collector holds the lock. Returns process exit code.

    The lock connection carries the whole collection through a ``CollectionSession``: a lost
    connection is replaced and the lock reacquired within the session's bounds, or the collection
    stops with ``db.DatabaseUnavailable`` (exit 1 via the CLI) without repeating any source request.
    """
    lock_conn = db.connect(settings.db_connect_timeout, autocommit=True)
    session: CollectionSession | None = None
    try:
        if not try_collector_lock(lock_conn):
            log.warning("another collector holds the lock; exiting")
            return 0
        session = CollectionSession(settings, lock_conn)
        try:
            result = run_collection(settings, max_details=max_details, browser=browser, session=session)
        except SchemaNotMigrated:
            log.error("database schema not migrated; run `python -m rackscout migrate`")
            return 1
        # A partial snapshot applied its valid records; its degraded state is visible in /api/status.
        return 0 if result.discovery in ("success", "partial") else 1
    finally:
        # Closing the sessions releases the advisory lock (a replacement connection holds it after recovery).
        try:
            if session is not None:
                session.close()
        finally:
            lock_conn.close()
