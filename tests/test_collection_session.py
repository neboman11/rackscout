"""CollectionSession recovers lost collector sessions under the exclusive collector lock, and only those."""
import os
import time
from contextlib import contextmanager
from dataclasses import replace

import psycopg
import pytest
from psycopg.rows import dict_row

from test_db import conn, requires_db, settings  # noqa: F401

pytestmark = requires_db


@pytest.fixture()
def opened(conn, monkeypatch):
    """Connections opened through db.connect (the session's replacements); all closed at teardown."""
    from rackscout import db

    connections = []
    real = db.connect

    def tracked(*args, **kwargs):
        connection = real(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(db, "connect", tracked)
    yield connections
    for connection in connections:
        connection.close()


@pytest.fixture()
def collector_conn(conn):
    """Factory for test-schema autocommit sessions, by default holding the collector lock."""
    from rackscout import db

    made = []

    def make(lock=True):
        connection = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row)
        made.append(connection)
        if lock:
            assert connection.execute("SELECT pg_try_advisory_lock(%s) AS ok", (db.COLLECTOR_LOCK_KEY,)).fetchone()["ok"]
        return connection

    yield make
    for connection in made:
        connection.close()


def lock_holders(admin):
    from rackscout import db

    key = db.COLLECTOR_LOCK_KEY
    return [row["pid"] for row in admin.execute(
        """SELECT pid FROM pg_locks
           WHERE locktype = 'advisory' AND granted AND objsubid = 1
             AND classid = %s::bigint::oid AND objid = %s::bigint::oid
             AND database = (SELECT oid FROM pg_database WHERE datname = current_database())""",
        (key >> 32, key & 0xFFFFFFFF),
    ).fetchall()]


def locks_released(admin):
    """Client close is asynchronous; wait for PostgreSQL to process its disconnect."""
    deadline = time.monotonic() + 3
    while lock_holders(admin):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def terminate(admin, connection):
    """Kill the session's backend like a PostgreSQL restart and wait until it and its lock are gone."""
    pid = connection.info.backend_pid
    assert admin.execute("SELECT pg_terminate_backend(%s) AS ok", (pid,)).fetchone()["ok"]
    deadline = time.monotonic() + 10
    while (pid in lock_holders(admin) or admin.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = %s) AS alive", (pid,)).fetchone()["alive"]):
        assert time.monotonic() < deadline, "terminated backend did not exit"
        time.sleep(0.05)


def count_probe(admin):
    return admin.execute("SELECT count(*) AS n FROM probe").fetchone()["n"]


class NoWaitTime:
    """Real monotonic clock; any recovery pause fails the test."""

    monotonic = staticmethod(time.monotonic)

    @staticmethod
    def sleep(seconds):
        raise AssertionError(f"recovery waited {seconds}s")


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_recovers_terminated_session_and_retries_only_the_db_callback(conn, opened, collector_conn):
    from rackscout.collection_session import CollectionSession

    conn.execute("CREATE TABLE probe (n integer NOT NULL)")
    initial = collector_conn()
    initial_pid = initial.info.backend_pid
    guard_pids = []

    def guard(connection):
        assert lock_holders(connection) == [connection.info.backend_pid]  # exclusive lock already re-taken
        guard_pids.append(connection.info.backend_pid)
        return True

    calls = []

    def write(connection):
        calls.append(connection.info.backend_pid)
        connection.execute("INSERT INTO probe VALUES (1)")
        return connection.execute("SELECT count(*) AS n FROM probe").fetchone()["n"]

    session = CollectionSession(settings(), initial, resume_guard=guard)
    try:
        assert session.run(write) == 1
        assert opened == [] and guard_pids == []

        terminate(conn, initial)
        assert session.run(write) == 2
        replacement = session.connection
        replacement_pid = replacement.info.backend_pid
        assert opened == [replacement] and initial.closed
        assert calls == [initial_pid, replacement_pid]  # never invoked on the dead session
        assert guard_pids == [replacement_pid]
        assert lock_holders(conn) == [replacement_pid]
        assert replacement.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "10s"
        assert replacement.execute("SHOW lock_timeout").fetchone()["lock_timeout"] == "10s"

        terminate(conn, replacement)
        session.check()
        second = session.connection
        assert opened == [replacement, second] and replacement.closed
        assert guard_pids == [replacement_pid, second.info.backend_pid]
        assert lock_holders(conn) == [second.info.backend_pid]
    finally:
        session.close()
    assert session.connection.closed
    assert locks_released(conn)
    assert count_probe(conn) == 2 and len(calls) == 2


