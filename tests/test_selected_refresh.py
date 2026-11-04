"""Selected-listing refresh: one queue slot shared with the global refresh, scoped to a single marketplace listing.

Uses the isolated PostgreSQL schema fixture from ``test_db`` and a controlled source fetcher (no network).
Execution tests rely on ``collector.run_collection(..., target_listing_id=...)`` (no discovery, at most
one normal enrichment of that listing).
"""
import math
import threading
from datetime import datetime, timezone

import pytest
from test_db import (  # noqa: F401 - `conn` is a fixture
    TRAP,
    UNKNOWN_SHIP,
    FakeFetcher,
    conn,
    requires_db,
    settings,
    snapshot,  # noqa: F401
)
from test_manual_refresh import (
    NoNetwork,
    SourceFetcher,
    _enqueue,
    _lock_free,
    _process,
    _row,
    _snap,
    refresh_settings,
)


def _seed(conn):
    """Discover two live listings through the real discovery path; returns {item_id: (listing_id, config_id)}."""
    from rackscout import collector

    assert collector.run_discovery(conn, settings(), FakeFetcher(_snap())) == "success"
    rows = conn.execute(
        """SELECT l.item_id, l.id AS listing_id, c.id AS config_id FROM listings l
           JOIN configurations c ON c.listing_id = l.id AND c.active ORDER BY l.item_id, c.id""").fetchall()
    out: dict[str, tuple[int, int]] = {}
    for r in rows:
        out.setdefault(r["item_id"], (r["listing_id"], r["config_id"]))
    assert set(out) == {TRAP[0], UNKNOWN_SHIP[0]}
    return out


def _selected(config_id, s=None):
    from rackscout import refresh

    c = _worker_conn()
    try:
        return refresh.enqueue(c, s or refresh_settings(), configuration_id=config_id)
    finally:
        c.close()


def _worker_conn():
    from rackscout import refresh

    return refresh.connect_worker(settings())


def _age_request(conn):
    conn.execute("UPDATE refresh_control SET requested_at = now() - interval '2 hours'")


def _discovery_runs(conn):
    return conn.execute("SELECT count(*) AS n FROM source_runs WHERE kind = 'discovery'").fetchone()["n"]


def _listing(conn, item_id):
    return conn.execute("SELECT * FROM listings WHERE item_id = %s", (item_id,)).fetchone()


def _selected_settings():
    return refresh_settings(max_details=1, browser_enabled=False)


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


@requires_db
def test_selected_request_stores_exact_stable_listing_and_reports_scope(conn):
    from rackscout import refresh

    ids = _seed(conn)
    listing_id, config_id = ids[TRAP[0]]
    outcome, state = _selected(config_id)
    assert outcome == "accepted"
    assert (state["scope"], state["target_item_id"], state["state"]) == ("listing", TRAP[0], "queued")
    row = _row(conn)
    assert row["target_listing_id"] == listing_id

    c = _worker_conn()
    try:
        polled = refresh.status(c, refresh_settings())
    finally:
        c.close()
    assert (polled["scope"], polled["target_item_id"]) == ("listing", TRAP[0])
    # Previous fields are preserved; no URL is exposed.
    assert {"enabled", "state", "request_id", "requested_at", "started_at", "finished_at", "retry_at",
            "retry_after_seconds", "collector_running", "can_refresh", "message"} <= set(polled)
    assert not any("url" in k for k in polled)


@requires_db
def test_idle_status_is_global_scope(conn):
    from rackscout import refresh

    c = _worker_conn()
    try:
        state = refresh.status(c, refresh_settings())
    finally:
        c.close()
    assert (state["scope"], state["target_item_id"]) == ("all", None)


@requires_db
def test_global_and_selected_share_one_slot_and_cooldown(conn):
    ids = _seed(conn)
    cfg_a, cfg_b = ids[TRAP[0]][1], ids[UNKNOWN_SHIP[0]][1]

    assert _selected(cfg_a)[0] == "accepted"
    assert _selected(cfg_b)[0] == "active"         # different listing, same slot
    assert _enqueue()[0] == "active"               # global blocked too
    assert _row(conn)["target_listing_id"] == ids[TRAP[0]][0]  # rejected requests never retarget

    assert _process(_selected_settings(), SourceFetcher(_snap())) is not None  # finish the first request
    # Hour cooldown is shared: neither a global nor a selected request of another listing is admitted.
    assert _selected(cfg_b)[0] == "cooldown"
    assert _enqueue()[0] == "cooldown"
    _age_request(conn)
    assert _selected(cfg_b)[0] == "accepted"


