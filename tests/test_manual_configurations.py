"""Manual configurations: user-entered RAM/price pairs as separate rows that survive collection.

Database tests use the isolated UUID-schema fixture from ``test_db``. New modules are imported inside
the tests so a missing implementation is a test failure, not a collection error.
"""
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from test_db import (  # noqa: F401 - `conn` is a fixture
    TEST_URL,
    TRAP,
    UNKNOWN_SHIP,
    FakeFetcher,
    conn,
    recompute_listing,
    requires_db,
    settings,
    snapshot,
    verified_page,
)

PERMISSIVE = dict(min_ram=None, min_bays=None, diskless=False)
ALL_PRESENT = {"cpu_included": "present", "psu": "present", "cooling": "present", "nic": "present",
               "drive_path": "present"}
# Two orderable variations that both remain in inventory (price at check equals the TRAP index price).
RAM_VARIATIONS = [
    {"variant_id": "1", "labels": {"Memory": "32GB"}, "price": "429.88"},
    {"variant_id": "2", "labels": {"Memory": "64GB"}, "price": "499.88"},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _body(revision, label="Manual option", notes="", fields=None, price_confirmed=False):
    return {"revision": revision, "label": label, "notes": notes, "fields": dict(fields or {}),
            "price_confirmed": price_confirmed}


def _discover(conn, items, when=None):
    from rackscout import collector

    when = when or datetime.now(timezone.utc)
    assert collector.run_discovery(conn, settings(), FakeFetcher(snapshot(when, items))) == "success"


def _source_id(conn, item_id, variant_key=""):
    return conn.execute(
        """SELECT c.id FROM configurations c JOIN listings l ON l.id = c.listing_id
           WHERE l.item_id = %s AND c.variant_key = %s""", (item_id, variant_key)).fetchone()["id"]


def _row(conn, config_id):
    return conn.execute("SELECT * FROM configurations WHERE id = %s", (config_id,)).fetchone()


def _revision(conn, config_id):
    from rackscout import manual

    return manual.editor(conn, config_id)["revision"]


def _create(conn, config_id, **body):
    from rackscout import manual

    return manual.create(conn, config_id, manual.parse_write(_body(_revision(conn, config_id), **body)))


def _update(conn, config_id, **body):
    from rackscout import manual

    return manual.update(conn, config_id, manual.parse_write(_body(_revision(conn, config_id), **body)))


def _delete(conn, config_id):
    from rackscout import manual

    return manual.delete(conn, config_id, _revision(conn, config_id))


def _listed(conn, **filters):
    from rackscout import queries

    return queries.query_listings(conn, queries.Filters(**filters))["items"]


def _ids(items, item_id=TRAP[0]):
    return {i["id"] for i in items if i["item_id"] == item_id}


def _detail(conn, config_id):
    from rackscout import queries

    return queries.get_listing(conn, config_id)


def _verify(conn, item_id, variations, price_at_check="268.88"):
    from rackscout import collector

    conn.execute(
        "UPDATE listings SET page_status = 'verified', page_claims = %s WHERE item_id = %s",
        (collector._json(verified_page(datetime.now(timezone.utc), price_at_check=price_at_check,
                                       variations=variations)), item_id),
    )
    recompute_listing(conn, item_id)


def _describe(conn, item_id, html):
    from rackscout import collector
    from rackscout.sources import parse_description

    claims = {**parse_description(html), "status": "verified",
              "checked_at": datetime.now(timezone.utc).isoformat()}
    conn.execute(
        "UPDATE listings SET description_status = 'verified', description_claims = %s WHERE item_id = %s",
        (collector._json(claims), item_id),
    )
    recompute_listing(conn, item_id)


# ---------------------------------------------------------------------------
# Payload validation (no database)
# ---------------------------------------------------------------------------


def test_parse_write_accepts_complete_body_with_decimal_money():
    from rackscout import manual

    parsed = manual.parse_write(_body(3, label="  128GB option ", notes="line one\nline two", fields={
        "model": "R640", "cpu_model": "Gold 6138", "cpu_count": 2, "total_cores": 40, "ram_gb": 128,
        "drive_bays": 8, "drive_size": "SFF", "storage_state": "diskless", "cpu_included": "present",
        "psu": "present", "cooling": "unknown", "nic": "missing", "drive_path": "present", "damage": False,
        "item_price": "499.99", "shipping_price": "0",
    }, price_confirmed=True))
    assert parsed.revision == 3 and parsed.label == "128GB option" and parsed.notes == "line one\nline two"
    assert parsed.price_confirmed is True
    assert parsed.fields["item_price"] == Decimal("499.99") and parsed.fields["shipping_price"] == Decimal("0")
    assert parsed.fields["ram_gb"] == 128 and parsed.fields["nic"] == "missing"
    cleared = manual.parse_write(_body(1, fields={"ram_gb": None, "item_price": None, "drive_size": None}))
    assert cleared.fields == {"ram_gb": None, "item_price": None, "drive_size": None}


@pytest.mark.parametrize("change, field", [
    ({"extra": 1}, "extra"),
    ({"revision": 0}, "revision"),
    ({"revision": True}, "revision"),
    ({"revision": "2"}, "revision"),
    ({"label": ""}, "label"),
    ({"label": "   "}, "label"),
    ({"label": "x" * 121}, "label"),
    ({"label": "bad\x00label"}, "label"),
    ({"notes": "n" * 2001}, "notes"),
    ({"notes": "lone \ud800 surrogate"}, "notes"),
    ({"notes": None}, "notes"),
    ({"price_confirmed": "true"}, "price_confirmed"),
    ({"fields": []}, "fields"),
    ({"fields": {"memory": 128}}, "memory"),
    ({"fields": {"ram_gb": 128.0}}, "ram_gb"),
    ({"fields": {"ram_gb": True}}, "ram_gb"),
    ({"fields": {"ram_gb": -1}}, "ram_gb"),
    ({"fields": {"ram_gb": 1048577}}, "ram_gb"),
    ({"fields": {"cpu_count": 9}}, "cpu_count"),
    ({"fields": {"total_cores": 0}}, "total_cores"),
    ({"fields": {"drive_bays": 129}}, "drive_bays"),
    ({"fields": {"drive_size": "sff"}}, "drive_size"),
    ({"fields": {"storage_state": "some"}}, "storage_state"),
    ({"fields": {"psu": "yes"}}, "psu"),
    ({"fields": {"damage": "no"}}, "damage"),
    ({"fields": {"cpu_model": "x" * 101}}, "cpu_model"),
    ({"fields": {"cpu_model": "Gold\x006138"}}, "cpu_model"),
    ({"fields": {"cpu_model": "No CPU"}}, "cpu_model"),
    ({"fields": {"cpu_model": "none"}}, "cpu_model"),
    ({"fields": {"cpu_model": "Unknown"}}, "cpu_model"),
    ({"fields": {"cpu_model": "N/A"}}, "cpu_model"),
    ({"fields": {"cpu_model": "missing"}}, "cpu_model"),
    ({"fields": {"cpu_model": "without CPUs"}}, "cpu_model"),
    ({"fields": {"cpu_model": "-"}}, "cpu_model"),
    ({"fields": {"model": "R640; DROP"}}, "model"),
    ({"fields": {"item_price": 499.99}}, "item_price"),
    ({"fields": {"item_price": "1.234"}}, "item_price"),
    ({"fields": {"item_price": "NaN"}}, "item_price"),
    ({"fields": {"item_price": "Infinity"}}, "item_price"),
    ({"fields": {"item_price": "1e3"}}, "item_price"),
    ({"fields": {"item_price": "-1.00"}}, "item_price"),
    ({"fields": {"item_price": "100000000.00"}}, "item_price"),
    ({"fields": {"shipping_price": " 5"}}, "shipping_price"),
    ({"price_confirmed": True, "fields": {"ram_gb": 64}}, "item_price"),
    ({"price_confirmed": True, "fields": {"item_price": None}}, "item_price"),
    ({"fields": {"cpu_included": "missing", "cpu_model": "Gold 6138"}}, "cpu_included"),
])
def test_parse_write_rejects_invalid_user_data_naming_the_field(change, field):
    from rackscout import manual

    body = _body(1)
    body.update(change)
    with pytest.raises(manual.ManualValidationError) as exc:
        manual.parse_write(body)
    assert exc.value.field == field
    assert field in str(exc.value)


@pytest.mark.parametrize("where", ["top", "fields"])
@pytest.mark.parametrize("key", ["bad\ud800key", "nul\x00key", "ünïcode"])
def test_unknown_keys_are_rejected_with_ascii_safe_error_text(where, key):
    from rackscout import manual

    body = _body(1)
    (body if where == "top" else body["fields"])[key] = 1
    with pytest.raises(manual.ManualValidationError) as exc:
        manual.parse_write(body)
    text = str(exc.value)
    assert text.isascii() and "\x00" not in text
    assert exc.value.field is not None and exc.value.field.isascii()
    text.encode("utf-8")  # always encodable in a JSON error response


@pytest.mark.parametrize("payload", [None, [], "body", {"revision": 1}])
def test_parse_write_rejects_non_object_or_incomplete_bodies(payload):
    from rackscout import manual

    with pytest.raises(manual.ManualValidationError):
        manual.parse_write(payload)


def test_domain_build_exposes_structured_essential_states():
    from rackscout import domain
    from rackscout.sources import parse_description

    record = {"item_id": "187634883092", "source_url": "https://www.ebay.com/itm/187634883092", "title": TRAP[1],
              "item_price": "268.88", "shipping_price": "0.00", "cpu_raw": "2× Gold 6150", "source_ram_gb": 64,
              "model": "R640", "no_ram_flag": False, "total_cores": 36}
    desc = {**parse_description("<p>Power supplies not included</p>"), "status": "verified"}
    (config,), _ = domain.build_configurations(record, {"socket_capacity": 2, "source_url": "x"}, desc, None)
    states = config["essential_states"]
    assert set(states) == {"cpu", "ram", "psu", "cooling", "nic", "drive_path"}
    assert states["psu"] == "missing" and config["hardware_state"] == "missing"


# ---------------------------------------------------------------------------
# Rows, filters, pagination
# ---------------------------------------------------------------------------


@requires_db
def test_multiple_explicit_ram_price_pairs_are_separate_filterable_rows(conn):
    from rackscout import queries

    _discover(conn, [TRAP])
    src = _source_id(conn, TRAP[0])
    a = _create(conn, src, label="128GB", fields={"ram_gb": 128, "item_price": "500.00", "shipping_price": "0.00"},
                price_confirmed=True)
    b = _create(conn, src, label="256GB", fields={"ram_gb": 256, "item_price": "900.00"}, price_confirmed=True)

    rows = conn.execute(
        """SELECT * FROM configurations WHERE listing_id = (SELECT listing_id FROM configurations WHERE id = %s)
           ORDER BY id""", (src,)).fetchall()
    assert len(rows) == 3  # one source + exactly the two submitted options; no combinations
    manual_rows = [r for r in rows if r["origin"] == "manual"]
    assert [(r["id"], r["ram_gb"], r["ram_state"], r["item_price"], r["shipping_price"], r["quoted_total"])
            for r in manual_rows] == [(a, 128, "confirmed", Decimal("500.00"), Decimal("0.00"), Decimal("500.00")),
                                      (b, 256, "confirmed", Decimal("900.00"), None, None)]
    for r in manual_rows:
        assert r["source_configuration_id"] == src and r["variant_id"] is None
        assert r["variant_key"].startswith("manual:") and r["variant_key"] != "manual:"
        assert r["revision"] == 1 and r["manual_created_at"] is not None
    assert {r["variant_label"] for r in manual_rows} == {"128GB", "256GB"}

    items = {i["id"]: i for i in _listed(conn)}
    assert _ids(items.values()) == {a, b}
    assert items[a]["match_state"] == items[b]["match_state"] == "match"
    assert items[a]["origin"] == "manual" and items[a]["manual"]["price_confirmed"] is True
    assert items[a]["manual"]["source_configuration_id"] == src
    assert items[a]["manual"]["fields"] == ["item_price", "ram_gb", "shipping_price"]
    assert items[a]["offer"]["kind"] == "manual_configuration"
    assert items[a]["offer"]["specification_price_paired"] is True and items[a]["offer"]["price_is_starting"] is False
    assert items[a]["page_verification_current"] is False
    assert items[a]["quoted_total"] == "500.00" and items[b]["quoted_total"] is None and items[b]["shipping_price"] is None
    assert items[a]["essential_states"]["ram"] == "confirmed" and "next_check_after" in items[a]

    assert _ids(_listed(conn, min_ram=200)) == {b}
    capped = _listed(conn, max_total=Decimal("600"))
    assert _ids(capped) == {a} and capped[0]["match_state"] == "match"
    assert [i["id"] for i in _listed(conn, sort="price")] == [a, b]
    assert [i["id"] for i in _listed(conn, sort="ram")] == [b, a]

    page = queries.query_listings(conn, queries.Filters(page_size=1, sort="ram"))
    assert (page["total"], page["total_matches"], page["pages"]) == (2, 2, 2)
    assert [i["id"] for i in page["items"]] == [b]


@requires_db
def test_source_hidden_even_when_manual_child_fails_filters_no_cheap_base_leak(conn):
    from rackscout import manual, queries

    _discover(conn, [TRAP])
    src = _source_id(conn, TRAP[0])
    assert _ids(_listed(conn, max_total=Decimal("300"), **PERMISSIVE)) == {src}
    low = _create(conn, src, label="8GB", fields={"ram_gb": 8, "item_price": "999.00", "shipping_price": "0.00"},
                  price_confirmed=True)

    result = queries.query_listings(conn, queries.Filters())
    assert result["items"] == [] and result["total"] == 0 and result["total_leads"] == 0
    assert _ids(_listed(conn, max_total=Decimal("300"), **PERMISSIVE)) == set()  # cheaper source quote stays hidden
    assert _ids(_listed(conn, **PERMISSIVE)) == {low}
    assert queries.status(conn)["inventory_count"] == 1

    detail = _detail(conn, src)  # detail and editor bypass hiding
    assert detail["origin"] == "source" and detail["item_price"] == "268.88" and detail["manual"] is None
    editor = manual.editor(conn, src)
    assert editor["configuration_id"] == src and editor["source_configuration_id"] is None and editor["fields"] == {}
    assert [(c["id"], c["ram_gb"], c["item_price"], c["price_confirmed"]) for c in editor["configurations"]] == [
        (low, 8, "999.00", True)]


@requires_db
def test_variant_siblings_are_independent_and_fork_keeps_root_parent(conn):
    from rackscout import manual

    _discover(conn, [TRAP])
    _verify(conn, TRAP[0], RAM_VARIATIONS)
    v1, v2 = _source_id(conn, TRAP[0], "1"), _source_id(conn, TRAP[0], "2")
    child = _create(conn, v2, label="v2 at 128GB", fields={"ram_gb": 128})
    assert _ids(_listed(conn, **PERMISSIVE)) == {v1, child}

    row = _row(conn, child)
    assert row["item_price"] is None and row["price_kind"] == "unknown"  # never borrows 429.88 option price

    fork = _create(conn, child, label="v2 at 256GB", fields={"ram_gb": 256, "item_price": "700.00"},
                   price_confirmed=True)
    assert _row(conn, fork)["source_configuration_id"] == v2
    assert _row(conn, child)["ram_gb"] == 128  # fork does not mutate its sibling
    assert manual.editor(conn, fork)["original"] == manual.editor(conn, child)["original"]
    assert manual.editor(conn, fork)["source_configuration_id"] == v2
    assert _ids(_listed(conn, **PERMISSIVE)) == {v1, child, fork}


# ---------------------------------------------------------------------------
# Persistence across collection
# ---------------------------------------------------------------------------


@requires_db
def test_manual_rows_survive_discovery_enrichment_and_source_price_change(conn):
    t0 = datetime.now(timezone.utc) - timedelta(hours=1)
    _discover(conn, [TRAP], t0)
    src = _source_id(conn, TRAP[0])
    child = _create(conn, src, label="Mine", fields={"ram_gb": 128, "item_price": "500.00", "shipping_price": "10.00"},
                    price_confirmed=True)
    before = _row(conn, child)
    base_revision = _row(conn, src)["revision"]

    _discover(conn, [TRAP], t0 + timedelta(minutes=10))  # unchanged source data
    assert _row(conn, child) == before
    assert _row(conn, src)["revision"] == base_revision
    assert _detail(conn, child)["manual"]["source_changed"] is False
    recompute_listing(conn, TRAP[0])  # enrichment recomputation path
    assert _row(conn, child) == before

    cheaper = (TRAP[0], TRAP[1], "250.00", "0.00", TRAP[4], TRAP[5])
    _discover(conn, [cheaper], t0 + timedelta(minutes=20))
    assert _row(conn, child) == before
    source = _row(conn, src)
    assert source["origin"] == "source" and source["item_price"] == Decimal("250.00")
    assert source["revision"] > base_revision and source["essential_states"] is not None
    item = _detail(conn, child)
    assert item["manual"]["source_changed"] is True
    assert (item["item_price"], item["shipping_price"], item["ram_gb"]) == ("500.00", "10.00", 128)
    assert _ids(_listed(conn, **PERMISSIVE)) == {child}


@requires_db
def test_manual_child_survives_source_variant_deactivation_and_reset_does_not_resurrect(conn):
    _discover(conn, [TRAP])
    _verify(conn, TRAP[0], RAM_VARIATIONS)
    v1, v2 = _source_id(conn, TRAP[0], "1"), _source_id(conn, TRAP[0], "2")
    child = _create(conn, v2, fields={"ram_gb": 64, "item_price": "450.00"}, price_confirmed=True)

    _verify(conn, TRAP[0], RAM_VARIATIONS[:1])  # variation 2 no longer offered
    assert _row(conn, v2)["active"] is False and _row(conn, child)["active"] is True
    assert _ids(_listed(conn, **PERMISSIVE)) == {v1, child}
    assert _detail(conn, child)["manual"]["source_changed"] is True

    assert _delete(conn, child) == {"deleted_id": child, "source_id": v2}
    assert _ids(_listed(conn, **PERMISSIVE)) == {v1}


@requires_db
def test_reset_last_child_exposes_active_source_and_ended_listing_excludes_manual_rows(conn):
    from rackscout import manual

    _discover(conn, [TRAP, UNKNOWN_SHIP])
    src = _source_id(conn, TRAP[0])
    history = conn.execute("SELECT count(*) AS n FROM price_history WHERE configuration_id = %s", (src,)).fetchone()["n"]
    a = _create(conn, src, label="A", fields={"ram_gb": 128})
    b = _create(conn, src, label="B", fields={"ram_gb": 256})
    assert _delete(conn, a) == {"deleted_id": a, "source_id": src}
    assert _ids(_listed(conn, **PERMISSIVE)) == {b}
    assert _delete(conn, b) == {"deleted_id": b, "source_id": src}
    assert _ids(_listed(conn, **PERMISSIVE)) == {src}
    assert conn.execute("SELECT count(*) AS n FROM configurations WHERE origin = 'manual'").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM price_history WHERE configuration_id = %s",
                        (src,)).fetchone()["n"] == history
    assert _row(conn, src)["active"] is True

    c = _create(conn, src, label="C", fields={"ram_gb": 128})
    conn.execute("UPDATE listings SET ended_at = now() WHERE item_id = %s", (TRAP[0],))
    assert _ids(_listed(conn, **PERMISSIVE)) == set()
    assert _ids(_listed(conn, **PERMISSIVE), UNKNOWN_SHIP[0])  # other listings unaffected
    with pytest.raises(manual.ManualNotFound):
        manual.editor(conn, c)
    with pytest.raises(manual.ManualNotFound):
        manual.create(conn, src, manual.parse_write(_body(1)))


