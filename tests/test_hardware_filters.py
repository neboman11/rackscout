"""CPU manufacturer, CPU architecture and memory-platform filters, serialization, writes and migration 007.

CPU platform facts come only from the bundled exact-model reference (``hardware_platforms``) through the
persisted ``configurations.cpu_platform_key``; the memory platform prefers an unambiguous explicit listing
memory type (DDR3/DDR4/DDR5) and otherwise the CPU reference's single platform. A contradiction between
the two never satisfies a positive filter. Database tests use the isolated UUID-schema fixture from
``test_db``; no test makes a network request.
"""
import json
import uuid
from datetime import datetime, timezone

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from test_db import TEST_URL, FakeFetcher, conn, requires_db, settings, snapshot  # noqa: F401 - `conn` is a fixture
from test_us_retailers import catalog_of, connector, discover, retail_record, variant  # noqa: F401 - fixture

PERMISSIVE = dict(min_ram=None, min_bays=None, diskless=False)
TITLE = "Dell PowerEdge R640 1U 8SFF Server 64GB No HDD"

# item_id -> (index CPU field, explicit index memory type)
SKYLAKE = "400000000001"
ROME = "400000000002"
CASCADE = "400000000003"
UNKNOWN_CPU = "400000000004"
CONFLICT = "400000000005"
MILAN = "400000000006"
INVENTORY = {
    SKYLAKE: ("2× Gold 6130", "DDR4"),
    ROME: ("1× EPYC 7402P", "DDR4"),
    CASCADE: ("2× Gold 6230", ""),
    UNKNOWN_CPU: ("2× Xeon Q9999", "DDR3"),
    CONFLICT: ("2× Gold 6130", "DDR3"),
    MILAN: ("1× EPYC 7443P", " ddr4 "),
}


def _discover(conn, inventory=None, when=None):
    from rackscout import collector

    inventory = INVENTORY if inventory is None else inventory
    snap = snapshot(when or datetime.now(timezone.utc),
                    [(item, TITLE, "300.00", "0.00", cpu, 64) for item, (cpu, _) in inventory.items()])
    for record in snap.records:
        record["memory_type"] = inventory[record["item_id"]][1]
    assert collector.run_discovery(conn, settings(), FakeFetcher(snap)) == "success"


def _source_id(conn, item_id):
    return conn.execute(
        """SELECT c.id FROM configurations c JOIN listings l ON l.id = c.listing_id
           WHERE l.item_id = %s AND c.origin = 'source' AND c.variant_key = ''""", (item_id,)).fetchone()["id"]


def _row(conn, config_id):
    return conn.execute("SELECT * FROM configurations WHERE id = %s", (config_id,)).fetchone()


def _result(conn, **filters):
    from rackscout import queries

    return queries.query_listings(conn, queries.Filters(**{**PERMISSIVE, **filters}))


def _items(conn, **filters):
    return {i["item_id"]: i for i in _result(conn, page_size=100, **filters)["items"]}


def _body(revision, fields=None, label="Manual option"):
    return {"revision": revision, "label": label, "notes": "", "fields": dict(fields or {}), "price_confirmed": False}


def _create(conn, config_id, fields):
    from rackscout import manual

    revision = manual.editor(conn, config_id)["revision"]
    return manual.create(conn, config_id, manual.parse_write(_body(revision, fields)))


def _update(conn, config_id, fields):
    from rackscout import manual

    revision = manual.editor(conn, config_id)["revision"]
    return manual.update(conn, config_id, manual.parse_write(_body(revision, fields)))


def _listed_by_id(conn, config_id, **filters):
    from rackscout import queries

    items = queries.query_listings(conn, queries.Filters(**{**PERMISSIVE, "page_size": 100, **filters}))["items"]
    return next((i for i in items if i["id"] == config_id), None)


def _reference(cpu_model):
    from rackscout import hardware_platforms

    found = hardware_platforms.lookup(cpu_model)
    assert found is not None, cpu_model
    return found


def _other_platform_reference(not_platform="DDR4"):
    """An exact reference CPU whose single memory platform differs from ``not_platform``."""
    from rackscout import hardware_platforms

    for row in hardware_platforms.references():
        if row["memory_platform"] not in (None, not_platform):
            return row
    pytest.fail(f"reference data has no CPU outside {not_platform}")


# ---------------------------------------------------------------------------
# API parameter validation (no database)
# ---------------------------------------------------------------------------


def test_api_hardware_parameters_are_repeated_validated_deduplicated_and_canonical():
    from fastapi import HTTPException

    from rackscout.api import parse_filters

    default = parse_filters()
    assert (default.cpu_manufacturers, default.cpu_architectures, default.memory_platforms) == ((), (), ())
    parsed = parse_filters(cpu_manufacturer=["AMD", " intel ", "", "Intel", "amd"],
                           cpu_architecture=["Rome", "skylake", "Rome", " "],
                           memory_platform=["ddr5", "DDR4", "DDR5"])
    assert parsed.cpu_manufacturers == ("AMD", "Intel")
    assert parsed.cpu_architectures == ("Rome", "Skylake")
    assert parsed.memory_platforms == ("DDR5", "DDR4")
    for bad in (dict(cpu_manufacturer=["Intel", "VIA"]), dict(cpu_architecture=["Skylake-SP"]),
                dict(cpu_architecture=["Rome; DROP"]), dict(memory_platform=["DDR2"]),
                dict(memory_platform=["DDR4/DDR5"])):
        with pytest.raises(HTTPException) as exc:
            parse_filters(**bad)
        assert exc.value.status_code == 422