@requires_db
def test_concurrent_global_and_selected_requests_accept_exactly_one(conn):
    ids = _seed(conn)
    configs = [ids[TRAP[0]][1], ids[UNKNOWN_SHIP[0]][1]]
    barrier = threading.Barrier(8)
    results: list[str] = []
    errors: list[BaseException] = []

    def submit(n):
        barrier.wait()
        try:
            results.append(_enqueue()[0] if n % 3 == 0 else _selected(configs[n % 2])[0])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=submit, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert sorted(results) == ["accepted"] + ["active"] * 7
    row = _row(conn)
    assert row["state"] == "queued"
    valid_targets = {None, ids[TRAP[0]][0], ids[UNKNOWN_SHIP[0]][0]}
    assert row["target_listing_id"] in valid_targets


@requires_db
def test_unknown_inactive_archived_ended_targets_are_rejected_without_state_change(conn):
    from rackscout.refresh import ListingRefreshNotFound

    ids = _seed(conn)
    before = _row(conn)
    assert before["state"] == "idle" and before["requested_at"] is None

    with pytest.raises(ListingRefreshNotFound):
        _selected(2_000_000_000)

    listing_id, config_id = ids[TRAP[0]]
    conn.execute("UPDATE configurations SET active = false WHERE id = %s", (config_id,))
    with pytest.raises(ListingRefreshNotFound):
        _selected(config_id)
    conn.execute("UPDATE configurations SET active = true WHERE id = %s", (config_id,))

    conn.execute("UPDATE listings SET archived_at = now() WHERE id = %s", (listing_id,))
    with pytest.raises(ListingRefreshNotFound):
        _selected(config_id)
    conn.execute("UPDATE listings SET archived_at = NULL, ended_at = now() WHERE id = %s", (listing_id,))
    with pytest.raises(ListingRefreshNotFound):
        _selected(config_id)

    after = _row(conn)
    assert (after["state"], after["request_id"], after["requested_at"], after["target_listing_id"]) == \
        ("idle", None, None, None)
    conn.execute("UPDATE listings SET ended_at = NULL WHERE id = %s", (listing_id,))
    assert _selected(config_id)[0] == "accepted"  # no slot or cooldown was consumed by the rejections


@requires_db
def test_cooling_listing_is_rejected_with_retry_after_and_consumes_nothing(conn):
    from rackscout.refresh import ListingRefreshCooldown

    ids = _seed(conn)
    listing_id, config_id = ids[TRAP[0]]
    conn.execute("UPDATE listings SET next_check_after = now() + interval '90 minutes' WHERE id = %s", (listing_id,))
    with pytest.raises(ListingRefreshCooldown) as info:
        _selected(config_id)
    assert isinstance(info.value.retry_after_seconds, int)
    assert math.ceil(89 * 60) < info.value.retry_after_seconds <= 90 * 60

    row = _row(conn)
    assert (row["state"], row["requested_at"], row["target_listing_id"]) == ("idle", None, None)
    # The other, due listing is still admitted: the rejection took no queue slot and no global cooldown.
    assert _selected(ids[UNKNOWN_SHIP[0]][1])[0] == "accepted"


@requires_db
def test_global_request_clears_previous_target(conn):
    ids = _seed(conn)
    assert _selected(ids[TRAP[0]][1])[0] == "accepted"
    assert _process(_selected_settings(), SourceFetcher(_snap())) is not None
    assert _row(conn)["target_listing_id"] == ids[TRAP[0]][0]  # historical scope stays visible after completion

    _age_request(conn)
    outcome, state = _enqueue()
    assert outcome == "accepted"
    assert (state["scope"], state["target_item_id"]) == ("all", None)
    assert _row(conn)["target_listing_id"] is None


