"""Manual refresh: protected enqueue API, durable singleton queue and dedicated worker.

The web API only enqueues; the worker executes the existing collector while
holding the same global advisory lock as the scheduled collector. Database
tests share the isolated UUID-schema fixture from ``test_db``.
"""
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from test_db import (  # noqa: F401 - `conn` is a fixture
    TRAP,
    UNKNOWN_SHIP,
    conn,
    requires_db,
    settings,
    snapshot,
)

ORIGIN = "http://rackscout.apartment"
HEADERS = {"Origin": ORIGIN, "X-RackScout-Refresh": "1", "Sec-Fetch-Site": "same-origin"}


def refresh_settings(**changes):
    return replace(settings(), refresh_origins=(ORIGIN, "https://rackscout.apartment"), **changes)


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from rackscout import api

    monkeypatch.setattr(api, "_settings", refresh_settings())
    return TestClient(api.app)


class SourceFetcher:
    """Controlled replacement for HttpFetcher: Dell page carries the snapshot, other pages are empty."""

    def __init__(self, snap, *, gate=None, delay=0.0, error=None):
        self.snap, self.gate, self.delay, self.error = snap, gate, delay, error
        self.index_calls: list[str] = []
        self.description_calls: list[str] = []

    def fetch_index(self, url, max_bytes):
        self.index_calls.append(url)
        if self.gate is not None:
            self.gate.wait(30)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if not url.endswith("/dell/"):
            return replace(self.snap, records=[])
        return self.snap

    def fetch_description(self, item_id, max_bytes):
        self.description_calls.append(item_id)
        return {"ram_gb": None, "exclusions": []}


class NoNetwork:
    def fetch_index(self, url, max_bytes):
        raise AssertionError("network fetch attempted")

    def fetch_description(self, item_id, max_bytes):
        raise AssertionError("network fetch attempted")


def _snap():
    return snapshot(datetime.now(timezone.utc), [TRAP, UNKNOWN_SHIP])


def _row(conn):
    conn.commit()
    return conn.execute("SELECT * FROM refresh_control WHERE id = 1").fetchone()


def _worker_conn():
    """The production worker session (bounded statement/lock timeouts)."""
    from rackscout import refresh

    return refresh.connect_worker(settings())


def _hold_collector_lock():
    """A separate session holding the global collector lock, like a running scheduled collect."""
    from rackscout import db

    holder = db.connect(autocommit=True)
    assert holder.execute("SELECT pg_try_advisory_lock(%s) AS ok", (db.COLLECTOR_LOCK_KEY,)).fetchone()["ok"]
    return holder


def _lock_free(conn):
    from rackscout import refresh

    conn.commit()
    return not refresh.collector_running(conn)


def _enqueue(s=None):
    from rackscout import refresh

    c = _worker_conn()
    try:
        return refresh.enqueue(c, s or refresh_settings())
    finally:
        c.close()


def _process(s=None, fetcher=None, **kwargs):
    from rackscout import refresh

    c = _worker_conn()
    try:
        return refresh.process_pending(c, s or refresh_settings(max_details=0), fetcher=fetcher or SourceFetcher(_snap()),
                                       **kwargs)
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Configuration boundary
# ---------------------------------------------------------------------------


def test_refresh_origins_parse_exact_origins_and_reject_unsafe_values(monkeypatch):
    from rackscout.config import ConfigError, load_settings

    monkeypatch.setenv("RACKSCOUT_REFRESH_ORIGINS", " http://RackScout.apartment , https://rackscout.apartment:443 ")
    assert load_settings().refresh_origins == ("http://rackscout.apartment", "https://rackscout.apartment")
    for bad in ("*", "http://*.apartment", "http://rackscout.apartment/", "http://rackscout.apartment/api",
                "https://user:pw@rackscout.apartment", "ftp://rackscout.apartment", "rackscout.apartment",
                "http://rackscout.apartment?x=1", "null"):
        monkeypatch.setenv("RACKSCOUT_REFRESH_ORIGINS", bad)
        with pytest.raises(ConfigError):
            load_settings()