def test_api_hardware_parameter_count_is_bounded():
    from fastapi import HTTPException

    from rackscout.api import parse_filters

    with pytest.raises(HTTPException) as exc:
        parse_filters(memory_platform=["DDR4"] * 1000)
    assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
# Filters, counts, paging and serialization
# ---------------------------------------------------------------------------


@requires_db
@pytest.mark.parametrize("filters,expected", [
    ({}, {SKYLAKE, ROME, CASCADE, UNKNOWN_CPU, CONFLICT, MILAN}),
    ({"cpu_manufacturers": ("AMD",)}, {ROME, MILAN}),
    ({"cpu_manufacturers": ("Intel", "AMD")}, {SKYLAKE, ROME, CASCADE, CONFLICT, MILAN}),
    ({"cpu_architectures": ("Skylake",)}, {SKYLAKE, CONFLICT}),
    ({"cpu_architectures": ("Rome", "Milan")}, {ROME, MILAN}),
    ({"cpu_architectures": ("Cascade Lake",)}, {CASCADE}),
    ({"memory_platforms": ("DDR4",)}, {SKYLAKE, ROME, CASCADE, MILAN}),
    ({"memory_platforms": ("DDR3",)}, {UNKNOWN_CPU}),
    ({"memory_platforms": ("DDR5",)}, set()),
    ({"cpu_manufacturers": ("Intel",), "memory_platforms": ("DDR4",)}, {SKYLAKE, CASCADE}),
    ({"cpu_manufacturers": ("AMD",), "cpu_architectures": ("Skylake",)}, set()),
    ({"cpu_architectures": ("Milan",), "q": "Gold"}, set()),
])
def test_filters_or_within_and_across_and_exclude_unknown_from_matches_and_leads(conn, filters, expected):
    _discover(conn)
    for include_leads in (True, False):
        result = _result(conn, page_size=100, include_leads=include_leads, **filters)
        found = {i["item_id"] for i in result["items"]}
        assert found <= expected
        if include_leads:
            assert found == expected
        assert result["total"] == len(found)
        assert result["total_matches"] + result["total_leads"] == result["total"]


@requires_db
def test_counts_and_pages_are_computed_server_side_after_hardware_filters(conn):
    _discover(conn)
    first = _result(conn, memory_platforms=("DDR4",), page_size=3, page=1)
    second = _result(conn, memory_platforms=("DDR4",), page_size=3, page=2)
    assert (first["total"], first["pages"], second["pages"]) == (4, 2, 2)
    assert len(first["items"]) == 3 and len(second["items"]) == 1
    assert {i["item_id"] for i in first["items"] + second["items"]} == {SKYLAKE, ROME, CASCADE, MILAN}
    empty = _result(conn, cpu_manufacturers=("AMD",), memory_platforms=("DDR3",))
    assert (empty["total"], empty["pages"], empty["items"]) == (0, 1, [])


@requires_db
def test_serialized_platform_fields_and_cpu_reference_provenance(conn):
    from rackscout import queries

    _discover(conn)
    items = _items(conn)
    gold = _reference("Gold 6130")
    assert (gold["manufacturer"], gold["architecture"], gold["memory_platform"]) == ("Intel", "Skylake", "DDR4")
    skylake = items[SKYLAKE]
    assert (skylake["cpu_manufacturer"], skylake["cpu_architecture"], skylake["memory_platform"]) == (
        "Intel", "Skylake", "DDR4")
    assert skylake["hardware_reference"] == {"source_url": gold["source_url"], "provider": gold["provider"],
                                             "retrieved_at": skylake["hardware_reference"]["retrieved_at"]}
    assert skylake["hardware_reference"]["retrieved_at"].startswith(gold["retrieved_at"][:10])
    assert skylake["memory_platform_source"] == "listing"
    # Existing table fields are retained beside the new ones.
    for key in ("cpu_model", "cpu_count", "ram_gb", "ram_state", "item_price", "quoted_total", "cpu_spec",
                "benchmark", "match_state", "warnings"):
        assert key in skylake

    rome, milan = _reference("EPYC 7402P"), _reference("EPYC 7443P")
    assert (rome["manufacturer"], rome["architecture"]) == ("AMD", "Rome")
    assert (milan["manufacturer"], milan["architecture"]) == ("AMD", "Milan")
    assert (items[ROME]["cpu_manufacturer"], items[ROME]["cpu_architecture"]) == ("AMD", "Rome")
    assert (items[MILAN]["cpu_architecture"], items[MILAN]["memory_platform"]) == ("Milan", "DDR4")
    assert items[MILAN]["memory_platform_source"] == "listing"  # " ddr4 " is an unambiguous explicit type

    cascade = _reference("Gold 6230")
    assert (cascade["architecture"], cascade["memory_platform"]) == ("Cascade Lake", "DDR4")
    assert (items[CASCADE]["cpu_architecture"], items[CASCADE]["memory_platform"],
            items[CASCADE]["memory_platform_source"]) == ("Cascade Lake", "DDR4", "cpu_reference")

    # An explicit DDR type stands without a known CPU, and implies no CPU platform or provenance.
    unknown = items[UNKNOWN_CPU]
    assert (unknown["cpu_manufacturer"], unknown["cpu_architecture"], unknown["hardware_reference"]) == (
        None, None, None)
    assert (unknown["memory_platform"], unknown["memory_platform_source"]) == ("DDR3", "listing")

    # Explicit DDR3 contradicting the Skylake (DDR4) reference: memory platform unknown, CPU kept.
    conflict = items[CONFLICT]
    assert (conflict["cpu_manufacturer"], conflict["cpu_architecture"]) == ("Intel", "Skylake")
    assert (conflict["memory_platform"], conflict["memory_platform_source"]) == (None, None)
    assert any("DDR3" in w and "DDR4" in w for w in conflict["warnings"])

    detail = queries.get_listing(conn, _source_id(conn, SKYLAKE))
    for key in ("cpu_manufacturer", "cpu_architecture", "memory_platform", "memory_platform_source",
                "hardware_reference"):
        assert detail[key] == skylake[key]


