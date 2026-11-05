"""Consumer-visible behaviour of the US retailer connectors; no network required.

Payload shapes mirror the publishers' public JSON (TechMikeNY agent API,
Shopify ``products.json`` / ``<handle>.json``). Transport is always an injected
fetcher, so every test also proves requests stay on the allowlist.
"""
import copy
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
import pytest

from rackscout import retail_sources as rs
from rackscout.sources import HttpFetcher, SourceBlocked, SourceError

OBS = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
FRESH = OBS + timedelta(hours=26)
MAX_BYTES = 5_000_000

TM_CATALOG = "https://techmikeny.com/api/agent/collections/servers"
SMS_CATALOG = "https://savemyserver.com/collections/pre-configured-servers/products.json"
CN_CATALOG = "https://cloudninjas.com/collections/cloud-ninjas-servers/products.json"


class FakeFetcher:
    """Mimics HttpFetcher.get_text: enforces the caller's allowlist, replays canned responses."""

    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []

    def get_text(self, url, allowed, max_bytes):
        if not allowed(url):
            raise SourceError("refusing non-allowlisted URL")
        self.calls.append(url)
        outcome = self.responses[url]
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            outcome = outcome()
        status, body = outcome
        return status, body if isinstance(body, str) else json.dumps(body)


def ok(body):
    return 200, body


# --------------------------------------------------------------------------- fixtures

TM_DESC = ("Model: Dell PowerEdge R640 10-Bay 2.5'' SAS / SATA 1U Rack Server Chassis "
           "Processors: 2x 2.10Ghz Intel Xeon Silver 4110 8-Core 85W Skylake Processors - 16x Cores Total "
           "Memory: 2x 16GB PC4-21300 (2666Mhz) RAM Sticks - 32GB Total "
           "Hard Drives: No Drives / Storage and No Caddies / Sleds Included "
           "Power Supplies: 2x Dell 750W Platinum AC 100V-240V Power Supplies "
           "RAID Controller: H730 Integrated 1GB Cache RAID Controller "
           "Network Interface: Networking Ports: Quad-Port 1GB RJ-45 Broadcom 5720 "
           "Condition: Refurbished")


def tm_variant(vid, price="354.95", currency="USD", available=True, title=None, sku="TM-630-SAP-322"):
    out = {"id": f"gid://shopify/ProductVariant/{vid}", "title": title, "sku": sku, "available": available,
           "quantityAvailable": "1-25"}
    if price is not None:
        out["price"] = {"amount": price, "currency": currency}
    return out


def tm_product(pid=8869466570812, handle="dell-poweredge-r640-10-bay", title="Dell PowerEdge R640 10-Bay 2x Silver 4110 32GB",
               description=TM_DESC, ptype="Server", variants=None, available=True):
    variants = variants if variants is not None else [tm_variant(44131282092092)]
    return {"id": f"gid://shopify/Product/{pid}", "handle": handle, "title": title, "description": description,
            "productType": ptype, "url": f"https://techmikeny.com/products/{handle}", "available": available,
            "totalInventory": "1-25", "price": {"amount": "354.95", "currency": "USD"},
            "warranty": {"included": True, "years": 1, "label": "Complimentary 1 Year Warranty"},
            "variants": variants}


def tm_page(products, next_cursor=None):
    return {"products": products, "pageInfo": {"hasNextPage": next_cursor is not None, "endCursor": next_cursor},
            "count": len(products)}


def tm_url(cursor=None):
    return f"{TM_CATALOG}?first=100" + (f"&after={quote(cursor, safe='')}" if cursor else "")


def sh_variant(vid, price="499.00", available=True, title="Default Title", options=None, updated="2026-11-04T09:00:00-05:00"):
    out = {"id": vid, "title": title, "price": price, "available": available, "sku": f"SKU{vid}", "updated_at": updated}
    for i, value in enumerate(options or [title], 1):
        out[f"option{i}"] = value
    return out


def sh_product(pid, handle, title, body="", variants=None, vendor="Store", ptype="Servers", tags=None):
    return {"id": pid, "handle": handle, "title": title, "body_html": body, "vendor": vendor, "product_type": ptype,
            "tags": tags or [], "updated_at": "2026-11-04T10:00:00-05:00",
            "options": [{"name": "Title", "values": ["Default Title"]}],
            "variants": variants if variants is not None else [sh_variant(pid * 10 + 1)]}


def sh_json_product(product, currency="USD"):
    product = copy.deepcopy(product)
    for variant in product["variants"]:
        if currency is not None:
            variant["price_currency"] = currency
    return {"product": product}


def sh_catalog_url(base, page):
    return f"{base}?limit=100&page={page}"


def sh_probe_url(origin, handle):
    return f"https://{origin}/products/{handle}.json"


def catalog(provider, fetcher):
    return rs.fetch_catalog(provider, fetcher, observed_at=OBS, fresh_until=FRESH, max_bytes=MAX_BYTES)


def tm_catalog(pages):
    """pages: list of (products, next_cursor) walked with deterministic cursors."""
    responses, cursor = {}, None
    for i, (products, nxt) in enumerate(pages):
        responses[tm_url(cursor)] = ok(tm_page(products, nxt))
        cursor = nxt
    return FakeFetcher(responses)


def shopify_one(provider, base, origin, products, currency="USD"):
    fetcher = FakeFetcher({sh_catalog_url(base, 1): ok({"products": products}),
                           sh_probe_url(origin, products[0]["handle"]): ok(sh_json_product(products[0], currency))})
    return fetcher, catalog(provider, fetcher)


def only(snapshot):
    assert len(snapshot.records) == 1
    return snapshot.records[0]


# --------------------------------------------------------------------------- registry & URL grammar


@pytest.mark.parametrize("provider,url", [
    ("TechMikeNY", TM_CATALOG),
    ("TechMikeNY", TM_CATALOG + "?first=100"),
    ("TechMikeNY", TM_CATALOG + "?first=100&after=eyJsYXN0X2lkIjo4ODY5NDY2NTcwODEyfQ%3D%3D"),
    ("TechMikeNY", "https://techmikeny.com/api/agent/products/dell-poweredge-r640"),
    ("TechMikeNY", "https://techmikeny.com/products/dell-poweredge-r640"),
    ("SaveMyServer", SMS_CATALOG + "?limit=100&page=3"),
    ("SaveMyServer", "https://savemyserver.com/products/some-server-1.json"),
    ("CloudNinjas", CN_CATALOG + "?limit=100&page=1"),
    ("CloudNinjas", "https://cloudninjas.com/products/dell-poweredge-r640-server.json"),
    ("CloudNinjas", "https://cloudninjas.com/products/dell-poweredge-r640-server"),
])
def test_allowed_url_accepts_fixed_grammar(provider, url):
    assert rs.allowed_url(provider, url) is True


