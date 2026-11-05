"""US retailer platforms: provider-qualified identity, migration 006, retailer quote truth and isolation.

Database tests use the isolated UUID-schema fixture from ``test_db``. Retailer acquisition is
controlled by replacing the connector's ``fetch_catalog``/``fetch_product`` with fixed normalized
records (the connector's own parsing has separate tests); no test makes a network request.
"""
import json
import uuid
from dataclasses import replace
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
    requires_db,
    settings,
    snapshot,
)

pytestmark = requires_db

PERMISSIVE = dict(min_ram=None, min_bays=None, diskless=False)
HOSTS = {"TechMikeNY": "techmikeny.com", "SaveMyServer": "savemyserver.com", "CloudNinjas": "cloudninjas.com"}
# The same native digits as the eBay TRAP listing: identities are qualified by provider.
SHARED_ID = TRAP[0]


def _iso(when):
    return when.astimezone(timezone.utc).isoformat()


def variant(variant_id, price, *, labels=None, available=True, paired=True):
    return {"variant_id": variant_id, "label": ", ".join(f"{k}: {v}" for k, v in (labels or {}).items()) or None,
            "labels": dict(labels or {}), "price": price, "available": available, "paired": paired}


def retail_record(provider, item_id, handle, *, observed_at, fresh_until, variants, kind="configured",
                  available=None, title="Dell PowerEdge R640 1U 8SFF 2x Gold 6130 64GB No HDD",
                  installed_text="Processors: 2x Intel Xeon Gold 6130. Memory: 64GB DDR4. Hard Drives: None.",
                  method="retailer_catalog", vendor="Dell", model="R640"):
    url = f"https://{HOSTS[provider]}/products/{handle}"
    prices = [Decimal(v["price"]) for v in variants if v["price"] is not None]
    return {
        "provider": provider, "item_id": item_id, "source_url": url, "original_url": url, "title": title,
        "condition": "Refurbished", "vendor": vendor, "model": model,
        "item_price": str(min(prices)) if prices else None, "shipping_price": None, "currency": "USD",
        "cpu_raw": None, "source_ram_gb": None, "total_cores": None, "no_ram_flag": False, "flags": [],
        "retail": {
            "handle": handle, "product_type": "Server", "kind": kind,
            "description_text": f"{title}. {installed_text} Supports up to 3TB memory.",
            "installed_text": installed_text, "warranty": None, "upstream_updated_at": None,
            "observed_at": _iso(observed_at), "fresh_until": _iso(fresh_until), "method": method,
            "currency_evidence": "variants[].price.currency=USD",
            "variants": variants,
            "available": any(v["available"] for v in variants) if available is None else available,
        },
    }


class Connector:
    """Controlled replacement for the retailer connector functions; records every call."""

    def __init__(self, catalogs=None, products=None):
        self.catalogs = catalogs or {}  # provider -> callable(observed_at, fresh_until) -> records | Exception
        self.products = products or {}  # (provider, item_id) -> callable(observed_at, fresh_until) -> record | Exception
        self.catalog_calls: list[str] = []
        self.product_calls: list[tuple[str, str]] = []
        self.complete = {}

    def fetch_catalog(self, provider, fetcher, *, observed_at, fresh_until, max_bytes):
        from rackscout.retail_sources import CatalogSnapshot

        self.catalog_calls.append(provider)
        source = self.catalogs.get(provider, lambda o, f: [])
        result = source(observed_at, fresh_until)
        if isinstance(result, Exception):
            raise result
        return CatalogSnapshot(records=result, observed_at=observed_at, complete=self.complete.get(provider, True))

    def fetch_product(self, provider, record, fetcher, *, observed_at, fresh_until, max_bytes):
        self.product_calls.append((provider, record["item_id"]))
        result = self.products[(provider, record["item_id"])](observed_at, fresh_until)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture()
def connector(monkeypatch):
    from rackscout import retail_sources

    fake = Connector()
    monkeypatch.setattr(retail_sources, "fetch_catalog", fake.fetch_catalog)
    monkeypatch.setattr(retail_sources, "fetch_product", fake.fetch_product)
    return fake


class EbayOnly(FakeFetcher):
    """Shared fetcher: eBay index pages answer from a snapshot; eBay detail origins must never be used here."""

    def __init__(self, result):
        super().__init__(result)
        self.description_calls: list[str] = []

    def fetch_description(self, item_id, max_bytes):
        self.description_calls.append(item_id)
        return None


def retail_settings(**changes):
    return replace(settings(), retailers_enabled=True, browser_enabled=False, **changes)


def configured_r640(provider="TechMikeNY", item_id=SHARED_ID, handle="dell-poweredge-r640-8sff", *, price="499.00",
                    available=True):
    def build(observed_at, fresh_until):
        return retail_record(provider, item_id, handle, observed_at=observed_at, fresh_until=fresh_until,
                             variants=[variant("40000000001", price, available=available)])
    return build


def catalog_of(*products):
    """Catalog source (list of records) from single-product builders."""
    return lambda observed_at, fresh_until: [build(observed_at, fresh_until) for build in products]


def discover(conn, connector, *, ebay_items=(TRAP,), **changes):
    from rackscout import collector

    fetcher = EbayOnly(snapshot(datetime.now(timezone.utc), list(ebay_items)))
    return collector.run_discovery(conn, retail_settings(**changes), fetcher), fetcher


def listed(conn, **filters):
    from rackscout import queries

    return queries.query_listings(conn, queries.Filters(**{**PERMISSIVE, **filters}))["items"]