# ---------------------------------------------------------------------------
# Revisions, locking, caps, prohibited targets
# ---------------------------------------------------------------------------


@requires_db
def test_stale_revisions_conflict_without_mutation(conn):
    from rackscout import manual

    t0 = datetime.now(timezone.utc) - timedelta(hours=1)
    _discover(conn, [TRAP], t0)
    src = _source_id(conn, TRAP[0])
    child = _create(conn, src, label="first", fields={"ram_gb": 64})
    rev = _revision(conn, child)
    assert manual.update(conn, child, manual.parse_write(_body(rev, label="second", fields={"ram_gb": 96}))) == child
    stale = manual.parse_write(_body(rev, label="lost update", fields={"ram_gb": 32}))
    with pytest.raises(manual.ManualConflict):
        manual.update(conn, child, stale)
    row = _row(conn, child)
    assert (row["variant_label"], row["ram_gb"], row["revision"]) == ("second", 96, rev + 1)
    with pytest.raises(manual.ManualConflict):
        manual.delete(conn, child, rev)
    assert _row(conn, child) is not None

    source_rev = _revision(conn, src)
    _discover(conn, [(TRAP[0], TRAP[1], "250.00", "0.00", TRAP[4], TRAP[5])], t0 + timedelta(minutes=5))
    with pytest.raises(manual.ManualConflict):
        manual.create(conn, src, manual.parse_write(_body(source_rev, fields={"ram_gb": 512})))
    assert conn.execute("SELECT count(*) AS n FROM configurations WHERE origin = 'manual'").fetchone()["n"] == 1


