"""Evidence boundaries: variants, negative hardware language, sockets, quotes."""
import json
from decimal import Decimal

import pytest

from rackscout import domain
from rackscout.sources import SourceError, parse_description, parse_index

CATALOG = {
    "R640": {"model": "R640", "generation": 14, "rack_units": 1, "socket_capacity": 2, "source_url": "https://i.dell.com/r640.pdf"},
    "R6415": {"model": "R6415", "generation": 14, "rack_units": 1, "socket_capacity": 1, "source_url": "https://i.dell.com/r6415.pdf"},
}
TRAP_TITLE = "Dell PowerEdge R640 1U 8SFF Server Barebones 2x Gold 6150 64GB H730 2x 750w"


def rec(title=TRAP_TITLE, item_id="187634883092", price="268.88", shipping="0.00", cpu="2× Gold 6150", ram=64,
        model="R640", cores=36, no_ram=False):
    return {
        "item_id": item_id, "source_url": f"https://www.ebay.com/itm/{item_id}", "original_url": "",
        "title": title, "condition": "Used", "item_price": price, "shipping_price": shipping, "quantity": 1,
        "total_cores": cores, "cpu_raw": cpu, "source_ram_gb": ram, "model": model, "no_ram_flag": no_ram, "flags": [],
    }


def verified_description(html):
    return {**parse_description(html), "status": "verified"}


def build(record, desc=None, page=None):
    configs, listing = domain.build_configurations(record, CATALOG.get(record["model"]), desc, page)
    return configs, listing


def test_title_ram_on_unverified_base_price_is_never_exact_or_confirmed():
    (config,), listing = build(rec())
    assert config["price_kind"] == "base"
    assert config["ram_gb"] == 64 and config["ram_state"] == "inferred"
    assert config["hardware_state"] != "confirmed"
    assert listing["detail_status"] == "never"
    assert any("variation" in w.lower() for w in config["warnings"])


def test_verified_variations_become_separate_exact_configurations():
    page = {"status": "verified", "has_variations": True, "price_kind": "exact", "variations": [
        {"variant_id": "1001", "labels": {"Memory": "NO RAM"}, "price": "268.88"},
        {"variant_id": "1002", "labels": {"Memory": "32GB (2x16GB)"}, "price": "429.88"},
    ]}
    configs, listing = build(rec(), page=page)
    by_id = {c["variant_id"]: c for c in configs}
    assert by_id["1001"]["ram_gb"] == 0 and by_id["1001"]["ram_state"] == "confirmed"
    assert by_id["1001"]["hardware_state"] == "missing"
    assert by_id["1002"]["ram_gb"] == 32 and by_id["1002"]["item_price"] == Decimal("429.88")
    assert all(c["price_kind"] == "exact" for c in configs)
    assert listing["detail_status"] == "verified"


def test_unreadable_variations_keep_base_price_unverified():
    page = {"status": "verified", "has_variations": True, "price_kind": "range", "variations": []}
    (config,), _ = build(rec(), page=page)
    assert config["price_kind"] == "range"
    assert config["ram_state"] == "inferred"


def test_seller_no_ram_conflicts_with_title_and_uses_lowest_value():
    desc = verified_description("<ul><li>Memory: NO RAM</li><li>Upgrade options: 32GB, 64GB</li></ul>")
    (config,), _ = build(rec(), desc=desc)
    assert config["ram_state"] == "conflicting"
    assert config["ram_gb"] == 0


def test_explicit_missing_psu_marks_hardware_missing():
    desc = verified_description("<p>2x Intel Xeon Gold 6150</p><p>64GB DDR4 RAM</p><p>Power supplies not included</p>")
    assert desc["psu"] == "missing"
    (config,), _ = build(rec(), desc=desc)
    assert config["hardware_state"] == "missing"
    assert any("Power supplies" in w for w in config["warnings"])


def test_power_cords_are_accessories_not_power_supplies():
    desc = verified_description("<p>2x 750w Platinum Power Supplies</p><p>NOT Included</p><p>Rails / Power Cords / Bezel</p>")
    assert desc["psu"] == "present"
    assert "Power cords not included" in desc["exclusions"]
    assert "Rack rails not included" in desc["exclusions"]
    assert "Front bezel not included" in desc["exclusions"]


def test_no_drives_does_not_remove_backplane_bays():
    desc = parse_description("<p>Dell PowerEdge R640 8x 2.5\" Bays</p><p>No Drives or Trays Included</p>")
    assert desc["storage_state"] == "diskless"
    assert desc["drive_bays"] == 8 and desc["drive_size"] == "SFF"


def test_options_heading_list_does_not_confirm_installed_ram():
    desc = parse_description("<p>Upgrade options:</p><ul><li>64GB</li><li>128GB DDR4</li></ul><p>DDR4 ECC Memory</p>")
    assert desc["ram_gb"] is None