def retail_items(conn, provider="TechMikeNY", **filters):
    return [i for i in listed(conn, **filters) if i["provider"] == provider]


# ---------------------------------------------------------------------------
# Migration 006 on a populated schema-005 database
# ---------------------------------------------------------------------------


@pytest.fixture()
def v5_conn(monkeypatch, tmp_path):
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
                for version, path in db.migration_files()[:5]:
                    c.execute(path.read_text(encoding="utf-8"))
                    c.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                db.sync_reference_data(c)
            yield c
        finally:
            c.close()
    finally:
        admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


def _seed_v5(c):
    """Rows exactly as the schema-005 release writes them: eBay listing, variant, manual child, history, target."""
    record = {"item_id": TRAP[0], "source_url": f"https://www.ebay.com/itm/{TRAP[0]}",
              "original_url": f"https://www.ebay.com/itm/{TRAP[0]}?hash=1", "title": TRAP[1], "condition": "Used",
              "item_price": "268.88", "shipping_price": "0.00", "cpu_raw": TRAP[4], "source_ram_gb": 64,
              "vendor": "Dell", "model": "R640", "no_ram_flag": False, "flags": [],
              "index_url": "https://perunitprice.com/servers/dell/"}
    with c.transaction():
        listing_id = c.execute(
            """INSERT INTO listings (provider, item_id, canonical_url, original_url, title, condition, model, vendor,
                 index_url, source_record, last_seen_source_at)
               VALUES ('PerUnitPrice', %(item_id)s, %(source_url)s, %(original_url)s, %(title)s, 'Used', 'R640', 'Dell',
                 %(index_url)s, %(record)s, now()) RETURNING id""",
            {**record, "record": json.dumps(record)},
        ).fetchone()["id"]
        common = """listing_id, model, cpu_model, cpu_key, cpu_model_state, cpu_count, cpu_count_state, ram_gb, ram_state,
                    bays_state, size_state, storage_state, storage_conf, hardware_state, price_kind, item_price,
                    shipping_price, shipping_context"""
        source_id = c.execute(
            f"""INSERT INTO configurations (variant_key, variant_id, variant_label, {common}, revision)
                VALUES ('2', '2', 'Memory: 32GB', %s, 'R640', 'Gold 6150', 'gold 6150', 'inferred', 2, 'inferred', 32,
                  'confirmed', 'unknown', 'unknown', 'unknown', 'unknown', 'review', 'exact', 429.88, 0.00,
                  'eBay page shipping quote', 3) RETURNING id""",
            (listing_id,),
        ).fetchone()["id"]
        manual_data = {"baseline": {"source_configuration_id": source_id, "source_revision": 3, "variant_key": "2",
                                    "values": {}, "essential_states": {}, "evidence": [], "warnings": [],
                                    "exclusions": []},
                       "label": "My 64GB build", "notes": "kept", "fields": {"ram_gb": 64},
                       "field_updated_at": {"ram_gb": "2026-11-01T00:00:00Z"}, "price_confirmed": True,
                       "price_confirmed_at": "2026-11-01T00:00:00Z"}
        manual_id = c.execute(
            f"""INSERT INTO configurations (variant_key, variant_label, {common}, origin, source_configuration_id,
                  manual_data, revision, manual_created_at, manual_updated_at)
                VALUES (%s, 'My 64GB build', %s, 'R640', 'Gold 6150', 'gold 6150', 'confirmed', 2, 'inferred', 64,
                  'confirmed', 'unknown', 'unknown', 'unknown', 'unknown', 'review', 'exact', 500.00, 0.00,
                  'Manually entered shipping quote', 'manual', %s, %s, 4, now(), now()) RETURNING id""",
            (f"manual:{uuid.uuid4()}", listing_id, source_id, json.dumps(manual_data)),
        ).fetchone()["id"]
        for cid, price in ((source_id, "449.88"), (source_id, "429.88"), (manual_id, "500.00")):
            c.execute("""INSERT INTO price_history (configuration_id, price_kind, item_price, shipping_price, source)
                         VALUES (%s, 'exact', %s, 0.00, 'enrichment')""", (cid, price))
        c.execute("""INSERT INTO source_pages (url, vendor, last_attempt_at, last_outcome, upstream_updated_at,
                       last_success_at, record_count)
                     VALUES ('https://perunitprice.com/servers/dell/', 'Dell', now(), 'success', now(), now(), 1)""")
        c.execute("""UPDATE refresh_control SET state = 'succeeded', request_id = %s, requested_at = now(),
                       started_at = now(), finished_at = now(), target_listing_id = %s WHERE id = 1""",
                  (uuid.uuid4(), listing_id))
    return listing_id, source_id, manual_id, manual_data