@requires_db
def test_source_rows_cannot_be_updated_or_deleted_and_unknown_ids_are_not_found(conn):
    from rackscout import manual

    _discover(conn, [TRAP])
    src = _source_id(conn, TRAP[0])
    before = _row(conn, src)
    with pytest.raises(manual.ManualConflict):
        manual.update(conn, src, manual.parse_write(_body(before["revision"], fields={"ram_gb": 512})))
    with pytest.raises(manual.ManualConflict):
        manual.delete(conn, src, before["revision"])
    assert _row(conn, src) == before
    missing = 10_000_000
    with pytest.raises(manual.ManualNotFound):
        manual.editor(conn, missing)
    for call in (lambda: manual.create(conn, missing, manual.parse_write(_body(1))),
                 lambda: manual.update(conn, missing, manual.parse_write(_body(1))),
                 lambda: manual.delete(conn, missing, 1)):
        with pytest.raises(manual.ManualNotFound):
            call()


@requires_db
def test_model_must_be_catalog_model_of_listing_vendor(conn):
    from rackscout import manual

    _discover(conn, [TRAP])
    src = _source_id(conn, TRAP[0])
    rev = _revision(conn, src)
    for model in ("HPE DL360 Gen10", "R9999"):
        with pytest.raises(manual.ManualValidationError) as exc:
            manual.create(conn, src, manual.parse_write(_body(rev, fields={"model": model})))
        assert exc.value.field == "model"
    child = manual.create(conn, src, manual.parse_write(_body(rev, fields={"model": "R440"})))
    assert _detail(conn, child)["model"] == "R440"