@pytest.mark.parametrize("provider,url", [
    ("TechMikeNY", TM_CATALOG.replace("https", "http")),
    ("TechMikeNY", "https://techmikeny.com:8443/api/agent/collections/servers"),
    ("TechMikeNY", "https://techmikeny.com:443/api/agent/collections/servers"),
    ("TechMikeNY", "https://user:pw@techmikeny.com/api/agent/collections/servers"),
    ("TechMikeNY", "https://evil.example/api/agent/collections/servers"),
    ("TechMikeNY", "https://techmikeny.com.evil.example/api/agent/collections/servers"),
    ("TechMikeNY", TM_CATALOG + "#frag"),
    ("TechMikeNY", TM_CATALOG + "?first=1000"),
    ("TechMikeNY", TM_CATALOG + "?first=abc"),
    ("TechMikeNY", TM_CATALOG + "?first=10&first=20"),
    ("TechMikeNY", TM_CATALOG + "?first=10&sort=PRICE"),
    ("TechMikeNY", TM_CATALOG + "?after=a%0Ab"),
    ("TechMikeNY", "https://techmikeny.com/api/agent/collections/other"),
    ("TechMikeNY", "https://techmikeny.com/api/agent/products/../cto/build"),
    ("TechMikeNY", "https://techmikeny.com/api/agent/products/UPPER_case"),
    ("TechMikeNY", "https://techmikeny.com/api/agent/products/ok?x=1"),
    ("TechMikeNY", "https://techmikeny.com/cart/add"),
    ("SaveMyServer", "https://savemyserver.com/collections/other/products.json?limit=100&page=1"),
    ("SaveMyServer", SMS_CATALOG + "?limit=100&page=0"),
    ("SaveMyServer", SMS_CATALOG + "?limit=100&page=26"),
    ("SaveMyServer", SMS_CATALOG + "?limit=100&page=1&sort_by=price"),
    ("SaveMyServer", "https://savemyserver.com/cart.js"),
    ("SaveMyServer", "https://savemyserver.com/products/a/../b.json"),
    ("SaveMyServer", "https://savemyserver.com/products/x.json?variant=1"),
    ("SaveMyServer", "https://cloudninjas.com/products/x.json"),
    ("CloudNinjas", SMS_CATALOG + "?limit=100&page=1"),
    ("CloudNinjas", "https://cloudninjas.com/products/x.json%0d%0a"),
    ("CloudNinjas", "https://cloudninjas.com/products/%2e%2e/admin.json"),
    ("Nope", TM_CATALOG),
    ("PerUnitPrice", "https://perunitprice.com/servers/dell/"),
])
def test_allowed_url_rejects_everything_else(provider, url):
    assert rs.allowed_url(provider, url) is False


# --------------------------------------------------------------------------- TechMikeNY normalisation


def test_techmikeny_configured_server_pairs_native_variant_price_with_labeled_facts():
    record = only(catalog("TechMikeNY", tm_catalog([([tm_product()], None)])))
    assert record["provider"] == "TechMikeNY"
    assert record["item_id"] == "8869466570812"
    assert record["source_url"] == "https://techmikeny.com/products/dell-poweredge-r640-10-bay"
    assert record["original_url"] == record["source_url"]
    assert record["currency"] == "USD"
    assert record["item_price"] == "354.95"
    assert record["shipping_price"] is None
    assert record["vendor"] == "Dell" and record["model"] == "R640"
    assert record["condition"] == "Refurbished"
    assert record["cpu_raw"] == "2× Silver 4110"
    assert record["source_ram_gb"] == 32
    assert record["total_cores"] == 16
    assert record["no_ram_flag"] is False
    retail = record["retail"]
    assert retail["kind"] == "configured" and retail["available"] is True
    assert retail["handle"] == "dell-poweredge-r640-10-bay" and retail["product_type"] == "Server"
    assert retail["variants"] == [{"variant_id": "44131282092092", "label": None, "labels": {},
                                   "price": "354.95", "available": True, "paired": True}]
    assert "Processors: 2x 2.10Ghz Intel Xeon Silver 4110" in retail["installed_text"]
    assert "Memory:" in retail["installed_text"] and "Hard Drives: No Drives" in retail["installed_text"]
    assert retail["warranty"]["years"] == 1
    assert retail["method"] == "retailer_catalog"
    assert retail["observed_at"] == "2026-11-04T12:00:00+00:00"
    assert retail["fresh_until"] == "2026-11-05T14:00:00+00:00"
    assert "USD" in retail["currency_evidence"]


def test_timestamps_come_from_arguments_never_the_clock():
    pages = [([tm_product()], None)]
    a = only(catalog("TechMikeNY", tm_catalog(pages)))["retail"]
    b = only(catalog("TechMikeNY", tm_catalog(pages)))["retail"]
    assert (a["observed_at"], a["fresh_until"]) == (b["observed_at"], b["fresh_until"])
    local = timezone(timedelta(hours=-5))
    snap = rs.fetch_catalog("TechMikeNY", tm_catalog(pages), observed_at=OBS.astimezone(local),
                            fresh_until=FRESH.astimezone(local), max_bytes=MAX_BYTES)
    assert snap.records[0]["retail"]["observed_at"] == "2026-11-04T12:00:00+00:00"
    assert snap.observed_at == OBS


def test_chassis_is_configurable_base_never_installed_hardware():
    chassis = tm_product(pid=1, handle="dell-poweredge-r640-chassis", title="Dell PowerEdge R640 10-Bay Server Chassis",
                         description="Model: Dell PowerEdge R640 10-Bay Chassis Processors: Up to 2x Intel Xeon Scalable Memory: Up to 3TB "
                                     "(24 DIMM slots) Power Supplies: 2x Dell 750W",
                         ptype="Server Chassis", variants=[tm_variant(7, price="199.95", sku="DELL_PE_R640_10B")])
    record = only(catalog("TechMikeNY", tm_catalog([([chassis], None)])))
    retail = record["retail"]
    assert retail["kind"] == "configurable"
    assert retail["variants"][0]["price"] == "199.95" and retail["variants"][0]["paired"] is False
    assert record["item_price"] == "199.95"
    assert record["cpu_raw"] is None and record["source_ram_gb"] is None and record["total_cores"] is None
    assert "Up to" not in retail["installed_text"] and "3TB" not in retail["installed_text"]
    assert "Up to 3TB" in retail["description_text"]  # kept as generic description only


def test_explicit_no_ram_is_recorded_but_unknown_memory_is_not_guessed():
    none_ram = tm_product(description=TM_DESC.replace("Memory: 2x 16GB PC4-21300 (2666Mhz) RAM Sticks - 32GB Total",
                                                      "Memory: No RAM Included"))
    unknown = tm_product(pid=2, handle="unknown-memory", description=TM_DESC.replace(
        "Memory: 2x 16GB PC4-21300 (2666Mhz) RAM Sticks - 32GB Total", "Memory: DDR4 ECC RDIMMs"))
    snap = catalog("TechMikeNY", tm_catalog([([none_ram, unknown], None)]))
    a, b = snap.records
    assert a["source_ram_gb"] == 0 and a["no_ram_flag"] is True
    assert b["source_ram_gb"] is None and b["no_ram_flag"] is False


