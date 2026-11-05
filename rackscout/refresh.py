"""Manual refresh: durable singleton request queue plus the dedicated worker that executes it.

The web API only enqueues (``enqueue``) and reads state (``status``). A separate
long-running ``python -m rackscout refresh-worker`` process polls the queue and
runs the existing collector in its own process while holding the same global
advisory lock as the scheduled collector, so manual and scheduled collections
never run concurrently and every source backoff is honored.

Invariants:
- One control row (``refresh_control.id = 1``) serializes requests; at most one
  request is queued or running. ``requested_at`` is the time of the last accepted
  request and alone drives the cooldown; completion never resets it.
- A worker claims a queued request (``queued`` → ``running``) only while holding
  the collector lock, and holds that same session lock until the terminal state
  is committed. Therefore ``running`` with the collector lock free means the
  worker died: the request is marked ``failed`` and never re-run automatically.
- A request is either global (``target_listing_id IS NULL``: full discovery plus bounded
  enrichment) or selected (one stable marketplace listing id: no discovery, at most one
  normal enrichment of that listing). Both share the single queue slot, the global cooldown and
  the collector lock. The target is stored as ``listings.id`` so it survives deactivation of the
  selected configuration while the request waits; the worker captures it under the claim.
- Lock order is collector lock → control row. The API never acquires the
  collector lock; it only inspects ``pg_locks`` (non-blocking, non-interfering).
"""
from __future__ import annotations

import logging
import math
import signal
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

import psycopg

from . import collector, db
from .config import Settings
from .queries import _iso
from .sources import HttpFetcher

log = logging.getLogger(__name__)

POLL_SECONDS = 5.0
# Upper bound for one manual collection, matching the scheduled collector's job deadline.
REFRESH_DEADLINE_SECONDS = 1800
ACTIVE = ("queued", "running")
# Bound for every queue statement and lock wait, so contention cannot hold the collector lock indefinitely.
QUEUE_TIMEOUT = "10s"
INTERRUPTED_MESSAGE = "Refresh interrupted before completion (worker stopped); not retried automatically"

# ``target_item_id``/``target_provider`` are bounded primary-key lookups (a native id is unique only per
# provider); no URL is ever exposed.
_COLUMNS = """state, request_id, requested_at, started_at, finished_at, message, target_listing_id,
    (SELECT l.item_id FROM listings l WHERE l.id = refresh_control.target_listing_id) AS target_item_id,
    (SELECT l.provider FROM listings l WHERE l.id = refresh_control.target_listing_id) AS target_provider,
    now() AS db_now"""
_SELECT = f"SELECT {_COLUMNS} FROM refresh_control WHERE id = 1"
_MARK_INTERRUPTED = f"""UPDATE refresh_control SET state = 'failed', finished_at = now(), message = %s, updated_at = now()
    WHERE id = 1 AND state = 'running' RETURNING {_COLUMNS}"""


class ListingRefreshNotFound(Exception):
    """The configuration does not exist or its listing is inactive, archived or ended."""