@requires_db
def test_effective_cpu_count_must_fit_effective_model(conn):
    from rackscout import manual

    _discover(conn, [TRAP])  # R640 with 2x Gold 6150
    src = _source_id(conn, TRAP[0])
    rev = _revision(conn, src)
    with pytest.raises(manual.ManualValidationError) as exc:
        manual.create(conn, src, manual.parse_write(_body(rev, fields={"model": "R240"})))  # inherits count 2
    assert exc.value.field == "cpu_count"
    cleared = manual.create(conn, src, manual.parse_write(_body(rev, fields={"model": "R240", "cpu_count": None})))
    assert (_row(conn, cleared)["model"], _row(conn, cleared)["cpu_count"]) == ("R240", None)
    single = manual.create(conn, src, manual.parse_write(_body(rev, fields={"model": "R240", "cpu_count": 1})))
    assert _row(conn, single)["cpu_count"] == 1


def test_real_unmatched_cpu_model_text_is_accepted():
    from rackscout import manual

    for text in ("Xeon Phi 7210", "EPYC 7B12", "Gold 6138 (engineering sample)"):
        assert manual.parse_write(_body(1, fields={"cpu_model": text})).fields["cpu_model"] == text


@requires_db
def test_listing_cap_of_one_hundred_manual_configurations(conn):
    from rackscout import manual

    _discover(conn, [TRAP])
    src = _source_id(conn, TRAP[0])
    rev = _revision(conn, src)
    for n in range(100):
        manual.create(conn, src, manual.parse_write(_body(rev, label=f"option {n}", fields={"ram_gb": 16 + n})))
    with pytest.raises(manual.ManualConflict):
        manual.create(conn, src, manual.parse_write(_body(rev, label="one too many")))
    assert conn.execute("SELECT count(*) AS n FROM configurations WHERE origin = 'manual'").fetchone()["n"] == 100