def test_migration_006_preserves_ids_manual_rows_history_and_qualifies_identity(v5_conn):
    from rackscout import db, queries, refresh

    listing_id, source_id, manual_id, manual_data = _seed_v5(v5_conn)
    assert not db.schema_ready(v5_conn)  # this release refuses a schema-005 database
    before = {r["id"]: r for r in v5_conn.execute("SELECT * FROM configurations ORDER BY id").fetchall()}
    history = v5_conn.execute("SELECT * FROM price_history ORDER BY id").fetchall()

    db.migrate(v5_conn)
    assert db.migrate(v5_conn) == []
    assert db.schema_ready(v5_conn)

    listing = v5_conn.execute("SELECT * FROM listings WHERE id = %s", (listing_id,)).fetchone()
    assert (listing["provider"], listing["item_id"]) == ("PerUnitPrice", TRAP[0])
    after = {r["id"]: r for r in v5_conn.execute("SELECT * FROM configurations ORDER BY id").fetchall()}
    assert set(after) == set(before) == {source_id, manual_id}
    for cid, row in before.items():
        assert {k: after[cid][k] for k in row} == row  # every pre-existing value unchanged, including keys/revisions
        assert after[cid]["source_quote"] is None
    assert after[manual_id]["manual_data"] == manual_data
    assert v5_conn.execute("SELECT * FROM price_history ORDER BY id").fetchall() == history
    page = v5_conn.execute("SELECT * FROM source_pages").fetchone()
    assert (page["provider"], page["vendor"]) == ("PerUnitPrice", "Dell")

    detail = queries.get_listing(v5_conn, manual_id)
    assert (detail["provider"], detail["platform"], detail["quote"]) == ("PerUnitPrice", "eBay (PerUnitPrice)", None)
    assert detail["source_url"] == f"https://www.ebay.com/itm/{TRAP[0]}"
    assert detail["offer"]["kind"] == "manual_configuration"
    from test_manual_refresh import refresh_settings

    state = refresh.status(v5_conn, refresh_settings())
    assert (state["target_item_id"], state["target_provider"]) == (TRAP[0], "PerUnitPrice")

    # Native IDs are unique per provider only; each provider keeps its own URL grammar.
    insert = """INSERT INTO listings (provider, item_id, canonical_url, original_url, title, source_record,
                  last_seen_source_at) VALUES (%s, %s, %s, %s, 'x', '{}', now())"""
    retail_url = "https://techmikeny.com/products/dell-r640"
    with v5_conn.transaction():
        v5_conn.execute(insert, ("TechMikeNY", TRAP[0], retail_url, retail_url))
    for bad in (("PerUnitPrice", TRAP[0], f"https://www.ebay.com/itm/{TRAP[0]}", "x"),  # duplicate provider identity
                ("TechMikeNY", "777", f"https://www.ebay.com/itm/{TRAP[0]}", "x"),  # retailer id on an eBay URL
                ("CloudNinjas", "778", retail_url, retail_url),  # another retailer's host
                ("PerUnitPrice", "123456789012", retail_url, retail_url),  # eBay id on a retailer URL
                ("ServerMonkey", "779", "https://servermonkey.com/products/x", "x")):  # unregistered provider
        with pytest.raises(psycopg.IntegrityError), v5_conn.transaction():
            v5_conn.execute(insert, bad)
    with pytest.raises(psycopg.IntegrityError), v5_conn.transaction():
        v5_conn.execute("""UPDATE configurations SET source_quote = '{"method":"retailer_catalog"}'
                           WHERE id = %s""", (manual_id,))


def test_schema_ready_requires_exact_release_version(conn):
    from rackscout import db

    assert db.schema_ready(conn)
    with conn.transaction():
        conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (db.latest_version() + 1,))
    assert not db.schema_ready(conn)  # a newer schema is never written by this release


# ---------------------------------------------------------------------------
# Identity, isolation and quote truth
# ---------------------------------------------------------------------------


def test_same_native_id_on_two_providers_stays_two_isolated_listings(conn, connector):
    connector.catalogs["TechMikeNY"] = catalog_of(configured_r640())
    outcome, fetcher = discover(conn, connector)
    assert outcome == "success"
    rows = conn.execute("SELECT provider, item_id, canonical_url, title FROM listings ORDER BY provider").fetchall()
    assert [(r["provider"], r["item_id"]) for r in rows] == [("PerUnitPrice", SHARED_ID), ("TechMikeNY", SHARED_ID)]
    ebay, retail = rows
    assert ebay["canonical_url"] == f"https://www.ebay.com/itm/{SHARED_ID}" and ebay["title"] == TRAP[1]
    assert retail["canonical_url"] == "https://techmikeny.com/products/dell-poweredge-r640-8sff"

    ebay_items = listed(conn, providers=("PerUnitPrice",))
    retail_only = listed(conn, providers=("TechMikeNY",))
    assert {i["provider"] for i in ebay_items} == {"PerUnitPrice"}
    assert [(i["provider"], i["platform"], i["item_price"]) for i in retail_only] == [("TechMikeNY", "TechMikeNY", "499.00")]
    assert {i["provider"] for i in listed(conn)} == {"PerUnitPrice", "TechMikeNY"}
    # Rediscovery with a changed retailer price never touches the eBay row with the same digits.
    connector.catalogs["TechMikeNY"] = catalog_of(configured_r640(price="459.00"))
    discover(conn, connector)
    assert [i["item_price"] for i in listed(conn, providers=("PerUnitPrice",))] == [i["item_price"] for i in ebay_items]
    assert [i["item_price"] for i in listed(conn, providers=("TechMikeNY",))] == ["459.00"]
    assert connector.catalog_calls.count("TechMikeNY") == 2 and fetcher.description_calls == []