# ---------------------------------------------------------------------------
# Execution against real database state and a controlled source
# ---------------------------------------------------------------------------


@requires_db
def test_selected_execution_enriches_only_the_target_and_fetches_no_index(conn):
    ids = _seed(conn)
    other_before = _listing(conn, UNKNOWN_SHIP[0])
    discovery_before = _discovery_runs(conn)
    inventory_before = conn.execute("SELECT count(*) AS n FROM configurations").fetchone()["n"]

    assert _selected(ids[TRAP[0]][1])[0] == "accepted"
    fetcher = SourceFetcher(_snap())
    state = _process(_selected_settings(), fetcher)

    assert fetcher.index_calls == []                          # discovery skipped: no index page fetched
    assert fetcher.description_calls == [TRAP[0]]             # exactly one normal enrichment
    assert state == "partial"                                 # page verification disabled in this run
    assert _discovery_runs(conn) == discovery_before          # no fake discovery run recorded
    target = _listing(conn, TRAP[0])
    assert target["last_checked_at"] is not None and target["next_check_after"] > datetime.now(timezone.utc)
    assert target["description_status"] in ("verified", "absent")

    other_after = _listing(conn, UNKNOWN_SHIP[0])
    for column in ("last_checked_at", "next_check_after", "description_status", "description_checked_at",
                   "page_status", "check_message", "archived_at", "ended_at", "last_seen_at", "source_record"):
        assert other_after[column] == other_before[column], column
    assert conn.execute("SELECT count(*) AS n FROM configurations").fetchone()["n"] == inventory_before

    row = _row(conn)
    assert row["state"] == "partial" and row["finished_at"] is not None
    assert "Discovery" not in row["message"] and "enrichment" in row["message"]
    assert _lock_free(conn)


@requires_db
def test_selected_listing_cooldown_and_description_cache_are_honored_after_execution(conn):
    from rackscout.refresh import ListingRefreshCooldown

    ids = _seed(conn)
    assert _selected(ids[TRAP[0]][1])[0] == "accepted"
    _process(_selected_settings(), SourceFetcher(_snap()))
    _age_request(conn)  # global hour cooldown elapsed; the listing's own next_check_after has not
    with pytest.raises(ListingRefreshCooldown):
        _selected(ids[TRAP[0]][1])


@requires_db
def test_queued_target_survives_configuration_deactivation(conn):
    ids = _seed(conn)
    listing_id, config_id = ids[TRAP[0]]
    assert _selected(config_id)[0] == "accepted"
    conn.execute("UPDATE configurations SET active = false WHERE id = %s", (config_id,))  # e.g. re-parse dropped variant

    fetcher = SourceFetcher(_snap())
    assert _process(_selected_settings(), fetcher) is not None
    assert fetcher.index_calls == [] and fetcher.description_calls == [TRAP[0]]
    assert _listing(conn, TRAP[0])["last_checked_at"] is not None
    assert _listing(conn, UNKNOWN_SHIP[0])["last_checked_at"] is None


@requires_db
@pytest.mark.parametrize("change", [
    "archived_at = now()",
    "ended_at = now()",
    "next_check_after = now() + interval '1 hour'",
])
def test_stale_queued_target_is_safe_when_listing_is_no_longer_due(conn, change):
    ids = _seed(conn)
    assert _selected(ids[TRAP[0]][1])[0] == "accepted"
    conn.execute(f"UPDATE listings SET {change} WHERE id = %s", (ids[TRAP[0]][0],))
    before = {i: _listing(conn, i) for i in (TRAP[0], UNKNOWN_SHIP[0])}

    fetcher = SourceFetcher(_snap())
    assert _process(_selected_settings(), fetcher) == "succeeded"
    assert fetcher.index_calls == [] and fetcher.description_calls == []   # nothing fetched, nothing else selected
    for item_id, old in before.items():
        new = _listing(conn, item_id)
        assert new["last_checked_at"] == old["last_checked_at"] and new["description_status"] == old["description_status"]
    row = _row(conn)
    assert row["state"] == "succeeded" and "Discovery" not in row["message"]


