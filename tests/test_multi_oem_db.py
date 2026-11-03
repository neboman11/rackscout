"""Multi-OEM persistence: vendor-scoped filters/facets, modern floor, scheduler targets,
source-local vendor page failures, synthetic benchmarks and the 002 migration.

Database tests share the isolated UUID-schema fixture from ``test_db`` (which seeds
a synthetic benchmark file); the migration test builds its own pre-002 schema.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from test_db import (  # noqa: F401 - `conn` is a fixture
    TEST_URL,
    conn,
    recompute_listing,
    requires_db,
    settings,
    verified_page,
)

pytestmark = requires_db

PAGES = {
    "Dell": "https://perunitprice.com/servers/dell/",
    "HPE": "https://perunitprice.com/servers/hpe/",
    "Supermicro": "https://perunitprice.com/servers/supermicro/",
    "Lenovo": "https://perunitprice.com/servers/lenovo/",
    "Cisco": "https://perunitprice.com/servers/cisco/",
}

# (item_id, vendor, model key as normalized by sources, title, cpu)
DELL_R640 = ("200000000001", "Dell", "R640", "Dell PowerEdge R640 1U 8SFF 2x Gold 6150 64GB No HDD", "2× Gold 6150")
DELL_R630 = ("200000000002", "Dell", "R630", "Dell PowerEdge R630 8SFF 2x E5-2683 v4 64GB No HDD", "2× E5-2683 v4")
HPE_DL360_G10 = ("200000000003", "HPE", "HPE DL360 Gen10", "HP ProLiant DL360 Gen10 8SFF 2x Gold 6138 64GB No HDD",
                 "2× Gold 6138")
HPE_DL360_G9 = ("200000000004", "HPE", "HPE DL360 Gen9", "HP ProLiant DL360 Gen9 8SFF 2x E5-2680 v4 64GB No HDD",
                "2× E5-2680 v4")
HPE_DL325_G11 = ("200000000005", "HPE", "HPE DL325 Gen11", "HPE DL325 Gen11 1U EPYC 9124 128GB No HDD", "EPYC 9124")
SMC_1029U = ("200000000006", "Supermicro", "Supermicro SYS-1029U-E1CR4T",
             "SuperMicro 119U-7 SYS-1029U-E1CR4T Server X11DPU 2*5115 GOLD CPU 64GB Ram No HDD", "2× Gold 5115")
SMC_UNKNOWN = ("200000000007", "Supermicro", None,
               "Supermicro CSE-119U 1U Server - w/ X11DPU LGA3647 + 2xIntel Xeon Gold 5220R CPU 64GB", "2× Gold 5220R")
LENOVO_SR630 = ("200000000008", "Lenovo", "Lenovo SR630", "Lenovo ThinkSystem SR630 V1 2x Gold 6130 64GB No HDD",
                "2× Gold 6130")
CISCO_C220_M5 = ("200000000009", "Cisco", "Cisco UCS C220 M5", "Cisco UCS C220 M5 2x Gold 6130 64GB No HDD",
                 "2× Gold 6130")
HPE_DL380_G10 = ("200000000010", "HPE", "HPE DL380 Gen10", "HPE DL380 Gen10 2U 12LFF 2x Gold 6130 64GB No HDD",
                 "2× Gold 6130")
ALL = [DELL_R640, DELL_R630, HPE_DL360_G10, HPE_DL360_G9, HPE_DL325_G11, SMC_1029U, SMC_UNKNOWN, LENOVO_SR630,
       CISCO_C220_M5, HPE_DL380_G10]
PERMISSIVE = dict(min_ram=None, min_bays=None, diskless=False)


def record(item, price="300.00"):
    item_id, vendor, model, title, cpu = item
    return {
        "item_id": item_id, "source_url": f"https://www.ebay.com/itm/{item_id}",
        "original_url": f"https://www.ebay.com/itm/{item_id}?hash=1", "title": title, "condition": "Used",
        "item_price": Decimal(price), "shipping_price": Decimal("0.00"), "quantity": 1, "total_cores": None,
        "cpu_raw": cpu, "cpu_model": None, "cpu_count": None, "source_ram_gb": 64, "model": model, "vendor": vendor,
        "brand": vendor, "memory_type": "DDR4", "form": "Rack", "no_ram_flag": False, "flags": [],
    }


def page_snapshot(updated, items, rejected=0):
    from rackscout.sources import IndexSnapshot

    return IndexSnapshot(updated_at=updated, records=[record(i) for i in items], rejected=rejected)


class PageFetcher:
    """Per-vendor-page results: IndexSnapshot or exception; records which pages were requested."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    def fetch_index(self, url, max_bytes):
        self.calls.append(url)
        result = self.results[url]
        if isinstance(result, Exception):
            raise result
        return result