@requires_db
@pytest.mark.parametrize("change", [
    {"cpu_model_state": "conflicting"},
    {"cpu_model_state": "unknown"},
    {"essential": "missing"},
])
def test_conflicting_unknown_or_missing_cpu_never_yields_an_authoritative_platform(conn, change):
    _discover(conn)
    src = _source_id(conn, CASCADE)  # memory platform would come only from the CPU reference
    conn.execute("UPDATE configurations SET cpu_model_state = %s, essential_states = essential_states || %s"
                 " WHERE id = %s",
                 (change.get("cpu_model_state", "inferred"), Jsonb({"cpu": change.get("essential", "inferred")}),
                  src))
    item = _listed_by_id(conn, src)
    assert (item["cpu_manufacturer"], item["cpu_architecture"], item["memory_platform"],
            item["hardware_reference"]) == (None, None, None, None)
    assert _listed_by_id(conn, src, cpu_manufacturers=("Intel",)) is None
    assert _listed_by_id(conn, src, cpu_architectures=("Cascade Lake",)) is None
    assert _listed_by_id(conn, src, memory_platforms=("DDR4",)) is None


@requires_db
def test_facets_list_supported_hardware_labels(conn):
    from rackscout import hardware_platforms, queries

    facets = queries.facets(conn)
    assert facets["cpu_manufacturers"] == ["Intel", "AMD"]
    assert facets["memory_platforms"] == ["DDR3", "DDR4", "DDR5"]
    assert facets["cpu_architectures"] == list(hardware_platforms.architectures())
    assert {"Skylake", "Cascade Lake", "Rome", "Milan"} <= set(facets["cpu_architectures"])
    for key in ("models", "generations", "drive_sizes", "vendors", "providers"):
        assert key in facets


@requires_db
def test_http_listing_filters_and_facets(conn, monkeypatch):
    from fastapi.testclient import TestClient

    from rackscout import api

    _discover(conn)
    monkeypatch.setattr(api, "_settings", settings())
    client = TestClient(api.app)
    base = "/api/listings?min_ram=&min_bays=&diskless=false&page_size=100"
    amd = client.get(base + "&cpu_manufacturer=AMD&cpu_manufacturer=amd").json()
    assert {i["item_id"] for i in amd["items"]} == {ROME, MILAN} and amd["total"] == 2
    both = client.get(base + "&cpu_architecture=Skylake&cpu_architecture=Rome&memory_platform=DDR4").json()
    assert {i["item_id"] for i in both["items"]} == {SKYLAKE, ROME}
    assert client.get(base + "&memory_platform=DDR9").status_code == 422
    assert client.get(base + "&cpu_manufacturer=").json()["total"] == len(INVENTORY)
    assert client.get("/api/facets").json()["memory_platforms"] == ["DDR3", "DDR4", "DDR5"]


# ---------------------------------------------------------------------------
# Persistence boundaries: collector and manual writes
# ---------------------------------------------------------------------------


@requires_db
def test_collector_persists_reference_key_and_explicit_memory_type_without_revision_churn(conn):
    from rackscout import hardware_platforms

    _discover(conn)
    rows = {item: _row(conn, _source_id(conn, item)) for item in INVENTORY}
    for item in INVENTORY:
        assert rows[item]["cpu_model"]
        assert rows[item]["cpu_platform_key"] == hardware_platforms.reference_key(rows[item]["cpu_model"])
    assert rows[SKYLAKE]["cpu_platform_key"] == rows[CONFLICT]["cpu_platform_key"] == _reference("Gold 6130")["cpu_key"]
    assert rows[UNKNOWN_CPU]["cpu_platform_key"] is None
    assert {item: rows[item]["memory_type"] for item in INVENTORY} == {
        SKYLAKE: "DDR4", ROME: "DDR4", CASCADE: None, UNKNOWN_CPU: "DDR3", CONFLICT: "DDR3", MILAN: "DDR4"}
    _discover(conn)
    assert {item: _row(conn, _source_id(conn, item))["revision"] for item in INVENTORY} == {
        item: row["revision"] for item, row in rows.items()}


@requires_db
@pytest.mark.parametrize("raw", ["DDR", "LPDDR5", "DDR4X", "", None])
def test_listing_without_an_explicit_generation_uses_the_cpu_reference(conn, raw):
    _discover(conn, {SKYLAKE: ("2× Gold 6130", raw)})
    src = _source_id(conn, SKYLAKE)
    assert _row(conn, src)["memory_type"] is None
    item = _listed_by_id(conn, src)
    assert (item["memory_platform"], item["memory_platform_source"]) == ("DDR4", "cpu_reference")