def test_unavailable_variants_are_kept_for_availability_transitions():
    product = tm_product(available=False, variants=[tm_variant(1, available=False)])
    record = only(catalog("TechMikeNY", tm_catalog([([product], None)])))
    assert record["retail"]["available"] is False
    assert record["retail"]["variants"][0]["available"] is False
    assert record["item_price"] == "354.95"
    mixed = tm_product(pid=3, handle="two", variants=[tm_variant(10, price="500.00", available=False, title="A"),
                                                       tm_variant(11, price="450.00", available=True, title="B")])
    record = only(catalog("TechMikeNY", tm_catalog([([mixed], None)])))
    assert record["retail"]["available"] is True
    assert record["item_price"] == "450.00"
    assert [v["variant_id"] for v in record["retail"]["variants"]] == ["10", "11"]
    assert [v["label"] for v in record["retail"]["variants"]] == ["A", "B"]


def test_techmikeny_variants_without_option_names_are_not_paired_with_product_level_facts():
    product = tm_product(variants=[tm_variant(1, price="300.00", title="8GB"), tm_variant(2, price="350.00", title="16GB")])
    record = only(catalog("TechMikeNY", tm_catalog([([product], None)])))
    assert record["retail"]["kind"] == "configurable"
    assert all(v["paired"] is False for v in record["retail"]["variants"])
    assert record["item_price"] == "300.00"
    assert record["cpu_raw"] is None and record["source_ram_gb"] is None


def test_named_techmikeny_memory_variants_keep_their_own_prices_and_ram():
    product = tm_product(variants=[
        tm_variant(1, price="450.00", title="64GB RAM"),
        tm_variant(2, price="350.00", title="32GB RAM"),
    ])
    record = only(catalog("TechMikeNY", tm_catalog([([product], None)])))
    variants = record["retail"]["variants"]
    assert record["retail"]["kind"] == "configured"
    assert [(v["variant_id"], v["price"], rs.variant_facts(record, v)["source_ram_gb"], v["paired"])
            for v in variants] == [
        ("1", "450.00", 64, True), ("2", "350.00", 32, True),
    ]
    assert record["source_ram_gb"] is None
    assert "Silver 4110" in record["cpu_raw"]


def test_priceless_available_variant_is_quote_only_not_purchasable():
    product = tm_product(variants=[tm_variant(1, price=None)])
    record = only(catalog("TechMikeNY", tm_catalog([([product], None)])))
    assert record["retail"]["kind"] == "quote_only"
    assert record["retail"]["available"] is False
    assert record["item_price"] is None
    assert record["retail"]["variants"][0]["paired"] is False


# --------------------------------------------------------------------------- currency


def test_non_usd_and_unspecified_currency_rows_are_rejected_not_tagged_usd():
    good = tm_product(pid=1, handle="good")
    cad = tm_product(pid=2, handle="cad", variants=[tm_variant(2, currency="CAD")])
    missing = tm_product(pid=3, handle="nocur", variants=[tm_variant(3, currency=None)])
    lower = tm_product(pid=4, handle="lower", variants=[tm_variant(4, currency="usd ")])
    snap = catalog("TechMikeNY", tm_catalog([([good, cad, missing, lower], None)]))
    assert [r["item_id"] for r in snap.records] == ["1"]
    assert snap.rejected == 3 and snap.complete is False


@pytest.mark.parametrize("provider,base,origin", [("SaveMyServer", SMS_CATALOG, "savemyserver.com"),
                                                  ("CloudNinjas", CN_CATALOG, "cloudninjas.com")])
def test_shopify_currency_confirmed_from_product_json_before_records_are_used(provider, base, origin):
    product = sh_product(11, "dell-r640-2x-gold-6148-768gb", "Dell PowerEdge R640 2x Gold 6148 768GB RAM")
    fetcher, snap = shopify_one(provider, base, origin, [product])
    record = only(snap)
    assert record["currency"] == "USD"
    assert "price_currency" in record["retail"]["currency_evidence"]
    assert fetcher.calls == [sh_catalog_url(base, 1), sh_probe_url(origin, product["handle"])]


@pytest.mark.parametrize("currency", ["CAD", None, ""])
def test_shopify_store_without_explicit_usd_fails_closed(currency):
    product = sh_product(11, "a-server", "Dell PowerEdge R640")
    with pytest.raises(SourceError):
        shopify_one("SaveMyServer", SMS_CATALOG, "savemyserver.com", [product], currency=currency)


def test_shopify_currency_probe_block_propagates():
    product = sh_product(11, "a-server", "Dell PowerEdge R640")
    fetcher = FakeFetcher({sh_catalog_url(SMS_CATALOG, 1): ok({"products": [product]}),
                           sh_probe_url("savemyserver.com", "a-server"): SourceBlocked("429", 120.0)})
    with pytest.raises(SourceBlocked) as info:
        catalog("SaveMyServer", fetcher)
    assert info.value.retry_after == 120.0


# --------------------------------------------------------------------------- Shopify spec extraction


def test_savemyserver_title_facts_pair_one_native_variant():
    product = sh_product(1224, "dell-poweredge-r740-server-2x-gold-6148-2-4ghz-40-cores-768gb-h730p-16x-trays",
                         "Dell PowerEdge R740 Server 2x Gold 6148 2.4Ghz 40 Cores 768GB H730p 16x Trays",
                         body="<p>Supports up to 3TB DDR4 and 24 drives. Great for virtualization.</p>",
                         vendor="Tech Vitality", tags=["server_Memory 3TB"])
    record = only(shopify_one("SaveMyServer", SMS_CATALOG, "savemyserver.com", [product])[1])
    assert record["vendor"] == "Dell"
    assert record["cpu_raw"] == "2× Gold 6148"
    assert record["source_ram_gb"] == 768
    assert record["retail"]["kind"] == "configured"
    assert record["retail"]["variants"][0]["paired"] is True
    assert record["source_url"] == "https://savemyserver.com/products/" + product["handle"]
    assert "3TB" not in record["retail"]["installed_text"]
    assert "Supports up to 3TB" in record["retail"]["description_text"]


