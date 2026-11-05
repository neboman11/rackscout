"""PostgreSQL behaviour: freshness, failure preservation, archiving, match/lead filters.

Requires an explicit dedicated test database: set RACKSCOUT_TEST_DATABASE_URL to
a DSN naming its host and a database whose name identifies it as a test
database (for example ``rackscout_test``). Every test runs in its own freshly
created UUID schema that is dropped afterwards; no other schema is touched and
ambient DATABASE_URL/PG* settings are never used as a fallback.
"""
import json
import os
import re
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

_TEST_DB_NAME = re.compile(r"(?:^|[_-])test(?:$|[_-])", re.IGNORECASE)


def dedicated_test_dsn(raw: str | None) -> str | None:
    """Validate the explicit test DSN before any connection is attempted.

    Returns None when unset or blank (tests skip). Raises ValueError when the
    DSN is unparsable or does not explicitly name a host and a dedicated test
    database, so libpq can never silently fill the gaps from ambient settings.
    """
    dsn = (raw or "").strip()
    if not dsn:
        return None
    try:
        parts = conninfo_to_dict(dsn)
    except psycopg.Error:
        raise ValueError("RACKSCOUT_TEST_DATABASE_URL is not a valid PostgreSQL DSN") from None
    host = str(parts.get("host") or parts.get("hostaddr") or "").strip()
    dbname = str(parts.get("dbname") or "").strip()
    if not host or not dbname:
        raise ValueError("RACKSCOUT_TEST_DATABASE_URL must explicitly name the host and database")
    if not _TEST_DB_NAME.search(dbname):
        raise ValueError("RACKSCOUT_TEST_DATABASE_URL must name a dedicated test database (e.g. rackscout_test)")
    return dsn


TEST_URL = dedicated_test_dsn(os.environ.get("RACKSCOUT_TEST_DATABASE_URL"))
requires_db = pytest.mark.skipif(TEST_URL is None, reason="RACKSCOUT_TEST_DATABASE_URL not set")
pytestmark = requires_db


def _bench(cpu, mark, single):
    return {"cpu_model": cpu, "cpu_mark": mark, "single_thread": single, "samples": 10,
            "version": "Synthetic test v1", "scope": "single_cpu",
            "source_url": f"https://bench.invalid/{cpu.replace(' ', '-')}", "retrieved_at": "2026-11-02T00:00:00Z"}


# Synthetic per-CPU reference values; real benchmark datasets are private and never committed.
SYNTHETIC_BENCHMARKS = {
    "provider": "Synthetic test provider",
    "benchmarks": [_bench("Gold 6150", 10000, 2000), _bench("Gold 6138", 8000, 1500), _bench("Gold 6130", 6000, 1800),
                   _bench("EPYC 7402P", 12000, 1900)],
}


@pytest.fixture()
def conn(monkeypatch, tmp_path):
    assert TEST_URL is not None
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
            assert c.execute("SELECT current_schema() AS s").fetchone()["s"] == schema
            db.migrate(c)
            assert db.migrate(c) == []  # idempotent
            assert db.schema_ready(c)
            c.commit()
            yield c
        finally:
            c.close()
    finally:
        admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


def snapshot(updated, items):
    from rackscout.sources import IndexSnapshot

    records = []
    for item_id, title, price, shipping, cpu, ram in items:
        records.append({
            "item_id": item_id, "source_url": f"https://www.ebay.com/itm/{item_id}",
            "original_url": f"https://www.ebay.com/itm/{item_id}?hash=1", "title": title, "condition": "Used",
            "item_price": Decimal(price), "shipping_price": None if shipping is None else Decimal(shipping),
            "quantity": 1, "total_cores": None, "cpu_raw": cpu, "cpu_model": None, "cpu_count": None,
            "source_ram_gb": ram, "model": "R640", "vendor": "Dell", "brand": "Dell", "memory_type": "DDR4", "form": "Rack",
            "no_ram_flag": ram == 0, "flags": [],
        })
    return IndexSnapshot(updated_at=updated, records=records)


