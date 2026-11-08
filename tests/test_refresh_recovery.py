"""Manual refresh recovery retains its request identity and never replays a superseded request."""
from datetime import datetime, timezone

import httpx
import pytest

from test_db import TRAP, UNKNOWN_SHIP, conn, requires_db, snapshot  # noqa: F401
from test_manual_refresh import NoNetwork, SourceFetcher, refresh_settings


@requires_db
@pytest.mark.parametrize("superseded", [False, True])
def test_manual_refresh_recovers_or_stops_without_overwriting_new_owner(conn, monkeypatch, superseded):
    from rackscout import db, refresh

    settings = refresh_settings(max_details=2, browser_enabled=False)
    outcome, accepted = refresh.enqueue(conn, settings)
    assert outcome == "accepted"
    opened = []
    original_connect = db.connect

    def tracked_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(db, "connect", tracked_connect)
    interrupted = []

    class InterruptedFetcher(SourceFetcher):
        def fetch_description(self, item_id, max_bytes):
            result = super().fetch_description(item_id, max_bytes)
            if not interrupted:
                interrupted.append(item_id)
                for connection in opened:
                    if not connection.closed:
                        conn.execute("SELECT pg_terminate_backend(%s)", (connection.info.backend_pid,))
                if superseded:
                    with refresh.connect_worker(settings) as replacement:
                        assert refresh.process_pending(replacement, settings, fetcher=NoNetwork()) == "failed"
            return result

    fetcher = InterruptedFetcher(snapshot(datetime.now(timezone.utc), [TRAP, UNKNOWN_SHIP]))
    worker = refresh.connect_worker(settings)
    try:
        state = refresh.process_pending(worker, settings, fetcher=fetcher)
    finally:
        worker.close()

    control = conn.execute("SELECT * FROM refresh_control WHERE id = 1").fetchone()
    assert str(control["request_id"]) == accepted["request_id"]
    assert control["finished_at"] is not None
    assert not refresh.collector_running(conn)
    if superseded:
        assert state == control["state"] == "failed"
        assert control["message"] == refresh.INTERRUPTED_MESSAGE
        assert fetcher.description_calls == interrupted
    else:
        assert state == control["state"] == "partial"
        assert sorted(fetcher.description_calls) == sorted([TRAP[0], UNKNOWN_SHIP[0]])
        rows = conn.execute("SELECT description_status, last_checked_at FROM listings").fetchall()
        assert all(row["description_status"] == "verified" and row["last_checked_at"] is not None for row in rows)
    assert conn.execute("SELECT count(*) AS n FROM source_runs WHERE finished_at IS NULL").fetchone()["n"] == 0


def test_request_guard_failure_emits_no_http_request():
    from rackscout import db
    from rackscout.sources import HttpFetcher

    requests = []
    client = httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200)))
    fetcher = HttpFetcher(1, 300, client)

    def lost_ownership():
        raise db.DatabaseUnavailable("collector ownership lost")

    fetcher.before_request = lost_ownership
    try:
        with pytest.raises(db.DatabaseUnavailable):
            fetcher.get_text("https://perunitprice.com/servers/dell/", lambda url: True, 1000)
    finally:
        fetcher.close()
    assert requests == []


def test_slow_ownership_check_preserves_actual_http_request_spacing(monkeypatch):
    from rackscout import sources

    clock = [0.0]
    requests = []
    monkeypatch.setattr(sources.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(sources.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: requests.append(clock[0]) or httpx.Response(200, text="ok")))
    fetcher = sources.HttpFetcher(1, 300, client)

    def slow_check():
        if not requests:
            clock[0] += 100

    fetcher.before_request = slow_check
    try:
        for _ in range(2):
            fetcher.get_text("https://perunitprice.com/servers/dell/", lambda url: True, 1000)
    finally:
        fetcher.close()
    assert requests == [100.0, 400.0]
