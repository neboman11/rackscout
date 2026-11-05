"""Multi-OEM source identity, cited catalog, filter contract and optional benchmark data.

No database or network required. Index rows below are copied from the real
PerUnitPrice vendor pages (2026-11-02) so normalization is exercised against
the actual feed shape: the Supermicro page leaves the model column as the bare
brand, so system IDs must come from the listing title.
"""
import json
from decimal import Decimal
from urllib.parse import urlsplit

import pytest
from test_db import SYNTHETIC_BENCHMARKS

from rackscout import domain

VENDOR_PAGES = {
    "Dell": "https://perunitprice.com/servers/dell/",
    "HPE": "https://perunitprice.com/servers/hpe/",
    "Supermicro": "https://perunitprice.com/servers/supermicro/",
    "Lenovo": "https://perunitprice.com/servers/lenovo/",
    "Cisco": "https://perunitprice.com/servers/cisco/",
}
MANUFACTURER_HOSTS = {
    "Dell": ("dell.com", "delltechnologies.com"),
    "HPE": ("hpe.com",),
    "Supermicro": ("supermicro.com",),
    "Lenovo": ("lenovo.com",),
    "Cisco": ("cisco.com",),
}


def index_html(records):
    cfg = {"updated": "2026-11-02T20:23:27+00:00", "dec": 2, "nsel": 3,
           "cols": [{"f": "int", "hide": False}, {"f": "text", "hide": False},
                    {"f": "gb", "hide": False}, {"f": "text", "hide": True},
                    {"f": "text", "hide": True}], "tog": [True], "flags": ["No RAM", "EPYC"]}
    return (f'<script id="d" type="application/json">{json.dumps(records)}</script>'
            f'<script id="cfg" type="application/json">{json.dumps(cfg)}</script>')


def row(item, title, model, brand, cpu="2× Gold 6130", ram=None):
    return [9.99, 299.99, 0.0, "Used", title, f"https://www.ebay.com/itm/{item}", 1, 32, cpu, ram, model, "DDR4",
            brand, "DDR4", "Rack", 0, ""]