def by_vendor(updated, items):
    return {url: page_snapshot(updated, [i for i in items if i[1] == vendor]) for vendor, url in PAGES.items()}


def discover(conn, items, when=None):
    from rackscout import collector

    when = when or datetime.now(timezone.utc)
    return collector.run_discovery(conn, settings(), PageFetcher(by_vendor(when, items)))


def listed(conn, **filters):
    from rackscout import queries

    return queries.query_listings(conn, queries.Filters(**filters))["items"]


def item_ids(items):
    return {i["item_id"] for i in items}


# ---------------------------------------------------------------------------
# Identity, modern floor and vendor-scoped generations
# ---------------------------------------------------------------------------


def test_default_modern_floor_spans_every_oem_and_hides_unknown_or_older(conn):
    assert discover(conn, ALL) == "success"
    default = listed(conn, **PERMISSIVE)
    assert item_ids(default) == {DELL_R640[0], HPE_DL360_G10[0], HPE_DL325_G11[0], SMC_1029U[0], LENOVO_SR630[0],
                                 CISCO_C220_M5[0]}
    row = next(i for i in default if i["item_id"] == HPE_DL360_G10[0])
    assert (row["vendor"], row["model"], row["generation"], row["generation_label"], row["target_era"],
            row["rack_units"], row["socket_capacity"]) == ("HPE", "HPE DL360 Gen10", 10, "Gen10", True, 1, 2)
    dell = next(i for i in default if i["item_id"] == DELL_R640[0])
    assert (dell["vendor"], dell["model"], dell["generation_label"]) == ("Dell", "R640", "14G")

    everything = listed(conn, modern_only=False, height=None, **PERMISSIVE)
    assert item_ids(everything) == {i[0] for i in ALL}
    unknown = next(i for i in everything if i["item_id"] == SMC_UNKNOWN[0])
    assert (unknown["vendor"], unknown["model"], unknown["generation"], unknown["target_era"], unknown["rack_units"],
            unknown["socket_capacity"]) == ("Supermicro", None, None, None, None, None)
    older = next(i for i in everything if i["item_id"] == HPE_DL360_G9[0])
    assert (older["vendor"], older["generation"], older["target_era"], older["rack_units"]) == ("HPE", None, None, None)
    # Unknown rack height never satisfies an explicit 1U constraint, even with the modern floor disabled.
    assert SMC_UNKNOWN[0] not in item_ids(listed(conn, modern_only=False, **PERMISSIVE))


def test_same_generation_number_under_different_vendors_is_not_conflated(conn):
    assert discover(conn, ALL) == "success"
    hpe11 = listed(conn, vendor="HPE", generation=11, **PERMISSIVE)
    smc11 = listed(conn, vendor="Supermicro", generation=11, **PERMISSIVE)
    assert item_ids(hpe11) == {HPE_DL325_G11[0]}
    assert item_ids(smc11) == {SMC_1029U[0]}
    assert listed(conn, vendor="Dell", generation=10, **PERMISSIVE) == []
    assert item_ids(listed(conn, vendor="HPE", generation=10, **PERMISSIVE)) == {HPE_DL360_G10[0]}
    assert item_ids(listed(conn, vendor="Lenovo", **PERMISSIVE)) == {LENOVO_SR630[0]}
    assert item_ids(listed(conn, models=("HPE DL360 Gen10",), **PERMISSIVE)) == {HPE_DL360_G10[0]}