@pytest.mark.parametrize("handle,title", [
    ("server-design-lab-dell-poweredge-r640-8-bay-2-5-configured-1224", "SERVER DESIGN LAB: Dell PowerEdge R640 8 Bay 2.5\""),
    ("dell-r640-configured-5521", "Dell R640 2x Gold 6148 768GB configured"),
])
def test_savemyserver_builder_products_stay_unpaired_base_prices(handle, title):
    product = sh_product(5521, handle, title)
    record = only(shopify_one("SaveMyServer", SMS_CATALOG, "savemyserver.com", [product])[1])
    assert record["retail"]["kind"] == "configurable"
    assert record["retail"]["variants"][0]["paired"] is False
    assert record["retail"]["installed_text"] == ""
    assert record["cpu_raw"] is None and record["source_ram_gb"] is None
    assert record["item_price"] == "499.00"


def test_savemyserver_title_without_explicit_cpu_and_memory_is_not_a_paired_configuration():
    product = sh_product(9, "dell-poweredge-r640-server", "Dell PowerEdge R640 Server")
    record = only(shopify_one("SaveMyServer", SMS_CATALOG, "savemyserver.com", [product])[1])
    assert record["retail"]["kind"] == "configurable"
    assert record["retail"]["variants"][0]["paired"] is False
    assert record["model"] == "R640"


CN_BODY = ("<h3>Server Specifications Offered:</h3><ul><li>2 x Intel Xeon 8568Y+</li>"
           "<li>2048GB ECC Registered Memory</li><li>8x NVIDIA H200 GPUs</li></ul>"
           "<h4>Memory Specs:</h4><p>Max # DIMMS 24x128GB</p><p>Condition: New</p>")


def test_cloudninjas_offered_section_is_installed_text_and_capacity_blurbs_are_not():
    product = sh_product(31, "supermicro-gpu-server-h200", "Supermicro GPU Server 8x H200", body=CN_BODY)
    record = only(shopify_one("CloudNinjas", CN_CATALOG, "cloudninjas.com", [product])[1])
    retail = record["retail"]
    assert retail["kind"] == "configured"
    assert record["cpu_raw"] == "2× Intel Xeon 8568Y+"
    assert record["source_ram_gb"] == 2048
    assert "8x NVIDIA H200 GPUs" in retail["installed_text"]
    assert "Max #" not in retail["installed_text"] and "24x128GB" not in retail["installed_text"]
    assert "Max # DIMMS 24x128GB" in retail["description_text"]
    assert record["condition"] == "New"
    assert retail["variants"][0]["paired"] is True


def test_cloudninjas_generic_platform_page_gives_capacity_not_installed_facts():
    body = ("<p>Configure your Dell PowerEdge R640 Server</p><p>Max # DIMMS 24x128GB</p><p>Max Size 3TB</p>"
            "<p>Supports up to 2 Intel Xeon Scalable processors</p>")
    product = sh_product(32, "dell-poweredge-r640-server", "Dell PowerEdge R640 Server", body=body,
                         variants=[sh_variant(320, price="599.00")], tags=["server_Memory DDR4", "server_CPU Xeon Gold"])
    record = only(shopify_one("CloudNinjas", CN_CATALOG, "cloudninjas.com", [product])[1])
    assert record["retail"]["kind"] == "configurable"
    assert record["retail"]["installed_text"] == ""
    assert record["cpu_raw"] is None and record["source_ram_gb"] is None and record["total_cores"] is None
    assert record["retail"]["variants"][0]["paired"] is False
    assert record["item_price"] == "599.00"
    assert record["model"] == "R640" and record["vendor"] == "Dell"


def test_call_for_availability_is_quote_only_and_never_purchasable():
    body = CN_BODY + '<p><a href="mailto:sales@cloudninjas.com">Call for Availability</a></p>'
    product = sh_product(33, "h200-cluster", "H200 Cluster", body=body,
                         variants=[sh_variant(330, price="450000.00", available=False)])
    record = only(shopify_one("CloudNinjas", CN_CATALOG, "cloudninjas.com", [product])[1])
    assert record["retail"]["kind"] == "quote_only"
    assert record["retail"]["available"] is False
    assert record["item_price"] is None
    assert record["retail"]["variants"][0]["paired"] is False
    assert record["retail"]["variants"][0]["available"] is False


def test_shopify_multi_variant_with_non_hardware_options_stays_unpaired_base():
    variants = [sh_variant(901, price="800.00", title="Gold / Extended", options=["Gold", "Extended"]),
                sh_variant(902, price="650.00", title="Silver / Standard", options=["Silver", "Standard"]),
                sh_variant(903, price="500.00", title="Bronze / Standard", options=["Bronze", "Standard"], available=False)]
    product = sh_product(90, "dell-r640-bundles", "Dell PowerEdge R640 2x Gold 6148 768GB", variants=variants)
    product["options"] = [{"name": "Bundle", "values": ["Gold", "Silver", "Bronze"]}, {"name": "Warranty", "values": ["Extended", "Standard"]}]
    record = only(shopify_one("SaveMyServer", SMS_CATALOG, "savemyserver.com", [product])[1])
    got = record["retail"]["variants"]
    assert [v["variant_id"] for v in got] == ["901", "902", "903"]
    assert got[1]["labels"] == {"Bundle": "Silver", "Warranty": "Standard"} and got[1]["label"] == "Silver / Standard"
    assert [v["paired"] for v in got] == [False, False, False]
    assert [v["available"] for v in got] == [True, True, False]
    assert record["item_price"] == "650.00"
    assert record["retail"]["kind"] == "configurable"
    assert record["source_ram_gb"] is None


def _memory_option_product():
    variants = [sh_variant(801, price="700.00", title="64GB", options=["64GB"]),
                sh_variant(802, price="550.00", title="32GB", options=["32GB"]),
                sh_variant(803, price="480.00", title="16GB", options=["16GB"], available=False)]
    product = sh_product(80, "dell-r640-2x-silver-4110", "Dell PowerEdge R640 8-Bay Server 2x Silver 4110 H730p", variants=variants)
    product["options"] = [{"name": "Memory", "values": ["64GB", "32GB", "16GB"]}]
    return product


def test_native_memory_variants_each_pair_with_common_cpu_and_their_own_ram():
    record = only(shopify_one("SaveMyServer", SMS_CATALOG, "savemyserver.com", [_memory_option_product()])[1])
    retail = record["retail"]
    assert retail["kind"] == "configured"
    got = retail["variants"]
    assert [(v["variant_id"], v["price"], v["available"], v["paired"]) for v in got] == [
        ("801", "700.00", True, True), ("802", "550.00", True, True), ("803", "480.00", False, True)]
    assert got[1]["labels"] == {"Memory": "32GB"}
    assert record["item_price"] == "550.00"
    # common fact kept; the dimension that varies per variant is not claimed at product level
    assert record["cpu_raw"] == "2× Silver 4110"
    assert record["source_ram_gb"] is None
    assert [rs.variant_facts(record, v) for v in got] == [
        {"cpu_raw": "2× Silver 4110", "source_ram_gb": 64},
        {"cpu_raw": "2× Silver 4110", "source_ram_gb": 32},
        {"cpu_raw": "2× Silver 4110", "source_ram_gb": 16}]