def test_commit_aborted_in_flight_is_replayed_once_without_duplicates(conn, opened, collector_conn):
    from rackscout.collection_session import CollectionSession

    conn.execute("CREATE TABLE probe (n integer NOT NULL)")
    conn.execute("CREATE SEQUENCE commit_kills")
    # The first COMMIT kills its own backend while running deferred triggers: the commit never completes.
    conn.execute("""CREATE FUNCTION die_once() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF nextval('commit_kills') = 1 THEN
            PERFORM pg_terminate_backend(pg_backend_pid());
            PERFORM pg_sleep(5);
          END IF;
          RETURN NULL;
        END $$""")
    conn.execute("CREATE CONSTRAINT TRIGGER die_at_commit AFTER INSERT ON probe"
                 " DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION die_once()")
    initial = collector_conn()
    calls = []

    def write(connection):
        calls.append(connection.info.backend_pid)
        connection.execute("INSERT INTO probe VALUES (1)")
        return "written"

    session = CollectionSession(settings(), initial)
    try:
        assert session.run(write) == "written"
        assert len(calls) == 2 and calls[1] == session.connection.info.backend_pid
        assert count_probe(conn) == 1
        assert lock_holders(conn) == [session.connection.info.backend_pid]
    finally:
        session.close()
    assert locks_released(conn)


@pytest.mark.parametrize("writes", [True, False], ids=["write", "read-only"])
def test_commit_acknowledgement_lost_after_commit_is_not_replayed(conn, opened, collector_conn, writes):
    from rackscout.collection_session import CollectionSession

    conn.execute("CREATE TABLE probe (n integer NOT NULL)")
    initial = collector_conn()
    real_transaction = initial.transaction

    @contextmanager
    def commit_then_lose(*args, **kwargs):
        with real_transaction(*args, **kwargs) as transaction:
            yield transaction
        # COMMIT succeeded; the session dies before the client learns of it.
        terminate(conn, initial)
        raise psycopg.errors.AdminShutdown("terminating connection due to administrator command")

    initial.transaction = commit_then_lose
    calls = []

    def callback(connection):
        calls.append(connection.info.backend_pid)
        if writes:
            connection.execute("INSERT INTO probe VALUES (1)")
        return connection.execute("SELECT count(*) AS n FROM probe").fetchone()["n"]

    session = CollectionSession(settings(), initial)
    try:
        assert session.run(callback) == (1 if writes else 0)
        assert len(calls) == 1 and len(opened) == 1 and session.connection is opened[0]
        assert count_probe(conn) == (1 if writes else 0)
        assert lock_holders(conn) == [opened[0].info.backend_pid]
    finally:
        session.close()
    assert locks_released(conn)


def test_competing_collector_lock_prevents_resume(conn, opened, collector_conn):
    from rackscout.collection_session import CollectionSession, CollectorOwnershipLost

    initial = collector_conn()
    session = CollectionSession(settings(), initial)
    terminate(conn, initial)
    competitor = collector_conn()  # a new collector started during the outage
    calls = []

    with pytest.raises(CollectorOwnershipLost):
        session.run(calls.append)
    assert calls == [] and len(opened) == 1 and opened[0].closed
    assert lock_holders(conn) == [competitor.info.backend_pid]

    # Loss is final: no further reconnect even once the competitor is gone.
    competitor.close()
    with pytest.raises(CollectorOwnershipLost):
        session.check()
    assert len(opened) == 1 and locks_released(conn)
    session.close()