@requires_db
def test_write_waits_for_listing_lock_only_boundedly(conn):
    from rackscout import db, manual

    _discover(conn, [TRAP])
    src = _source_id(conn, TRAP[0])
    child = _create(conn, src, fields={"ram_gb": 64})
    before = _row(conn, child)
    other = db.connect()
    try:
        other.execute("SELECT id FROM listings WHERE id = %s FOR UPDATE", (before["listing_id"],))
        started = time.monotonic()
        with pytest.raises(manual.ManualConflict):
            manual.update(conn, child, manual.parse_write(_body(before["revision"], fields={"ram_gb": 128})))
        assert time.monotonic() - started < 30
    finally:
        other.rollback()
        other.close()
    assert _row(conn, child) == before


# ---------------------------------------------------------------------------
# Price semantics
# ---------------------------------------------------------------------------


@requires_db
def test_manual_price_pairing_versus_base_and_unknown_shipping(conn):
    from rackscout import manual

    _discover(conn, [TRAP])
    src = _source_id(conn, TRAP[0])

    unpaired = _detail(conn, _create(conn, src, label="unpaired", fields={"ram_gb": 128}))
    assert (unpaired["price_kind"], unpaired["item_price"]) == ("base", "268.88")
    assert unpaired["offer"]["kind"] != "manual_configuration"
    assert unpaired["offer"]["price_is_starting"] is True and unpaired["offer"]["specification_price_paired"] is False
    assert unpaired["match_state"] == "lead"

    entered_id = _create(conn, src, label="entered", fields={"ram_gb": 256, "item_price": "600.00",
                                                             "shipping_price": "0.00"})
    entered = _detail(conn, entered_id)
    assert (entered["item_price"], entered["quoted_total"], entered["price_kind"]) == ("600.00", "600.00", "unknown")
    assert entered["offer"]["kind"] == "manual_unconfirmed"
    assert entered["offer"]["specification_price_paired"] is False and entered["match_state"] == "lead"
    assert manual.editor(conn, entered_id)["fields"]["item_price"] == "600.00"

    no_ship = _detail(conn, _create(conn, src, label="no ship", fields={"ram_gb": 384, "item_price": "700.00"},
                                    price_confirmed=True))
    assert (no_ship["item_price"], no_ship["shipping_price"], no_ship["quoted_total"]) == ("700.00", None, None)
    free = _detail(conn, _create(conn, src, label="free", fields={"ram_gb": 384, "item_price": "700.00",
                                                                 "shipping_price": "0.00"}, price_confirmed=True))
    assert free["quoted_total"] == "700.00" and free["offer"]["kind"] == "manual_configuration"

    # Changing the configuration without re-asserting the price drops the pairing.
    _update(conn, free["id"], label="free", fields={"ram_gb": 512, "item_price": "700.00", "shipping_price": "0.00"})
    changed = _detail(conn, free["id"])
    assert changed["item_price"] == "700.00" and changed["offer"]["kind"] == "manual_unconfirmed"
    assert changed["manual"]["price_confirmed"] is False