@requires_db
@pytest.mark.parametrize("raw", ["DDR4/DDR5", "DDR4 or DDR3"])
def test_listing_stating_mixed_generations_has_unknown_memory_platform(conn, raw):
    from rackscout import queries

    _discover(conn, {SKYLAKE: ("2× Gold 6130", raw)})
    src = _source_id(conn, SKYLAKE)
    item = _listed_by_id(conn, src)
    # The CPU reference's single platform never hides the listing's own contradiction.
    assert (item["memory_platform"], item["memory_platform_source"]) == (None, None)
    for platform in ("DDR3", "DDR4", "DDR5"):
        assert _listed_by_id(conn, src, memory_platforms=(platform,)) is None
    assert queries.get_listing(conn, src)["memory_platform"] is None
    assert (item["cpu_manufacturer"], item["cpu_architecture"]) == ("Intel", "Skylake")
    assert any("conflicting memory" in w for w in item["warnings"])
    # A manual child of the same CPU inherits the frozen contradiction; a changed CPU inherits nothing.
    same = _create(conn, src, {"ram_gb": 128})
    assert _listed_by_id(conn, same)["memory_platform"] is None
    for platform in ("DDR3", "DDR4", "DDR5"):
        assert _listed_by_id(conn, same, memory_platforms=(platform,)) is None
    other = _other_platform_reference("DDR4")
    changed = _create(conn, src, {"cpu_model": other["cpu_model"]})
    assert _listed_by_id(conn, changed)["memory_platform"] == other["memory_platform"]
    assert _listed_by_id(conn, changed, memory_platforms=(other["memory_platform"],)) is not None


@requires_db
def test_newly_observed_mixed_generations_is_a_source_change(conn):
    _discover(conn)
    src = _source_id(conn, CASCADE)  # no explicit memory type
    manual_id = _create(conn, src, {"ram_gb": 128})
    assert _listed_by_id(conn, manual_id)["manual"]["source_changed"] is False
    _discover(conn)  # identical rediscovery is no change
    assert _listed_by_id(conn, manual_id)["manual"]["source_changed"] is False
    _discover(conn, {**INVENTORY, CASCADE: ("2× Gold 6230", "DDR4 or DDR3")})
    assert _listed_by_id(conn, manual_id)["manual"]["source_changed"] is True


@requires_db
def test_manual_row_keeps_base_memory_type_for_the_same_cpu(conn):
    _discover(conn)
    src = _source_id(conn, SKYLAKE)
    manual_id = _create(conn, src, {"ram_gb": 128})
    row = _row(conn, manual_id)
    assert (row["memory_type"], row["cpu_platform_key"]) == ("DDR4", _row(conn, src)["cpu_platform_key"])
    item = _listed_by_id(conn, manual_id, memory_platforms=("DDR4",), cpu_architectures=("Skylake",))
    assert item is not None and item["origin"] == "manual"
    assert _listed_by_id(conn, src) is None  # replaced in inventory by its manual child

    # The same CPU named with benign decoration is not a CPU change.
    decorated = _create(conn, src, {"cpu_model": "Intel Xeon Gold 6130"})
    assert _row(conn, decorated)["memory_type"] == "DDR4"


@requires_db
def test_changed_manual_cpu_never_inherits_the_base_option_memory_type(conn):
    from rackscout import hardware_platforms

    _discover(conn)
    src = _source_id(conn, SKYLAKE)  # explicit DDR4 listing with a Skylake CPU
    other = _other_platform_reference("DDR4")
    manual_id = _create(conn, src, {"cpu_model": other["cpu_model"]})
    row = _row(conn, manual_id)
    assert row["memory_type"] is None
    assert row["cpu_platform_key"] == hardware_platforms.reference_key(other["cpu_model"]) == other["cpu_key"]
    item = _listed_by_id(conn, manual_id)
    assert (item["cpu_manufacturer"], item["cpu_architecture"], item["memory_platform"]) == (
        other["manufacturer"], other["architecture"], other["memory_platform"])
    assert item["memory_platform_source"] == "cpu_reference"
    assert item["hardware_reference"]["source_url"] == other["source_url"]
    assert _listed_by_id(conn, manual_id, memory_platforms=("DDR4",)) is None
    assert _listed_by_id(conn, manual_id, memory_platforms=(other["memory_platform"],)) is not None

    # An unknown manual CPU has no platform at all; the base DDR4 does not leak into it.
    unknown_id = _create(conn, src, {"cpu_model": "Xeon Q9999"})
    unknown = _listed_by_id(conn, unknown_id)
    assert (unknown["cpu_manufacturer"], unknown["memory_platform"], unknown["hardware_reference"]) == (
        None, None, None)
    assert _row(conn, unknown_id)["cpu_platform_key"] is None

    # Updating back to the original CPU restores the base option's explicit memory type.
    _update(conn, manual_id, {"cpu_model": "Gold 6130"})
    row = _row(conn, manual_id)
    assert (row["memory_type"], row["cpu_platform_key"]) == ("DDR4", _row(conn, src)["cpu_platform_key"])
    _update(conn, manual_id, {"cpu_model": other["cpu_model"], "ram_gb": 32})
    assert _row(conn, manual_id)["memory_type"] is None


@requires_db
def test_manual_values_stay_unchanged_when_the_source_memory_type_changes(conn):
    _discover(conn)
    src = _source_id(conn, SKYLAKE)
    manual_id = _create(conn, src, {"ram_gb": 128})
    before = _row(conn, manual_id)
    _discover(conn, {**INVENTORY, SKYLAKE: ("2× Gold 6130", "DDR3")})
    assert _row(conn, src)["memory_type"] == "DDR3"
    after = _row(conn, manual_id)
    assert after == before  # the collector never rewrites manual rows
    item = _listed_by_id(conn, manual_id)
    assert item["memory_platform"] == "DDR4" and item["manual"]["source_changed"] is True