# ---------------------------------------------------------------------------
# Request protection (rejected before any database access)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("headers, status", [
    ({"X-RackScout-Refresh": "1"}, 403),                                   # missing Origin
    ({"Origin": "null", "X-RackScout-Refresh": "1"}, 403),
    ({"Origin": "http://evil.invalid", "X-RackScout-Refresh": "1"}, 403),
    ({"Origin": "http://rackscout.apartment.evil.invalid", "X-RackScout-Refresh": "1"}, 403),
    ({"Origin": "http://rackscout.apartment:8080", "X-RackScout-Refresh": "1"}, 403),
    ({**HEADERS, "Sec-Fetch-Site": "cross-site"}, 403),
    ({**HEADERS, "Sec-Fetch-Site": "same-site"}, 403),
    ({"Origin": ORIGIN}, 403),                                             # missing custom header
    ({"Origin": ORIGIN, "X-RackScout-Refresh": "0"}, 403),
])
def test_post_rejects_untrusted_requests_without_touching_database(client, monkeypatch, headers, status):
    from rackscout import db

    monkeypatch.setattr(db, "connect", lambda *a, **k: pytest.fail("database opened for rejected request"))
    response = client.post("/api/refresh", json={}, headers=headers)
    assert response.status_code == status
    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}


@pytest.mark.parametrize("kwargs, status", [
    ({"content": b"{}", "headers": {"Content-Type": "text/plain"}}, 415),
    ({"content": b"{}", "headers": {"Content-Type": "application/x-www-form-urlencoded"}}, 415),
    ({"json": {"force": True}}, 422),
    ({"json": {"max_details": 500}}, 422),
    ({"json": []}, 422),
    ({"content": b"", "headers": {"Content-Type": "application/json"}}, 422),
    ({"content": b"{" + b" " * 100000 + b"}", "headers": {"Content-Type": "application/json"}}, 413),
])
def test_post_accepts_no_options_and_bounds_body(client, monkeypatch, kwargs, status):
    from rackscout import db

    monkeypatch.setattr(db, "connect", lambda *a, **k: pytest.fail("database opened for rejected request"))
    headers = {**HEADERS, **kwargs.pop("headers", {})}
    assert client.post("/api/refresh", headers=headers, **kwargs).status_code == status


def test_post_when_disabled_is_forbidden(monkeypatch):
    from fastapi.testclient import TestClient

    from rackscout import api, db

    monkeypatch.setattr(api, "_settings", replace(settings(), refresh_origins=()))
    monkeypatch.setattr(db, "connect", lambda *a, **k: pytest.fail("database opened for disabled refresh"))
    assert TestClient(api.app).post("/api/refresh", json={}, headers=HEADERS).status_code == 403


def test_other_methods_remain_blocked(client):
    for method in ("PUT", "PATCH", "DELETE"):
        assert client.request(method, "/api/refresh", json={}, headers=HEADERS).status_code == 405
    assert client.post("/api/listings", json={}, headers=HEADERS).status_code == 405
    assert client.post("/api/status", json={}, headers=HEADERS).status_code == 405


def test_database_failure_is_sanitized_503(client, monkeypatch):
    from rackscout import db

    def boom(*args, **kwargs):
        raise db.DatabaseUnavailable("database connection failed")

    monkeypatch.setattr(db, "connect", boom)
    for response in (client.get("/api/refresh"), client.post("/api/refresh", json={}, headers=HEADERS)):
        assert response.status_code == 503
        assert response.json() == {"detail": "Database unavailable"}


# ---------------------------------------------------------------------------
# Enqueue, deduplication, cooldown
# ---------------------------------------------------------------------------


STATUS_KEYS = {"enabled", "state", "request_id", "requested_at", "started_at", "finished_at", "retry_at",
               "retry_after_seconds", "collector_running", "can_refresh", "message"}