def test_multi_model_selection_unions_models_and_intersects_other_filters(conn):
    assert discover(conn, ALL) == "success"
    both_dell = listed(conn, models=("R640", "R630"), modern_only=False, **{**PERMISSIVE, "height": None})
    assert item_ids(both_dell) == {DELL_R640[0], DELL_R630[0]}
    # All-vendor selection may span OEMs.
    cross = listed(conn, models=("R640", "HPE DL360 Gen10", "Lenovo SR630"), **PERMISSIVE)
    assert item_ids(cross) == {DELL_R640[0], HPE_DL360_G10[0], LENOVO_SR630[0]}
    # Empty selection is unconstrained; duplicate entries do not change the result.
    assert item_ids(listed(conn, models=(), **PERMISSIVE)) == item_ids(listed(conn, **PERMISSIVE))
    assert item_ids(listed(conn, models=("R640", "R640"), **PERMISSIVE)) == {DELL_R640[0]}
    # Vendor AND models: models of other OEMs match nothing; same-OEM models remain.
    assert listed(conn, vendor="Dell", models=("HPE DL360 Gen10",), **PERMISSIVE) == []
    assert item_ids(listed(conn, vendor="Dell", models=("R640", "HPE DL360 Gen10"), **PERMISSIVE)) == {DELL_R640[0]}
    # Other filters still apply to every selected model (HPE DL360 Gen10 is 1U; DL380 Gen10 is 2U).
    assert item_ids(listed(conn, models=("HPE DL360 Gen10", "HPE DL380 Gen10"), **PERMISSIVE)) == {HPE_DL360_G10[0]}
    # A hostile model value is only ever a bound parameter.
    assert listed(conn, models=("R640'; DROP TABLE listings; --",), **PERMISSIVE) == []


def test_facets_expose_vendor_scoped_models_and_generations(conn):
    from rackscout import queries

    assert discover(conn, ALL) == "success"
    facets = queries.facets(conn)
    assert facets["vendors"] == ["Dell", "HPE", "Supermicro", "Lenovo", "Cisco"]
    assert facets["drive_sizes"] == ["SFF", "LFF"]
    assert {"R640", "HPE DL360 Gen10", "Supermicro SYS-1029U-E1CR4T", "HPE DL360 Gen9"} <= set(facets["models"])
    assert facets["model_vendors"]["R640"] == "Dell"
    assert facets["model_vendors"]["HPE DL360 Gen10"] == "HPE"
    assert facets["model_vendors"]["HPE DL360 Gen9"] == "HPE"  # active uncataloged model keeps its vendor
    assert facets["model_vendors"]["Supermicro SYS-1029U-E1CR4T"] == "Supermicro"
    assert set(facets["model_vendors"]) == set(facets["models"])
    gens = facets["generations_by_vendor"]
    assert 14 in gens["Dell"] and 10 not in gens["Dell"]
    assert {10, 11} <= set(gens["HPE"]) and 11 in gens["Supermicro"]
    assert sorted({g for v in gens.values() for g in v}) == facets["generations"]
    assert facets["generation_labels"]["HPE"]["10"] == "Gen10"
    assert "X11" in facets["generation_labels"]["Supermicro"]["11"]
    assert facets["generation_labels"]["Dell"]["14"] == "14G"


def test_scheduler_targets_are_catalog_target_era_1u_for_every_vendor(conn):
    from rackscout import collector

    assert discover(conn, ALL) == "success"
    new_targets = {r["item_id"] for r in conn.execute(collector.NEW_TARGETS_SQL, (50,)).fetchall()}
    assert new_targets == {DELL_R640[0], HPE_DL360_G10[0], HPE_DL325_G11[0], SMC_1029U[0], LENOVO_SR630[0],
                           CISCO_C220_M5[0]}