@requires_db
def test_legacy_manual_baseline_never_borrows_the_current_source_memory_type(conn):
    _discover(conn)
    src = _source_id(conn, SKYLAKE)
    manual_id = _create(conn, src, {"ram_gb": 128})
    # A baseline captured before memory types were frozen, and a source listing changed since the copy.
    conn.execute("""UPDATE configurations SET memory_type = NULL,
                      manual_data = manual_data #- '{baseline,values,memory_type}' WHERE id = %s""", (manual_id,))
    _discover(conn, {**INVENTORY, SKYLAKE: ("2× Gold 6130", "DDR3")})
    assert _row(conn, src)["memory_type"] == "DDR3"

    _update(conn, manual_id, {"ram_gb": 256})
    assert _row(conn, manual_id)["memory_type"] is None
    sibling = _create(conn, manual_id, {"ram_gb": 64})  # new child of the same legacy baseline
    assert _row(conn, sibling)["memory_type"] is None
    for cid in (manual_id, sibling):
        item = _listed_by_id(conn, cid)
        assert (item["memory_platform"], item["memory_platform_source"]) == ("DDR4", "cpu_reference")
        assert not any("DDR3" in w for w in item["warnings"])

    # A new manual row of the changed source freezes the source's actual current type.
    fresh = _create(conn, _source_id(conn, SKYLAKE), {"ram_gb": 32})
    assert _row(conn, fresh)["memory_type"] == "DDR3"


# ---------------------------------------------------------------------------
# Retailer memory types: fixed "Memory:" installed field or the native variant's Memory label only
# ---------------------------------------------------------------------------


def _dual_platform_reference():
    from rackscout import hardware_platforms

    for row in hardware_platforms.references():
        if row["memory_platform"] is None and row["manufacturer"] == "Intel":
            return row
    pytest.fail("reference data has no dual-memory Intel CPU")


def _retail_row(conn, item_id, variant_key):
    return conn.execute(
        """SELECT c.* FROM configurations c JOIN listings l ON l.id = c.listing_id
           WHERE l.provider = 'TechMikeNY' AND l.item_id = %s AND c.variant_key = %s""",
        (item_id, variant_key)).fetchone()


@requires_db
def test_retailer_memory_type_comes_only_from_installed_memory_field_or_variant_memory_label(conn, connector):
    dual = _dual_platform_reference()
    gold = "Processors: 2x Intel Xeon Gold 6130."

    def product(item_id, handle, installed_text, variants, title="Dell PowerEdge R640 1U 8SFF Server DDR5 Ready"):
        return lambda observed_at, fresh_until: retail_record(
            "TechMikeNY", item_id, handle, observed_at=observed_at, fresh_until=fresh_until, variants=variants,
            installed_text=installed_text, title=title)

    connector.catalogs["TechMikeNY"] = catalog_of(
        product("500000000001", "r640-labelled", f"{gold} Memory: 64GB (4x16GB) DDR4-2666 RDIMM. Hard Drives: None.",
                [variant("41000000001", "499.00"),
                 variant("41000000002", "599.00", labels={"Memory": "128GB DDR3"}),
                 variant("41000000003", "650.00", labels={"Memory": "256GB"})]),
        # Marketing/platform text naming DDR outside the fixed Memory field is never a memory type.
        product("500000000002", "r640-marketing", f"{gold} Hard Drives: None.",
                [variant("41000000004", "450.00")]),
        product("500000000003", "r640-ambiguous", f"{gold} Memory: 64GB DDR4 or DDR5. Hard Drives: None.",
                [variant("41000000005", "460.00")]),
        # A dual-DDR CPU has no single reference platform; the explicit installed DDR4 still decides.
        product("500000000004", "r240-dual", f"Processors: 1x Intel Xeon {dual['cpu_model']}. "
                                             "Memory: 32GB DDR4. Hard Drives: None.",
                [variant("41000000006", "300.00")], title="Dell PowerEdge R240 1U Server"),
        # Standard module designations (PC3/PC3L, PC4, PC5) name the DDR generation explicitly.
        product("500000000005", "r640-pc-modules", f"{gold} Memory: 64GB (4x16GB) PC4-21300-R. Hard Drives: None.",
                [variant("41000000007", "480.00"),
                 variant("41000000008", "520.00", labels={"Memory": "32GB (4x8GB) PC3L-12800R"}),
                 variant("41000000009", "540.00", labels={"Memory": "64GB PC3-10600R"}),
                 variant("41000000010", "560.00", labels={"Memory": "128GB PC5-38400"}),
                 variant("41000000011", "580.00", labels={"Memory": "64GB DDR4 PC3-10600"})]),
    )
    discover(conn, connector, ebay_items=())
    rows = {key: _retail_row(conn, item, key)["memory_type"] for item, key in (
        ("500000000001", "41000000001"), ("500000000001", "41000000002"), ("500000000001", "41000000003"),
        ("500000000002", "41000000004"), ("500000000003", "41000000005"), ("500000000004", "41000000006"),
        ("500000000005", "41000000007"), ("500000000005", "41000000008"), ("500000000005", "41000000009"),
        ("500000000005", "41000000010"), ("500000000005", "41000000011"))}
    assert rows == {"41000000001": "DDR4", "41000000002": "DDR3", "41000000003": None,
                    "41000000004": None, "41000000005": None, "41000000006": "DDR4",
                    "41000000007": "DDR4", "41000000008": "DDR3", "41000000009": "DDR3",
                    "41000000010": "DDR5", "41000000011": None}

    items = {i["variant_id"]: i for i in _result(conn, page_size=100, modern_only=False, height=None)["items"]
             if i["provider"] == "TechMikeNY"}
    assert (items["41000000001"]["memory_platform"], items["41000000001"]["memory_platform_source"]) == (
        "DDR4", "listing")
    assert items["41000000002"]["memory_platform"] is None  # explicit DDR3 contradicts the Skylake reference
    assert (items["41000000003"]["memory_platform"], items["41000000003"]["memory_platform_source"]) == (
        "DDR4", "cpu_reference")
    assert items["41000000004"]["memory_platform_source"] == "cpu_reference"
    # Mixed explicit generations ("DDR4 or DDR5"; "DDR4 PC3-10600") stay unknown despite the Skylake DDR4
    # reference, and never pass any memory filter.
    for key in ("41000000005", "41000000011"):
        assert (items[key]["memory_platform"], items[key]["memory_platform_source"]) == (None, None)
        for platform in ("DDR3", "DDR4", "DDR5"):
            assert key not in {i["variant_id"] for i in _result(
                conn, page_size=100, modern_only=False, height=None, memory_platforms=(platform,))["items"]}
    assert (items["41000000006"]["memory_platform"], items["41000000006"]["memory_platform_source"]) == (
        "DDR4", "listing")
    ddr4 = {i["variant_id"] for i in _result(conn, page_size=100, modern_only=False, height=None,
                                              memory_platforms=("DDR4",))["items"] if i["provider"] == "TechMikeNY"}
    assert {"41000000001", "41000000006"} <= ddr4 and "41000000002" not in ddr4
    # Rediscovery of unchanged observations never counts as a revision.
    revisions = {r["id"]: r["revision"] for r in conn.execute("SELECT id, revision FROM configurations").fetchall()}
    discover(conn, connector, ebay_items=())
    assert {r["id"]: r["revision"] for r in conn.execute("SELECT id, revision FROM configurations").fetchall()} == revisions