@requires_db
def test_idle_request_is_accepted_and_duplicates_conflict(conn, client):
    idle = client.get("/api/refresh")
    assert idle.status_code == 200
    body = idle.json()
    assert set(body) == STATUS_KEYS
    assert (body["enabled"], body["state"], body["can_refresh"], body["request_id"]) == (True, "idle", True, None)

    accepted = client.post("/api/refresh", json={}, headers=HEADERS)
    assert accepted.status_code == 202
    queued = accepted.json()
    assert set(queued) == STATUS_KEYS
    assert queued["state"] == "queued" and queued["request_id"] and queued["requested_at"]
    assert queued["can_refresh"] is False
    assert _row(conn)["state"] == "queued"

    duplicate = client.post("/api/refresh", json={}, headers=HEADERS)
    assert duplicate.status_code == 409
    assert duplicate.json()["request_id"] == queued["request_id"]
    assert client.get("/api/refresh").json()["can_refresh"] is False


@requires_db
def test_concurrent_requests_accept_exactly_one(conn):
    barrier = threading.Barrier(8)
    results: list[str] = []

    def submit():
        barrier.wait()
        results.append(_enqueue()[0])

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(results) == ["accepted"] + ["active"] * 7
    assert _row(conn)["state"] == "queued"


@requires_db
def test_cooldown_counts_from_last_accepted_request_and_survives_completion(conn, client):
    first = client.post("/api/refresh", json={}, headers=HEADERS).json()
    assert _process() == "succeeded"

    done = client.get("/api/refresh").json()
    assert done["state"] == "succeeded" and done["request_id"] == first["request_id"]
    assert done["can_refresh"] is False and 3500 < done["retry_after_seconds"] <= 3600
    assert done["retry_at"] is not None

    limited = client.post("/api/refresh", json={}, headers=HEADERS)
    assert limited.status_code == 429
    assert 3500 < int(limited.headers["Retry-After"]) <= 3600
    assert limited.json()["request_id"] == first["request_id"]

    # Durable: age the accepted request in the database (as after pod restarts) past the cooldown.
    conn.execute("UPDATE refresh_control SET requested_at = now() - interval '3601 seconds'")
    assert client.get("/api/refresh").json()["can_refresh"] is True
    again = client.post("/api/refresh", json={}, headers=HEADERS)
    assert again.status_code == 202
    assert again.json()["request_id"] != first["request_id"]


# ---------------------------------------------------------------------------
# Shared collector lock with the scheduled collector
# ---------------------------------------------------------------------------


@requires_db
def test_running_scheduled_collect_blocks_requests_and_defers_queued_work(conn, client):
    holder = _hold_collector_lock()
    try:
        state = client.get("/api/refresh").json()
        assert state["collector_running"] is True and state["can_refresh"] is False
        assert client.post("/api/refresh", json={}, headers=HEADERS).status_code == 409
        assert _row(conn)["state"] == "idle"
    finally:
        holder.close()

    assert _enqueue()[0] == "accepted"
    holder = _hold_collector_lock()
    try:
        assert _process(fetcher=NoNetwork()) is None  # never runs concurrently, never skipped as success
        assert _row(conn)["state"] == "queued"
    finally:
        holder.close()
    assert _process() == "succeeded"


@requires_db
def test_worker_holds_global_lock_and_second_worker_never_duplicates_claim(conn):
    from rackscout import collector

    assert _enqueue()[0] == "accepted"
    gate = threading.Event()
    fetcher = SourceFetcher(_snap(), gate=gate)
    outcome: list = []
    worker = threading.Thread(target=lambda: outcome.append(
        _process(refresh_settings(max_details=0), fetcher, deadline_seconds=None)))
    worker.start()
    deadline = time.monotonic() + 20
    while not fetcher.index_calls and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        assert _row(conn)["state"] == "running"
        assert not _lock_free(conn)
        # A second worker (rolling update) and the scheduled collector must both stand aside.
        assert _process(fetcher=NoNetwork()) is None
        assert _row(conn)["state"] == "running"
        assert collector.collect(refresh_settings()) == 0
    finally:
        gate.set()
        worker.join(60)
    assert outcome == ["succeeded"]
    row = _row(conn)
    assert row["state"] == "succeeded" and row["finished_at"] is not None
    assert _lock_free(conn)


