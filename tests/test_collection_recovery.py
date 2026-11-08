"""Collection resumes database checkpoints without replaying marketplace requests."""
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import partial

import pytest
from test_db import TRAP, UNKNOWN_SHIP, conn, requires_db, settings, snapshot  # noqa: F401
from test_manual_refresh import SourceFetcher

pytestmark = requires_db


@requires_db
def test_collect_survives_lost_sessions_without_refetching_listing(conn, monkeypatch):
    from rackscout import collector, db

    opened = []
    connect = db.connect

    def tracked_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        opened.append(connection)
        return connection

    class InterruptedFetcher(SourceFetcher):
        interrupted = False

        def fetch_description(self, item_id, max_bytes):
            result = super().fetch_description(item_id, max_bytes)
            if not self.interrupted:
                self.interrupted = True
                for connection in opened:
                    if not connection.closed:
                        conn.execute("SELECT pg_terminate_backend(%s)", (connection.info.backend_pid,))
            return result

        def close(self):
            pass

    fetcher = InterruptedFetcher(snapshot(datetime.now(timezone.utc), [TRAP, UNKNOWN_SHIP]))
    monkeypatch.setattr(db, "connect", tracked_connect)
    monkeypatch.setattr(collector, "HttpFetcher", lambda *args, **kwargs: fetcher)

    assert collector.collect(settings(), max_details=2, browser=False) == 0
    assert sorted(fetcher.description_calls) == sorted([TRAP[0], UNKNOWN_SHIP[0]])
    assert len(fetcher.index_calls) == len(set(fetcher.index_calls))
    rows = conn.execute("SELECT item_id, description_status, last_checked_at FROM listings ORDER BY item_id").fetchall()
    assert [row["item_id"] for row in rows] == sorted([TRAP[0], UNKNOWN_SHIP[0]])
    assert all(row["description_status"] == "verified" and row["last_checked_at"] is not None for row in rows)
    runs = conn.execute("SELECT kind, finished_at, discovered_count, enriched_count FROM source_runs ORDER BY id").fetchall()
    assert [run["kind"] for run in runs] == ["discovery", "enrichment"]
    assert all(run["finished_at"] is not None for run in runs)
    assert runs[0]["discovered_count"] == 2 and runs[1]["enriched_count"] == 2


# --------------------------------------------------------------------------- helpers


@pytest.fixture()
def opened(monkeypatch):
    """Every connection the collector (and its session) opens through ``db.connect``."""
    from rackscout import db

    connections = []
    connect = db.connect

    def tracked_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(db, "connect", tracked_connect)
    return connections


def _terminate(admin, connections):
    """Kill the collector's backends (like a PostgreSQL pod recreation) and wait until they are gone."""
    pids = [c.info.backend_pid for c in connections if not c.closed]
    admin.execute("SELECT pg_terminate_backend(pid) FROM unnest(%s::int[]) AS pid", (pids,))
    deadline = time.monotonic() + 10
    while admin.execute("SELECT count(*) AS n FROM pg_stat_activity WHERE pid = ANY(%s)", (pids,)).fetchone()["n"]:
        assert time.monotonic() < deadline, "terminated backends did not exit"
        time.sleep(0.05)


def _lock_free(conn):
    from rackscout import refresh

    return not refresh.collector_running(conn)


def _runs(conn):
    return conn.execute(
        "SELECT kind, outcome, message, finished_at, discovered_count, enriched_count FROM source_runs ORDER BY id"
    ).fetchall()


def _no_duplicate_history(conn):
    configs = conn.execute("SELECT count(*) AS n FROM configurations").fetchone()["n"]
    history = conn.execute("SELECT count(*) AS n FROM price_history").fetchone()["n"]
    return configs > 0 and history == configs


class ReplayingSession:
    """Session double: commits each checkpoint, then runs it again, as a session does when a commit's
    acknowledgement is lost with the connection. Resume guards must accept the collection's own commit."""

    def __init__(self, connection):
        self.connection = connection
        self.guards = []
        self.replays = 0

    def run(self, callback):
        with self.connection.transaction():
            callback(self.connection)
        assert all(guard(self.connection) for guard in self.guards)
        self.replays += 1
        with self.connection.transaction():
            return callback(self.connection)

    def check(self):
        assert all(guard(self.connection) for guard in self.guards)

    def add_resume_guard(self, guard):
        self.guards.append(guard)


def _browser_result():
    return {"status": "verified", "has_variations": False, "price_kind": "exact", "item_price": "400.00",
            "variations": []}


# --------------------------------------------------------------------------- recovery