@requires_db
@pytest.mark.parametrize("item, shipping, total", [
    ("123.45", None, None),
    ("123.45", "0.00", "123.45"),
    ("123.45", "10.00", "133.45"),
    ("0.00", "0.00", "0.00"),
])
def test_unconfirmed_manual_amount_stays_visible_without_price_pairing(conn, item, shipping, total):
    _discover(conn, [TRAP])
    src = _source_id(conn, TRAP[0])
    fields = {"ram_gb": 64, "item_price": item}
    if shipping is not None:
        fields["shipping_price"] = shipping
    child = _create(conn, src, fields=fields)
    detail = _detail(conn, child)
    assert (detail["item_price"], detail["shipping_price"], detail["quoted_total"]) == (item, shipping, total)
    assert detail["offer"]["kind"] == "manual_unconfirmed"
    assert not detail["offer"]["price_is_starting"] and not detail["offer"]["specification_price_paired"]
    assert detail["manual"]["price_confirmed"] is False and detail["match_state"] == "lead"
    assert child not in _ids(_listed(conn, match_status="match"))
    assert child in _ids(_listed(conn, match_status="lead"))
    if Decimal(item) > 0:
        assert child not in _ids(_listed(conn, max_total=Decimal("100.00")))

    _update(conn, child, fields={"ram_gb": 64, "item_price": None})
    cleared = _detail(conn, child)
    assert cleared["item_price"] is None and cleared["quoted_total"] is None
    assert cleared["offer"]["kind"] != "manual_unconfirmed"


@requires_db
@pytest.mark.parametrize("money, expected", [
    ({"item_price": "456.78", "shipping_price": "12.34"}, ("456.78", "12.34", "469.12")),
    ({"shipping_price": "12.34"}, (None, "12.34", None)),
    ({"shipping_price": "0.00"}, (None, "0.00", None)),
    ({"item_price": None, "shipping_price": "12.34"}, (None, "12.34", None)),
])
def test_upgrade_restores_saved_unconfirmed_prices_without_revising_user_work(conn, money, expected):
    from rackscout import db

    _discover(conn, [TRAP])
    src = _source_id(conn, TRAP[0])
    child = _create(conn, src, fields={"ram_gb": 128, **money})
    confirmed = _create(conn, src, fields={"item_price": "700.00", "shipping_price": "0.00"},
                        price_confirmed=True)
    untouched = _create(conn, src, fields={"ram_gb": 256})
    # Reconstruct the old persisted state, including its misleading unknown-total warning.
    conn.execute("UPDATE configurations SET item_price=NULL, shipping_price=NULL, price_kind='unknown', "
                 "warnings='[\"Price not confirmed for this manual configuration; total unknown\"]'::jsonb "
                 "WHERE id=%s", (child,))
    conn.execute("DELETE FROM schema_migrations WHERE version=8")
    before = _row(conn, child)
    preserved = {cid: _row(conn, cid) for cid in (src, confirmed, untouched)}
    history = conn.execute("SELECT * FROM price_history ORDER BY id").fetchall()
    db.migrate(conn)
    restored = _detail(conn, child)
    assert (restored["item_price"], restored["shipping_price"], restored["quoted_total"]) == expected
    assert restored["offer"]["specification_price_paired"] is False and restored["match_state"] == "lead"
    assert (restored["offer"]["kind"] == "manual_unconfirmed") is (expected[0] is not None)
    after = _row(conn, child)
    changed = {"item_price", "shipping_price", "quoted_total", "price_kind", "shipping_context", "warnings"}
    assert {k: v for k, v in before.items() if k not in changed} == {
        k: v for k, v in after.items() if k not in changed}
    assert {cid: _row(conn, cid) for cid in preserved} == preserved
    assert conn.execute("SELECT * FROM price_history ORDER BY id").fetchall() == history
    assert db.migrate(conn) == []


@requires_db
def test_price_confirmation_time_restamps_when_configuration_changes_only(conn):
    _discover(conn, [TRAP])
    src = _source_id(conn, TRAP[0])
    money = {"item_price": "500.00", "shipping_price": "0.00"}
    child = _create(conn, src, label="A", fields={"ram_gb": 128, **money}, price_confirmed=True)
    stamp = lambda: _row(conn, child)["manual_data"]["price_confirmed_at"]  # noqa: E731
    first = stamp()
    assert first is not None

    _update(conn, child, label="A renamed", notes="seller confirmed by message", fields={"ram_gb": 128, **money},
            price_confirmed=True)
    assert stamp() == first  # label/notes only

    for change in ({"ram_gb": 256}, {"ram_gb": 256, "cpu_model": "Gold 6138"}, {"ram_gb": 256, "cpu_model": "Gold 6138",
                                                                                  "psu": "present"}):
        before = stamp()
        time.sleep(0.01)
        _update(conn, child, label="A renamed", fields={**change, **money}, price_confirmed=True)
        assert stamp() != before  # same money re-confirmed for a different configuration