class ListingRefreshCooldown(Exception):
    """The selected listing was checked recently; ``next_check_after`` has not passed."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"listing refresh available in {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


class RefreshDeadline(BaseException):
    """Raised by SIGALRM when a manual collection exceeds its deadline.

    A BaseException so source, database and generic ``except Exception``
    handlers inside the collector cannot swallow it.
    """


def _bound(conn: psycopg.Connection) -> None:
    """Bound one enqueue transaction (API connection) against database contention."""
    conn.execute(f"SET LOCAL statement_timeout = '{QUEUE_TIMEOUT}'")
    conn.execute(f"SET LOCAL lock_timeout = '{QUEUE_TIMEOUT}'")


def connect_worker(settings: Settings) -> psycopg.Connection:
    """Autocommit worker session with session-wide statement/lock timeouts.

    Every queue query, collector-lock acquisition and unlock on it is bounded,
    so database contention cannot keep a worker (or its collector lock) stuck.
    """
    conn = db.connect(settings.db_connect_timeout, autocommit=True)
    try:
        conn.execute(f"SET statement_timeout = '{QUEUE_TIMEOUT}'")
        conn.execute(f"SET lock_timeout = '{QUEUE_TIMEOUT}'")
    except BaseException:  # never leak an unconfigured session, whatever interrupted setup
        conn.close()
        raise
    return conn


def collector_running(conn: psycopg.Connection) -> bool:
    """Whether any session in this database holds the global collector lock (scheduled or manual)."""
    key = db.COLLECTOR_LOCK_KEY
    return bool(conn.execute(
        """SELECT EXISTS (
             SELECT 1 FROM pg_locks
             WHERE locktype = 'advisory' AND granted AND objsubid = 1
               AND classid = %s::bigint::oid AND objid = %s::bigint::oid
               AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
           ) AS running""",
        (key >> 32, key & 0xFFFFFFFF),
    ).fetchone()["running"])


def _status(row: dict[str, Any] | None, now: datetime, running: bool, settings: Settings) -> dict[str, Any]:
    row = row or {"state": "idle", "request_id": None, "requested_at": None, "started_at": None,
                  "finished_at": None, "message": None, "target_listing_id": None, "target_item_id": None,
                  "target_provider": None}
    retry_at: datetime | None = None
    retry_after = 0
    if row["requested_at"] is not None:
        until = row["requested_at"] + timedelta(seconds=settings.refresh_cooldown_seconds)
        if until > now:
            retry_at, retry_after = until, math.ceil((until - now).total_seconds())
    enabled = bool(settings.refresh_origins)
    return {
        "enabled": enabled,
        "state": row["state"],
        "request_id": str(row["request_id"]) if row["request_id"] is not None else None,
        "requested_at": _iso(row["requested_at"]),
        "started_at": _iso(row["started_at"]),
        "finished_at": _iso(row["finished_at"]),
        "retry_at": _iso(retry_at),
        "retry_after_seconds": retry_after,
        "collector_running": running,
        "can_refresh": enabled and row["state"] not in ACTIVE and not running and retry_after == 0,
        "message": row["message"],
        "scope": "all" if row["target_listing_id"] is None else "listing",
        "target_item_id": row["target_item_id"] if row["target_listing_id"] is not None else None,
        "target_provider": row["target_provider"] if row["target_listing_id"] is not None else None,
    }


def status(conn: psycopg.Connection, settings: Settings) -> dict[str, Any]:
    """Current refresh state. Read-only; never fetches external data."""
    row = conn.execute(_SELECT).fetchone()
    now = row["db_now"] if row else conn.execute("SELECT now() AS n").fetchone()["n"]
    return _status(row, now, collector_running(conn), settings)


_RESOLVE_TARGET = """SELECT l.id, l.next_check_after, now() AS db_now
    FROM configurations c JOIN listings l ON l.id = c.listing_id
    WHERE c.id = %s AND c.active AND l.archived_at IS NULL AND l.ended_at IS NULL
      AND l.manual_sold_at IS NULL"""


def enqueue(conn: psycopg.Connection, settings: Settings, *,
            configuration_id: int | None = None) -> tuple[str, dict[str, Any]]:
    """Atomically accept one manual refresh request.

    With ``configuration_id`` the request targets that active configuration's live
    marketplace listing (any origin, including manual configurations) instead of the
    whole source. Raises ``ListingRefreshNotFound`` for an unknown/inactive/archived/ended/manually sold
    target and ``ListingRefreshCooldown`` while the listing's ``next_check_after`` lies in
    the future; neither consumes the queue slot or the global cooldown. A global request
    clears any previous target.

    Returns ``(outcome, status)`` with outcome ``accepted`` (newly queued),
    ``active`` (a request is queued/running or a collector holds the lock) or
    ``cooldown`` (the last accepted request is too recent). Concurrent callers
    serialize on the control row, so exactly one of them is accepted.
    """
    rejection: Exception | None = None
    result: tuple[str, dict[str, Any]] | None = None
    with conn.transaction():
        _bound(conn)
        conn.execute("INSERT INTO refresh_control (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        row = conn.execute(_SELECT + " FOR UPDATE").fetchone()
        running = collector_running(conn)
        if row["state"] == "running" and not running:
            # The claiming worker holds the collector lock until the terminal update, which needs
            # this row lock; with the lock free it is gone. Record that instead of wedging.
            row = conn.execute(_MARK_INTERRUPTED, (INTERRUPTED_MESSAGE,)).fetchone()
            log.warning("manual refresh %s was interrupted; marked failed", row["request_id"])
        current = _status(row, row["db_now"], running, settings)
        target_id: int | None = None
        if configuration_id is not None:
            target = conn.execute(_RESOLVE_TARGET, (configuration_id,)).fetchone()
            if target is None:
                rejection = ListingRefreshNotFound("no live listing for this configuration")
            elif target["next_check_after"] is not None and target["next_check_after"] > target["db_now"]:
                wait = math.ceil((target["next_check_after"] - target["db_now"]).total_seconds())
                rejection = ListingRefreshCooldown(wait)
            else:
                target_id = target["id"]
        if rejection is None:
            if row["state"] in ACTIVE or running:
                result = "active", current
            elif current["retry_after_seconds"] > 0:
                result = "cooldown", current
            else:
                row = conn.execute(
                    f"""UPDATE refresh_control SET state = 'queued', request_id = %s, requested_at = now(),
                          started_at = NULL, finished_at = NULL, message = NULL, target_listing_id = %s,
                          updated_at = now()
                        WHERE id = 1 RETURNING {_COLUMNS}""",
                    (uuid.uuid4(), target_id),
                ).fetchone()
                log.info("manual refresh %s queued (%s)", row["request_id"], "listing" if target_id else "all")
                result = "accepted", _status(row, row["db_now"], running, settings)
    # Raised after commit so an interrupted-request repair above is never rolled back by a rejection.
    if rejection is not None:
        raise rejection
    assert result is not None
    return result


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


@contextmanager
def _deadline(seconds: float | None) -> Iterator[None]:
    """Raise RefreshDeadline in the main thread after ``seconds``; disarm and restore on exit."""
    if seconds is None:
        yield
        return
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("a refresh deadline can only be armed in the main thread")

    def expire(signum: int, frame: Any) -> None:
        raise RefreshDeadline

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
        finally:
            signal.signal(signal.SIGALRM, previous)


def final_state(result: collector.CollectionResult) -> tuple[str, str]:
    """Map the run's recorded discovery/enrichment outcomes to a terminal request state.

    A selected-listing run skips discovery; its state is the single enrichment's outcome and the
    message never claims a discovery success.
    """
    discovery, enrichment = result.discovery, result.enrichment
    if discovery == "skipped":
        state = {"success": "succeeded", "skipped": "succeeded", "blocked": "blocked", "failed": "failed"}.get(
            enrichment, "partial")
        message = f"Selected listing refresh; enrichment {enrichment}"
        if enrichment == "skipped":
            message += " (no detail-check budget configured)"
        if state != "succeeded":
            message += "; see source status for details"
        return state, message
    if discovery == "failed":
        state = "failed"
    elif discovery == "blocked":
        state = "blocked"
    elif discovery == "success" and enrichment in ("success", "skipped"):
        state = "succeeded"
    else:
        state = "partial"
    message = f"Discovery {discovery}; enrichment {enrichment}"
    if state != "succeeded":
        message += "; see source status for details"
    return state, message


def _execute(settings: Settings, fetcher: HttpFetcher | None, deadline_seconds: float | None,
             target_listing_id: int | None = None) -> tuple[str, str]:
    """Run one collection (caller holds the collector lock); never raises for collection failures.

    A target runs no discovery and enriches at most that one listing within the configured detail-check budget.
    """
    try:
        with _deadline(deadline_seconds):
            if target_listing_id is None:
                result = collector.run_collection(settings, fetcher=fetcher)
            else:
                result = collector.run_collection(settings, fetcher=fetcher, target_listing_id=target_listing_id)
    except RefreshDeadline:
        log.error("manual refresh exceeded its %ss deadline; aborted", deadline_seconds)
        return "failed", f"Refresh aborted after exceeding its {deadline_seconds:g}s deadline"
    except collector.SchemaNotMigrated:
        log.error("database schema not migrated; run `python -m rackscout migrate`")
        return "failed", "Database schema not migrated"
    except db.DatabaseUnavailable:
        log.error("database unavailable during manual refresh")
        return "failed", "Database unavailable during refresh"
    except psycopg.Error as exc:
        log.error("database error during manual refresh: %s", type(exc).__name__)
        return "failed", "Database error during refresh"
    except Exception as exc:  # noqa: BLE001 - record any collector failure instead of leaving the request running
        log.exception("manual refresh failed")
        return "failed", f"Refresh failed ({type(exc).__name__})"
    return final_state(result)


def _unlock(lock_conn: psycopg.Connection) -> None:
    try:
        lock_conn.execute("SELECT pg_advisory_unlock(%s)", (db.COLLECTOR_LOCK_KEY,))
    except psycopg.Error as exc:
        # Cancellation/timeouts can leave a healthy session holding the lock.
        # Discard it so the next poll reconnects instead of blocking all collection.
        log.warning("could not release collector lock; discarding session: %s", type(exc).__name__)
        lock_conn.close()


def process_pending(lock_conn: psycopg.Connection, settings: Settings, *, fetcher: HttpFetcher | None = None,
                    deadline_seconds: float | None = REFRESH_DEADLINE_SECONDS,
                    should_stop: Callable[[], bool] = lambda: False) -> str | None:
    """Handle the queued or interrupted request, if any. Returns its terminal state, else None.

    ``lock_conn`` is a ``connect_worker`` session (autocommit, bounded timeouts)
    that does not already hold the collector lock. Does nothing while another
    session (scheduled collector or another worker) holds the lock, so queued
    requests simply wait. ``should_stop`` is checked before taking the lock and
    again just before claiming; when it reports a shutdown the request is left
    untouched and the lock released.
    """
    row = lock_conn.execute("SELECT state FROM refresh_control WHERE id = 1").fetchone()
    if row is None or row["state"] not in ACTIVE:
        return None
    if should_stop() or not collector.try_collector_lock(lock_conn):
        return None
    try:
        with lock_conn.transaction():
            row = lock_conn.execute(_SELECT + " FOR UPDATE").fetchone()
            if row is None or row["state"] not in ACTIVE or should_stop():
                return None
            if row["state"] == "running":
                lock_conn.execute(_MARK_INTERRUPTED, (INTERRUPTED_MESSAGE,))
                log.warning("manual refresh %s was interrupted; marked failed", row["request_id"])
                return "failed"
            request_id = row["request_id"]
            target_listing_id = row["target_listing_id"]  # captured under the claim; never re-read
            lock_conn.execute(
                """UPDATE refresh_control SET state = 'running', started_at = now(), finished_at = NULL, message = NULL,
                     updated_at = now() WHERE id = 1"""
            )
        log.info("manual refresh %s started", request_id)
        if should_stop():
            # Shutdown may arrive during UPDATE/COMMIT. No source request has
            # started yet, so preserve this accepted request for the next worker.
            with lock_conn.transaction():
                lock_conn.execute(
                    """UPDATE refresh_control SET state = 'queued', started_at = NULL, updated_at = now()
                       WHERE id = 1 AND request_id = %s AND state = 'running'""",
                    (request_id,),
                )
            return None
        state, message = _execute(settings, fetcher, deadline_seconds, target_listing_id)
        with lock_conn.transaction():
            lock_conn.execute(
                """UPDATE refresh_control SET state = %s, message = %s, finished_at = now(), updated_at = now()
                   WHERE id = 1 AND request_id = %s AND state = 'running'""",
                (state, message[:500], request_id),
            )
        log.info("manual refresh %s finished: %s (%s)", request_id, state, message)
        return state
    finally:
        _unlock(lock_conn)


def run_worker(settings: Settings, poll_seconds: float = POLL_SECONDS) -> int:
    """Poll the queue until SIGTERM/SIGINT. A signal stops new claims; an active collection finishes
    (bounded by its deadline) unless the process is killed, in which case the next worker marks it failed."""
    stopping = False

    def stop(signum: int, frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("refresh worker started; polling every %gs", poll_seconds)
    lock_conn: psycopg.Connection | None = None
    try:
        while not stopping:
            try:
                if lock_conn is None or lock_conn.closed:
                    lock_conn = connect_worker(settings)
                process_pending(lock_conn, settings, should_stop=lambda: stopping)
            except db.DatabaseUnavailable:
                log.warning("database unavailable; retrying in %gs", poll_seconds)
            except psycopg.Error as exc:
                log.error("refresh worker database error: %s; reconnecting", type(exc).__name__)
                if lock_conn is not None:
                    lock_conn.close()  # also releases any collector lock still held by this session
                lock_conn = None
            if not stopping:
                time.sleep(poll_seconds)
    finally:
        if lock_conn is not None:
            lock_conn.close()
    log.info("refresh worker stopped")
    return 0