@requires_db
def test_source_backoff_is_honored_by_manual_refresh(conn):
    from rackscout import collector

    collector.set_backoff(conn, collector.INDEX_ORIGIN, 3600, "challenge")
    conn.commit()
    assert _enqueue()[0] == "accepted"
    assert _process(fetcher=NoNetwork()) == "blocked"
    row = _row(conn)
    assert row["state"] == "blocked" and row["finished_at"] is not None
    run = conn.execute("SELECT outcome FROM source_runs WHERE kind = 'discovery' ORDER BY id DESC LIMIT 1").fetchone()
    assert run["outcome"] == "blocked"


# ---------------------------------------------------------------------------
# Worker execution and outcomes
# ---------------------------------------------------------------------------


@requires_db
def test_worker_collects_and_reports_partial_when_page_checks_disabled(conn, client):
    from rackscout import queries

    request = client.post("/api/refresh", json={}, headers=HEADERS).json()
    fetcher = SourceFetcher(_snap())
    assert _process(refresh_settings(max_details=25, browser_enabled=False), fetcher) == "partial"

    assert len(fetcher.index_calls) == 5
    assert sorted(fetcher.description_calls) == sorted([TRAP[0], UNKNOWN_SHIP[0]])
    assert queries.status(conn)["inventory_count"] >= 2
    runs = {r["kind"]: r["outcome"] for r in conn.execute(
        "SELECT DISTINCT ON (kind) kind, outcome FROM source_runs ORDER BY kind, id DESC").fetchall()}
    assert runs == {"discovery": "success", "enrichment": "partial"}
    state = client.get("/api/refresh").json()
    assert (state["state"], state["request_id"]) == ("partial", request["request_id"])
    assert state["started_at"] and state["finished_at"] and state["message"]
    assert state["collector_running"] is False


@requires_db
def test_worker_reports_failed_discovery_with_sanitized_message(conn):
    from rackscout.sources import SourceError

    assert _enqueue()[0] == "accepted"
    assert _process(fetcher=SourceFetcher(_snap(), error=SourceError("layout changed"))) == "failed"
    assert _row(conn)["state"] == "failed"

    conn.execute("UPDATE refresh_control SET requested_at = now() - interval '2 hours'")
    assert _enqueue()[0] == "accepted"
    secret = RuntimeError("postgresql://rackscout:hunter2@db.internal/rackscout")
    assert _process(fetcher=SourceFetcher(_snap(), error=secret)) == "failed"
    row = _row(conn)
    assert row["state"] == "failed" and row["finished_at"] is not None
    assert "hunter2" not in (row["message"] or "") and "db.internal" not in (row["message"] or "")
    assert _lock_free(conn)


@requires_db
def test_worker_deadline_aborts_collection_and_releases_lock(conn):
    assert _enqueue()[0] == "accepted"
    fetcher = SourceFetcher(_snap(), delay=3.0)
    started = time.monotonic()
    assert _process(fetcher=fetcher, deadline_seconds=1) == "failed"
    assert time.monotonic() - started < 3.0
    row = _row(conn)
    assert row["state"] == "failed" and "deadline" in row["message"].lower()
    assert len(fetcher.index_calls) == 1
    assert _lock_free(conn)


@requires_db
def test_interrupted_running_request_fails_once_lock_is_free_and_is_not_rerun(conn, client):
    assert _enqueue()[0] == "accepted"
    conn.execute("UPDATE refresh_control SET state = 'running', started_at = now()")

    holder = _hold_collector_lock()  # the original worker may still be alive
    try:
        assert _process(fetcher=NoNetwork()) is None
        assert _row(conn)["state"] == "running"
    finally:
        holder.close()

    assert _process(fetcher=NoNetwork()) == "failed"
    row = _row(conn)
    assert row["state"] == "failed" and row["finished_at"] is not None and "interrupt" in row["message"].lower()
    assert _process(fetcher=NoNetwork()) is None  # never re-run automatically
    assert client.get("/api/refresh").json()["state"] == "failed"