@requires_db
def test_manual_cpu_naming_the_other_manufacturer_is_a_changed_cpu(conn):
    from rackscout import hardware_platforms

    ddr3 = _reference("X5650")  # concise Intel SKU without a vendor marker
    assert (ddr3["manufacturer"], ddr3["memory_platform"]) == ("Intel", "DDR3")
    item = "400000000099"
    _discover(conn, {item: (f"2× {ddr3['cpu_model']}", "DDR3")})
    src = _source_id(conn, item)
    assert _row(conn, src)["memory_type"] == "DDR3"
    contradicted = f"AMD {ddr3['cpu_model']}"
    assert hardware_platforms.reference_key(contradicted) is None
    manual_id = _create(conn, src, {"cpu_model": contradicted})
    row = _row(conn, manual_id)
    assert (row["memory_type"], row["cpu_platform_key"]) == (None, None)
    listed = _listed_by_id(conn, manual_id)
    assert (listed["cpu_manufacturer"], listed["memory_platform"], listed["hardware_reference"]) == (None, None, None)
    assert _listed_by_id(conn, manual_id, memory_platforms=("DDR3",)) is None
    # The unchanged raw CPU text (only re-entered) is genuinely the same CPU.
    same_id = _create(conn, src, {"cpu_model": ddr3["cpu_model"]})
    assert _row(conn, same_id)["memory_type"] == "DDR3"


@requires_db
def test_retailer_ram_and_memory_size_labels_own_their_memory_type(conn, connector):
    dual = _dual_platform_reference()
    cpu = f"Processors: 1x Intel Xeon {dual['cpu_model']}."

    def product(item_id, handle, installed_text, variants):
        return lambda observed_at, fresh_until: retail_record(
            "TechMikeNY", item_id, handle, observed_at=observed_at, fresh_until=fresh_until, variants=variants,
            installed_text=installed_text, title="Dell PowerEdge R240 1U Server")

    connector.catalogs["TechMikeNY"] = catalog_of(
        product("510000000001", "r240-ram-options", f"{cpu} Memory: 16GB DDR4. Hard Drives: None.",
                [variant("42000000001", "300.00"),
                 variant("42000000002", "320.00", labels={"RAM": "32GB DDR3L"}),
                 variant("42000000003", "340.00", labels={"Memory Size": "64GB"}),
                 variant("42000000004", "360.00", labels={"RAM": "32GB DDR4", "Memory Size": "32GB DDR3"}),
                 variant("42000000005", "380.00", labels={"Memory Size": "64GB PC3L-12800R"})]),
        # Installed fields named by the same memory label grammar; capacity/maximum statements never count.
        product("510000000002", "r240-ram-field", f"{cpu} RAM: 32GB PC4-2400T. Max Memory: 64GB DDR5.",
                [variant("42000000006", "310.00")]),
        product("510000000003", "r240-max-only", f"{cpu} Max Memory: 64GB DDR4. Hard Drives: None.",
                [variant("42000000007", "290.00")]),
    )
    discover(conn, connector, ebay_items=())
    rows = {key: _retail_row(conn, item, key)["memory_type"] for item, key in (
        ("510000000001", "42000000001"), ("510000000001", "42000000002"), ("510000000001", "42000000003"),
        ("510000000001", "42000000004"), ("510000000001", "42000000005"), ("510000000002", "42000000006"),
        ("510000000003", "42000000007"))}
    assert rows == {"42000000001": "DDR4", "42000000002": "DDR3", "42000000003": None, "42000000004": None,
                    "42000000005": "DDR3", "42000000006": "DDR4", "42000000007": None}
    items = {i["variant_id"]: i for i in _result(conn, page_size=100, modern_only=False, height=None)["items"]
             if i["provider"] == "TechMikeNY"}
    # A dual-DDR CPU reference gives no platform, so a no-type or conflicting native RAM option stays unknown.
    assert items["42000000002"]["memory_platform"] == "DDR3"
    assert items["42000000003"]["memory_platform"] is None
    assert items["42000000004"]["memory_platform"] is None