def test_rejecting_resume_guard_aborts_and_releases_lock(conn, opened, collector_conn):
    from rackscout.collection_session import CollectionSession, CollectorOwnershipLost

    initial = collector_conn()
    seen = []
    session = CollectionSession(settings(), initial, resume_guard=lambda connection: seen.append("first") or True)
    session.add_resume_guard(lambda connection: seen.append("superseded") and False)
    session.add_resume_guard(lambda connection: seen.append("unreached") or True)
    terminate(conn, initial)
    calls = []

    with pytest.raises(CollectorOwnershipLost):
        session.run(calls.append)
    assert seen == ["first", "superseded"] and calls == []
    assert len(opened) == 1 and opened[0].closed and locks_released(conn)

    with pytest.raises(CollectorOwnershipLost):
        session.run(calls.append)
    assert len(opened) == 1 and calls == []
    session.close()


def test_recovery_count_is_bounded_without_waiting(conn, opened, collector_conn, monkeypatch):
    from rackscout import collection_session
    from rackscout.collection_session import MAX_RECOVERIES, CollectionSession, CollectorRecoveryExhausted

    monkeypatch.setattr(collection_session, "time", NoWaitTime)
    session = CollectionSession(settings(), collector_conn())
    try:
        for _ in range(MAX_RECOVERIES):
            terminate(conn, session.connection)
            session.check()
        assert len(opened) == MAX_RECOVERIES == 3
        terminate(conn, session.connection)
        with pytest.raises(CollectorRecoveryExhausted):
            session.check()
        with pytest.raises(CollectorRecoveryExhausted):
            session.check()
        assert len(opened) == MAX_RECOVERIES  # no connection attempt past the bound
    finally:
        session.close()
    assert all(connection.closed for connection in opened) and locks_released(conn)


def test_recovery_deadline_is_bounded_without_real_waiting(conn, collector_conn, monkeypatch):
    from rackscout import collection_session, db
    from rackscout.collection_session import (RECOVERY_INTERVAL, RECOVERY_TIMEOUT, CollectionSession,
                                              CollectorRecoveryExhausted)

    clock = FakeClock()
    attempts = []

    def unreachable(connect_timeout=10, autocommit=False):
        attempts.append((clock.now, connect_timeout))
        clock.now += connect_timeout  # every attempt uses its whole connect timeout
        raise db.DatabaseUnavailable("database connection failed")

    monkeypatch.setattr(collection_session, "time", clock)
    monkeypatch.setattr(db, "connect", unreachable)
    initial = collector_conn()
    session = CollectionSession(replace(settings(), db_connect_timeout=10), initial)
    terminate(conn, initial)

    with pytest.raises(CollectorRecoveryExhausted):
        session.check()
    start = attempts[0][0]
    assert start == 1000.0 and clock.now == start + RECOVERY_TIMEOUT
    assert len(attempts) == 10  # 10s connect + 3s pause per attempt within 120s
    assert all(timeout <= start + RECOVERY_TIMEOUT - at for at, timeout in attempts)
    assert clock.sleeps and all(0 < pause <= RECOVERY_INTERVAL for pause in clock.sleeps)

    with pytest.raises(CollectorRecoveryExhausted):
        session.check()
    assert len(attempts) == 10
    session.close()


def test_slow_resume_guard_past_deadline_is_never_adopted(conn, opened, collector_conn, monkeypatch):
    from rackscout import collection_session
    from rackscout.collection_session import RECOVERY_TIMEOUT, CollectionSession, CollectorRecoveryExhausted

    clock = FakeClock()
    monkeypatch.setattr(collection_session, "time", clock)
    guarded = []

    def slow_guard(connection):
        guarded.append(connection.info.backend_pid)
        clock.now += RECOVERY_TIMEOUT  # the guard alone spends the whole recovery budget
        return True

    initial = collector_conn()
    session = CollectionSession(settings(), initial, resume_guard=slow_guard)
    terminate(conn, initial)
    calls = []

    with pytest.raises(CollectorRecoveryExhausted):
        session.run(calls.append)
    assert calls == [] and len(guarded) == 1 and len(opened) == 1 and opened[0].closed
    assert locks_released(conn)
    with pytest.raises(CollectorRecoveryExhausted):  # sticky: no further connection or callback
        session.run(calls.append)
    assert calls == [] and len(opened) == 1 and clock.sleeps == []
    session.close()