def test_memory_variants_without_common_installed_cpu_or_on_builders_are_not_paired():
    no_cpu = _memory_option_product()
    no_cpu["title"] = "Dell PowerEdge R640 8-Bay Server"
    no_cpu["handle"] = "dell-r640-8-bay"
    record = only(shopify_one("SaveMyServer", SMS_CATALOG, "savemyserver.com", [no_cpu])[1])
    assert record["retail"]["kind"] == "configurable"
    assert not any(v["paired"] for v in record["retail"]["variants"])
    builder = _memory_option_product()
    builder["handle"] = "server-design-lab-r640-configured-1224"
    record = only(shopify_one("SaveMyServer", SMS_CATALOG, "savemyserver.com", [builder])[1])
    assert record["retail"]["kind"] == "configurable"
    assert not any(v["paired"] for v in record["retail"]["variants"])


def test_shopify_upstream_updated_at_is_utc_and_snapshot_reports_newest():
    product = sh_product(1, "a-2x-gold-6148-128gb", "Dell PowerEdge R640 2x Gold 6148 128GB")
    fetcher, snap = shopify_one("SaveMyServer", SMS_CATALOG, "savemyserver.com", [product])
    assert snap.records[0]["retail"]["upstream_updated_at"] == "2026-11-04T15:00:00+00:00"
    assert snap.upstream_updated_at == datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)
    assert catalog("TechMikeNY", tm_catalog([([tm_product()], None)])).upstream_updated_at is None


def test_vendor_and_model_only_when_unambiguous_and_cataloged():
    products = [
        sh_product(1, "a", "Dell PowerEdge R640 2x Gold 6148 128GB"),
        sh_product(2, "b", "HPE ProLiant DL360 Gen10 2x Gold 6148 128GB"),
        sh_product(3, "c", "Dell PowerEdge R640 / HPE DL360 Gen10 bundle"),
        sh_product(4, "d", "Mystery Server 2x Gold 6148 128GB", vendor="Dell"),
        sh_product(5, "e", "Dell PowerEdge R999 2x Gold 6148 128GB"),
    ]
    fetcher = FakeFetcher({sh_catalog_url(SMS_CATALOG, 1): ok({"products": products}),
                           sh_probe_url("savemyserver.com", "a"): ok(sh_json_product(products[0]))})
    got = {r["retail"]["handle"]: (r["vendor"], r["model"])
           for r in catalog("SaveMyServer", fetcher).records}
    assert got == {"a": ("Dell", "R640"), "b": ("HPE", "HPE DL360 Gen10"), "c": (None, None),
                   "d": ("Dell", None), "e": ("Dell", None)}


# --------------------------------------------------------------------------- pagination / completeness


def test_techmikeny_walks_cursors_and_reports_complete():
    a, b, c = (tm_product(pid=i, handle=f"p{i}") for i in (1, 2, 3))
    fetcher = tm_catalog([([a], "CUR1=="), ([b], "CUR2=="), ([c], None)])
    snap = catalog("TechMikeNY", fetcher)
    assert [r["item_id"] for r in snap.records] == ["1", "2", "3"]
    assert snap.complete is True and snap.rejected == 0 and snap.duplicates == 0
    assert fetcher.calls == [tm_url(), tm_url("CUR1=="), tm_url("CUR2==")]
    assert all(rs.allowed_url("TechMikeNY", u) for u in fetcher.calls)


def test_techmikeny_repeated_cursor_stops_and_marks_incomplete():
    pages = {tm_url(): ok(tm_page([tm_product(pid=1, handle="p1")], "LOOP")),
             tm_url("LOOP"): ok(tm_page([tm_product(pid=2, handle="p2")], "LOOP"))}
    fetcher = FakeFetcher(pages)
    snap = catalog("TechMikeNY", fetcher)
    assert [r["item_id"] for r in snap.records] == ["1", "2"]
    assert snap.complete is False
    assert len(fetcher.calls) == 2


def test_techmikeny_next_page_without_cursor_is_incomplete():
    payload = tm_page([tm_product()], None)
    payload["pageInfo"] = {"hasNextPage": True, "endCursor": None}
    snap = catalog("TechMikeNY", FakeFetcher({tm_url(): ok(payload)}))
    assert len(snap.records) == 1 and snap.complete is False


def test_techmikeny_page_cap_is_25_and_incomplete():
    responses = {}
    cursor = None
    for i in range(1, 40):
        nxt = f"C{i}"
        responses[tm_url(cursor)] = ok(tm_page([tm_product(pid=i, handle=f"p{i}")], nxt))
        cursor = nxt
    fetcher = FakeFetcher(responses)
    snap = catalog("TechMikeNY", fetcher)
    assert len(fetcher.calls) == 25
    assert len(snap.records) == 25 and snap.complete is False


def test_shopify_short_page_completes_and_full_pages_continue():
    def products(start, n):
        return [sh_product(start + i, f"h{start + i}", f"Dell PowerEdge R640 2x Gold 6148 {128}GB") for i in range(n)]
    p1, p2 = products(1, 100), products(101, 7)
    fetcher = FakeFetcher({sh_catalog_url(SMS_CATALOG, 1): ok({"products": p1}),
                           sh_catalog_url(SMS_CATALOG, 2): ok({"products": p2}),
                           sh_probe_url("savemyserver.com", "h1"): ok(sh_json_product(p1[0]))})
    snap = catalog("SaveMyServer", fetcher)
    assert len(snap.records) == 107 and snap.complete is True
    assert fetcher.calls.count(sh_catalog_url(SMS_CATALOG, 2)) == 1
    assert sum("products.json" in u for u in fetcher.calls) == 2


def test_shopify_page_cap_is_25_and_incomplete():
    responses = {}
    first = None
    for page in range(1, 40):
        items = [sh_product(page * 1000 + i, f"h{page}-{i}", "Dell PowerEdge R640 2x Gold 6148 128GB") for i in range(100)]
        first = first or items[0]
        responses[sh_catalog_url(CN_CATALOG, page)] = ok({"products": items})
    responses[sh_probe_url("cloudninjas.com", first["handle"])] = ok(sh_json_product(first))
    fetcher = FakeFetcher(responses)
    snap = catalog("CloudNinjas", fetcher)
    assert sum("products.json" in u for u in fetcher.calls) == 25
    assert snap.complete is False and len(snap.records) == 2500