def test_disconnects_in_quiet_gap_and_after_browser_check_resume_without_repeating_requests(
        conn, opened, monkeypatch):
    from types import SimpleNamespace

    from rackscout import browser, collector

    fetcher = SourceFetcher(snapshot(datetime.now(timezone.utc), [TRAP, UNKNOWN_SHIP]))
    fetcher.close = lambda: None
    gaps = []

    class Checker:
        started = True
        checked: list[str] = []

        def __init__(self, *args, **kwargs):
            pass

        def check(self, item_id):
            Checker.checked.append(item_id)
            result = _browser_result()
            if len(Checker.checked) == 1:
                _terminate(conn, opened)  # the page was acquired; its persistence must not re-run the browser
            return result

        def close(self):
            pass

    def quiet_gap(seconds):
        gaps.append(seconds)
        _terminate(conn, opened)

    monkeypatch.setattr(collector, "HttpFetcher", lambda *args, **kwargs: fetcher)
    monkeypatch.setattr(collector, "time", SimpleNamespace(sleep=quiet_gap))
    monkeypatch.setattr(browser, "BrowserChecker", Checker)

    assert collector.collect(replace(settings(), request_delay=6.0, browser_enabled=True), max_details=2, browser=True) == 0
    assert gaps == [6.0]  # the quiet gap is kept, exactly once between the two listings
    assert sorted(fetcher.description_calls) == sorted(Checker.checked) == sorted([TRAP[0], UNKNOWN_SHIP[0]])
    rows = conn.execute("SELECT description_status, page_status, last_checked_at FROM listings").fetchall()
    assert all(r["description_status"] == "verified" and r["page_status"] == "verified" and r["last_checked_at"]
               for r in rows)
    runs = _runs(conn)
    assert [r["kind"] for r in runs] == ["discovery", "enrichment"]
    assert runs[1]["finished_at"] is not None and runs[1]["enriched_count"] == 2
    assert _no_duplicate_history(conn) and _lock_free(conn)


def test_discovery_apply_disconnect_retries_application_without_refetching(conn, opened, monkeypatch):
    from rackscout import collector

    fetcher = SourceFetcher(snapshot(datetime.now(timezone.utc), [TRAP, UNKNOWN_SHIP]))
    fetcher.close = lambda: None
    apply_page = collector.apply_page
    applied = []

    def interrupted_apply(c, page, catalog, seen):
        duplicates = apply_page(c, page, catalog, seen)
        applied.append(page.url)
        if page.snapshot.records and len(applied) == len(set(applied)):
            _terminate(conn, opened)  # mid-transaction: the whole snapshot application rolls back and is retried
        return duplicates

    monkeypatch.setattr(collector, "HttpFetcher", lambda *args, **kwargs: fetcher)
    monkeypatch.setattr(collector, "apply_page", interrupted_apply)

    assert collector.collect(settings(), max_details=0, browser=False) == 0
    assert len(fetcher.index_calls) == len(set(fetcher.index_calls)) == len(settings().source_pages)
    assert len(applied) > len(set(applied))  # the application really was retried
    runs = _runs(conn)
    assert [r["kind"] for r in runs] == ["discovery"]
    assert runs[0]["outcome"] == "success" and runs[0]["discovered_count"] == 2
    assert "merged" not in runs[0]["message"]  # the retry did not see its rolled-back attempt's items as duplicates
    assert conn.execute("SELECT count(*) AS n FROM listings").fetchone()["n"] == 2
    assert _no_duplicate_history(conn) and _lock_free(conn)


def test_newer_collection_during_outage_fences_old_collector(conn, opened, monkeypatch):
    import psycopg
    from psycopg.rows import dict_row

    from rackscout import collector
    from rackscout.collection_session import CollectorOwnershipLost
    from rackscout.config import database_conninfo

    class TakeoverFetcher(SourceFetcher):
        def fetch_description(self, item_id, max_bytes):
            result = super().fetch_description(item_id, max_bytes)
            if len(self.description_calls) == 1:
                _terminate(conn, opened)
                # Untracked: the newer collector is another process, outside the old one's session.
                newer = psycopg.connect(database_conninfo(), autocommit=True, row_factory=dict_row)
                try:
                    deadline = time.monotonic() + 10
                    while not collector.try_collector_lock(newer):
                        assert time.monotonic() < deadline
                        time.sleep(0.05)
                    checkpoints = collector.Checkpoints(newer)
                    checkpoints.begin()
                    run_id = checkpoints.start_run("discovery")
                    checkpoints.run(partial(collector.finish_run, run_id=run_id, outcome="failed",
                                            message="newer collection"))
                finally:
                    newer.close()  # releases the lock before the old collector reconnects
            return result

    fetcher = TakeoverFetcher(snapshot(datetime.now(timezone.utc), [TRAP, UNKNOWN_SHIP]))
    fetcher.close = lambda: None
    monkeypatch.setattr(collector, "HttpFetcher", lambda *args, **kwargs: fetcher)

    with pytest.raises(CollectorOwnershipLost):
        collector.collect(settings(), max_details=2, browser=False)
    assert len(fetcher.description_calls) == 1  # nothing more fetched after losing ownership
    assert conn.execute("SELECT count(*) AS n FROM listings WHERE last_checked_at IS NOT NULL").fetchone()["n"] == 0
    runs = _runs(conn)
    assert [(r["kind"], r["outcome"]) for r in runs] == [
        ("discovery", "success"), ("enrichment", "failed"), ("discovery", "failed")]
    assert runs[1]["message"] == collector.ORPHAN_RUN_MESSAGE and runs[2]["message"] == "newer collection"
    assert _lock_free(conn)