def test_retailer_exact_quote_is_current_until_expiry_and_never_page_verified(conn, connector):
    from rackscout import collector, db

    connector.catalogs["TechMikeNY"] = catalog_of(configured_r640())
    discover(conn, connector)
    item, = retail_items(conn)
    assert item["price_kind"] == "exact" and item["offer"]["kind"] == "configuration"
    assert item["offer"]["specification_price_paired"] is True
    assert item["quote"]["method"] == "retailer_catalog" and item["quote"]["current"] is True
    assert item["quote"]["status"] == "success"
    assert (item["page_verification_current"], item["page_verified_at"]) == (False, None)
    assert item["detail_status"] != "verified"
    assert item["source_url"] == "https://techmikeny.com/products/dell-poweredge-r640-8sff?variant=40000000001"
    assert item["shipping_price"] is None and item["quoted_total"] is None  # unknown shipping never free
    assert item["ram_gb"] == 64 and item["cpu_model"] == "Gold 6130"
    observed = item["quote"]["observed_at"]
    revision = item["revision"]

    # Expire the stored observation; recomputation must not renew it.
    expired = _iso(datetime.now(timezone.utc) - timedelta(minutes=1))
    conn.execute("""UPDATE listings SET source_record = jsonb_set(source_record, '{retail,fresh_until}', to_jsonb(%s::text))
                    WHERE provider = 'TechMikeNY'""", (expired,))
    row = conn.execute("""SELECT id, provider, source_record, description_claims, page_claims, page_status, quote_status
                          FROM listings WHERE provider = 'TechMikeNY'""").fetchone()
    collector.recompute(conn, row, db.catalog(conn), "test")
    item, = retail_items(conn)
    assert item["quote"]["current"] is False and item["quote"]["observed_at"] == observed
    assert item["offer"]["kind"] == "historical_configuration" and item["offer"]["specification_price_paired"] is False
    assert item["revision"] == revision  # freshness alone never revises material specs
    assert item["match_state"] == "lead"


def test_configurable_retailer_variants_stay_base_and_unavailable_variants_deactivate(conn, connector):
    def configurable(observed_at, fresh_until, *, second_available=True):
        return [retail_record(
            "SaveMyServer", "8800001", "configured-r640", kind="configurable", observed_at=observed_at,
            fresh_until=fresh_until, installed_text="", title="Dell PowerEdge R640 Configure-to-Order Server",
            variants=[variant("51", "299.00", labels={"Chassis": "8SFF"}, paired=False),
                      variant("52", "349.00", labels={"Chassis": "10SFF"}, paired=False,
                              available=second_available)])]

    connector.catalogs["SaveMyServer"] = configurable
    discover(conn, connector)
    items = sorted(retail_items(conn, "SaveMyServer"), key=lambda i: i["variant_id"])
    assert [(i["variant_id"], i["price_kind"], i["item_price"]) for i in items] == [("51", "base", "299.00"),
                                                                                     ("52", "base", "349.00")]
    for i in items:
        assert i["offer"]["kind"] != "configuration" and i["offer"]["price_is_starting"] is True
        assert i["offer"]["specification_price_paired"] is False and i["ram_gb"] is None  # no capacity as installed
        assert i["match_state"] == "lead"
    connector.catalogs["SaveMyServer"] = lambda o, f: configurable(o, f, second_available=False)
    discover(conn, connector)
    assert [i["variant_id"] for i in retail_items(conn, "SaveMyServer")] == ["51"]
    history = conn.execute("""SELECT count(*) AS n FROM price_history h JOIN configurations c ON c.id = h.configuration_id
                              WHERE c.variant_key = '52'""").fetchone()["n"]
    assert history == 1  # the deactivated option keeps its history


CN_INSTALLED = "Processors: 2x Intel Xeon Gold 6130. Hard Drives: None."


def two_builds(*variant_ids, price_71="520.00", installed_text=CN_INSTALLED, available_71=True, kind="configured"):
    """CloudNinjas catalog with one product offering native builds 71 (64GB) and 72 (128GB), filtered."""
    def build(observed_at, fresh_until):
        return [retail_record(
            "CloudNinjas", "9900002", "dell-r640-two-builds", observed_at=observed_at, fresh_until=fresh_until,
            installed_text=installed_text, title="Dell PowerEdge R640 8SFF 2x Gold 6130 Server", kind=kind,
            variants=[variant(v, price, labels={"Memory": ram}, available=available)
                      for v, price, ram, available in (("71", price_71, "64GB", available_71),
                                                       ("72", "610.00", "128GB", True)) if v in variant_ids])]
    return build


def _cloudninjas_by_variant(conn):
    return {i["variant_id"]: i for i in retail_items(conn, "CloudNinjas")}


def test_incomplete_acquisition_never_deactivates_unseen_native_variants(conn, connector):
    connector.catalogs["CloudNinjas"] = two_builds("71", "72")
    discover(conn, connector)
    assert sorted(_cloudninjas_by_variant(conn)) == ["71", "72"]
    connector.catalogs["CloudNinjas"] = two_builds("71")
    connector.complete["CloudNinjas"] = False  # truncated acquisition: variant 72 merely unseen
    assert discover(conn, connector)[0] == "partial"
    assert sorted(_cloudninjas_by_variant(conn)) == ["71", "72"]
    connector.complete["CloudNinjas"] = True  # complete observation without 72: explicit removal
    discover(conn, connector)
    assert sorted(_cloudninjas_by_variant(conn)) == ["71"]