@requires_db
def test_reference_resync_derives_keys_without_touching_revisions_or_history(conn):
    from rackscout import db, hardware_platforms

    _discover(conn)
    src = _source_id(conn, SKYLAKE)
    manual_id = _create(conn, src, {"ram_gb": 128})
    conn.execute("UPDATE configurations SET cpu_platform_key = NULL")
    before = {r["id"]: r for r in conn.execute("SELECT * FROM configurations").fetchall()}
    history = conn.execute("SELECT * FROM price_history ORDER BY id").fetchall()
    with conn.transaction():
        db.sync_reference_data(conn)
    after = {r["id"]: r for r in conn.execute("SELECT * FROM configurations").fetchall()}
    for cid, row in after.items():
        assert row["cpu_platform_key"] == hardware_platforms.reference_key(row["cpu_model"])
        assert {k: v for k, v in row.items() if k != "cpu_platform_key"} == {
            k: v for k, v in before[cid].items() if k != "cpu_platform_key"}
    assert after[manual_id]["cpu_platform_key"] is not None
    assert conn.execute("SELECT * FROM price_history ORDER BY id").fetchall() == history
    stored = {r["cpu_key"]: r for r in conn.execute("SELECT * FROM cpu_platforms").fetchall()}
    assert set(stored) == {r["cpu_key"] for r in hardware_platforms.references()}


# ---------------------------------------------------------------------------
# Migration 007 on a populated schema-006 database
# ---------------------------------------------------------------------------


@pytest.fixture()
def v6_conn(monkeypatch, tmp_path):
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
                for version, path in db.migration_files()[:6]:
                    c.execute(path.read_text(encoding="utf-8"))
                    c.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                db.sync_reference_data(c)
            yield c
        finally:
            c.close()
    finally:
        admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


_COMMON = """listing_id, model, cpu_model, cpu_key, cpu_model_state, cpu_count, cpu_count_state, ram_gb, ram_state,
             bays_state, size_state, storage_state, storage_conf, hardware_state, price_kind, item_price,
             shipping_price, shipping_context"""


def _seed_v6(c, other_cpu):
    """Rows as the schema-006 release writes them: eBay source row whose memory type changed after its legacy
    manual children were copied, a retailer row, price history and an active backoff."""
    item_id = "187634883092"
    record = {"item_id": item_id, "source_url": f"https://www.ebay.com/itm/{item_id}",
              "original_url": f"https://www.ebay.com/itm/{item_id}?hash=1", "title": TITLE, "condition": "Used",
              "item_price": "268.88", "shipping_price": "0.00", "cpu_raw": "2× Gold 6150", "source_ram_gb": 64,
              "vendor": "Dell", "model": "R640", "memory_type": "DDR3", "no_ram_flag": False, "flags": [],
              "index_url": "https://perunitprice.com/servers/dell/"}
    with c.transaction():
        listing_id = c.execute(
            """INSERT INTO listings (provider, item_id, canonical_url, original_url, title, condition, model, vendor,
                 index_url, source_record, last_seen_source_at)
               VALUES ('PerUnitPrice', %(item_id)s, %(source_url)s, %(original_url)s, %(title)s, 'Used', 'R640', 'Dell',
                 %(index_url)s, %(record)s, now()) RETURNING id""",
            {**record, "record": json.dumps(record)},
        ).fetchone()["id"]
        source_id = c.execute(
            f"""INSERT INTO configurations (variant_key, {_COMMON}, revision)
                VALUES ('', %s, 'R640', 'Gold 6150', 'gold 6150', 'inferred', 2, 'inferred', 64, 'inferred',
                  'unknown', 'unknown', 'unknown', 'unknown', 'review', 'base', 268.88, 0.00,
                  'Index shipping quote', 3) RETURNING id""",
            (listing_id,),
        ).fetchone()["id"]
        manual_ids = []
        for cpu, fields in (("Gold 6150", {"ram_gb": 128}), (other_cpu, {"cpu_model": other_cpu})):
            data = {"baseline": {"source_configuration_id": source_id, "source_revision": 3, "variant_key": "",
                                 "values": {"cpu_model": "Gold 6150", "cpu_key": "gold 6150"},
                                 "essential_states": {}, "evidence": [], "warnings": [], "exclusions": []},
                    "label": f"Manual {cpu}", "notes": "kept", "fields": fields,
                    "field_updated_at": {k: "2026-11-01T00:00:00Z" for k in fields}, "price_confirmed": True,
                    "price_confirmed_at": "2026-11-01T00:00:00Z"}
            manual_ids.append(c.execute(
                f"""INSERT INTO configurations (variant_key, variant_label, {_COMMON}, origin, source_configuration_id,
                      manual_data, revision, manual_created_at, manual_updated_at)
                    VALUES (%s, %s, %s, 'R640', %s, %s, 'confirmed', 2, 'inferred', 128, 'confirmed', 'unknown',
                      'unknown', 'unknown', 'unknown', 'review', 'exact', 500.00, 0.00, 'Manually entered shipping quote',
                      'manual', %s, %s, 4, now(), now()) RETURNING id""",
                (f"manual:{uuid.uuid4()}", data["label"], listing_id, cpu, cpu.lower().replace("-", " "), source_id,
                 json.dumps(data)),
            ).fetchone()["id"])
        for cid, price in ((source_id, "299.00"), (source_id, "268.88"), (manual_ids[0], "500.00")):
            c.execute("""INSERT INTO price_history (configuration_id, price_kind, item_price, shipping_price, source)
                         VALUES (%s, 'exact', %s, 0.00, 'index')""", (cid, price))
        retail_url = "https://techmikeny.com/products/r640-seeded"
        retail = {"item_id": "500000000009", "source_url": retail_url, "original_url": retail_url, "title": TITLE,
                  "condition": "Refurbished", "vendor": "Dell", "model": "R640", "item_price": "499.00",
                  "shipping_price": None, "currency": "USD", "cpu_raw": None, "source_ram_gb": None,
                  "total_cores": None, "no_ram_flag": False, "flags": [], "memory_type": "DDR5",
                  "retail": {"handle": "r640-seeded", "product_type": "Server", "kind": "configured",
                             "description_text": "Supports DDR5 memory.",
                             "installed_text": "Processors: 2x Intel Xeon Gold 6150. Memory: 64GB DDR4.",
                             "variants": [{"variant_id": "42000000001", "label": None, "labels": {},
                                           "price": "499.00", "available": True, "paired": True}]}}
        retail_listing = c.execute(
            """INSERT INTO listings (provider, item_id, canonical_url, original_url, title, condition, model, vendor,
                 index_url, source_record, last_seen_source_at)
               VALUES ('TechMikeNY', %(item_id)s, %(source_url)s, %(source_url)s, %(title)s, 'Refurbished', 'R640',
                 'Dell', 'https://techmikeny.com/collections/servers', %(record)s, now()) RETURNING id""",
            {**retail, "record": json.dumps(retail)},
        ).fetchone()["id"]
        retail_id = c.execute(
            f"""INSERT INTO configurations (variant_key, variant_id, {_COMMON}, revision)
                VALUES ('42000000001', '42000000001', %s, 'R640', 'Gold 6150', 'gold 6150', 'inferred', 2,
                  'inferred', 64, 'inferred', 'unknown', 'unknown', 'unknown', 'unknown', 'review', 'exact', 499.00,
                  NULL, 'Shipping not quoted', 2) RETURNING id""",
            (retail_listing,),
        ).fetchone()["id"]
        c.execute("""INSERT INTO origin_backoff (origin, blocked_until, reason)
                     VALUES ('perunitprice.com', now() + interval '2 hours', 'challenge page')""")
    return source_id, manual_ids, retail_id