def test_collection_start_finalizes_runs_a_stopped_collector_left_open(conn, monkeypatch):
    from rackscout import collector

    conn.execute("INSERT INTO source_runs (provider, kind) VALUES ('all', 'enrichment')")
    fetcher = SourceFetcher(snapshot(datetime.now(timezone.utc), [TRAP]))
    session = ReplayingSession(conn)
    result = collector.run_collection(settings(), max_details=0, browser=False, fetcher=fetcher, session=session)
    assert result.discovery == "success"
    runs = _runs(conn)
    assert [(r["kind"], r["outcome"]) for r in runs] == [("enrichment", "failed"), ("discovery", "success")]
    assert runs[0]["message"] == collector.ORPHAN_RUN_MESSAGE
    assert all(r["finished_at"] is not None for r in runs)


# --------------------------------------------------------------------------- ambiguous commits


def test_replayed_checkpoints_never_duplicate_runs_history_or_counts(conn):
    from rackscout import collector

    fetcher = SourceFetcher(snapshot(datetime.now(timezone.utc), [TRAP, UNKNOWN_SHIP]))
    session = ReplayingSession(conn)
    result = collector.run_collection(settings(), max_details=2, browser=False, fetcher=fetcher, session=session)

    assert result == collector.CollectionResult("success", "partial")  # no browser: prices stay unverified
    assert session.replays > 0
    assert sorted(fetcher.description_calls) == sorted([TRAP[0], UNKNOWN_SHIP[0]])
    assert len(fetcher.index_calls) == len(set(fetcher.index_calls))
    runs = _runs(conn)
    assert [r["kind"] for r in runs] == ["discovery", "enrichment"]
    assert runs[0]["discovered_count"] == 2 and runs[1]["enriched_count"] == 2
    assert all(r["finished_at"] is not None for r in runs)
    assert _no_duplicate_history(conn)
    assert session.guards and all(guard(conn) for guard in session.guards)  # own completed work stays acceptable


def test_backoff_expiry_is_fixed_at_observation_and_never_shortened(conn):
    from rackscout import collector
    from rackscout.config import DESCRIPTION_HOST
    from rackscout.sources import SourceBlocked

    class Challenged(SourceFetcher):
        def fetch_description(self, item_id, max_bytes):
            self.description_calls.append(item_id)
            raise SourceBlocked("description origin returned a challenge page", 600)

    fetcher = Challenged(snapshot(datetime.now(timezone.utc), [TRAP, UNKNOWN_SHIP]))
    collector.run_collection(settings(), max_details=2, browser=False, fetcher=fetcher,
                             session=ReplayingSession(conn))
    assert len(fetcher.description_calls) == 1  # the block stops further requests to that origin
    blocked = conn.execute("SELECT blocked_until FROM origin_backoff WHERE origin = %s",
                           (DESCRIPTION_HOST,)).fetchone()["blocked_until"]
    checked = conn.execute("SELECT description_checked_at FROM listings WHERE item_id = %s",
                           (fetcher.description_calls[0],)).fetchone()["description_checked_at"]
    assert blocked == checked + timedelta(seconds=600)  # replayed checkpoint kept the observed expiry

    collector.set_backoff(conn, DESCRIPTION_HOST, 5, "shorter")
    assert collector.backoff_until(conn, DESCRIPTION_HOST) == blocked

    conn.execute("UPDATE listings SET next_check_after = NULL")
    again = Challenged(snapshot(datetime.now(timezone.utc), [TRAP, UNKNOWN_SHIP]))
    collector.run_collection(settings(), max_details=2, browser=False, fetcher=again, session=ReplayingSession(conn))
    assert again.description_calls == []  # backoff honored by the next collection