# ---------------------------------------------------------------------------
# Essentials, blockers and verification honesty
# ---------------------------------------------------------------------------


@requires_db
def test_manual_essentials_blockers_and_confirmed_only_without_browser_claim(conn):
    from rackscout import queries

    _discover(conn, [TRAP])
    src = _source_id(conn, TRAP[0])
    confirmed = _create(conn, src, label="all confirmed",
                        fields={**ALL_PRESENT, "ram_gb": 128, "item_price": "500.00", "shipping_price": "0.00"},
                        price_confirmed=True)
    psu_missing = _create(conn, src, label="no psu", fields={"ram_gb": 128, "psu": "missing",
                                                             "item_price": "400.00", "shipping_price": "0.00"},
                          price_confirmed=True)
    item = _detail(conn, confirmed)
    assert item["hardware_state"] == "confirmed" and item["page_verification_current"] is False
    assert item["page_verified_at"] is None
    assert item["essential_states"] == {k: "confirmed" for k in ("cpu", "ram", "psu", "cooling", "nic", "drive_path")}
    strict = queries.query_listings(conn, queries.Filters(confirmed_only=True))
    assert [i["id"] for i in strict["items"]] == [confirmed]
    assert _detail(conn, psu_missing)["hardware_state"] == "missing"
    assert psu_missing not in _ids(_listed(conn, **PERMISSIVE))  # missing essential is a hard contradiction


@requires_db
def test_source_missing_essential_cannot_be_lost_by_unrelated_manual_edit(conn):
    _discover(conn, [TRAP])
    _describe(conn, TRAP[0], "<p>2x Intel Xeon Gold 6150</p><p>64GB DDR4 RAM</p><p>Power supplies not included</p>")
    src = _source_id(conn, TRAP[0])
    assert _row(conn, src)["hardware_state"] == "missing"
    ram_only = _create(conn, src, label="more ram", fields={"ram_gb": 256, "item_price": "500.00"}, price_confirmed=True)
    row = _row(conn, ram_only)
    assert row["hardware_state"] == "missing" and row["essential_states"]["psu"] == "missing"
    assert ram_only not in _ids(_listed(conn, **PERMISSIVE))
    fixed = _create(conn, src, label="psu added", fields={"psu": "present", "item_price": "550.00"},
                    price_confirmed=True)
    assert _row(conn, fixed)["hardware_state"] != "missing"
    assert fixed in _ids(_listed(conn, **PERMISSIVE))
    original = _detail(conn, fixed)
    assert any(e["field"] == "psu" and e["source_url"] for e in original["evidence"])  # source evidence retained


@requires_db
def test_cpu_edit_updates_benchmark_and_invalidates_borrowed_cores(conn):
    from rackscout import collector

    record_cores = 36
    _discover(conn, [TRAP])
    conn.execute("UPDATE listings SET source_record = source_record || %s WHERE item_id = %s",
                 (collector._json({"total_cores": record_cores}), TRAP[0]))
    recompute_listing(conn, TRAP[0])
    src = _source_id(conn, TRAP[0])
    assert _row(conn, src)["total_cores"] == record_cores
    assert _detail(conn, src)["benchmark"]["cpu_mark"] == 10000

    cpu = _detail(conn, _create(conn, src, label="6138", fields={"cpu_model": "Gold 6138"}))
    assert cpu["cpu_model"] == "Gold 6138" and cpu["benchmark"]["cpu_mark"] == 8000 and cpu["total_cores"] is None
    count = _detail(conn, _create(conn, src, label="single", fields={"cpu_count": 1}))
    assert count["cpu_count"] == 1 and count["total_cores"] is None
    explicit = _detail(conn, _create(conn, src, label="explicit", fields={"cpu_model": "Gold 6138", "total_cores": 40}))
    assert explicit["total_cores"] == 40
    unchanged = _detail(conn, _create(conn, src, label="ram", fields={"ram_gb": 128}))
    assert unchanged["total_cores"] == record_cores and unchanged["benchmark"]["cpu_mark"] == 10000
    assert _ids(_listed(conn, q="6138", **PERMISSIVE)) == {cpu["id"], explicit["id"]}


# ---------------------------------------------------------------------------
# Migration 004 on an existing database
# ---------------------------------------------------------------------------


@pytest.fixture()
def v3_conn(monkeypatch, tmp_path):
    from test_db import SYNTHETIC_BENCHMARKS

    from rackscout import db

    schema = f"rackscout_test_{uuid.uuid4().hex}"
    admin = psycopg.connect(TEST_URL, autocommit=True, connect_timeout=10)
    try:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        monkeypatch.delenv("PGOPTIONS", raising=False)
        monkeypatch.setenv("DATABASE_URL", make_conninfo(TEST_URL, options=f"-c search_path={schema}"))
        bench = tmp_path / "benchmarks.json"
        bench.write_text(json.dumps(SYNTHETIC_BENCHMARKS), encoding="utf-8")
        monkeypatch.setenv("RACKSCOUT_BENCHMARKS_FILE", str(bench))
        c = db.connect(autocommit=True)
        try:
            with c.transaction():
                c.execute("CREATE TABLE schema_migrations (version integer PRIMARY KEY,"
                          " applied_at timestamptz NOT NULL DEFAULT now())")
                for version, path in db.migration_files():
                    if version <= 3:
                        c.execute(path.read_text(encoding="utf-8"))
                        c.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                db.sync_reference_data(c)
            yield c
        finally:
            c.close()
    finally:
        admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


