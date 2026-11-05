"""Persistent manual "sold" mark on a marketplace listing (schema 009, ``listings.manual_sold_at``).

The mark is listing-level, independent of source ``ended_at``/``archived_at``, never cleared by the collector, and
never changes hardware, price, revisions or history. Database tests share the isolated UUID-schema fixture from
``test_db`` and controlled fetchers (no network).
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from test_db import (  # noqa: F401 - `conn` is a fixture
    TRAP,
    TRAP_VARIATIONS,
    UNKNOWN_SHIP,
    FakeFetcher,
    conn,
    recompute_listing,
    requires_db,
    settings,
    snapshot,
    verified_page,
)
from test_manual_api import HEADERS, ORIGIN, body, no_database
from test_manual_refresh import SourceFetcher, _process, _row, _snap, refresh_settings
from test_persistence_regressions import _discover, _set_page

SOLD_HEADERS = {**HEADERS, "Content-Type": "application/json"}
STABLE_COLUMNS = ("variant_key", "revision", "active", "origin", "model", "cpu_model", "cpu_count", "ram_gb",
                  "drive_bays", "hardware_state", "price_kind", "item_price", "shipping_price", "essential_states",
                  "manual_data", "evidence", "warnings", "exclusions", "observed_at")


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from rackscout import api

    monkeypatch.setenv("RACKSCOUT_EDIT_ORIGINS", ORIGIN)
    monkeypatch.setenv("RACKSCOUT_REFRESH_ORIGINS", ORIGIN)
    monkeypatch.setattr(api, "_settings", refresh_settings(edit_origins=(ORIGIN,)))
    return TestClient(api.app)


def sold(client, config_id, value=True, **kwargs):
    return client.post(f"/api/listings/{config_id}/sold", headers=SOLD_HEADERS, json={"sold": value}, **kwargs)


def seed(conn):
    """Two live eBay listings; the TRAP listing has native variants 1/2 and one manual child of variant 2."""
    from rackscout import manual

    _discover(conn, [TRAP, UNKNOWN_SHIP])
    checked = datetime.now(timezone.utc)
    _set_page(conn, TRAP[0], verified_page(checked), checked_at=checked)
    recompute_listing(conn, TRAP[0])
    recompute_listing(conn, UNKNOWN_SHIP[0])
    trap = {r["variant_id"]: r["id"] for r in conn.execute(
        """SELECT c.id, c.variant_id FROM configurations c JOIN listings l ON l.id = c.listing_id
           WHERE l.item_id = %s AND c.origin = 'source' AND c.active""", (TRAP[0],)).fetchall()}
    assert set(trap) == {"1", "2"}
    revision = conn.execute("SELECT revision FROM configurations WHERE id = %s", (trap["2"],)).fetchone()["revision"]
    child = manual.create(conn, trap["2"], manual.parse_write(body(revision, 32, "300.00")))
    other = conn.execute(
        "SELECT c.id FROM configurations c JOIN listings l ON l.id = c.listing_id WHERE l.item_id = %s AND c.active",
        (UNKNOWN_SHIP[0],)).fetchone()["id"]
    listing_id = conn.execute("SELECT id FROM listings WHERE item_id = %s", (TRAP[0],)).fetchone()["id"]
    return {"trap": trap, "child": child, "other": other, "listing_id": listing_id}


def listing_ids(client, **params):
    response = client.get("/api/listings", params={"min_ram": "", "min_bays": "", "height": "", "modern_only": "false",
                                                   "diskless": "false", "page_size": 100, **params})
    assert response.status_code == 200, response.text
    return {i["id"]: i for i in response.json()["items"]}


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def active_config_ids(conn, listing_id):
    return {r["id"] for r in conn.execute(
        "SELECT id FROM configurations WHERE listing_id = %s AND active", (listing_id,)).fetchall()}


def snapshot_rows(conn, listing_id):
    rows = conn.execute(f"SELECT id, {', '.join(STABLE_COLUMNS)} FROM configurations WHERE listing_id = %s ORDER BY id",
                        (listing_id,)).fetchall()
    history = conn.execute(
        "SELECT h.* FROM price_history h JOIN configurations c ON c.id = h.configuration_id WHERE c.listing_id = %s "
        "ORDER BY h.id", (listing_id,)).fetchall()
    return rows, history


# ---------------------------------------------------------------------------
# Migration and boundary (no database for the protocol checks)
# ---------------------------------------------------------------------------


def test_migration_009_takes_collector_lock_and_adds_nullable_timestamp():
    from rackscout import db

    assert db.latest_version() == 9
    path = dict(db.migration_files())[9]
    text = path.read_text(encoding="utf-8")
    assert path.name.startswith("009_")
    assert "pg_advisory_xact_lock" in text and "x'53434F55'" in text
    assert "manual_sold_at" in text and "timestamptz" in text.lower() and "NOT NULL" not in text.upper()


@requires_db
def test_schema_has_nullable_timestamptz_manual_sold_at_defaulting_to_null(conn):
    col = conn.execute("""SELECT data_type, is_nullable, column_default FROM information_schema.columns
                          WHERE table_schema = current_schema() AND table_name = 'listings'
                            AND column_name = 'manual_sold_at'""").fetchone()
    assert col == {"data_type": "timestamp with time zone", "is_nullable": "YES", "column_default": None}


@pytest.mark.parametrize("payload", [{}, {"sold": None}, {"sold": 1}, {"sold": 0}, {"sold": "true"},
                                     {"sold": True, "extra": 1}, {"sold": True, "revision": 1}, {"Sold": True}])
def test_parse_sold_requires_exactly_one_boolean(payload):
    from rackscout import manual

    with pytest.raises(manual.ManualValidationError):
        manual.parse_sold(payload)


def test_parse_sold_accepts_strict_booleans():
    from rackscout import manual

    assert manual.parse_sold({"sold": True}) is True
    assert manual.parse_sold({"sold": False}) is False


@pytest.mark.parametrize("headers", [
    {},
    {**SOLD_HEADERS, "Origin": "https://untrusted.invalid"},
    {k: v for k, v in SOLD_HEADERS.items() if k != "X-RackScout-Edit"},
    {**SOLD_HEADERS, "Sec-Fetch-Site": "cross-site"},
])
def test_sold_rejects_untrusted_request_before_database(client, monkeypatch, headers):
    from rackscout import api

    monkeypatch.setattr(api.db, "connect", no_database)
    assert client.post("/api/listings/1/sold", headers=headers, json={"sold": True}).status_code == 403


def test_sold_disabled_origin_is_read_only(client, monkeypatch):
    from rackscout import api

    monkeypatch.delenv("RACKSCOUT_EDIT_ORIGINS", raising=False)
    monkeypatch.setattr(api, "_settings", refresh_settings(edit_origins=()))
    monkeypatch.setattr(api.db, "connect", no_database)
    assert client.post("/api/listings/1/sold", headers=SOLD_HEADERS, json={"sold": True}).status_code == 403


@pytest.mark.parametrize("raw", [b"{}", b'{"sold": 1}', b'{"sold": "true"}', b'{"sold": null}',
                                 b'{"sold": true, "x": 1}', b"[true]", b"true", b"not json", b'{"sold": NaN}'])
def test_sold_invalid_body_is_rejected_before_database(client, monkeypatch, raw):
    from rackscout import api

    monkeypatch.setattr(api.db, "connect", no_database)
    assert client.post("/api/listings/1/sold", headers=SOLD_HEADERS, content=raw).status_code == 422


def test_sold_media_type_size_and_id_are_checked_before_database(client, monkeypatch):
    from rackscout import api

    monkeypatch.setattr(api.db, "connect", no_database)
    assert client.post("/api/listings/1/sold", headers={**HEADERS, "Content-Type": "text/plain"},
                       content='{"sold": true}').status_code == 415
    assert client.post("/api/listings/1/sold", headers=SOLD_HEADERS, content=b" " * 32769).status_code == 413
    for bad in ("0", str(2**63)):
        assert client.post(f"/api/listings/{bad}/sold", headers=SOLD_HEADERS, json={"sold": True}).status_code == 404


def test_sold_route_allows_post_only_beyond_reads(client):
    for method in ("PUT", "PATCH", "DELETE"):
        response = client.request(method, "/api/listings/1/sold", headers=SOLD_HEADERS, json={"sold": True})
        assert response.status_code == 405 and "POST" in response.headers["Allow"]


@requires_db
def test_include_sold_filter_is_strict_boolean(conn, client):
    assert client.get("/api/listings", params={"include_sold": "maybe"}).status_code == 422
    assert client.get("/api/listings", params={"include_sold": "true"}).status_code == 200
    assert client.get("/api/listings", params={"include_sold": "false"}).status_code == 200


# ---------------------------------------------------------------------------
# Visibility, undo, idempotence
# ---------------------------------------------------------------------------


@requires_db
def test_mark_hides_every_native_and_manual_variant_of_the_listing_only(conn, client):
    ids = seed(conn)
    before = listing_ids(client)
    same_listing = {*ids["trap"].values(), ids["child"]}
    assert ids["other"] in before
    visible_before = same_listing & set(before)
    assert ids["child"] in before and visible_before
    states_before = {i: before[i]["match_state"] for i in visible_before}

    response = sold(client, ids["trap"]["1"])  # any variant of the listing marks the whole listing
    assert response.status_code == 200, response.text
    item = response.json()["item"]
    assert item["id"] == ids["trap"]["1"] and item["manual_sold_at"].endswith("Z")

    hidden = listing_ids(client)
    assert not (same_listing & set(hidden)) and ids["other"] in hidden
    assert hidden[ids["other"]]["manual_sold_at"] is None
    shown = listing_ids(client, include_sold="true")
    assert visible_before <= set(shown) and ids["other"] in shown
    stamps = {shown[i]["manual_sold_at"] for i in visible_before}
    assert len(stamps) == 1 and None not in stamps
    # Hardware/price classification is independent of the availability mark.
    assert {i: shown[i]["match_state"] for i in visible_before} == states_before
    for config_id in same_listing:  # details stay reachable for undo and carry the mark
        detail = client.get(f"/api/listings/{config_id}").json()
        assert detail["manual_sold_at"] == item["manual_sold_at"]
        assert detail["match_state"] in ("match", "lead")


@requires_db
def test_marking_via_manual_configuration_hides_source_variants_too(conn, client):
    ids = seed(conn)
    assert sold(client, ids["child"]).status_code == 200
    assert not (set(listing_ids(client)) & {*ids["trap"].values(), ids["child"]})
    assert {*ids["trap"].values(), ids["child"]} & set(listing_ids(client, include_sold="true"))


@requires_db
def test_total_counts_and_pages_follow_the_hide(conn, client):
    ids = seed(conn)
    total = client.get("/api/listings", params={"min_ram": "", "min_bays": "", "height": "", "modern_only": "false",
                                                "diskless": "false"}).json()["total"]
    assert sold(client, ids["trap"]["2"]).status_code == 200
    params = {"min_ram": "", "min_bays": "", "height": "", "modern_only": "false", "diskless": "false"}
    assert client.get("/api/listings", params=params).json()["total"] < total
    assert client.get("/api/listings", params={**params, "include_sold": "true"}).json()["total"] == total


@requires_db
def test_idempotent_true_keeps_timestamp_and_false_clears_idempotently(conn, client):
    ids = seed(conn)
    visible_before = set(listing_ids(client))
    cfg = ids["trap"]["1"]
    first = sold(client, cfg).json()["item"]["manual_sold_at"]
    stored = conn.execute("SELECT manual_sold_at FROM listings WHERE id = %s", (ids["listing_id"],)).fetchone()
    assert stored["manual_sold_at"] is not None
    again = sold(client, ids["child"])  # another configuration of the same listing
    assert again.status_code == 200 and again.json()["item"]["manual_sold_at"] == first
    assert sold(client, cfg).json()["item"]["manual_sold_at"] == first
    assert conn.execute("SELECT manual_sold_at FROM listings WHERE id = %s",
                        (ids["listing_id"],)).fetchone() == stored

    cleared = sold(client, cfg, False)
    assert cleared.status_code == 200 and cleared.json()["item"]["manual_sold_at"] is None
    assert sold(client, cfg, False).json()["item"]["manual_sold_at"] is None
    assert conn.execute("SELECT manual_sold_at FROM listings WHERE id = %s", (ids["listing_id"],)).fetchone() == \
        {"manual_sold_at": None}
    visible = listing_ids(client)
    assert set(visible) == visible_before and ids["child"] in visible
    assert ids["trap"]["2"] not in visible  # Source override hiding survives the availability round trip.


@requires_db
def test_undo_works_from_include_sold_detail_and_is_not_blocked_by_the_hide(conn, client):
    ids = seed(conn)
    sold(client, ids["trap"]["1"])
    assert client.get(f"/api/listings/{ids['trap']['1']}").status_code == 200
    assert sold(client, ids["trap"]["1"], False).status_code == 200


@requires_db
def test_marking_changes_no_hardware_price_revision_history_or_source_state(conn, client):
    ids = seed(conn)
    listing_before = conn.execute(
        "SELECT source_record, last_checked_at, next_check_after, detail_status, page_status, page_claims, "
        "archived_at, ended_at, last_seen_at, last_seen_source_at FROM listings WHERE id = %s",
        (ids["listing_id"],)).fetchone()
    rows_before = snapshot_rows(conn, ids["listing_id"])
    backoff_before = conn.execute("SELECT * FROM origin_backoff ORDER BY origin").fetchall()
    runs_before = conn.execute("SELECT count(*) AS n FROM source_runs").fetchone()["n"]
    control_before = _row(conn)

    for value in (True, True, False, True, False):
        assert sold(client, ids["trap"]["2"], value).status_code == 200

    assert snapshot_rows(conn, ids["listing_id"]) == rows_before
    assert conn.execute(
        "SELECT source_record, last_checked_at, next_check_after, detail_status, page_status, page_claims, "
        "archived_at, ended_at, last_seen_at, last_seen_source_at FROM listings WHERE id = %s",
        (ids["listing_id"],)).fetchone() == listing_before
    assert conn.execute("SELECT * FROM origin_backoff ORDER BY origin").fetchall() == backoff_before
    assert conn.execute("SELECT count(*) AS n FROM source_runs").fetchone()["n"] == runs_before
    assert _row(conn) == control_before  # no refresh queued, no cooldown consumed


@requires_db
def test_manual_price_and_edits_survive_mark_and_undo(conn, client):
    ids = seed(conn)
    child_before = conn.execute("SELECT * FROM configurations WHERE id = %s", (ids["child"],)).fetchone()
    sold(client, ids["child"])
    sold(client, ids["child"], False)
    assert conn.execute("SELECT * FROM configurations WHERE id = %s", (ids["child"],)).fetchone() == child_before
    item = client.get(f"/api/listings/{ids['child']}").json()
    assert item["item_price"] == "300.00" and item["manual"]["price_confirmed"] is True
    # Manual editing still works on a sold listing's undo path and keeps the mark independent.
    sold(client, ids["child"])
    edited = client.patch(f"/api/listings/{ids['child']}/manual", headers=HEADERS,
                          json=body(child_before["revision"], 32, "310.00"))
    assert edited.status_code == 200, edited.text
    assert edited.json()["item"]["manual_sold_at"] is not None


@requires_db
def test_unknown_inactive_ended_or_archived_target_cannot_be_marked_or_resurrected(conn, client):
    ids = seed(conn)
    assert sold(client, 2_000_000_000).status_code == 404
    cfg = ids["trap"]["1"]
    for change in ("archived_at = now()", "ended_at = now()"):
        conn.execute("UPDATE listings SET manual_sold_at = NULL WHERE id = %s", (ids["listing_id"],))
        sold(client, cfg)  # visible listing: mark succeeds
        conn.execute(f"UPDATE listings SET {change} WHERE id = %s", (ids["listing_id"],))
        stamp = conn.execute("SELECT manual_sold_at FROM listings WHERE id = %s", (ids["listing_id"],)).fetchone()
        assert sold(client, cfg, False).status_code == 404       # undo cannot touch a really ended/archived source
        assert sold(client, cfg, True).status_code == 404
        assert conn.execute("SELECT manual_sold_at FROM listings WHERE id = %s",
                            (ids["listing_id"],)).fetchone() == stamp
        assert not ({*ids["trap"].values()} & set(listing_ids(client, include_sold="true")))
        assert client.get(f"/api/listings/{cfg}").status_code == 404
        conn.execute("UPDATE listings SET archived_at = NULL, ended_at = NULL WHERE id = %s", (ids["listing_id"],))
    conn.execute("UPDATE configurations SET active = false WHERE id = %s", (cfg,))
    assert sold(client, cfg).status_code == 404


@requires_db
def test_manually_sold_listing_that_really_ends_stays_hidden_with_include_sold(conn, client):
    ids = seed(conn)
    sold(client, ids["trap"]["1"])
    conn.execute("UPDATE listings SET ended_at = now() WHERE id = %s", (ids["listing_id"],))
    assert not ({*ids["trap"].values(), ids["child"]} & set(listing_ids(client, include_sold="true")))


@requires_db
def test_busy_listing_lock_conflicts_without_changing_the_mark(conn, client, monkeypatch):
    from rackscout import db, manual

    ids = seed(conn)
    monkeypatch.setattr(manual, "LOCK_TIMEOUT", "200ms")
    holder = db.connect()
    try:
        holder.execute("SELECT id FROM listings WHERE id = %s FOR UPDATE", (ids["listing_id"],))
        with pytest.raises(manual.ManualConflict):
            manual.mark_sold(conn, ids["trap"]["1"], True)
        holder.rollback()
        holder.execute("SELECT id FROM listings WHERE id = %s FOR UPDATE", (ids["listing_id"],))
        response = client.post(f"/api/listings/{ids['trap']['1']}/sold", headers=SOLD_HEADERS, json={"sold": True})
        assert response.status_code == 409
    finally:
        holder.rollback()
        holder.close()
    assert conn.execute("SELECT manual_sold_at FROM listings WHERE id = %s", (ids["listing_id"],)).fetchone() == \
        {"manual_sold_at": None}


# ---------------------------------------------------------------------------
# Collector persistence and detail scheduling
# ---------------------------------------------------------------------------


@requires_db
def test_rediscovery_price_change_and_archive_cycle_never_clear_the_mark(conn, client):
    from rackscout import collector

    ids = seed(conn)
    stamp = sold(client, ids["trap"]["1"]).json()["item"]["manual_sold_at"]
    changed = list(TRAP)
    changed[2] = "199.00"
    now = datetime.now(timezone.utc)
    assert collector.run_discovery(conn, settings(), FakeFetcher(snapshot(now, [tuple(changed), UNKNOWN_SHIP]))) == "success"
    row = conn.execute("SELECT manual_sold_at, archived_at FROM listings WHERE id = %s", (ids["listing_id"],)).fetchone()
    assert row["archived_at"] is None
    assert iso(row["manual_sold_at"]) == stamp
    active = active_config_ids(conn, ids["listing_id"])
    assert active and not (active & set(listing_ids(client)))
    assert active & set(listing_ids(client, include_sold="true"))  # visible only through include_sold

    # Disappears for over seven days (archived), then is rediscovered: archive clears, the mark stays.
    conn.execute("UPDATE listings SET last_seen_source_at = now() - interval '8 days' WHERE id = %s", (ids["listing_id"],))
    later = datetime.now(timezone.utc) + timedelta(minutes=1)
    collector.run_discovery(conn, settings(), FakeFetcher(snapshot(later, [UNKNOWN_SHIP])))
    assert conn.execute("SELECT archived_at IS NOT NULL AS archived FROM listings WHERE id = %s",
                        (ids["listing_id"],)).fetchone()["archived"]
    collector.run_discovery(conn, settings(), FakeFetcher(snapshot(later + timedelta(minutes=1), [TRAP, UNKNOWN_SHIP])))
    row = conn.execute("SELECT manual_sold_at, archived_at FROM listings WHERE id = %s", (ids["listing_id"],)).fetchone()
    assert row["archived_at"] is None and row["manual_sold_at"] is not None
    assert row["manual_sold_at"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z") == stamp
    active = active_config_ids(conn, ids["listing_id"])
    assert active and not (active & set(listing_ids(client)))
    assert active & set(listing_ids(client, include_sold="true"))  # visible only through include_sold
    assert ids["other"] in listing_ids(client)


@requires_db
def test_source_native_rows_and_manual_children_keep_prices_and_history_through_rediscovery(conn, client):
    from rackscout import collector

    ids = seed(conn)
    sold(client, ids["trap"]["1"])
    child_before = conn.execute("SELECT item_price, shipping_price, revision, manual_data FROM configurations "
                                "WHERE id = %s", (ids["child"],)).fetchone()
    history_before = conn.execute("SELECT count(*) AS n FROM price_history WHERE configuration_id = %s",
                                  (ids["child"],)).fetchone()["n"]
    changed = list(TRAP)
    changed[2] = "150.00"
    collector.run_discovery(conn, settings(), FakeFetcher(snapshot(datetime.now(timezone.utc), [tuple(changed)])))
    assert conn.execute("SELECT item_price, shipping_price, revision, manual_data FROM configurations "
                        "WHERE id = %s", (ids["child"],)).fetchone() == child_before
    assert conn.execute("SELECT count(*) AS n FROM price_history WHERE configuration_id = %s",
                        (ids["child"],)).fetchone()["n"] == history_before


@requires_db
def test_scheduled_detail_candidates_skip_manually_sold_listings(conn, client):
    from rackscout import collector

    ids = seed(conn)
    conn.execute("UPDATE listings SET next_check_after = NULL, last_checked_at = NULL")
    assert {r["item_id"] for r in collector.select_candidates(conn, 10)} == {TRAP[0], UNKNOWN_SHIP[0]}
    sold(client, ids["trap"]["1"])
    assert {r["item_id"] for r in collector.select_candidates(conn, 10)} == {UNKNOWN_SHIP[0]}
    assert [r["item_id"] for r in collector.select_candidates(conn, 1)] == [UNKNOWN_SHIP[0]]
    sold(client, ids["trap"]["1"], False)
    assert TRAP[0] in {r["item_id"] for r in collector.select_candidates(conn, 10)}


@requires_db
def test_enrichment_run_never_fetches_a_manually_sold_listing(conn, client):
    from rackscout import collector

    class Recorder:
        def __init__(self):
            self.calls = []

        def fetch_description(self, item_id, max_bytes):
            self.calls.append(item_id)
            return {"ram_gb": None, "exclusions": []}

    ids = seed(conn)
    conn.execute("UPDATE listings SET next_check_after = NULL, last_checked_at = NULL")
    sold(client, ids["trap"]["1"])
    before = conn.execute("SELECT last_checked_at, next_check_after, detail_status FROM listings WHERE id = %s",
                          (ids["listing_id"],)).fetchone()
    fetcher = Recorder()
    collector.run_enrichment(conn, settings(), fetcher, 5, browser_enabled=False)
    assert fetcher.calls == [UNKNOWN_SHIP[0]]
    assert conn.execute("SELECT last_checked_at, next_check_after, detail_status FROM listings WHERE id = %s",
                        (ids["listing_id"],)).fetchone() == before
    # A targeted run for the sold listing is a no-op too.
    fetcher = Recorder()
    collector.run_enrichment(conn, settings(), fetcher, 5, browser_enabled=False, target_listing_id=ids["listing_id"])
    assert fetcher.calls == []


@requires_db
def test_listing_marked_sold_during_a_quiet_gap_is_not_fetched_or_touched(conn, client, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace

    from rackscout import collector

    class Recorder:
        def __init__(self):
            self.calls = []

        def fetch_description(self, item_id, max_bytes):
            self.calls.append(item_id)
            return {"ram_gb": None, "exclusions": []}

    ids = seed(conn)
    conn.execute("UPDATE listings SET next_check_after = NULL, last_checked_at = NULL")
    configs = {TRAP[0]: ids["trap"]["1"], UNKNOWN_SHIP[0]: ids["other"]}
    fetcher = Recorder()
    gaps = []
    cols = "last_checked_at, next_check_after, description_status, description_checked_at, check_message"
    before = {i: conn.execute(f"SELECT {cols} FROM listings WHERE item_id = %s", (i,)).fetchone() for i in configs}

    def quiet_gap(seconds):  # deterministic: the user marks the not-yet-fetched candidate sold during the gap
        gaps.append(seconds)
        pending = next(i for i in configs if i not in fetcher.calls)
        assert sold(client, configs[pending]).status_code == 200

    monkeypatch.setattr(collector, "time", SimpleNamespace(sleep=quiet_gap))
    collector.run_enrichment(conn, replace(settings(), request_delay=6.0), fetcher, 5, browser_enabled=False)

    assert gaps == [6.0] and len(fetcher.calls) == 1
    (fetched,) = fetcher.calls
    skipped = next(i for i in configs if i != fetched)
    assert conn.execute("SELECT manual_sold_at IS NOT NULL AS s FROM listings WHERE item_id = %s",
                        (skipped,)).fetchone()["s"]
    assert conn.execute(f"SELECT {cols} FROM listings WHERE item_id = %s", (skipped,)).fetchone() == before[skipped]
    assert conn.execute("SELECT last_checked_at FROM listings WHERE item_id = %s", (fetched,)).fetchone()[
        "last_checked_at"] is not None


# ---------------------------------------------------------------------------
# Selected refresh
# ---------------------------------------------------------------------------


@requires_db
def test_selected_refresh_of_a_sold_listing_is_rejected_without_state_change(conn, client):
    from rackscout.refresh import ListingRefreshNotFound

    ids = seed(conn)
    sold(client, ids["trap"]["1"])
    before = _row(conn)
    for config_id in (*ids["trap"].values(), ids["child"]):
        response = client.post(f"/api/listings/{config_id}/refresh",
                               headers={"Origin": ORIGIN, "X-RackScout-Refresh": "1", "Sec-Fetch-Site": "same-origin",
                                        "Content-Type": "application/json"}, json={})
        assert response.status_code == 404, response.text
    from rackscout import refresh

    c = refresh.connect_worker(settings())
    try:
        with pytest.raises(ListingRefreshNotFound):
            refresh.enqueue(c, refresh_settings(), configuration_id=ids["trap"]["2"])
    finally:
        c.close()
    after = _row(conn)
    assert (after["state"], after["request_id"], after["requested_at"], after["target_listing_id"]) == \
        (before["state"], before["request_id"], before["requested_at"], before["target_listing_id"])

    sold(client, ids["trap"]["1"], False)  # nothing consumed: undo allows the refresh immediately
    c = refresh.connect_worker(settings())
    try:
        assert refresh.enqueue(c, refresh_settings(), configuration_id=ids["trap"]["2"])[0] == "accepted"
    finally:
        c.close()


@requires_db
def test_queued_selected_refresh_does_nothing_when_listing_is_marked_sold_afterwards(conn, client):
    from rackscout import refresh

    ids = seed(conn)
    conn.execute("UPDATE listings SET next_check_after = NULL")
    c = refresh.connect_worker(settings())
    try:
        assert refresh.enqueue(c, refresh_settings(), configuration_id=ids["trap"]["1"])[0] == "accepted"
    finally:
        c.close()
    sold(client, ids["trap"]["1"])
    before = conn.execute("SELECT last_checked_at, next_check_after, description_status, source_record FROM listings "
                          "WHERE id = %s", (ids["listing_id"],)).fetchone()
    fetcher = SourceFetcher(_snap())
    assert _process(refresh_settings(max_details=5, browser_enabled=False), fetcher) == "succeeded"
    assert fetcher.index_calls == [] and fetcher.description_calls == []
    assert conn.execute("SELECT last_checked_at, next_check_after, description_status, source_record FROM listings "
                        "WHERE id = %s", (ids["listing_id"],)).fetchone() == before


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


@requires_db
def test_every_listing_serializes_manual_sold_at_null_or_iso_without_changing_match_state(conn, client):
    ids = seed(conn)
    items = listing_ids(client)
    assert items and all("manual_sold_at" in i and i["manual_sold_at"] is None for i in items.values())
    detail_before = client.get(f"/api/listings/{ids['trap']['1']}").json()
    sold(client, ids["trap"]["1"])
    detail_after = client.get(f"/api/listings/{ids['trap']['1']}").json()
    stamp = detail_after.pop("manual_sold_at")
    assert stamp is not None and datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo is not None
    detail_before.pop("manual_sold_at")
    assert detail_after == detail_before  # includes match_state, prices, hardware, evidence
    assert json.dumps(detail_after)