def test_capacity_statement_is_not_installed_ram():
    desc = parse_description("<p>Memory: supports up to 768GB DDR4</p>")
    assert desc["ram_gb"] is None


def test_title_no_ram_slash_hdd():
    claims = domain.parse_title("Dell PowerEdge R640 10Bay SFF 2x Gold 6152 H740 NO RAM/ HDD")
    assert claims["ram_gb"] == 0
    assert claims["storage_state"] == "diskless"
    assert claims["drive_bays"] == 10 and claims["drive_size"] == "SFF"


def test_ssd_capacity_in_title_is_not_ram():
    claims = domain.parse_title("Dell R640 2x Gold 6130 2x 480GB SSD")
    assert claims["ram_gb"] is None
    assert claims["storage_state"] == "included"


@pytest.mark.parametrize(
    "title,cpu,model,expected,state",
    [
        ("Dell R640 8SFF Gold 6132 32GB", "Gold 6132", "R640", None, "unknown"),
        ("Dell R640 8SFF 2x Gold 6132 32GB", "2× Gold 6132", "R640", 2, "inferred"),
        ("Dell R640 8SFF 1x Gold 6132 32GB", "Gold 6132", "R640", 1, "inferred"),
        ("DELL POWEREDGE R6415 8 BAY 2.5\" SFF EPYC 7401P 32GB", "EPYC 7401P", "R6415", 1, "inferred"),
    ],
)
def test_installed_cpu_count_single_dual_unknown(title, cpu, model, expected, state):
    (config,), _ = build(rec(title=title, cpu=cpu, model=model, cores=None))
    assert config["cpu_count"] == expected
    assert config["cpu_count_state"] == state


def test_cpu_count_above_socket_capacity_conflicts():
    (config,), _ = build(rec(title="Dell R6415 2x EPYC 7401P", cpu="2× EPYC 7401P", model="R6415"))
    assert config["cpu_count"] is None
    assert config["cpu_count_state"] == "conflicting"


def test_unknown_shipping_stays_unknown():
    (config,), _ = build(rec(shipping=None))
    assert config["shipping_price"] is None
    assert any("Shipping unknown" in w for w in config["warnings"])


def test_damage_disclosure_and_negated_damage():
    damaged = verified_description("<p>Bent drive bay divider; dent near fan access</p>")
    (config,), _ = build(rec(), desc=damaged)
    assert config["hardware_state"] == "damaged" and config["damage"] is True
    clean = parse_description("<p>No damage, tested working</p>")
    assert clean["damage"] is None


def _index(records, cols=None):
    cfg = {"updated": "2026-11-02T16:23:11+00:00", "dec": 2, "nsel": 3,
           "cols": cols or [{"f": "int"}, {"f": "text"}, {"f": "gb"}, {"f": "text"}, {"f": "text"}],
           "tog": [True], "flags": ["No RAM", "EPYC"]}
    return f'<script id="d" type="application/json">{json.dumps(records)}</script><script id="cfg" type="application/json">{json.dumps(cfg)}</script>'


def _row(item, url=None, shipping=0.0, flags=""):
    return [7.47, 268.88, shipping, "Used", "Dell R640 NO RAM NO HDD", url or f"https://www.ebay.com/itm/{item}?hash=x", 1,
            36, "2× Gold 6150", 0, "Dell R640", "DDR4", "Dell", "DDR4", "Rack", 1 if flags else 0, flags]


def test_index_free_shipping_is_known_zero_and_flags_decode():
    snapshot = parse_index(_index([_row("100000000001", flags="0")]))
    record = snapshot.records[0]
    assert record["shipping_price"] == Decimal("0.00")
    assert record["no_ram_flag"] is True
    assert record["source_url"] == "https://www.ebay.com/itm/100000000001"
    assert record["model"] == "R640"


def test_index_dedups_item_ids_and_rejects_foreign_urls():
    rows = [_row("100000000001"), _row("100000000001"), _row("100000000002", url="https://evil.example/itm/100000000002")]
    rows += [_row(str(100000000010 + i)) for i in range(20)]
    snapshot = parse_index(_index(rows))
    ids = [r["item_id"] for r in snapshot.records]
    assert len(ids) == len(set(ids)) == 21
    assert snapshot.duplicates == 1 and snapshot.rejected == 1


def test_index_schema_change_fails_explicitly():
    with pytest.raises(SourceError):
        parse_index(_index([_row("100000000001")], cols=[{"f": "text"}]))
    with pytest.raises(SourceError):
        parse_index(_index([_row("100000000001")[:-1]]))
    with pytest.raises(SourceError):
        parse_index(_index([]))