def test_unseen_variant_keeps_last_good_quote_through_failed_checks_and_stays_editable(conn, connector):
    from rackscout import collector, manual
    from rackscout.sources import SourceBlocked, SourceError

    connector.catalogs["CloudNinjas"] = two_builds("71", "72")
    discover(conn, connector)
    first = _cloudninjas_by_variant(conn)
    # Incomplete acquisition with unchanged common hardware: the seen build takes the new observation,
    # the unseen build keeps its own last observation.
    connector.catalogs["CloudNinjas"] = two_builds("71", price_71="530.00")
    connector.complete["CloudNinjas"] = False
    discover(conn, connector)
    partial = _cloudninjas_by_variant(conn)
    assert partial["72"]["quote"]["observed_at"] == first["72"]["quote"]["observed_at"]
    assert partial["72"]["quote"]["fresh_until"] == first["72"]["quote"]["fresh_until"]
    assert partial["71"]["quote"]["observed_at"] > first["71"]["quote"]["observed_at"]
    assert partial["71"]["item_price"] == "530.00"
    assert (partial["72"]["item_price"], partial["72"]["ram_gb"], partial["72"]["cpu_model"]) == ("610.00", 128, "Gold 6130")

    # Incomplete acquisition that changes the common hardware: the unseen build cannot be combined with the
    # new facts, so the whole last-good product observation is kept (nothing renewed, nothing inherited).
    connector.catalogs["CloudNinjas"] = two_builds(
        "71", price_71="540.00", installed_text="Processors: 2x Intel Xeon Gold 6230. Hard Drives: None.")
    assert discover(conn, connector)[0] == "partial"
    kept = _cloudninjas_by_variant(conn)
    for v in ("71", "72"):
        assert (kept[v]["cpu_model"], kept[v]["ram_gb"], kept[v]["item_price"], kept[v]["essential_states"],
                kept[v]["quote"]["observed_at"]) == (partial[v]["cpu_model"], partial[v]["ram_gb"],
                                                     partial[v]["item_price"], partial[v]["essential_states"],
                                                     partial[v]["quote"]["observed_at"])
    assert (kept["71"]["quote"]["status"], kept["71"]["quote"]["current"]) == ("failed", False)
    assert kept["71"]["offer"]["kind"] == "historical_configuration"
    assert (kept["72"]["quote"]["status"], kept["72"]["quote"]["current"]) == ("success", True)

    listing_id = conn.execute("SELECT id FROM listings WHERE provider = 'CloudNinjas'").fetchone()["id"]
    s = retail_settings(max_details=1)
    for failure in (SourceError("cloudninjas.com returned HTTP 500"),
                    SourceBlocked("cloudninjas.com returned HTTP 429", 300)):
        conn.execute("UPDATE listings SET next_check_after = NULL WHERE id = %s", (listing_id,))
        connector.products[("CloudNinjas", "9900002")] = lambda o, f, failure=failure: failure
        collector.run_enrichment(conn, s, EbayOnly(snapshot(datetime.now(timezone.utc), [])), 1, False,
                                 target_listing_id=listing_id)
        after = _cloudninjas_by_variant(conn)
        assert sorted(after) == ["71", "72"]  # a failed product check never removes the unseen build
        assert after["72"]["quote"]["observed_at"] == first["72"]["quote"]["observed_at"]
        assert after["72"]["quote"]["current"] is False and after["72"]["item_price"] == "610.00"
        assert (after["72"]["cpu_model"], after["72"]["ram_gb"]) == ("Gold 6130", 128)
        conn.execute("DELETE FROM origin_backoff")

    # Manual baseline reconstruction still finds the unseen build; editing never renews its quote.
    config_72 = after["72"]["id"]
    editor = manual.editor(conn, config_72)
    assert (editor["original"]["values"]["ram_gb"], editor["original"]["values"]["cpu_model"]) == (128, "Gold 6130")
    child = manual.create(conn, config_72, manual.parse_write({
        "revision": editor["revision"], "label": "72 with notes", "notes": "", "fields": {},
        "price_confirmed": False}))
    assert conn.execute("SELECT source_quote FROM configurations WHERE id = %s", (child,)).fetchone()["source_quote"] is None
    quote = conn.execute("SELECT source_quote FROM configurations WHERE id = %s", (config_72,)).fetchone()["source_quote"]
    assert quote["observed_at"] == first["72"]["quote"]["observed_at"]


def test_unseen_variant_stays_noncurrent_after_failed_check_until_reobserved(conn, connector):
    from rackscout import collector
    from rackscout.sources import SourceError

    connector.catalogs["CloudNinjas"] = two_builds("71", "72")
    discover(conn, connector)
    first = _cloudninjas_by_variant(conn)
    listing_id = conn.execute("SELECT id FROM listings WHERE provider = 'CloudNinjas'").fetchone()["id"]
    connector.products[("CloudNinjas", "9900002")] = lambda o, f: SourceError("cloudninjas.com returned HTTP 500")
    collector.run_enrichment(conn, retail_settings(max_details=1), EbayOnly(snapshot(datetime.now(timezone.utc), [])),
                             1, False, target_listing_id=listing_id)
    assert {v: i["quote"]["status"] for v, i in _cloudninjas_by_variant(conn).items()} == {"71": "failed", "72": "failed"}

    # An incomplete catalog re-observes 71 only: 71 is current again, 72 keeps its failed, non-current quote.
    connector.catalogs["CloudNinjas"] = two_builds("71")
    connector.complete["CloudNinjas"] = False
    discover(conn, connector)
    after = _cloudninjas_by_variant(conn)
    assert (after["71"]["quote"]["status"], after["71"]["quote"]["current"]) == ("success", True)
    assert (after["72"]["quote"]["status"], after["72"]["quote"]["current"]) == ("failed", False)
    assert after["72"]["quote"]["observed_at"] == first["72"]["quote"]["observed_at"]
    assert after["72"]["offer"]["kind"] == "historical_configuration"
    # A second incomplete observation still does not resurrect it.
    discover(conn, connector)
    assert _cloudninjas_by_variant(conn)["72"]["quote"]["current"] is False