@requires_db
def test_queued_request_survives_worker_restart(conn):
    accepted, status = _enqueue()
    assert accepted == "accepted"
    from rackscout import db

    # A worker that connected and exited without claiming leaves the request untouched.
    c = db.connect(autocommit=True)
    c.close()
    row = _row(conn)
    assert row["state"] == "queued" and str(row["request_id"]) == status["request_id"]
    assert _process() == "succeeded"


@requires_db
def test_stop_request_before_claim_leaves_request_queued_and_lock_free(conn):
    assert _enqueue()[0] == "accepted"
    fetcher = SourceFetcher(_snap())
    assert _process(fetcher=fetcher, should_stop=lambda: True) is None
    assert _row(conn)["state"] == "queued" and _lock_free(conn)

    # Stop arriving after the collector lock was acquired but before the claim commits.
    assert _process(fetcher=fetcher, should_stop=lambda: not _lock_free(conn)) is None
    row = _row(conn)
    assert row["state"] == "queued" and row["started_at"] is None
    assert _lock_free(conn)
    assert fetcher.index_calls == []
    assert _process(fetcher=fetcher) == "succeeded"


@requires_db
def test_worker_queue_queries_are_bounded_under_contention(conn):
    import psycopg

    from rackscout import db, refresh

    assert _enqueue()[0] == "accepted"
    production = _worker_conn()
    try:
        assert production.execute("SHOW lock_timeout").fetchone()["lock_timeout"] not in ("0", "0ms")
        assert production.execute("SHOW statement_timeout").fetchone()["statement_timeout"] not in ("0", "0ms")
    finally:
        production.close()

    blocker = db.connect()  # holds conflicting locks in an open transaction
    worker = _worker_conn()
    worker.execute("SET lock_timeout = '200ms'")
    try:
        for lock_sql in ("SELECT 1 FROM refresh_control WHERE id = 1 FOR UPDATE",       # claim row contention
                         "LOCK TABLE refresh_control IN ACCESS EXCLUSIVE MODE"):         # initial poll contention
            blocker.execute(lock_sql)
            started = time.monotonic()
            with pytest.raises(psycopg.errors.LockNotAvailable):
                refresh.process_pending(worker, refresh_settings(max_details=0), fetcher=NoNetwork())
            assert time.monotonic() - started < 5
            blocker.rollback()
            assert _row(conn)["state"] == "queued"
            assert _lock_free(conn)
    finally:
        blocker.close()
        worker.close()
    assert _process() == "succeeded"


@requires_db
def test_status_reads_never_fetch_external_data(conn, client, monkeypatch):
    import httpx

    from rackscout import sources

    def no_network(*args, **kwargs):
        raise AssertionError("network fetch attempted")

    monkeypatch.setattr(sources, "HttpFetcher", no_network)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    assert client.post("/api/refresh", json={}, headers=HEADERS).status_code == 202
    for path in ("/api/refresh", "/api/status"):
        assert client.get(path).status_code == 200
    assert _row(conn)["state"] == "queued"


@requires_db
def test_shutdown_after_claim_commit_preserves_unstarted_request(conn):
    assert _enqueue()[0] == "accepted"
    original = _row(conn)
    assert _process(fetcher=NoNetwork(), should_stop=lambda: _row(conn)["state"] == "running") is None
    row = _row(conn)
    assert row["state"] == "queued" and row["started_at"] is None
    assert row["request_id"] == original["request_id"] and row["requested_at"] == original["requested_at"]
    assert _lock_free(conn)


@requires_db
def test_cancelled_unlock_discards_session_and_releases_global_lock(conn):
    import psycopg
    from rackscout import collector, refresh

    worker = _worker_conn()
    assert collector.try_collector_lock(worker)
    assert not _lock_free(conn)

    class CancelledUnlock:
        def execute(self, *args, **kwargs):
            raise psycopg.errors.QueryCanceled("statement timeout")

        def close(self):
            worker.close()

    try:
        refresh._unlock(CancelledUnlock())
        assert _lock_free(conn)
    finally:
        worker.close()