def test_benchmark_is_per_cpu_and_sorts_across_vendors(conn):
    assert discover(conn, [DELL_R640, HPE_DL360_G10, LENOVO_SR630]) == "success"
    items = listed(conn, sort="benchmark", **PERMISSIVE)
    assert [i["item_id"] for i in items] == [DELL_R640[0], HPE_DL360_G10[0], LENOVO_SR630[0]]
    hpe = items[1]
    assert hpe["cpu_count"] == 2
    assert hpe["benchmark"]["cpu_mark"] == 8000  # synthetic per-CPU value, never multiplied by count
    assert hpe["benchmark"]["scope"] == "single_cpu"


# ---------------------------------------------------------------------------
# Source-local vendor page outcomes
# ---------------------------------------------------------------------------


def test_failed_vendor_page_keeps_its_listings_and_staleness(conn):
    from rackscout import collector, queries
    from rackscout.sources import SourceError

    t0 = datetime.now(timezone.utc) - timedelta(days=10)
    dell_gone = ("200000000011", "Dell", "R640", "Dell PowerEdge R640 1U 2x Gold 6150 64GB No HDD", "2× Gold 6150")
    assert discover(conn, [DELL_R640, dell_gone, HPE_DL360_G10], when=t0) == "success"

    now = datetime.now(timezone.utc)
    results = by_vendor(now, [DELL_R640])
    results[PAGES["HPE"]] = SourceError("index cfg.cols changed; refusing to guess field offsets")
    assert collector.run_discovery(conn, settings(), PageFetcher(results)) == "partial"

    archived = {r["item_id"] for r in conn.execute("SELECT item_id FROM listings WHERE archived_at IS NOT NULL")}
    assert archived == {dell_gone[0]}  # Dell page complete and fresh: absence counts there only
    status = queries.status(conn)
    assert status["discovery"]["state"] == "partial"
    assert "HPE" in status["discovery"]["message"]
    sources = {s["vendor"]: s for s in status["sources"]}
    assert list(sources) == ["Dell", "HPE", "Supermicro", "Lenovo", "Cisco"]
    assert sources["Dell"]["state"] == "success" and sources["Dell"]["stale"] is False
    assert sources["HPE"]["state"] == "failed" and sources["HPE"]["stale"] is True
    assert sources["HPE"]["url"] == PAGES["HPE"]
    assert sources["HPE"]["source_updated_at"].startswith(t0.isoformat()[:19])
    # A fresh OEM cannot make the whole source look fresh while another OEM is stale.
    assert status["stale"] is True
    assert status["source_updated_at"].startswith(t0.isoformat()[:19])
    assert HPE_DL360_G10[0] in item_ids(listed(conn, **PERMISSIVE))


def test_block_stops_remaining_vendor_pages_and_backs_off(conn):
    from rackscout import collector, queries
    from rackscout.sources import SourceBlocked

    now = datetime.now(timezone.utc)
    results = by_vendor(now, [DELL_R640, HPE_DL360_G10])
    results[PAGES["HPE"]] = SourceBlocked("perunitprice.com returned HTTP 403", 600)
    fetcher = PageFetcher(results)
    assert collector.run_discovery(conn, settings(), fetcher) == "partial"
    assert fetcher.calls == [PAGES["Dell"], PAGES["HPE"]]  # no further same-origin requests after a block
    status = queries.status(conn)
    sources = {s["vendor"]: s for s in status["sources"]}
    assert sources["HPE"]["state"] == "blocked"
    assert sources["Supermicro"]["state"] == "blocked"  # skipped because of the origin backoff
    assert any(b["origin"] == "perunitprice.com" for b in status["backoff"])
    assert DELL_R640[0] in item_ids(listed(conn, **PERMISSIVE))