def test_explicit_unavailability_applies_even_when_incomplete_snapshot_changes_common_facts(conn, connector):
    changed = "Processors: 2x Intel Xeon Gold 6230. Hard Drives: None."
    connector.catalogs["CloudNinjas"] = two_builds("71", "72")
    discover(conn, connector)
    first = _cloudninjas_by_variant(conn)
    connector.complete["CloudNinjas"] = False
    connector.catalogs["CloudNinjas"] = two_builds("71", available_71=False, installed_text=changed)
    assert discover(conn, connector)[0] == "partial"
    kept = _cloudninjas_by_variant(conn)
    assert sorted(kept) == ["72"]  # 71 explicitly unavailable; 72 unseen
    assert (kept["72"]["cpu_model"], kept["72"]["ram_gb"], kept["72"]["item_price"]) == ("Gold 6130", 128, "610.00")
    assert kept["72"]["quote"]["observed_at"] == first["72"]["quote"]["observed_at"]

    # A quote-only signal makes the whole product non-purchasable, even from an incomplete snapshot.
    connector.catalogs["CloudNinjas"] = two_builds("71", kind="quote_only", installed_text=changed)
    discover(conn, connector)
    assert _cloudninjas_by_variant(conn) == {}


def test_non_ascii_native_id_is_refused_without_rolling_back_other_providers(conn, connector):
    def techmikeny(observed_at, fresh_until):
        bad = configured_r640(item_id="\u0661\u0662\u0663", handle="arabic-indic-id")(observed_at, fresh_until)
        good = configured_r640(item_id="5550001", handle="valid-r640")(observed_at, fresh_until)
        return [bad, good]

    connector.catalogs["TechMikeNY"] = techmikeny
    connector.catalogs["CloudNinjas"] = catalog_of(configured_r640("CloudNinjas", "9900003", "dell-r640-cn3"))
    outcome, _ = discover(conn, connector)
    assert outcome == "partial"  # the refused record makes TechMikeNY partial; nothing is rolled back
    stored = {(r["provider"], r["item_id"]) for r in conn.execute("SELECT provider, item_id FROM listings")}
    assert stored == {("PerUnitPrice", SHARED_ID), ("TechMikeNY", "5550001"), ("CloudNinjas", "9900003")}
    page = conn.execute("SELECT last_outcome FROM source_pages WHERE provider = 'TechMikeNY'").fetchone()
    assert page["last_outcome"] == "partial"


def test_status_counts_every_oem_of_one_retailer(conn, connector):
    from rackscout import queries

    def mixed(observed_at, fresh_until):
        dell = configured_r640("CloudNinjas", "9900004", "dell-r640-mixed")(observed_at, fresh_until)
        hpe = retail_record("CloudNinjas", "9900005", "hpe-dl360-gen10", observed_at=observed_at,
                            fresh_until=fresh_until, vendor="HPE", model="HPE DL360 Gen10",
                            title="HPE ProLiant DL360 Gen10 8SFF 2x Gold 6130 64GB No HDD",
                            variants=[variant("81", "650.00")])
        return [dell, hpe]

    connector.catalogs["CloudNinjas"] = mixed
    discover(conn, connector)
    status = queries.status(conn)
    cloud = next(s for s in status["sources"] if s["provider"] == "CloudNinjas")
    assert cloud["inventory_count"] == 2
    assert status["inventory_count"] == 3  # plus the eBay listing


def test_native_variant_title_is_kept_when_labels_name_no_hardware_dimension(conn, connector):
    def builds(observed_at, fresh_until):
        record = retail_record(
            "TechMikeNY", "5550002", "dell-r640-memory-choice", kind="configurable", observed_at=observed_at,
            fresh_until=fresh_until, installed_text="", title="Dell PowerEdge R640 8SFF Server",
            variants=[variant("91", "400.00", paired=False), variant("92", "450.00", paired=False)])
        for v, label in zip(record["retail"]["variants"], ("8GB", "16GB")):
            v["label"] = label  # native option title without a named dimension
        return [record]

    connector.catalogs["TechMikeNY"] = builds
    discover(conn, connector)
    items = {i["variant_id"]: i for i in retail_items(conn)}
    assert {v: i["variant_label"] for v, i in items.items()} == {"91": "8GB", "92": "16GB"}
    for i in items.values():
        assert i["price_kind"] == "base" and i["ram_gb"] is None  # an unnamed option is not installed RAM


def test_single_configurable_option_without_choice_evidence_is_unverified_base(conn, connector):
    def single(observed_at, fresh_until):
        return [retail_record(
            "SaveMyServer", "8800002", "dell-r640-base", kind="configurable", observed_at=observed_at,
            fresh_until=fresh_until, installed_text="", title="Dell PowerEdge R640 8SFF Server",
            variants=[variant("61", "299.00", paired=False)])]

    connector.catalogs["SaveMyServer"] = single
    discover(conn, connector)
    item, = retail_items(conn, "SaveMyServer")
    assert item["offer"]["kind"] == "unverified" and item["offer"]["price_is_starting"] is True
    assert item["offer"]["evidence"] == []


def test_several_native_configurable_options_are_multiple_options(conn, connector):
    def several(observed_at, fresh_until):
        return [retail_record(
            "SaveMyServer", "8800003", "dell-r640-chassis", kind="configurable", observed_at=observed_at,
            fresh_until=fresh_until, installed_text="", title="Dell PowerEdge R640 Server",
            variants=[variant("63", "299.00", labels={"Chassis": "8SFF"}, paired=False),
                      variant("64", "349.00", labels={"Chassis": "10SFF"}, paired=False)])]

    connector.catalogs["SaveMyServer"] = several
    discover(conn, connector)
    for item in retail_items(conn, "SaveMyServer"):
        assert item["offer"]["kind"] == "multiple_options" and item["offer"]["evidence"]