FEED_ROWS = [
    # (item, title, model column, brand column) -> expected (vendor, model)
    (row(100000000001, "DELL POWEREDGE R640 8SFF 2x Gold 6130 64GB", "Dell R640", "Dell"), ("Dell", "R640")),
    (row(100000000002, "Dell PowerEdge R740XD 12LFF 2x Gold 6130", "Dell R740xd", "Dell"), ("Dell", "R740xd")),
    (row(100000000003, "HP ProLiant DL360 Gen10 8SFF 2x Gold 6130 NO RAM", "HPE DL360 Gen10", "HPE"),
     ("HPE", "HPE DL360 Gen10")),
    (row(100000000004, "HP ProLiant DL360 Gen10 8SFF 2x Gold 6130", "HP DL360 Gen10", "HP"),
     ("HPE", "HPE DL360 Gen10")),
    (row(100000000005, "HPE ProLiant DL325 Gen10 Plus 1U Server AMD EPYC 7402P 24-Core NO RAM", "HPE DL325 Gen10", "HPE"),
     ("HPE", "HPE DL325 Gen10 Plus")),
    (row(100000000006, "HPE DL325 Gen10 8SFF, 1x EPYC 7542, 64GB RAM", "HPE DL325 Gen10", "HPE"),
     ("HPE", "HPE DL325 Gen10")),
    (row(100000000007, "SuperMicro 119U-7 SYS-1029U-E1CR4T Server X11DPU 2*5115 GOLD CPU 16GB Ram No HDD", "Supermicro",
         "Supermicro"), ("Supermicro", "Supermicro SYS-1029U-E1CR4T")),
    (row(100000000008, "Supermicro SuperServer 6019U-TRTP2 2x Xeon Gold 5120 / 64GB / 2x PS / X11DPU", "Supermicro",
         "Supermicro"), ("Supermicro", "Supermicro SYS-6019U-TRTP2")),
    (row(100000000009, "Supermicro SYS-6019P-ACR12L 2x Xeon Silver 4110 2.1GHz 0GB RAM 1U Rack Server", "Supermicro",
         "Supermicro"), ("Supermicro", "Supermicro SSG-6019P-ACR12L")),
    (row(100000000010, "Supermicro 1023US-TR4 1U Dual SP3 4-Bay NVMe Server H11DSU-iN + AMD Epyc 7301", "Supermicro",
         "Supermicro"), ("Supermicro", "Supermicro AS-1023US-TR4")),
    (row(100000000011, "Supermicro SYS-2028U-E1CNRT+ 24-bay 2x Intel Xeon E5-2680v3 X10DRU-i+ 2U Server", "Supermicro",
         "Supermicro"), ("Supermicro", "Supermicro SYS-2028U-E1CNRT+")),
    # Chassis + motherboard only: a generic board cannot establish a system model, height or sockets.
    (row(100000000012, "Supermicro CSE-119U 1U Server - w/ X11DPU LGA3647 + 2xIntel Xeon Gold 5220R CPU", "Supermicro",
         "Supermicro"), ("Supermicro", None)),
    # Two different system IDs: ambiguous, never pick one.
    (row(100000000013, "Supermicro SYS-1029U-TR4 / SYS-6019U-TR25M lot 2x Gold 6130", "Supermicro", "Supermicro"),
     ("Supermicro", None)),
    (row(100000000014, "Lenovo ThinkSystem SR630 V1 Server 2x Xeon 6240R / No RAM / 10-Bay SFF", "Lenovo SR630", "Lenovo"),
     ("Lenovo", "Lenovo SR630")),
    (row(100000000015, "Lenovo ThinkSystem SR630 V2 1U 2x Xeon Gold 6330 64GB", "Lenovo SR630", "Lenovo"),
     ("Lenovo", "Lenovo SR630 V2")),
    (row(100000000016, "Cisco HX220C-M5SX UCS C220 M5 2x Intel GOLD 6226 2.7Ghz, No RAM No HDDs", "Cisco UCS C220 M5",
         "Cisco"), ("Cisco", "Cisco UCS C220 M5")),
    # Unknown OEM: preserved as unknown, never coerced into a supported vendor or Dell model key.
    (row(100000000017, "Fujitsu Primergy RX2530 M4 1U 2x Gold 6130", "Fujitsu RX2530 M4", "Fujitsu"), (None, None)),
]


@pytest.mark.parametrize("brand,expected", [
    ("Dell", "Dell"), ("HPE", "HPE"), ("HP", "HPE"), ("Hewlett Packard Enterprise", "HPE"), ("hp", "HPE"),
    ("Supermicro", "Supermicro"), ("SuperMicro", "Supermicro"), ("Lenovo", "Lenovo"), ("Cisco", "Cisco"),
    ("Fujitsu", None), ("", None), (None, None),
])
def test_vendor_aliases_canonicalize_and_unknown_stays_unknown(brand, expected):
    assert domain.normalize_vendor(brand) == expected


def test_parse_index_assigns_vendor_qualified_identity_for_every_oem():
    from rackscout.sources import parse_index

    snapshot = parse_index(index_html([raw for raw, _ in FEED_ROWS]))
    got = {r["item_id"]: (r["vendor"], r["model"]) for r in snapshot.records}
    expected = {str(raw[5]).rsplit("/", 1)[1]: identity for raw, identity in FEED_ROWS}
    assert got == expected
    hp_alias = next(r for r in snapshot.records if r["item_id"] == "100000000004")
    assert hp_alias["brand"] == "HP"  # raw source value retained for evidence
    # Non-Dell keys are vendor-qualified, so equal generation numbers or model stems never collide with Dell keys.
    for r in snapshot.records:
        if r["vendor"] not in (None, "Dell") and r["model"] is not None:
            assert r["model"].startswith(f"{r['vendor']} ")