def test_status_coverage_is_oem_neutral(conn):
    from rackscout import queries

    status = queries.status(conn)
    assert all(vendor in status["coverage"] for vendor in PAGES)
    assert status["source_urls"] == list(PAGES.values())
    assert [s["state"] for s in status["sources"]] == ["never"] * 5
    assert status["stale"] is True and status["source_updated_at"] is None


def test_older_vendor_page_snapshot_is_ignored_per_page(conn):
    from rackscout import collector

    now = datetime.now(timezone.utc)
    assert discover(conn, [DELL_R640, HPE_DL360_G10], when=now) == "success"
    results = by_vendor(now, [DELL_R640])
    results[PAGES["HPE"]] = page_snapshot(now - timedelta(hours=3), [])
    assert collector.run_discovery(conn, settings(), PageFetcher(results)) == "partial"
    hpe = conn.execute("SELECT archived_at FROM listings WHERE item_id = %s", (HPE_DL360_G10[0],)).fetchone()
    assert hpe["archived_at"] is None


# ---------------------------------------------------------------------------
# Migration 002 on a pre-existing Dell-only database
# ---------------------------------------------------------------------------


@pytest.fixture()
def legacy_conn(monkeypatch, tmp_path):
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
                c.execute((db.MIGRATIONS_DIR / "001_initial.sql").read_text(encoding="utf-8"))
                c.execute("INSERT INTO schema_migrations (version) VALUES (1)")
            yield c
        finally:
            c.close()
    finally:
        admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


LEGACY_UPSTREAM = datetime(2026, 11, 2, 12, 23, 10, tzinfo=timezone.utc)
LEGACY_RECORD = {
    "item_id": "187634883092", "source_url": "https://www.ebay.com/itm/187634883092",
    "original_url": "https://www.ebay.com/itm/187634883092?hash=1",
    "title": "Dell PowerEdge R640 1U 8SFF Server 2x Gold 6150 64GB No HDD", "condition": "Used",
    "item_price": "268.88", "shipping_price": "0.00", "quantity": 1, "total_cores": 36, "cpu_raw": "2× Gold 6150",
    "cpu_model": "Gold 6150", "cpu_count": 2, "source_ram_gb": 64, "model": "R640", "brand": "Dell",
    "memory_type": "DDR4", "form": "Rack", "no_ram_flag": False, "flags": [],
}


def _seed_legacy_dell(c):
    """Rows as written by the Dell-only release (schema 001, no vendor columns)."""
    with c.transaction():
        c.execute(
            """INSERT INTO server_models (model, generation, rack_units, socket_capacity, cpu_family, source_url,
                 source_text, retrieved_at)
               VALUES ('R640', 14, 1, 2, 'Intel Xeon Scalable',
                 'https://i.dell.com/sites/csdocuments/Product_Docs/en/poweredge-r640-spec-sheet.pdf',
                 'Dual-socket, 1U platform', '2026-11-02T00:00:00Z')"""
        )
        listing_id = c.execute(
            """INSERT INTO listings (provider, item_id, canonical_url, original_url, title, condition, model,
                 source_record, last_seen_source_at, first_seen_at)
               VALUES ('PerUnitPrice', %(item_id)s, %(source_url)s, %(original_url)s, %(title)s, 'Used', 'R640',
                 %(record)s, %(upstream)s, %(upstream)s) RETURNING id""",
            {**LEGACY_RECORD, "record": json.dumps(LEGACY_RECORD), "upstream": LEGACY_UPSTREAM},
        ).fetchone()["id"]
        config_id = c.execute(
            """INSERT INTO configurations (listing_id, variant_key, model, cpu_model, cpu_key, cpu_model_state,
                 cpu_count, cpu_count_state, ram_gb, ram_state, drive_bays, bays_state, drive_size, size_state,
                 storage_state, storage_conf, hardware_state, price_kind, item_price, shipping_price,
                 shipping_context, evidence)
               VALUES (%s, '', 'R640', 'Gold 6150', 'gold 6150', 'inferred', 2, 'inferred', 64, 'inferred', 8,
                 'inferred', 'SFF', 'inferred', 'diskless', 'inferred', 'review', 'base', 268.88, 0.00,
                 'Index shipping quote; destination not confirmed; tax excluded',
                 '[{"field":"ram_gb","state":"inferred","text":"index: Index RAM field: 64GB","source_url":"https://perunitprice.com/servers/dell/"}]')
               RETURNING id""",
            (listing_id,),
        ).fetchone()["id"]
        for price in ("299.00", "268.88"):
            c.execute("INSERT INTO price_history (configuration_id, price_kind, item_price, shipping_price, source)"
                      " VALUES (%s, 'base', %s, 0.00, 'index')", (config_id, price))
        c.execute("""INSERT INTO source_runs (provider, kind, finished_at, outcome, upstream_updated_at, discovered_count,
                       message) VALUES ('PerUnitPrice', 'discovery', %s, 'success', %s, 1, '1 listings')""",
                  (LEGACY_UPSTREAM + timedelta(minutes=5), LEGACY_UPSTREAM))
    return listing_id, config_id