def test_incomplete_or_failed_catalog_never_archives_and_archiving_is_provider_local(conn, connector):
    from rackscout import collector

    connector.catalogs["TechMikeNY"] = catalog_of(configured_r640())
    connector.catalogs["CloudNinjas"] = catalog_of(configured_r640("CloudNinjas", "9900001", "dell-r640-cn"))
    discover(conn, connector)
    old = datetime.now(timezone.utc) - timedelta(days=9)
    conn.execute("UPDATE listings SET last_seen_source_at = %s", (old,))
    connector.catalogs["TechMikeNY"] = lambda o, f: []
    connector.complete["TechMikeNY"] = False  # truncated pagination
    from rackscout.sources import SourceError

    connector.catalogs["CloudNinjas"] = lambda o, f: SourceError("schema changed")
    outcome, _ = discover(conn, connector, ebay_items=(TRAP, UNKNOWN_SHIP))
    assert outcome == "partial"
    archived = {r["provider"] for r in conn.execute("SELECT provider FROM listings WHERE archived_at IS NOT NULL")}
    assert archived == set()
    connector.complete["TechMikeNY"] = True
    outcome, _ = discover(conn, connector, ebay_items=(TRAP, UNKNOWN_SHIP))
    archived = {(r["provider"], r["item_id"]) for r in conn.execute(
        "SELECT provider, item_id FROM listings WHERE archived_at IS NOT NULL")}
    assert archived == {("TechMikeNY", SHARED_ID)}  # failing CloudNinjas and the eBay row with same digits kept
    assert collector.ARCHIVE_AFTER == timedelta(days=7)


def test_blocked_retailer_backs_off_its_origin_only_and_status_is_provider_local(conn, connector):
    from rackscout import queries
    from rackscout.sources import SourceBlocked

    connector.catalogs["TechMikeNY"] = catalog_of(configured_r640())
    connector.catalogs["CloudNinjas"] = lambda o, f: SourceBlocked("cloudninjas.com returned HTTP 429", 120)
    outcome, _ = discover(conn, connector)
    assert outcome == "partial"
    status = queries.status(conn)
    backoff = {b["origin"] for b in status["backoff"]}
    assert "cloudninjas.com" in backoff and "techmikeny.com" not in backoff and "perunitprice.com" not in backoff
    sources = {(s["provider"], s["vendor"]): s for s in status["sources"]}
    assert sources[("CloudNinjas", None)]["state"] == "blocked"
    assert sources[("TechMikeNY", None)]["state"] == "success" and sources[("TechMikeNY", None)]["inventory_count"] == 1
    assert sources[("PerUnitPrice", "Dell")]["state"] == "success"
    assert sources[("TechMikeNY", None)]["platform"] == "TechMikeNY"
    assert sources[("PerUnitPrice", "Dell")]["platform"] == "eBay (PerUnitPrice)"
    # Next run honors the backoff without requesting the origin again.
    connector.catalog_calls.clear()
    discover(conn, connector)
    assert "CloudNinjas" not in connector.catalog_calls and "TechMikeNY" in connector.catalog_calls


# ---------------------------------------------------------------------------
# Selected refresh isolation
# ---------------------------------------------------------------------------


def _config_id(conn, provider):
    return conn.execute("""SELECT c.id FROM configurations c JOIN listings l ON l.id = c.listing_id
                           WHERE l.provider = %s AND c.active ORDER BY c.id LIMIT 1""", (provider,)).fetchone()["id"]


def test_selected_retailer_refresh_uses_only_its_product_endpoint_and_failures_keep_last_quote(conn, connector):
    from rackscout import collector, refresh
    from rackscout.sources import SourceBlocked, SourceError
    from test_manual_refresh import refresh_settings

    connector.catalogs["TechMikeNY"] = catalog_of(configured_r640())
    discover(conn, connector)
    before, = retail_items(conn)
    listing_id = conn.execute("SELECT id FROM listings WHERE provider = 'TechMikeNY'").fetchone()["id"]

    class NoEbay:
        def fetch_index(self, url, max_bytes):
            raise AssertionError("discovery during selected refresh")

        def fetch_description(self, item_id, max_bytes):
            raise AssertionError("eBay description requested for a retailer listing")

    catalog_calls = list(connector.catalog_calls)
    connector.products[("TechMikeNY", SHARED_ID)] = lambda o, f: SourceError("techmikeny.com returned HTTP 500")
    s = retail_settings(max_details=1, refresh_origins=refresh_settings().refresh_origins)
    result = collector.run_enrichment(conn, s, NoEbay(), 1, False, target_listing_id=listing_id)
    assert result in ("partial", "failed")
    assert connector.product_calls == [("TechMikeNY", SHARED_ID)] and connector.catalog_calls == catalog_calls
    after, = retail_items(conn)
    assert (after["item_price"], after["ram_gb"], after["quote"]["observed_at"]) == (
        before["item_price"], before["ram_gb"], before["quote"]["observed_at"])
    assert after["quote"]["status"] == "failed" and after["quote"]["current"] is False
    assert after["offer"]["kind"] == "historical_configuration"

    conn.execute("UPDATE listings SET next_check_after = NULL WHERE id = %s", (listing_id,))
    connector.products[("TechMikeNY", SHARED_ID)] = lambda o, f: SourceBlocked("techmikeny.com returned HTTP 429", 300)
    collector.run_enrichment(conn, s, NoEbay(), 1, False, target_listing_id=listing_id)
    assert collector.backoff_until(conn, "techmikeny.com") is not None
    blocked, = retail_items(conn)
    assert blocked["quote"]["status"] == "blocked" and blocked["quote"]["observed_at"] == before["quote"]["observed_at"]
    conn.execute("UPDATE listings SET next_check_after = NULL WHERE id = %s", (listing_id,))
    collector.run_enrichment(conn, s, NoEbay(), 1, False, target_listing_id=listing_id)
    assert len(connector.product_calls) == 2  # backoff honored: no third request

    # A successful product observation renews the quote with its own time.
    conn.execute("DELETE FROM origin_backoff")
    conn.execute("UPDATE listings SET next_check_after = NULL WHERE id = %s", (listing_id,))
    connector.products[("TechMikeNY", SHARED_ID)] = lambda o, f: {
        **configured_r640(price="479.00")(o, f), "retail": {
            **configured_r640(price="479.00")(o, f)["retail"], "method": "retailer_product"}}
    assert collector.run_enrichment(conn, s, NoEbay(), 1, False, target_listing_id=listing_id) == "success"
    fresh, = retail_items(conn)
    assert fresh["quote"]["method"] == "retailer_product" and fresh["quote"]["current"] is True
    assert fresh["item_price"] == "479.00" and fresh["quote"]["observed_at"] > before["quote"]["observed_at"]
    ebay = conn.execute("SELECT last_checked_at FROM listings WHERE provider = 'PerUnitPrice'").fetchone()
    assert ebay["last_checked_at"] is None  # the eBay listing with the same digits was never touched

    state = refresh.status(conn, refresh_settings())
    assert "target_provider" in state


