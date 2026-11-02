"""Regression boundaries for claim extraction, conservative resolution and markup traversal."""
import pytest

from rackscout import domain
from rackscout.sources import SourceError, html_segments, parse_description
from tests.test_domain import TRAP_TITLE, build, rec, verified_description

EXACT_PAGE = {"status": "verified", "has_variations": False, "price": "300.00", "shipping": "25.00"}
COMPLETE_SELLER = (
    "<p>2x Intel Xeon Gold 6150</p><p>64GB DDR4 RAM</p><p>2x 750W Power Supplies</p>"
    "<p>Fans and heatsinks included</p><p>Intel X710 NIC</p><p>8x 2.5\" Bays</p>"
)


def variant_page(*variations, shipping=None, shipping_text=None):
    return {"status": "verified", "has_variations": True, "price_kind": "exact", "shipping": shipping,
            "shipping_text": shipping_text, "variations": list(variations)}


# 1. Option wording never hides explicit negative claims ---------------------


@pytest.mark.parametrize("html", [
    "<p>No RAM installed; upgrade options available</p>",
    "<p>Memory: NO RAM (upgrade options available)</p>",
    "<p>RAM not included, can be added for an additional cost</p>",
])
def test_negative_ram_survives_option_wording(html):
    assert parse_description(html)["ram_gb"] == 0


def test_other_negative_claims_survive_option_wording():
    details = parse_description(
        "<p>No CPU; processor upgrades available</p>"
        "<p>Power supplies not included (available for additional cost)</p>"
        "<p>No HDD; drive options available upon request</p>"
    )
    assert details["cpu_missing"] is True
    assert details["psu"] == "missing"
    assert details["storage_state"] == "diskless"


def test_option_wording_still_suppresses_optional_positive_specs_and_choices():
    assert parse_description("<p>Memory options: 64GB, 128GB, 256GB</p>")["ram_gb"] is None
    # "No RAM" offered as one choice of an option list is not an installed-state claim.
    assert parse_description("<p>Memory options: No RAM, 32GB, 64GB</p>")["ram_gb"] is None
    assert parse_description("<p>Choose: No HDD or 4x 1TB SAS</p>")["storage_state"] == "unknown"


# 2. Mixed diskless/included storage is a conflict, never diskless -----------


def test_no_hdd_with_included_ssd_is_conflicting_description():
    details = parse_description("<p>No HDD; includes 480GB SSD</p>")
    assert details["storage_conflict"] is True
    assert details["storage_state"] == "unknown"
    (config,), _ = build(rec(), desc={**details, "status": "verified"}, page=EXACT_PAGE)
    assert config["storage_state"] != "diskless"
    assert config["storage_conf"] == "conflicting"


def test_no_hdd_with_included_ssd_is_conflicting_title():
    claims = domain.parse_title("Dell R640 8SFF 2x Gold 6150 64GB No HDD 480GB SSD")
    assert claims["storage_conflict"] is True
    assert claims["storage_state"] == "unknown"
    (config,), _ = build(rec(title="Dell R640 8SFF 2x Gold 6150 64GB No HDD 480GB SSD"))
    assert config["storage_state"] != "diskless"
    assert config["storage_conf"] == "conflicting"


def test_negated_included_wording_is_still_diskless():
    assert parse_description("<p>No drives included</p>")["storage_state"] == "diskless"
    assert parse_description("<p>480GB SSD not included</p>")["storage_state"] == "diskless"
    assert parse_description("<p>2x 480GB SSD included</p>")["storage_state"] == "included"


# 3. Seller conflicts are preserved instead of falling back to the title -----


def test_seller_bay_conflict_is_not_replaced_by_title_bays():
    desc = verified_description("<p>4x 3.5\" Bays</p><p>8x 2.5\" Bays</p>")
    assert desc["bays_conflict"] is True and desc["size_conflict"] is True
    (config,), _ = build(rec(), desc=desc, page=EXACT_PAGE)
    assert config["drive_bays"] is None and config["bays_state"] == "conflicting"
    assert config["drive_size"] is None and config["size_state"] == "conflicting"


def test_seller_size_conflict_is_not_replaced_by_title_size():
    desc = verified_description("<p>2.5\" SFF drives</p><p>3.5\" LFF chassis</p>")
    assert desc["size_conflict"] is True
    (config,), _ = build(rec(), desc=desc, page=EXACT_PAGE)
    assert config["drive_size"] is None and config["size_state"] == "conflicting"


def test_seller_cpu_count_conflict_is_not_replaced_by_title_count():
    desc = verified_description("<p>2x Intel Xeon Gold 6150</p><p>1x Gold 6150 installed</p>")
    assert desc["cpu_conflict"] is True
    (config,), _ = build(rec(), desc=desc, page=EXACT_PAGE)
    assert config["cpu_count"] is None and config["cpu_count_state"] == "conflicting"


def test_title_cpu_count_conflict_for_same_model_conflicts():
    title = "Dell R640 8SFF 1x Gold 6150 2x Gold 6150 64GB"
    assert domain.parse_title(title)["cpu_conflict"] is True
    (config,), _ = build(rec(title=title, cpu="Gold 6150"))
    assert config["cpu_count"] is None and config["cpu_count_state"] == "conflicting"


# 4. Unresolved hardware-changing variations invalidate inherited values -----