def test_shopify_repeated_page_content_is_incomplete_and_counted_duplicate():
    items = [sh_product(i, f"h{i}", "Dell PowerEdge R640 2x Gold 6148 128GB") for i in range(1, 101)]
    fetcher = FakeFetcher({sh_catalog_url(SMS_CATALOG, 1): ok({"products": items}),
                           sh_catalog_url(SMS_CATALOG, 2): ok({"products": items}),
                           sh_probe_url("savemyserver.com", "h1"): ok(sh_json_product(items[0]))})
    snap = catalog("SaveMyServer", fetcher)
    assert len(snap.records) == 100 and snap.duplicates == 100 and snap.complete is False
    assert sum("products.json" in u for u in fetcher.calls) == 2  # stops instead of paging on


def test_malformed_rows_are_counted_and_make_snapshot_incomplete_without_dropping_good_rows():
    good = tm_product(pid=1, handle="good")
    bad = [None, "str", {"id": "gid://shopify/Product/9"},
           tm_product(pid=3, handle="../evil"),
           {**tm_product(pid=4, handle="badid"), "id": "gid://shopify/Product/abc"},
           tm_product(pid=5, handle="novariants", variants=[]),
           tm_product(pid=6, handle="badprice", variants=[tm_variant(6, price="free")]),
           tm_product(pid=7, handle="negprice", variants=[tm_variant(7, price="-5.00")]),
           {**tm_product(pid=8, handle="notitle"), "title": None},
           tm_product(pid=10, handle="badvid", variants=[{**tm_variant(1), "id": "nope"}])]
    snap = catalog("TechMikeNY", tm_catalog([([good, *bad], None)]))
    assert [r["item_id"] for r in snap.records] == ["1"]
    assert snap.rejected == len(bad) and snap.complete is False


def test_duplicate_native_ids_keep_first_and_are_counted():
    a = tm_product(pid=1, handle="first")
    dup = tm_product(pid=1, handle="second")
    snap = catalog("TechMikeNY", tm_catalog([([a, dup], None)]))
    assert [r["retail"]["handle"] for r in snap.records] == ["first"]
    assert snap.duplicates == 1 and snap.complete is True


@pytest.mark.parametrize("payload", ["not json", "[]", '{"nope": 1}', '{"products": "x"}', '{"products": []}'])
def test_unusable_first_page_never_looks_like_complete_empty_inventory(payload):
    fetcher = FakeFetcher({tm_url(): (200, payload)})
    try:
        snap = catalog("TechMikeNY", fetcher)
    except SourceError:
        return
    assert snap.records == [] and snap.complete is False


def test_malformed_later_page_returns_partial_incomplete():
    fetcher = FakeFetcher({tm_url(): ok(tm_page([tm_product(pid=1, handle="p1")], "N1")), tm_url("N1"): (200, "<html>oops")})
    try:
        snap = catalog("TechMikeNY", fetcher)
    except SourceError:
        return
    assert [r["item_id"] for r in snap.records] == ["1"] and snap.complete is False


def test_non_200_and_challenge_pages_are_errors_not_empty_results():
    with pytest.raises(SourceError):
        catalog("TechMikeNY", FakeFetcher({tm_url(): (500, "")}))
    with pytest.raises(SourceError):
        catalog("TechMikeNY", FakeFetcher({tm_url(): (404, "{}")}))
    with pytest.raises(SourceBlocked):
        catalog("TechMikeNY", FakeFetcher({tm_url(): (200, "<html><title>Just a moment...</title><body>Verify you are human</body></html>")}))


def test_source_block_mid_walk_propagates_and_stops_immediately():
    fetcher = FakeFetcher({tm_url(): ok(tm_page([tm_product()], "N1")), tm_url("N1"): SourceBlocked("HTTP 429", 300.0),
                           tm_url("N2"): ok(tm_page([], None))})
    with pytest.raises(SourceBlocked) as info:
        catalog("TechMikeNY", fetcher)
    assert info.value.retry_after == 300.0
    assert fetcher.calls == [tm_url(), tm_url("N1")]


def test_unknown_provider_and_non_allowlisted_calls_never_reach_network():
    fetcher = FakeFetcher({})
    with pytest.raises(ValueError):
        catalog("PerUnitPrice", fetcher)
    assert fetcher.calls == []


# --------------------------------------------------------------------------- SSRF / redirects via real HttpFetcher