def test_catalog_is_cited_bounded_and_covers_actual_feed_models():
    catalog = json.loads((domain.DATA_DIR / "models.json").read_text(encoding="utf-8"))["models"]
    keys = [m["model"] for m in catalog]
    assert len(keys) == len(set(keys))
    for m in catalog:
        assert m["vendor"] in domain.SUPPORTED_VENDORS
        assert isinstance(m["target_era"], bool)
        assert isinstance(m["generation"], int) and m["generation"] > 0
        assert isinstance(m["generation_label"], str) and m["generation_label"]
        assert m["rack_units"] >= 1 and m["socket_capacity"] in (1, 2)
        assert m["source_text"].strip()
        url = urlsplit(m["source_url"])
        assert url.scheme == "https"
        assert any(url.hostname == h or url.hostname.endswith("." + h) for h in MANUFACTURER_HOSTS[m["vendor"]])
        if m["vendor"] != "Dell":
            assert m["model"].startswith(m["vendor"] + " ")
    by_key = {m["model"]: m for m in catalog}
    # Real 1U target-era models observed in the vendor feeds, not only selectable empty filters.
    for key in ("R640", "R440", "HPE DL360 Gen10", "HPE DL325 Gen10", "HPE DL325 Gen10 Plus", "HPE DL325 Gen11",
                "Supermicro SYS-1029U-E1CR4T", "Supermicro SYS-1029U-TR4", "Supermicro SYS-6019U-TR25M",
                "Supermicro SYS-6019U-TRTP2", "Supermicro SSG-6019P-ACR12L", "Supermicro SYS-1019P-WTR",
                "Supermicro AS-1023US-TR4", "Supermicro AS-1014S-WTRT", "Lenovo SR630", "Lenovo SR530",
                "Cisco UCS C220 M5"):
        assert by_key[key]["target_era"] is True and by_key[key]["rack_units"] == 1, key
    assert by_key["Supermicro SYS-1019P-WTR"]["socket_capacity"] == 1
    assert by_key["Supermicro SYS-1029U-E1CR4T"]["socket_capacity"] == 2
    # Equal numbers stay vendor-scoped facts, never cross-vendor equivalences.
    assert by_key["HPE DL325 Gen11"]["generation"] == by_key["Supermicro SYS-1029U-TR4"]["generation"] == 11
    assert by_key["HPE DL325 Gen11"]["generation_label"] == "Gen11"
    assert by_key["Supermicro SYS-1029U-TR4"]["generation_label"] == "X11"


def test_settings_use_fixed_five_vendor_pages():
    from rackscout.config import load_settings

    settings = load_settings()
    assert dict(settings.source_pages) == VENDOR_PAGES


def test_index_evidence_cites_the_vendor_page_it_came_from():
    record = {
        "item_id": "100000000003", "source_url": "https://www.ebay.com/itm/100000000003", "original_url": "",
        "title": "HP ProLiant DL360 Gen10 8SFF 2x Gold 6130 64GB", "condition": "Used", "item_price": "299.99",
        "shipping_price": "0.00", "quantity": 1, "total_cores": 32, "cpu_raw": "2× Gold 6130", "source_ram_gb": 64,
        "model": "HPE DL360 Gen10", "vendor": "HPE", "index_url": VENDOR_PAGES["HPE"], "no_ram_flag": False, "flags": [],
    }
    catalog = {"model": "HPE DL360 Gen10", "vendor": "HPE", "generation": 10, "rack_units": 1, "socket_capacity": 2,
               "source_url": "https://www.hpe.com/psnow/doc/a00008159enw"}
    (config,), _ = domain.build_configurations(record, catalog, None, None)
    index_evidence = [e for e in config["evidence"] if e["text"].startswith("index:") or "Index" in e["text"]]
    assert index_evidence and {e["source_url"] for e in index_evidence} == {VENDOR_PAGES["HPE"]}


