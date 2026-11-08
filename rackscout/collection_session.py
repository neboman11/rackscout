"""Collector database session that survives lost PostgreSQL connections without replaying network work.

A collection holds the global collector advisory lock on one autocommit session for its whole run. When
that session dies (PostgreSQL restart, pod recreation, terminated backend), ``CollectionSession``
reconnects within fixed bounds, re-takes the lock on the new session without waiting, runs every resume
guard under the new exclusive lock and only then retries the interrupted database-only transaction.
Callers keep acquisition results (fetched pages, parsed records) in memory, so a recovery never repeats a
marketplace request. Only broken connections are retried; SQL, schema, timeout and programming errors
propagate unchanged on first occurrence.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Callable, TypeVar

import psycopg

from . import db
from .config import Settings

log = logging.getLogger(__name__)

RECOVERY_TIMEOUT = 120.0  # seconds one recovery may spend reconnecting and re-taking the lock
RECOVERY_INTERVAL = 3.0  # seconds between reconnect attempts
MAX_RECOVERIES = 3  # recoveries allowed across one collection
SESSION_TIMEOUT_MS = 10_000  # statement_timeout and lock_timeout of replacement sessions

# Connection exceptions (08xxx) and operator intervention that ends the session (57P01-57P03).
_LOST_SQLSTATES = frozenset({"57P01", "57P02", "57P03"})

# Whether a session holds the session-level collector lock; the own-session check names the current
# backend itself, not merely some backend.
_LOCK_HELD = """SELECT EXISTS (
    SELECT 1 FROM pg_locks
    WHERE locktype = 'advisory' AND granted AND objsubid = 1 {holder}
      AND classid = %s::bigint::oid AND objid = %s::bigint::oid
      AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
) AS held"""
_OWN_LOCK_SQL = _LOCK_HELD.format(holder="AND pid = pg_backend_pid()")
_PID_LOCK_SQL = _LOCK_HELD.format(holder="AND pid = %s")
_ANY_LOCK_SQL = _LOCK_HELD.format(holder="")
_LOCK_PARAMS = (db.COLLECTOR_LOCK_KEY >> 32, db.COLLECTOR_LOCK_KEY & 0xFFFFFFFF)

T = TypeVar("T")
Guard = Callable[[psycopg.Connection], bool]


class CollectorOwnershipLost(db.DatabaseUnavailable):
    """This collection no longer exclusively owns the collector lock or its persisted run; it must stop."""


class CollectorRecoveryExhausted(db.DatabaseUnavailable):
    """The database session could not be recovered within the fixed bounds."""


def connection_lost(exc: BaseException, connection: psycopg.Connection) -> bool:
    """Whether ``exc`` means the session itself is gone, as opposed to a failed statement."""
    if not isinstance(exc, psycopg.Error):
        return False
    sqlstate = exc.sqlstate
    if sqlstate:
        return sqlstate.startswith("08") or sqlstate in _LOST_SQLSTATES
    return isinstance(exc, psycopg.OperationalError) and (connection.broken or connection.closed)


class CollectionSession:
    """Collector work session that carries the collector lock and recovers it after connection loss.

    ``connection`` is an autocommit session that already holds ``db.COLLECTOR_LOCK_KEY``. It is borrowed:
    ``close()`` never closes it, although recovery closes it once it has failed. ``.connection`` is the
    current session; read it again after every ``run``/``check`` instead of keeping a reference.
    """

    def __init__(self, settings: Settings, connection: psycopg.Connection, *,
                 resume_guard: Guard | None = None) -> None:
        self.settings = settings
        self.connection = connection
        self._initial = connection
        self._backend_pid = connection.info.backend_pid
        self._guards: list[Guard] = [] if resume_guard is None else [resume_guard]
        self._recoveries = 0
        self._fatal: db.DatabaseUnavailable | None = None

    def add_resume_guard(self, guard: Guard) -> None:
        """Register a DB-only check run under every newly reacquired lock before resuming; False aborts."""
        self._guards.append(guard)

    def check(self) -> None:
        """Verify (recovering if needed) lock ownership before a network action; a DB-only no-op checkpoint."""
        self.run(_noop)

    def run(self, callback: Callable[[psycopg.Connection], T]) -> T:
        """Run DB-only ``callback(connection)`` in one transaction on a session that owns the collector lock.

        After a lost connection the callback is retried on the recovered session, unless its transaction
        is known to have committed. Its result is kept until the commit succeeds, so it must be
        materialized (no open cursors). Every other error propagates unchanged without a retry.
        """
        # (xid, result) of a callback whose COMMIT was sent but not acknowledged; xid None = read-only.
        pending: tuple[str | None, T] | None = None
        while True:
            if self._fatal is not None:
                raise type(self._fatal)(str(self._fatal))
            connection = self.connection
            try:
                with connection.transaction():
                    self._verify_lock(connection)
                    if pending is not None:
                        xid, result = pending
                        if xid is None or self._committed(connection, xid):
                            return result
                        pending = None
                    result = callback(connection)
                    row = connection.execute("SELECT pg_current_xact_id_if_assigned()::text AS xid").fetchone()
                    pending = (row["xid"], result)
                return result
            except psycopg.Error as exc:
                if not connection_lost(exc, connection):
                    raise
                self._recover(exc)

    def close(self) -> None:
        """Close a replacement session (releasing its lock); the borrowed initial session stays open."""
        if self.connection is not self._initial:
            self.connection.close()

    def _fail(self, error: db.DatabaseUnavailable) -> db.DatabaseUnavailable:
        self._fatal = error
        return error

    def _verify_lock(self, connection: psycopg.Connection) -> None:
        if not connection.execute(_OWN_LOCK_SQL, _LOCK_PARAMS).fetchone()["held"]:
            raise self._fail(CollectorOwnershipLost("collector lock is no longer held by this session"))

    def _committed(self, connection: psycopg.Connection, xid: str) -> bool:
        status = connection.execute("SELECT pg_xact_status(%s::xid8) AS status", (xid,)).fetchone()["status"]
        if status == "committed":
            return True
        if status == "aborted":
            return False
        raise self._fail(CollectorRecoveryExhausted("outcome of the interrupted commit is unknown"))

    def _recover(self, exc: psycopg.Error) -> None:
        if self._recoveries >= MAX_RECOVERIES:
            raise self._fail(CollectorRecoveryExhausted(
                f"database session lost again ({type(exc).__name__}) after {MAX_RECOVERIES} recoveries")) from None
        self._recoveries += 1
        log.warning("collector database session lost (%s); recovering (%d/%d)",
                    type(exc).__name__, self._recoveries, MAX_RECOVERIES)
        # The failed session (borrowed or not) must not keep the lock from its replacement.
        self.connection.close()
        deadline = time.monotonic() + RECOVERY_TIMEOUT
        attempt = 0
        while True:
            remaining = self._remaining(deadline)
            attempt += 1
            candidate = self._connect(remaining)
            if candidate is not None and self._adopt(candidate, deadline):
                log.warning("collector database session recovered after %d attempt(s)", attempt)
                return
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(RECOVERY_INTERVAL, remaining))

    def _remaining(self, deadline: float) -> float:
        """Seconds left in this recovery; raises (sticky) once the bound is spent."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise self._fail(CollectorRecoveryExhausted(
                f"database session not recovered within {RECOVERY_TIMEOUT:g}s")) from None
        return remaining

    def _connect(self, remaining: float) -> psycopg.Connection | None:
        timeout = max(1, min(self.settings.db_connect_timeout, math.ceil(remaining)))
        try:
            return db.connect(connect_timeout=timeout, autocommit=True)
        except db.DatabaseUnavailable:
            return None  # message already credential-free; retried until the deadline

    def _bound_timeouts(self, candidate: psycopg.Connection, deadline: float) -> bool:
        """Cap statement/lock timeouts at the normal value and the remaining budget; True if shortened."""
        ms = min(SESSION_TIMEOUT_MS, max(1, int(self._remaining(deadline) * 1000)))
        _set_timeouts(candidate, ms)
        return ms < SESSION_TIMEOUT_MS

    def _adopt(self, candidate: psycopg.Connection, deadline: float) -> bool:
        """Take the lock and pass every guard on ``candidate`` before ``deadline``; False = retry later.

        Never leaks ``candidate``: unless adopted it is closed, releasing any lock it took.
        """
        adopted = False
        shortened = False
        try:
            shortened = self._bound_timeouts(candidate, deadline)
            if not self._acquire(candidate, deadline):
                return False
            with candidate.transaction():
                self._verify_lock(candidate)
                for guard in self._guards:
                    shortened = self._bound_timeouts(candidate, deadline) or shortened
                    if not guard(candidate):
                        raise self._fail(CollectorOwnershipLost("collection was superseded while disconnected"))
                    self._remaining(deadline)
            self._remaining(deadline)
            _set_timeouts(candidate, SESSION_TIMEOUT_MS)
            pid = candidate.info.backend_pid
            self._remaining(deadline)
            adopted = True
        except psycopg.Error as exc:
            # A statement cut short by the budget-capped timeouts means the recovery budget is spent.
            if shortened and isinstance(exc, (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable)):
                raise self._fail(CollectorRecoveryExhausted(
                    f"database session not recovered within {RECOVERY_TIMEOUT:g}s")) from None
            if not connection_lost(exc, candidate):
                raise
            log.info("replacement collector session lost during setup (%s)", type(exc).__name__)
            return False
        finally:
            if not adopted:
                candidate.close()  # releases any lock it took
        self.connection = candidate
        self._backend_pid = pid
        return True

    def _acquire(self, candidate: psycopg.Connection, deadline: float) -> bool:
        """Take the collector lock without waiting. False while only our own dead backend still holds it."""
        while True:
            self._remaining(deadline)
            if candidate.execute("SELECT pg_try_advisory_lock(%s) AS ok", (db.COLLECTOR_LOCK_KEY,)).fetchone()["ok"]:
                return True
            if candidate.execute(_PID_LOCK_SQL, (self._backend_pid, *_LOCK_PARAMS)).fetchone()["held"]:
                return False  # the failed session's backend has not exited yet
            if candidate.execute(_ANY_LOCK_SQL, _LOCK_PARAMS).fetchone()["held"]:
                raise self._fail(CollectorOwnershipLost("another collector took the lock while disconnected"))
            # The holder released it between the two checks; try again.


def _set_timeouts(connection: psycopg.Connection, ms: int) -> None:
    connection.execute("SELECT set_config('statement_timeout', %s, false), set_config('lock_timeout', %s, false)",
                       (f"{ms}ms", f"{ms}ms"))


def _noop(connection: psycopg.Connection) -> None:
    return None