def _http_fetcher(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return HttpFetcher(5, 0, client=client)


def test_catalog_redirect_off_allowlist_is_refused_and_nothing_is_fetched_there():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "techmikeny.com":
            return httpx.Response(302, headers={"location": "https://evil.example/steal"})
        return httpx.Response(200, text="{}")

    with pytest.raises(SourceError):
        catalog("TechMikeNY", _http_fetcher(handler))
    assert seen == [tm_url()]


@pytest.mark.parametrize("location", ["http://techmikeny.com/api/agent/collections/servers?first=100",
                                      "https://techmikeny.com/cart/add", "https://techmikeny.com@evil.example/"])
def test_catalog_redirect_to_non_grammar_locations_is_refused(location):
    def handler(request):
        return httpx.Response(302, headers={"location": location})

    with pytest.raises(SourceError):
        catalog("TechMikeNY", _http_fetcher(handler))


def test_same_grammar_redirect_is_followed_by_shared_fetcher():
    body = json.dumps(tm_page([tm_product()], None))

    def handler(request):
        if request.url.query == b"first=100":
            return httpx.Response(302, headers={"location": "/api/agent/collections/servers?first=100&after=AAA"})
        return httpx.Response(200, text=body)

    snap = catalog("TechMikeNY", _http_fetcher(handler))
    assert len(snap.records) == 1


def test_http_429_through_shared_fetcher_is_a_block_with_retry_after():
    def handler(request):
        return httpx.Response(429, headers={"retry-after": "90"})

    with pytest.raises(SourceBlocked) as info:
        catalog("SaveMyServer", _http_fetcher(handler))
    assert info.value.retry_after == 90.0


# --------------------------------------------------------------------------- fetch_product


def tm_record(**kw):
    return only(catalog("TechMikeNY", tm_catalog([([tm_product(**kw)], None)])))


def product_call(provider, record, fetcher, observed=OBS + timedelta(hours=3)):
    return rs.fetch_product(provider, record, fetcher, observed_at=observed, fresh_until=observed + timedelta(hours=26),
                            max_bytes=MAX_BYTES)


TM_PRODUCT_URL = "https://techmikeny.com/api/agent/products/dell-poweredge-r640-10-bay"


def test_fetch_product_requests_only_that_product_and_refreshes_quote_times():
    record = tm_record()
    fresh = tm_product(variants=[tm_variant(44131282092092, price="329.95")])
    fetcher = FakeFetcher({TM_PRODUCT_URL: ok({"product": fresh})})
    updated = product_call("TechMikeNY", record, fetcher)
    assert fetcher.calls == [TM_PRODUCT_URL]
    assert updated["item_id"] == record["item_id"] and updated["source_url"] == record["source_url"]
    assert updated["item_price"] == "329.95"
    assert updated["retail"]["method"] == "retailer_product"
    assert updated["retail"]["observed_at"] == "2026-11-04T15:00:00+00:00"
    assert updated["retail"]["fresh_until"] == "2026-11-05T17:00:00+00:00"
    assert record["retail"]["observed_at"] == "2026-11-04T12:00:00+00:00"  # input untouched


def test_fetch_product_rejects_identity_mismatch_and_bad_handles():
    record = tm_record()
    other = tm_product(pid=999)
    with pytest.raises(SourceError):
        product_call("TechMikeNY", record, FakeFetcher({TM_PRODUCT_URL: ok({"product": other})}))
    bad = copy.deepcopy(record)
    bad["retail"]["handle"] = "../cto/build"
    bad["source_url"] = "https://techmikeny.com/products/../cto/build"
    fetcher = FakeFetcher({})
    with pytest.raises(SourceError):
        product_call("TechMikeNY", bad, fetcher)
    assert fetcher.calls == []


def test_fetch_product_404_is_an_explicit_unavailable_product_not_a_failure():
    record = tm_record()
    fetcher = FakeFetcher({TM_PRODUCT_URL: (404, '{"error":"not found"}')})
    updated = product_call("TechMikeNY", record, fetcher)
    assert updated["item_id"] == record["item_id"]
    assert updated["retail"]["available"] is False
    assert all(v["available"] is False for v in updated["retail"]["variants"])
    assert updated["item_price"] == record["item_price"]
    assert updated["retail"]["method"] == "retailer_product"
    assert updated["retail"]["observed_at"] == "2026-11-04T15:00:00+00:00"


def test_fetch_product_block_server_error_and_garbage_propagate():
    record = tm_record()
    with pytest.raises(SourceBlocked):
        product_call("TechMikeNY", record, FakeFetcher({TM_PRODUCT_URL: SourceBlocked("403", None)}))
    with pytest.raises(SourceError):
        product_call("TechMikeNY", record, FakeFetcher({TM_PRODUCT_URL: (500, "boom")}))
    with pytest.raises(SourceError):
        product_call("TechMikeNY", record, FakeFetcher({TM_PRODUCT_URL: (200, "garbage")}))
    with pytest.raises(SourceError):
        product_call("TechMikeNY", record, FakeFetcher({TM_PRODUCT_URL: ok({"product": tm_product(variants=[tm_variant(1, currency="CAD")])})}))


def test_fetch_product_shopify_uses_product_json_currency_without_other_requests():
    product = sh_product(31, "supermicro-gpu-server-h200", "Supermicro GPU Server 8x H200", body=CN_BODY)
    record = only(shopify_one("CloudNinjas", CN_CATALOG, "cloudninjas.com", [product])[1])
    url = sh_probe_url("cloudninjas.com", product["handle"])
    cheaper = copy.deepcopy(product)
    cheaper["variants"][0]["price"] = "399.00"
    fetcher = FakeFetcher({url: ok(sh_json_product(cheaper))})
    updated = product_call("CloudNinjas", record, fetcher)
    assert fetcher.calls == [url]
    assert updated["item_price"] == "399.00" and updated["currency"] == "USD"
    assert updated["retail"]["method"] == "retailer_product"
    assert "price_currency" in updated["retail"]["currency_evidence"]
    with pytest.raises(SourceError):
        product_call("CloudNinjas", record, FakeFetcher({url: ok(sh_json_product(cheaper, currency=None))}))
    with pytest.raises(SourceError):
        product_call("CloudNinjas", record, FakeFetcher({url: ok(sh_json_product(cheaper, currency="EUR"))}))


@pytest.mark.parametrize("available", [True, False])
def test_product_refresh_uses_explicit_stock_when_product_json_omits_availability(available):
    product = sh_product(31, "supermicro-gpu-server-h200", "Supermicro GPU Server 8x H200", body=CN_BODY)
    record = only(shopify_one("CloudNinjas", CN_CATALOG, "cloudninjas.com", [product])[1])
    response = sh_json_product(product)
    response["product"]["variants"][0].pop("available")
    response["product"]["variants"][0]["inventory_management"] = "shopify"
    stock = {"id": product["id"], "handle": product["handle"], "variants": [
        {"id": product["variants"][0]["id"], "price": 49900, "available": available},
    ]}
    base = "https://cloudninjas.com/products/" + product["handle"]
    updated = product_call("CloudNinjas", record, FakeFetcher({
        base + ".json": ok(response), base + ".js": ok(stock),
    }))
    assert updated["retail"]["available"] is available
    assert updated["retail"]["variants"][0]["available"] is available
    assert updated["item_price"] == "499.00"
    assert record["retail"]["available"] is True


@pytest.mark.parametrize("change", ["product", "variant", "price", "missing_stock"])
def test_ambiguous_product_stock_cannot_replace_a_valid_quote(change):
    product = sh_product(31, "supermicro-gpu-server-h200", "Supermicro GPU Server 8x H200", body=CN_BODY)
    record = only(shopify_one("CloudNinjas", CN_CATALOG, "cloudninjas.com", [product])[1])
    response = sh_json_product(product)
    response["product"]["variants"][0].pop("available")
    stock = {"id": product["id"], "handle": product["handle"], "variants": [
        {"id": product["variants"][0]["id"], "price": 49900, "available": True},
    ]}
    if change == "product":
        stock["id"] += 1
    elif change == "variant":
        stock["variants"][0]["id"] += 1
    elif change == "price":
        stock["variants"][0]["price"] = 59900
    else:
        stock["variants"][0].pop("available")
    base = "https://cloudninjas.com/products/" + product["handle"]
    with pytest.raises(SourceError):
        product_call("CloudNinjas", record, FakeFetcher({
            base + ".json": ok(response), base + ".js": ok(stock),
        }))
    assert record["retail"]["available"] is True
    assert record["item_price"] == "499.00"


def test_fetch_product_rejects_wrong_provider_record():
    record = tm_record()
    with pytest.raises(SourceError):
        product_call("SaveMyServer", record, FakeFetcher({}))
    with pytest.raises(ValueError):
        product_call("PerUnitPrice", record, FakeFetcher({}))


def test_tower_workstation_and_accessory_products_are_skipped_without_failing_the_snapshot():
    products = [
        sh_product(1, "rack", "Dell PowerEdge R640 2x Gold 6148 128GB"),
        sh_product(2, "tower", "Lenovo ThinkSystem ST250 Tower Server Xeon E-2124 16GB"),
        sh_product(3, "st50", "Lenovo ThinkSystem ST50 Server 8GB"),
        sh_product(4, "ws", "Dell Precision 7920 Workstation 2x Gold 6148 128GB"),
        sh_product(5, "ram", "64GB DDR4 ECC RDIMM Memory Module"),
        sh_product(6, "conv", "Dell PowerEdge T640 Rack/Tower Server 2x Gold 6148 128GB"),
    ]
    fetcher = FakeFetcher({sh_catalog_url(SMS_CATALOG, 1): ok({"products": products}),
                           sh_probe_url("savemyserver.com", "rack"): ok(sh_json_product(products[0]))})
    snap = catalog("SaveMyServer", fetcher)
    assert [r["retail"]["handle"] for r in snap.records] == ["rack", "conv"]
    assert snap.rejected == 0 and snap.complete is True


def test_fetch_product_for_out_of_scope_tower_is_an_error_not_a_quote():
    tower = sh_product(2, "tower", "Lenovo ThinkSystem ST250 Tower Server")
    record = only(shopify_one("SaveMyServer", SMS_CATALOG, "savemyserver.com",
                              [sh_product(2, "tower", "Dell PowerEdge R640 2x Gold 6148 128GB")])[1])
    url = sh_probe_url("savemyserver.com", "tower")
    with pytest.raises(SourceError):
        product_call("SaveMyServer", record, FakeFetcher({url: ok(sh_json_product(tower))}))


def test_supported_shopify_native_variants_are_not_silently_discarded_above_one_hundred():
    variants = [
        sh_variant(100001 + i, price=str(300 + i) + ".00", title=f"{(i + 1) * 8}GB", options=[f"{(i + 1) * 8}GB"])
        for i in range(200)
    ]
    product = sh_product(80, "dell-r640-memory-options", "Dell PowerEdge R640 Server 2x Silver 4110", variants=variants)
    product["options"] = [{"name": "Memory", "values": [v["option1"] for v in variants]}]
    snapshot = shopify_one("SaveMyServer", SMS_CATALOG, "savemyserver.com", [product])[1]
    assert snapshot.complete is True
    native = only(snapshot)["retail"]["variants"]
    assert {v["variant_id"]: v["price"] for v in native} == {
        str(v["id"]): v["price"] for v in variants
    }


@pytest.mark.parametrize("memory", ["Up to 64GB RAM", "Maximum 128GB RAM", "Supports 256GB RAM"])
@pytest.mark.parametrize("provider", ["TechMikeNY", "SaveMyServer"])
def test_capacity_only_native_options_never_become_installed_memory_quotes(provider, memory):
    if provider == "TechMikeNY":
        product = tm_product(variants=[tm_variant(1, title=memory)])
        snapshot = catalog(provider, tm_catalog([([product], None)]))
    else:
        product = sh_product(80, "dell-r640", "Dell PowerEdge R640 2x Silver 4110 32GB",
                             variants=[sh_variant(801, title=memory, options=[memory])])
        product["options"] = [{"name": "Memory"}]
        snapshot = shopify_one(provider, SMS_CATALOG, "savemyserver.com", [product])[1]
    record = only(snapshot)
    variant, = record["retail"]["variants"]
    assert rs.variant_facts(record, variant)["source_ram_gb"] is None
    assert variant["paired"] is False
    assert record["retail"]["kind"] != "configured"


@pytest.mark.parametrize("wording", ["Call for availability", "Request a quote"])
def test_techmikeny_explicit_quote_only_language_overrides_placeholder_price(wording):
    product = tm_product(description=TM_DESC + " " + wording)
    record = only(catalog("TechMikeNY", tm_catalog([([product], None)])))
    assert record["retail"]["kind"] == "quote_only"
    assert record["item_price"] is None
    assert all(not v["paired"] and not v["available"] for v in record["retail"]["variants"])


def test_malformed_inventory_metadata_cannot_abort_a_good_catalog_product():
    product = tm_product()
    product["totalInventory"] = {}
    record = only(catalog("TechMikeNY", tm_catalog([([product], None)])))
    assert record["retail"].get("stock_band") is None
    assert record["retail"]["available"] is True


@pytest.mark.parametrize("currency", [{}, [], 1])
def test_malformed_shopify_currency_is_controlled_source_failure(currency):
    product = sh_product(80, "dell-r640", "Dell PowerEdge R640 2x Silver 4110 32GB")
    with pytest.raises(SourceError):
        shopify_one("SaveMyServer", SMS_CATALOG, "savemyserver.com", [product], currency=currency)


def test_malformed_stock_variants_are_controlled_source_failure_without_extra_request():
    product = sh_product(80, "dell-r640", "Dell PowerEdge R640 2x Silver 4110 32GB")
    record = only(shopify_one("SaveMyServer", SMS_CATALOG, "savemyserver.com", [product])[1])
    bad = sh_json_product(product)
    bad["product"]["variants"].append(None)
    url = sh_probe_url("savemyserver.com", "dell-r640")
    fetcher = FakeFetcher({url: ok(bad)})
    with pytest.raises(SourceError):
        product_call("SaveMyServer", record, fetcher)
    assert fetcher.calls == [url]


def test_repeated_terminal_techmikeny_page_is_not_complete():
    product = tm_product()
    snapshot = catalog("TechMikeNY", tm_catalog([([product], "cursor"), ([product], None)]))
    assert snapshot.complete is False
    assert {r["item_id"] for r in snapshot.records} == {"8869466570812"}


def test_repeated_short_terminal_shopify_page_is_not_complete():
    products = [sh_product(i, f"r640-{i}", "Dell PowerEdge R640 2x Silver 4110 32GB") for i in range(1, 101)]
    fetcher = FakeFetcher({
        sh_catalog_url(SMS_CATALOG, 1): ok({"products": products}),
        sh_catalog_url(SMS_CATALOG, 2): ok({"products": [products[0]]}),
        sh_probe_url("savemyserver.com", "r640-1"): ok(sh_json_product(products[0])),
    })
    snapshot = catalog("SaveMyServer", fetcher)
    assert snapshot.complete is False
    assert {r["item_id"] for r in snapshot.records} == {str(i) for i in range(1, 101)}


def test_dimm_multiplier_title_pairs_total_installed_ram_not_stick_capacity():
    product = sh_product(80, "dell-r640", "Dell PowerEdge R640 2x Gold 6148 4 x 32GB RAM")
    record = only(shopify_one("SaveMyServer", SMS_CATALOG, "savemyserver.com", [product])[1])
    assert record["source_ram_gb"] == 128
    assert record["retail"]["variants"][0]["paired"] is True


@pytest.mark.parametrize("field", ["product", "variant"])
def test_non_ascii_native_ids_reject_only_the_bad_catalog_record(field):
    product = tm_product()
    if field == "product":
        product["id"] = "gid://shopify/Product/١٢٣"
    else:
        product["variants"][0]["id"] = "gid://shopify/ProductVariant/١٢٣"
    good = tm_product(pid=81, handle="dell-r640-good")
    snapshot = catalog("TechMikeNY", tm_catalog([([product, good], None)]))
    assert snapshot.complete is False
    assert snapshot.rejected == 1
    assert {r["item_id"] for r in snapshot.records} == {"81"}