def test_selected_refresh_request_reports_provider_qualified_target(conn, connector):
    from rackscout import refresh
    from test_manual_refresh import refresh_settings

    connector.catalogs["TechMikeNY"] = catalog_of(configured_r640())
    discover(conn, connector)
    c = refresh.connect_worker(settings())
    try:
        outcome, state = refresh.enqueue(c, refresh_settings(), configuration_id=_config_id(conn, "TechMikeNY"))
    finally:
        c.close()
    assert outcome == "accepted"
    assert (state["scope"], state["target_item_id"], state["target_provider"]) == ("listing", SHARED_ID, "TechMikeNY")


# ---------------------------------------------------------------------------
# Manual configurations on retailer rows
# ---------------------------------------------------------------------------


def test_manual_child_of_retailer_row_has_no_quote_and_editing_never_renews_source_quote(conn, connector):
    from rackscout import manual

    connector.catalogs["TechMikeNY"] = catalog_of(configured_r640())
    discover(conn, connector)
    source_id = _config_id(conn, "TechMikeNY")
    source_quote = conn.execute("SELECT source_quote FROM configurations WHERE id = %s", (source_id,)).fetchone()
    editor = manual.editor(conn, source_id)
    assert all("ebaydesc" not in (e.get("source_url") or "") for e in editor["original"]["evidence"])
    child = manual.create(conn, source_id, manual.parse_write({
        "revision": editor["revision"], "label": "128GB", "notes": "", "fields": {"ram_gb": 128},
        "price_confirmed": False}))
    row = conn.execute("SELECT source_quote, origin FROM configurations WHERE id = %s", (child,)).fetchone()
    assert (row["origin"], row["source_quote"]) == ("manual", None)
    assert conn.execute("SELECT source_quote FROM configurations WHERE id = %s", (source_id,)).fetchone() == source_quote
    items = {i["id"]: i for i in listed(conn, providers=("TechMikeNY",))}
    assert set(items) == {child}  # source row replaced by its manual child within the provider filter
    assert items[child]["quote"] is None and items[child]["provider"] == "TechMikeNY"
    assert listed(conn, providers=("CloudNinjas",)) == []


# ---------------------------------------------------------------------------
# API: provider filter and facets
# ---------------------------------------------------------------------------


def test_provider_filter_is_repeated_validated_and_facets_list_providers(conn, connector, monkeypatch):
    from fastapi.testclient import TestClient

    from rackscout import api

    connector.catalogs["TechMikeNY"] = catalog_of(configured_r640())
    discover(conn, connector)
    conn.commit()
    monkeypatch.setattr(api, "_settings", retail_settings())
    client = TestClient(api.app)
    base = "/api/listings?min_ram=&min_bays=&diskless=false"
    both = client.get(base + "&provider=TechMikeNY&provider=PerUnitPrice&provider=TechMikeNY").json()
    assert {i["provider"] for i in both["items"]} == {"TechMikeNY", "PerUnitPrice"}
    only = client.get(base + "&provider=TechMikeNY").json()
    assert only["total"] == 1 and only["items"][0]["provider"] == "TechMikeNY"
    for bad in ("ServerMonkey", "techmikeny", "BargainHardware"):
        assert client.get(base + f"&provider={bad}").status_code == 422
    facets = client.get("/api/facets").json()
    assert facets["providers"] == [{"id": "PerUnitPrice", "name": "eBay (PerUnitPrice)"},
                                   {"id": "TechMikeNY", "name": "TechMikeNY"},
                                   {"id": "SaveMyServer", "name": "SaveMyServer"},
                                   {"id": "CloudNinjas", "name": "Cloud Ninjas"}]


def test_disabled_retailers_make_no_retailer_requests(conn, connector):
    from rackscout import collector

    fetcher = EbayOnly(snapshot(datetime.now(timezone.utc), [TRAP]))
    assert collector.run_discovery(conn, settings(), fetcher) == "success"
    assert connector.catalog_calls == []