def test_setup_delay_caps_session_timeouts_at_remaining_budget(conn, opened, collector_conn, monkeypatch):
    from rackscout import collection_session, db
    from rackscout.collection_session import RECOVERY_TIMEOUT, CollectionSession, CollectorRecoveryExhausted

    clock = FakeClock()
    monkeypatch.setattr(collection_session, "time", clock)
    tracked = db.connect

    def slow_connect(*args, **kwargs):
        connection = tracked(*args, **kwargs)
        clock.now += RECOVERY_TIMEOUT - 0.5  # connecting left half a second of the budget
        return connection

    monkeypatch.setattr(db, "connect", slow_connect)
    seen = []

    def guard(connection):
        seen.append(connection.execute("SHOW statement_timeout").fetchone()["statement_timeout"])
        connection.execute("SELECT pg_sleep(1)")  # outlives the capped statement timeout
        return True

    initial = collector_conn()
    session = CollectionSession(settings(), initial, resume_guard=guard)
    terminate(conn, initial)
    calls = []

    with pytest.raises(CollectorRecoveryExhausted):
        session.run(calls.append)
    assert seen == ["500ms"] and calls == []
    assert len(opened) == 1 and opened[0].closed and locks_released(conn)
    session.close()



def test_sql_and_program_errors_are_not_replayed(conn, opened, collector_conn):
    from rackscout.collection_session import CollectionSession

    initial = collector_conn()
    session = CollectionSession(settings(), initial)
    calls = []

    def missing_table(connection):
        calls.append("sql")
        connection.execute("SELECT * FROM no_such_table")

    def bug(connection):
        calls.append("bug")
        raise ValueError("bug")

    def slow(connection):
        calls.append("timeout")
        connection.execute("SET LOCAL statement_timeout = '10ms'")
        connection.execute("SELECT pg_sleep(1)")

    with pytest.raises(psycopg.errors.UndefinedTable):
        session.run(missing_table)
    with pytest.raises(ValueError):
        session.run(bug)
    with pytest.raises(psycopg.errors.QueryCanceled):
        session.run(slow)
    assert calls == ["sql", "bug", "timeout"] and opened == []
    assert session.connection is initial and lock_holders(conn) == [initial.info.backend_pid]
    assert session.run(lambda connection: connection.execute("SELECT 1 AS one").fetchone()["one"]) == 1

    # close() leaves the healthy borrowed session (and its lock) to its owner.
    session.close()
    assert not initial.closed and lock_holders(conn) == [initial.info.backend_pid]


def test_lock_must_be_held_by_this_very_backend(conn, opened, collector_conn):
    from rackscout import db
    from rackscout.collection_session import CollectionSession, CollectorOwnershipLost

    holder = collector_conn()
    calls = []
    with pytest.raises(CollectorOwnershipLost):  # some other backend holding the lock is not ownership
        CollectionSession(settings(), collector_conn(lock=False)).run(calls.append)

    holder.execute("SELECT pg_advisory_unlock(%s)", (db.COLLECTOR_LOCK_KEY,))
    with pytest.raises(CollectorOwnershipLost):  # released on a healthy session
        CollectionSession(settings(), holder).check()
    assert calls == [] and opened == [] and not holder.closed


def test_interrupts_propagate_and_never_leak_sessions(conn, opened, collector_conn):
    from rackscout.collection_session import CollectionSession

    def interrupted_guard(connection):
        raise KeyboardInterrupt

    initial = collector_conn()
    session = CollectionSession(settings(), initial, resume_guard=interrupted_guard)
    terminate(conn, initial)
    calls = []
    with pytest.raises(KeyboardInterrupt):
        session.run(calls.append)
    assert calls == [] and len(opened) == 1 and opened[0].closed and locks_released(conn)
    session.close()

    def interrupted_callback(connection):
        calls.append(connection.info.backend_pid)
        raise KeyboardInterrupt

    healthy = collector_conn()
    session = CollectionSession(settings(), healthy)
    with pytest.raises(KeyboardInterrupt):
        session.run(interrupted_callback)
    assert calls == [healthy.info.backend_pid] and len(opened) == 1
    assert session.connection is healthy and lock_holders(conn) == [healthy.info.backend_pid]
    session.close()
    assert not healthy.closed