@requires_db
def test_selected_run_honors_origin_backoff_without_network(conn):
    from rackscout import collector

    ids = _seed(conn)
    collector.set_backoff(conn, collector.DESCRIPTION_HOST, 3600, "challenge")
    assert _selected(ids[TRAP[0]][1])[0] == "accepted"
    assert _process(_selected_settings(), NoNetwork()) in ("blocked", "partial", "failed")
    assert _lock_free(conn)


# ---------------------------------------------------------------------------
# Interruption and shutdown
# ---------------------------------------------------------------------------


@requires_db
def test_shutdown_after_claim_requeues_with_target_retained_then_runs_it(conn):
    ids = _seed(conn)
    assert _selected(ids[TRAP[0]][1])[0] == "accepted"
    original = _row(conn)
    assert _process(_selected_settings(), NoNetwork(), should_stop=lambda: _row(conn)["state"] == "running") is None
    row = _row(conn)
    assert row["state"] == "queued" and row["started_at"] is None
    assert row["target_listing_id"] == ids[TRAP[0]][0] and row["request_id"] == original["request_id"]
    assert _lock_free(conn)

    fetcher = SourceFetcher(_snap())
    assert _process(_selected_settings(), fetcher) is not None
    assert fetcher.index_calls == [] and fetcher.description_calls == [TRAP[0]]


@requires_db
def test_stop_before_claim_keeps_selected_request_queued_with_target(conn):
    ids = _seed(conn)
    assert _selected(ids[TRAP[0]][1])[0] == "accepted"
    assert _process(_selected_settings(), NoNetwork(), should_stop=lambda: True) is None
    row = _row(conn)
    assert row["state"] == "queued" and row["target_listing_id"] == ids[TRAP[0]][0]


@requires_db
def test_interrupted_selected_request_fails_without_automatic_replay(conn):
    ids = _seed(conn)
    assert _selected(ids[TRAP[0]][1])[0] == "accepted"
    conn.execute("UPDATE refresh_control SET state = 'running', started_at = now()")

    assert _process(_selected_settings(), NoNetwork()) == "failed"   # lock free: previous worker is gone
    row = _row(conn)
    assert row["state"] == "failed"
    assert row["target_listing_id"] == ids[TRAP[0]][0]               # reported honestly as the selected scope
    assert _process(_selected_settings(), NoNetwork()) is None       # never re-run
    assert _listing(conn, TRAP[0])["last_checked_at"] is None


@requires_db
def test_zero_budget_selected_run_fetches_nothing_and_changes_no_listing(conn):
    ids = _seed(conn)
    before = {i: _listing(conn, i) for i in (TRAP[0], UNKNOWN_SHIP[0])}
    discovery_before = _discovery_runs(conn)
    assert _selected(ids[TRAP[0]][1])[0] == "accepted"
    fetcher = SourceFetcher(_snap())
    assert _process(refresh_settings(max_details=0, browser_enabled=False), fetcher) == "succeeded"
    assert fetcher.index_calls == [] and fetcher.description_calls == []
    for item_id, old in before.items():
        new = _listing(conn, item_id)
        for column in ("last_checked_at", "next_check_after", "description_status", "page_status"):
            assert new[column] == old[column], column
    assert _discovery_runs(conn) == discovery_before


def test_final_state_maps_selected_and_full_collection_outcomes():
    from rackscout import collector, refresh

    def mapped(enrichment):
        return refresh.final_state(collector.CollectionResult("skipped", enrichment))

    for enrichment, expected in [("success", "succeeded"), ("skipped", "succeeded"), ("blocked", "blocked"),
                                 ("failed", "failed"), ("partial", "partial")]:
        state, _ = mapped(enrichment)
        assert state == expected
    # Full-collection semantics are unchanged.
    assert refresh.final_state(collector.CollectionResult("success", "success"))[0] == "succeeded"
    assert refresh.final_state(collector.CollectionResult("success", "partial"))[0] == "partial"
    assert refresh.final_state(collector.CollectionResult("blocked", "skipped"))[0] == "blocked"
    assert refresh.final_state(collector.CollectionResult("failed", "skipped"))[0] == "failed"