class FakeFetcher:
    def __init__(self, result):
        self.result = result

    def fetch_index(self, url, max_bytes):
        if isinstance(self.result, Exception):
            raise self.result
        if not url.endswith("/dell/"):
            from dataclasses import replace

            return replace(self.result, records=[])
        return self.result


def settings():
    from rackscout.config import load_settings

    # Fake sources never contact providers; pacing tests supply their own controlled clock. Retailer
    # discovery is disabled so these controlled eBay-source tests never reach the retailer connector.
    return replace(load_settings(), request_delay=0.0, retailers_enabled=False)


TRAP = ("187634883092", "Dell PowerEdge R640 1U 8SFF Server Barebones 2x Gold 6150 64GB H730 2x 750w", "268.88", "0.00", "2× Gold 6150", 64)
UNKNOWN_SHIP = ("126905936007", "Dell R640 8SFF 2x Gold 6138 16GB No HDD", "402.00", None, "2× Gold 6138", 16)
TRAP_VARIATIONS = [
    {"variant_id": "1", "labels": {"Memory": "NO RAM"}, "price": "268.88"},
    {"variant_id": "2", "labels": {"Memory": "32GB"}, "price": "429.88"},
]


def verified_page(checked_at, *, price_at_check="268.88", variations=TRAP_VARIATIONS, fresh_hours=6):
    """Stored eBay page verification as written by the collector after a successful check."""
    return {"status": "verified", "has_variations": True, "price_kind": "exact", "index_price_at_check": price_at_check,
            "variations": variations, "checked_at": checked_at.isoformat(),
            "fresh_until": (checked_at + timedelta(hours=fresh_hours)).isoformat()}


def recompute_listing(conn, item_id, provider="PerUnitPrice"):
    from rackscout import collector, db

    row = conn.execute(f"SELECT {collector.RECOMPUTE_COLUMNS} FROM listings WHERE provider = %s AND item_id = %s",
                       (provider, item_id)).fetchone()
    collector.recompute(conn, row, db.catalog(conn), "test")
    conn.commit()


def test_unchanged_old_upstream_stays_stale_after_local_success(conn):
    from rackscout import collector, queries

    old = datetime.now(timezone.utc) - timedelta(hours=12)
    for _ in range(2):
        assert collector.run_discovery(conn, settings(), FakeFetcher(snapshot(old, [TRAP]))) == "success"
    status = queries.status(conn)
    assert status["stale"] is True
    assert status["source_updated_at"].startswith(old.isoformat()[:19])
    assert status["discovery"]["state"] == "success"
    assert "unchanged" in status["discovery"]["message"]


def test_blocked_and_failed_runs_preserve_inventory_and_freshness(conn):
    from rackscout import collector, queries
    from rackscout.sources import SourceBlocked, SourceError

    now = datetime.now(timezone.utc)
    collector.run_discovery(conn, settings(), FakeFetcher(snapshot(now, [TRAP, UNKNOWN_SHIP])))
    before = queries.status(conn)
    assert collector.run_discovery(conn, settings(), FakeFetcher(SourceError("schema changed"))) == "failed"
    assert collector.run_discovery(conn, settings(), FakeFetcher(SourceBlocked("challenge", 60))) == "blocked"
    after = queries.status(conn)
    assert after["inventory_count"] == before["inventory_count"] == 2
    assert after["source_updated_at"] == before["source_updated_at"]
    assert after["discovery"]["state"] == "blocked"
    assert any(b["origin"] == "perunitprice.com" for b in after["backoff"])
    # Backoff suppresses the next attempt without touching inventory.
    assert collector.run_discovery(conn, settings(), FakeFetcher(snapshot(now, []))) == "blocked"