@pytest.mark.parametrize("labels", [
    {"Memory": "Please select"},
    {"RAM": "Not specified"},
])
def test_unreadable_memory_variant_does_not_inherit_title_ram(labels):
    page = variant_page({"variant_id": "2001", "labels": labels, "price": "199.00"})
    (config,), _ = build(rec(), page=page)
    assert config["ram_gb"] is None
    assert config["ram_state"] == "unknown"
    assert not any(e["field"] == "ram_gb" and "64" in e["text"] for e in config["evidence"])


def test_memory_none_variant_is_explicit_zero():
    page = variant_page({"variant_id": "2002", "labels": {"Memory": "None"}, "price": "199.00"})
    (config,), _ = build(rec(), page=page)
    assert config["ram_gb"] == 0 and config["ram_state"] == "confirmed"


def test_unreadable_cpu_variant_does_not_inherit_title_cpu():
    page = variant_page({"variant_id": "2003", "labels": {"Processor": "Select option"}, "price": "199.00"})
    (config,), _ = build(rec(), page=page)
    assert config["cpu_model"] is None and config["cpu_model_state"] == "unknown"
    assert config["cpu_count"] is None


def test_uninterpreted_configuration_variant_invalidates_hardware_dimensions():
    page = variant_page({"variant_id": "2004", "labels": {"Configuration": "Bundle A"}, "price": "199.00"})
    (config,), _ = build(rec(), page=page)
    assert config["ram_gb"] is None and config["ram_state"] == "unknown"
    assert config["cpu_model"] is None
    assert config["storage_state"] == "unknown"
    assert config["hardware_state"] != "confirmed"


def test_mixed_storage_variant_is_not_diskless():
    page = variant_page({"variant_id": "2005", "labels": {"Storage": "No HDD + 480GB SSD"}, "price": "199.00"})
    (config,), _ = build(rec(), page=page)
    assert config["storage_state"] != "diskless"


# 5. Drive path essentials ----------------------------------------------------


def test_complete_seller_description_still_confirms_hardware():
    (config,), _ = build(rec(), desc=verified_description(COMPLETE_SELLER), page=EXACT_PAGE)
    assert config["hardware_state"] == "confirmed"


def test_missing_backplane_fails_hardware_confirmation():
    desc = verified_description(COMPLETE_SELLER + "<p>Backplane not included</p>")
    assert desc["backplane"] == "missing"
    (config,), _ = build(rec(), desc=desc, page=EXACT_PAGE)
    assert config["hardware_state"] == "missing"
    assert any("backplane" in w.lower() for w in config["warnings"])


def test_excluded_list_backplane_is_missing():
    desc = verified_description(COMPLETE_SELLER + "<p>Not included:</p><ul><li>Drive backplane</li><li>Rails</li></ul>")
    (config,), _ = build(rec(), desc=desc, page=EXACT_PAGE)
    assert config["hardware_state"] == "missing"


def test_missing_raid_controller_is_review_not_missing():
    desc = verified_description(COMPLETE_SELLER + "<p>No RAID controller</p>")
    (config,), _ = build(rec(), desc=desc, page=EXACT_PAGE)
    assert config["hardware_state"] == "review"
    assert any("controller" in w.lower() for w in config["warnings"])


def test_explicit_missing_essential_wins_over_damage_state():
    desc = verified_description(COMPLETE_SELLER + "<p>Power supplies not included</p><p>Cracked front panel</p>")
    (config,), _ = build(rec(), desc=desc, page=EXACT_PAGE)
    assert config["hardware_state"] == "missing"
    assert config["damage"] is True


def test_no_drives_is_not_missing_backplane():
    desc = verified_description(COMPLETE_SELLER + "<p>No drives or trays included</p>")
    assert desc["backplane"] is None
    (config,), _ = build(rec(), desc=desc, page=EXACT_PAGE)
    assert config["hardware_state"] == "confirmed"


# 6. Non-numeric page shipping clears index shipping -------------------------


def test_non_numeric_page_shipping_clears_index_quote():
    page = {**EXACT_PAGE, "shipping": None, "shipping_text": "Shipping: May not ship to your location"}
    (config,), _ = build(rec(shipping="0.00"), page=page)
    assert config["shipping_price"] is None
    assert any("Shipping unknown" in w for w in config["warnings"])
    assert "total unknown" in config["shipping_context"]


def test_non_numeric_page_shipping_clears_index_quote_for_variations():
    page = variant_page({"variant_id": "3001", "labels": {"Memory": "64GB"}, "price": "300.00"},
                        shipping_text="Local pickup only")
    (config,), _ = build(rec(shipping="0.00"), page=page)
    assert config["shipping_price"] is None


def test_numeric_page_shipping_still_applies():
    (config,), _ = build(rec(shipping="0.00"), page=EXACT_PAGE)
    assert str(config["shipping_price"]) == "25.00"


# 7. Bounded iterative markup traversal ---------------------------------------


def test_table_cells_keep_label_value_segments():
    assert html_segments("<table><tr><th>Memory</th><td>64GB DDR4</td></tr><tr><td>Drives</td><td>None</td></tr></table>") == [
        "Memory : 64GB DDR4", "Drives None",
    ]


def test_normal_nested_markup_still_parses():
    html = "<div>" * 60 + "<table><tr><th>Installed RAM</th><td>16GB</td></tr></table><script>RAM 512GB</script>" + "</div>" * 60
    assert parse_description(html)["ram_gb"] == 16


@pytest.mark.parametrize("tag", ["div", "span"])
def test_excessive_nesting_is_source_error(tag):
    html = f"<{tag}>" * 900 + "Installed RAM: 16GB" + f"</{tag}>" * 900
    with pytest.raises(SourceError):
        parse_description(html)