def test_migration_007_preserves_ids_values_manual_rows_history_and_backoff(v6_conn):
    from rackscout import db, hardware_platforms, queries

    c = v6_conn
    other = _other_platform_reference("DDR4")
    source_id, (same_cpu_id, changed_cpu_id), retail_id = _seed_v6(c, other["cpu_model"])
    assert not db.schema_ready(c)  # this release refuses a schema-006 database
    before = {r["id"]: r for r in c.execute("SELECT * FROM configurations ORDER BY id").fetchall()}
    listings = c.execute("SELECT * FROM listings ORDER BY id").fetchall()
    history = c.execute("SELECT * FROM price_history ORDER BY id").fetchall()
    backoff = c.execute("SELECT * FROM origin_backoff ORDER BY origin").fetchall()
    refresh = c.execute("SELECT * FROM refresh_control").fetchall()

    db.migrate(c)
    assert db.migrate(c) == []
    assert db.schema_ready(c)

    after = {r["id"]: r for r in c.execute("SELECT * FROM configurations ORDER BY id").fetchall()}
    assert set(after) == set(before) == {source_id, same_cpu_id, changed_cpu_id, retail_id}
    for cid, row in before.items():
        assert {k: after[cid][k] for k in row} == row  # every pre-existing value, revision and manual_data kept
        assert after[cid]["cpu_platform_key"] == hardware_platforms.reference_key(row["cpu_model"])
    assert after[source_id]["cpu_platform_key"] is not None
    assert after[changed_cpu_id]["cpu_platform_key"] == other["cpu_key"]
    # Source rows take the type their own observation states (eBay index field; retailer installed Memory
    # field, never marketing text or an unrelated key). Legacy manual baselines never captured a type: they
    # stay NULL instead of borrowing the source's current (since changed) DDR3.
    assert {cid: after[cid]["memory_type"] for cid in after} == {
        source_id: "DDR3", same_cpu_id: None, changed_cpu_id: None, retail_id: "DDR4"}
    assert c.execute("SELECT * FROM listings ORDER BY id").fetchall() == listings
    assert c.execute("SELECT * FROM price_history ORDER BY id").fetchall() == history
    assert c.execute("SELECT * FROM origin_backoff ORDER BY origin").fetchall() == backoff
    assert c.execute("SELECT * FROM refresh_control").fetchall() == refresh

    changed = queries.get_listing(c, changed_cpu_id)
    assert (changed["cpu_architecture"], changed["memory_platform"]) == (other["architecture"],
                                                                        other["memory_platform"])
    same = queries.get_listing(c, same_cpu_id)
    assert (same["cpu_manufacturer"], same["memory_platform"], same["memory_platform_source"]) == (
        "Intel", "DDR4", "cpu_reference")
    source = queries.get_listing(c, source_id)
    assert source["memory_platform"] is None  # explicit DDR3 contradicts the Gold 6150 (DDR4) reference
    result = queries.query_listings(c, queries.Filters(memory_platforms=("DDR4",), page_size=100, **PERMISSIVE))
    assert {i["id"] for i in result["items"]} == {same_cpu_id, retail_id}  # source hidden by manual children