_LEGACY_COLUMNS = ("variant_key", "variant_id", "variant_label", "model", "cpu_model", "cpu_key", "cpu_model_state",
                   "cpu_count", "cpu_count_state", "total_cores", "ram_gb", "ram_state", "drive_bays", "bays_state",
                   "drive_size", "size_state", "storage_state", "storage_conf", "hardware_state", "damage",
                   "price_kind", "item_price", "shipping_price", "shipping_context", "evidence", "warnings",
                   "exclusions")


def _seed_v3(c, item_id, desc_html, *, variant_key=None, hardware_state=None):
    """Listing plus configuration exactly as the pre-004 collector wrote them (no structured essentials)."""
    from rackscout import collector, db, domain
    from rackscout.sources import parse_description

    record = {
        "item_id": item_id, "source_url": f"https://www.ebay.com/itm/{item_id}",
        "original_url": f"https://www.ebay.com/itm/{item_id}?hash=1", "title": TRAP[1], "condition": "Used",
        "item_price": "268.88", "shipping_price": "0.00", "quantity": 1, "total_cores": 36, "cpu_raw": "2× Gold 6150",
        "cpu_model": None, "cpu_count": None, "source_ram_gb": 64, "model": "R640", "vendor": "Dell", "brand": "Dell",
        "memory_type": "DDR4", "form": "Rack", "no_ram_flag": False, "flags": [],
        "index_url": "https://perunitprice.com/servers/dell/",
    }
    desc = {**parse_description(desc_html), "status": "verified", "checked_at": "2026-11-02T00:00:00+00:00"}
    (config,), _ = domain.build_configurations(record, db.catalog(c)["R640"], desc, None)
    config = {**config, "evidence": collector._json(config["evidence"]),
              "warnings": collector._json(config["warnings"]), "exclusions": collector._json(config["exclusions"])}
    if variant_key is not None:
        config["variant_key"] = variant_key
    if hardware_state is not None:
        config["hardware_state"] = hardware_state
    with c.transaction():
        listing_id = c.execute(
            """INSERT INTO listings (provider, item_id, canonical_url, original_url, title, condition, model, vendor,
                 index_url, source_record, last_seen_source_at, description_status, description_claims)
               VALUES ('PerUnitPrice', %s, %s, %s, %s, 'Used', 'R640', 'Dell', %s, %s, now(), 'verified', %s)
               RETURNING id""",
            (item_id, record["source_url"], record["original_url"], record["title"], record["index_url"],
             collector._json(record), collector._json(desc)),
        ).fetchone()["id"]
        config_id = c.execute(
            f"""INSERT INTO configurations (listing_id, {", ".join(_LEGACY_COLUMNS)})
                VALUES (%(listing_id)s, {", ".join(f"%({k})s" for k in _LEGACY_COLUMNS)}) RETURNING id""",
            {**config, "listing_id": listing_id},
        ).fetchone()["id"]
        c.execute("INSERT INTO price_history (configuration_id, price_kind, item_price, shipping_price, source)"
                  " VALUES (%s, 'base', 268.88, 0.00, 'index')", (config_id,))
    return listing_id, config_id


@requires_db
def test_migration_004_preserves_existing_rows_and_reconstructs_legacy_essentials(v3_conn):
    from rackscout import db, manual

    c = v3_conn
    _, missing_psu = _seed_v3(c, "300000000001", "<p>Power supplies not included</p>")
    # Stored row whose variation is no longer reproducible; the stored blocker must survive.
    _, stale = _seed_v3(c, "300000000002", "<p>Includes 2x 750W power supplies</p>", variant_key="999",
                        hardware_state="missing")
    before = {r["id"]: r for r in c.execute("SELECT * FROM configurations ORDER BY id").fetchall()}

    db.migrate(c)
    assert db.migrate(c) == [] and db.schema_ready(c)
    after = {r["id"]: r for r in c.execute("SELECT * FROM configurations ORDER BY id").fetchall()}
    assert set(after) == set(before)
    for cid, row in after.items():
        assert {k: row[k] for k in before[cid]} == before[cid]
        assert (row["origin"], row["revision"], row["source_configuration_id"], row["manual_data"],
                row["essential_states"]) == ("source", 1, None, None, None)
    assert c.execute("SELECT count(*) AS n FROM price_history").fetchone()["n"] == 2

    assert manual.editor(c, missing_psu)["original"]["essential_states"]["psu"] == "missing"
    child = manual.create(c, missing_psu, manual.parse_write(_body(1, fields={"ram_gb": 256})))
    assert _row(c, child)["hardware_state"] == "missing"
    assert _row(c, child)["essential_states"]["psu"] == "missing"

    stale_child = manual.create(c, stale, manual.parse_write(_body(1, fields={"ram_gb": 256})))
    assert _row(c, stale_child)["hardware_state"] == "missing"
    assert _row(c, missing_psu)["essential_states"] is None  # source rows stay pristine