# ---------------------------------------------------------------------------
# Filter contract
# ---------------------------------------------------------------------------




def test_generation_without_vendor_is_rejected_by_query_layer():
    from rackscout import queries

    with pytest.raises(ValueError):
        queries.build_predicates(queries.Filters(generation=10))


def test_api_filter_parsing_vendor_generation_and_modern_only():
    from fastapi import HTTPException

    from rackscout.api import parse_filters

    default = parse_filters()
    assert (default.vendor, default.generation, default.modern_only, default.height) == (None, None, True, 1)
    with pytest.raises(HTTPException) as exc:
        parse_filters(generation="10")
    assert exc.value.status_code == 422
    hp = parse_filters(vendor="hp", generation="10", modern_only="false")
    assert (hp.vendor, hp.generation, hp.modern_only) == ("HPE", 10, False)
    assert parse_filters(vendor="Dell", generation="").generation is None
    with pytest.raises(HTTPException) as exc:
        parse_filters(vendor="Acme")
    assert exc.value.status_code == 422
    assert parse_filters(model=["Supermicro SYS-2028U-E1CNRT+"]).models == ("Supermicro SYS-2028U-E1CNRT+",)
    with pytest.raises(HTTPException):
        parse_filters(model=["R640; DROP"])


def test_api_model_parameter_is_repeated_trimmed_deduplicated_and_bounded():
    from fastapi import HTTPException

    from rackscout.api import parse_filters

    assert parse_filters(model=["R640", " R740 ", "R640", "", "R740"]).models == ("R640", "R740")
    # One malformed entry rejects the whole request.
    with pytest.raises(HTTPException) as exc:
        parse_filters(model=["R640", "R740; DROP"])
    assert exc.value.status_code == 422
    assert len(parse_filters(model=[f"M{i}" for i in range(100)]).models) == 100
    with pytest.raises(HTTPException) as exc:
        parse_filters(model=[f"M{i}" for i in range(101)])
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        parse_filters(model=["R640"] * 101)
    assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
# Optional external benchmark dataset (never bundled)
# ---------------------------------------------------------------------------


def test_no_benchmark_dataset_is_bundled():
    assert not (domain.DATA_DIR / "benchmarks.json").exists()


def test_benchmarks_file_unset_means_not_configured(monkeypatch):
    from rackscout.config import benchmarks_file

    monkeypatch.delenv("RACKSCOUT_BENCHMARKS_FILE", raising=False)
    assert benchmarks_file() is None
    monkeypatch.setenv("RACKSCOUT_BENCHMARKS_FILE", "   ")
    assert benchmarks_file() is None


@pytest.mark.parametrize("content", [None, "not json", '{"provider": "x"}', '{"provider": "x", "benchmarks": []}',
                                     '{"provider": "x", "benchmarks": [{"cpu_model": "Gold 6130", "cpu_mark": "fast"}]}'])
def test_configured_benchmark_file_missing_or_invalid_fails_clearly(tmp_path, monkeypatch, content):
    from rackscout import db

    path = tmp_path / "benchmarks.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("RACKSCOUT_BENCHMARKS_FILE", str(path))
    with pytest.raises(db.ReferenceDataError) as exc:
        db.load_benchmarks()
    assert "RACKSCOUT_BENCHMARKS_FILE" in str(exc.value)


def test_valid_synthetic_benchmark_file_loads(tmp_path, monkeypatch):
    from rackscout import db

    path = tmp_path / "benchmarks.json"
    path.write_text(json.dumps(SYNTHETIC_BENCHMARKS), encoding="utf-8")
    monkeypatch.setenv("RACKSCOUT_BENCHMARKS_FILE", str(path))
    doc = db.load_benchmarks()
    assert doc["provider"] == SYNTHETIC_BENCHMARKS["provider"]
    assert [b["cpu_model"] for b in doc["benchmarks"]] == [b["cpu_model"] for b in SYNTHETIC_BENCHMARKS["benchmarks"]]