def test_absence_archives_only_after_seven_days_of_newer_valid_snapshots(conn):
    from rackscout import collector

    t0 = datetime.now(timezone.utc) - timedelta(days=10)
    collector.run_discovery(conn, settings(), FakeFetcher(snapshot(t0, [TRAP, UNKNOWN_SHIP])))
    collector.run_discovery(conn, settings(), FakeFetcher(snapshot(t0 + timedelta(days=3), [TRAP])))
    archived = lambda: {r["item_id"] for r in conn.execute("SELECT item_id FROM listings WHERE archived_at IS NOT NULL")}
    assert archived() == set()
    collector.run_discovery(conn, settings(), FakeFetcher(snapshot(t0 + timedelta(days=8), [TRAP])))
    assert archived() == {UNKNOWN_SHIP[0]}
    ended = conn.execute("SELECT count(*) AS n FROM listings WHERE ended_at IS NOT NULL").fetchone()["n"]
    assert ended == 0  # absence never means sold
    # An older snapshot is ignored rather than regressing data.
    assert collector.run_discovery(conn, settings(), FakeFetcher(snapshot(t0, [TRAP, UNKNOWN_SHIP]))) == "failed"
    assert archived() == {UNKNOWN_SHIP[0]}


def test_base_price_title_ram_is_lead_and_verified_variant_is_match(conn):
    from rackscout import collector, db, queries

    collector.run_discovery(conn, settings(), FakeFetcher(snapshot(datetime.now(timezone.utc), [TRAP, UNKNOWN_SHIP])))
    result = queries.query_listings(conn, queries.Filters(min_bays=None, diskless=False))
    assert result["total_matches"] == 0
    assert {i["match_state"] for i in result["items"]} == {"lead"}

    conn.execute(
        "UPDATE listings SET page_status = 'verified', page_claims = %s WHERE item_id = %s",
        (collector._json(verified_page(datetime.now(timezone.utc))), TRAP[0]),
    )
    row = conn.execute(f"SELECT {collector.RECOMPUTE_COLUMNS} FROM listings WHERE item_id = %s",
                       (TRAP[0],)).fetchone()
    collector.recompute(conn, row, db.catalog(conn), "test")
    conn.commit()

    result = queries.query_listings(conn, queries.Filters())
    matches = [i for i in result["items"] if i["match_state"] == "match"]
    assert [(m["variant_id"], m["ram_gb"], m["quoted_total"]) for m in matches] == [("2", 32, "429.88")]
    assert all(i["variant_id"] != "1" for i in result["items"])  # NO RAM variant contradicts min RAM

    capped = queries.query_listings(conn, queries.Filters(max_total=Decimal("500"), min_bays=None, diskless=False))
    unknown = [i for i in capped["items"] if i["item_id"] == UNKNOWN_SHIP[0]]
    assert unknown and unknown[0]["match_state"] == "lead" and unknown[0]["quoted_total"] is None
    below_item = queries.query_listings(conn, queries.Filters(max_total=Decimal("300"), min_bays=None, diskless=False))
    assert all(i["item_id"] != UNKNOWN_SHIP[0] for i in below_item["items"])  # item price already exceeds cap

    detail = queries.get_listing(conn, matches[0]["id"])
    assert detail["benchmark"]["scope"] == "single_cpu" and detail["benchmark"]["cpu_mark"] == 10000  # synthetic, per CPU
    assert detail["socket_capacity"] == 2 and detail["cpu_count"] == 2

    confirmed = queries.query_listings(conn, replace(queries.Filters(), confirmed_only=True))
    assert confirmed["total_leads"] == 0


def test_chassis_scope_excludes_unsupported_models(conn):
    from rackscout import collector, queries

    snap = snapshot(datetime.now(timezone.utc), [TRAP])
    other = dict(snap.records[0], item_id="168520795277", source_url="https://www.ebay.com/itm/168520795277",
                 original_url="https://www.ebay.com/itm/168520795277", model="R630", title="Dell R630 8SFF 2x E5-2683 v4 64GB")
    snap.records.append(other)
    collector.run_discovery(conn, settings(), FakeFetcher(snap))
    default = queries.query_listings(conn, queries.Filters(min_bays=None, diskless=False))
    assert {i["model"] for i in default["items"]} == {"R640"}
    anything = queries.query_listings(conn, queries.Filters(modern_only=False, height=None, min_bays=None, diskless=False))
    r630 = [i for i in anything["items"] if i["model"] == "R630"]
    assert r630 and r630[0]["generation"] is None and r630[0]["rack_units"] is None