def test_migration_002_preserves_dell_inventory_history_and_freshness(legacy_conn):
    from rackscout import db, queries

    listing_id, config_id = _seed_legacy_dell(legacy_conn)
    assert db.migrate(legacy_conn) == [2]
    assert db.migrate(legacy_conn) == []
    assert db.schema_ready(legacy_conn)

    listing = legacy_conn.execute("SELECT * FROM listings WHERE id = %s", (listing_id,)).fetchone()
    assert listing["vendor"] == "Dell" and listing["index_url"] == PAGES["Dell"]
    assert listing["source_record"]["vendor"] == "Dell" and listing["source_record"]["index_url"] == PAGES["Dell"]
    assert listing["source_record"]["item_price"] == LEGACY_RECORD["item_price"]
    assert listing["last_seen_source_at"] == LEGACY_UPSTREAM and listing["archived_at"] is None
    history = legacy_conn.execute("SELECT item_price FROM price_history WHERE configuration_id = %s ORDER BY id",
                                  (config_id,)).fetchall()
    assert [h["item_price"] for h in history] == [Decimal("299.00"), Decimal("268.88")]
    runs = legacy_conn.execute("SELECT count(*) AS n FROM source_runs").fetchone()["n"]
    assert runs == 1

    model = legacy_conn.execute("SELECT * FROM server_models WHERE model = 'R640'").fetchone()
    assert (model["vendor"], model["generation"], model["generation_label"], model["target_era"]) == ("Dell", 14, "14G", True)

    items = queries.query_listings(legacy_conn, queries.Filters(vendor="Dell", **PERMISSIVE))["items"]
    assert [(i["id"], i["vendor"], i["model"], i["target_era"]) for i in items] == [(config_id, "Dell", "R640", True)]
    assert items[0]["benchmark"]["cpu_mark"] == 10000  # synthetic per-CPU reference

    status = queries.status(legacy_conn)
    sources = {s["vendor"]: s for s in status["sources"]}
    assert sources["Dell"]["source_updated_at"].startswith(LEGACY_UPSTREAM.isoformat()[:19])
    assert sources["Dell"]["state"] == "success"
    assert sources["HPE"]["state"] == "never" and sources["HPE"]["source_updated_at"] is None
    assert status["source_updated_at"] is None  # other OEM pages never collected yet
    assert status["inventory_count"] == 1


def test_benchmark_rows_survive_when_dataset_unconfigured(conn, monkeypatch):
    from rackscout import db

    before = conn.execute("SELECT count(*) AS n FROM cpu_benchmarks").fetchone()["n"]
    assert before > 0
    monkeypatch.delenv("RACKSCOUT_BENCHMARKS_FILE", raising=False)
    with conn.transaction():
        db.sync_reference_data(conn)
    assert conn.execute("SELECT count(*) AS n FROM cpu_benchmarks").fetchone()["n"] == before
