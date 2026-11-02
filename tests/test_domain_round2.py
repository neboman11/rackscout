"""Round-2 regressions: status wording, damage negation, exclusion scope, combined variant labels, core totals."""
import pytest

from rackscout import domain
from rackscout.sources import html_segments, parse_description
from tests.test_domain import build, rec, verified_description

PLAIN_TITLE = "Dell PowerEdge R640 8SFF 2x Gold 6150 64GB"
VARIANT_TITLE = "Dell PowerEdge R640 8SFF 2x Gold 6130 16GB No HDD"


def variant_page(labels, price="399.00"):
    return {"status": "verified", "has_variations": True, "price_kind": "exact", "shipping": "0.00",
            "variations": [{"variant_id": "5001", "labels": labels, "price": price}]}


def variant_config(labels):
    (config,), _ = build(rec(title=VARIANT_TITLE, cpu="2× Gold 6130", cores=32, ram=16), page=variant_page(labels))
    return config


# 1. "No <component> errors" is health wording, not absent hardware ----------


def test_component_error_wording_is_not_missing_hardware():
    details = parse_description("<p>Fully tested, no memory errors, no fan errors, no PSU errors in iDRAC log</p>")
    assert details["ram_gb"] is None
    assert details["psu"] != "missing"
    assert details["cooling"] != "missing"


@pytest.mark.parametrize("html", [
    "<p>No drive errors in lifecycle log</p>",
    "<p>Hard drives: no issues reported</p>",
    "<p>Passed diagnostics without disk faults</p>",
])
def test_storage_error_wording_is_not_diskless(html):
    assert parse_description(html)["storage_state"] == "unknown"


@pytest.mark.parametrize("html", ["<p>No CPU errors</p>", "<p>Processor: no issues</p>"])
def test_cpu_error_wording_is_not_missing_cpu(html):
    assert parse_description(html)["cpu_missing"] is False


@pytest.mark.parametrize("html", ["<p>Memory: no errors</p>", "<p>RAM - no issues</p>", "<p>Zero memory faults logged</p>"])
def test_memory_value_status_wording_is_not_zero_ram(html):
    assert parse_description(html)["ram_gb"] is None


def test_error_wording_does_not_create_confirmed_diskless_match():
    desc = verified_description("<p>Tested. No drive errors in lifecycle log.</p>")
    page = {"status": "verified", "has_variations": False, "price": "300.00", "shipping": "0.00"}
    (config,), _ = build(rec(title=PLAIN_TITLE), desc=desc, page=page)
    assert config["storage_state"] == "unknown"
    assert config["storage_conf"] != "confirmed"


def test_real_negative_parts_survive_next_to_status_wording():
    assert parse_description("<p>No RAM, no issues</p>")["ram_gb"] == 0
    assert parse_description("<p>No RAM and no fan errors</p>")["ram_gb"] == 0
    assert parse_description("<p>No HDD. No errors in log</p>")["storage_state"] == "diskless"
    assert parse_description("<p>No CPU; no errors</p>")["cpu_missing"] is True
    details = parse_description("<p>Power supplies not included, no fan errors</p>")
    assert details["psu"] == "missing"
    assert details["cooling"] != "missing"


# 2. Clause-scoped damage negation ---------------------------------------------


@pytest.mark.parametrize("html", [
    "<p>No scratches or damage</p>",
    "<p>No dents, cracks or damage. Excellent condition</p>",
    "<p>Does not have any damage</p>",
    "<p>Never damaged</p>",
    "<p>Free of any dents or damage</p>",
    "<p>No damage, dents or cracks</p>",
])
def test_negated_damage_lists_are_not_damage(html):
    assert parse_description(html)["damage"] is None


@pytest.mark.parametrize("html", [
    "<p>No scratches; cracked rear panel</p>",
    "<p>Not damaged but bent ear on rail</p>",
    "<p>No issues, cracked bezel</p>",
    "<p>Excellent condition. Dent on lid</p>",
    "<p>Never used in production. Broken PSU latch</p>",
])
def test_separate_real_damage_clauses_still_count(html):
    assert parse_description(html)["damage"] is True


# 3. "Not included" scope ends at the list / heading / prose boundary ---------


def test_excluded_heading_stops_at_list_end_and_next_heading():
    details = parse_description(
        "<p>Not included:</p><ul><li>Rails</li></ul><p>Specifications</p><p>2x 750W PSU, 64GB RAM</p>"
    )
    assert details["ram_gb"] == 64
    assert details["psu"] == "present"
    assert "Rack rails not included" in details["exclusions"]


def test_excluded_heading_stops_before_prose():
    details = parse_description(
        "<p>Not included:</p><ul><li>Bezel</li></ul>"
        "<p>Server ships with 2x 750W power supplies and 64GB DDR4 memory installed.</p>"
    )
    assert details["ram_gb"] == 64
    assert details["psu"] == "present"
    assert "Front bezel not included" in details["exclusions"]


def test_what_is_not_included_heading_is_recognized():
    details = parse_description("<p>What is not included:</p><ul><li>Rails</li><li>Bezel</li></ul>")
    assert "Rack rails not included" in details["exclusions"]
    assert "Front bezel not included" in details["exclusions"]


def test_excluded_list_components_with_quantities_stay_missing():
    details = parse_description(
        "<p>Not included:</p><ul><li>2x 750W PSU</li><li>16GB RAM</li><li>Drive backplane</li></ul>"
    )
    assert details["psu"] == "missing"
    assert details["ram_gb"] == 0
    assert details["backplane"] == "missing"


def test_excluded_heading_line_group_in_same_block():
    details = parse_description("<p>Not included:<br>Rails<br>Power supplies</p><p>64GB DDR4 RAM</p>")
    assert details["psu"] == "missing"
    assert "Rack rails not included" in details["exclusions"]
    assert details["ram_gb"] == 64


def test_html_segments_public_shape_unchanged():
    assert html_segments("<p>Not included:</p><ul><li>Rails</li></ul>") == ["Not included:", "Rails"]


# 4. Combined variation labels resolve every touched dimension -----------------


def test_cpu_ram_label_resolves_both_and_drops_index_cores():
    config = variant_config({"CPU/RAM": "2x Gold 6148 / 128GB"})
    assert config["cpu_model"] == "Gold 6148" and config["cpu_count"] == 2
    assert config["ram_gb"] == 128
    assert config["total_cores"] is None


def test_ram_storage_label_resolves_storage():
    config = variant_config({"RAM/Storage": "64GB / 2x 1.92TB SSD"})
    assert config["ram_gb"] == 64
    assert config["storage_state"] == "included"


def test_cpu_ram_storage_label_resolves_all():
    config = variant_config({"CPU/RAM/Storage": "2x Gold 6148 / 128GB / No HDD"})
    assert config["cpu_model"] == "Gold 6148"
    assert config["ram_gb"] == 128
    assert config["storage_state"] == "diskless" and config["storage_conf"] == "confirmed"


def test_combined_label_does_not_read_ssd_capacity_as_ram():
    config = variant_config({"Memory & Storage": "SSD480GB / 32GB RAM"})
    assert config["ram_gb"] == 32
    assert config["storage_state"] == "included"


def test_ambiguous_combined_label_is_unknown_not_inherited():
    config = variant_config({"RAM/Storage": "128GB 480GB"})
    assert config["ram_gb"] is None and config["ram_state"] == "unknown"
    assert config["storage_state"] == "unknown"


def test_unreadable_combined_label_is_unknown_for_every_dimension():
    config = variant_config({"CPU/RAM": "Option A"})
    assert config["cpu_model"] is None and config["cpu_count"] is None
    assert config["ram_gb"] is None


def test_single_field_variant_labels_still_inherit_other_dimensions():
    config = variant_config({"Memory": "64GB"})
    assert config["ram_gb"] == 64
    assert config["cpu_model"] == "Gold 6130"
    bays = variant_config({"Drive Bays": "10 SFF"})
    assert bays["drive_bays"] == 10
    assert bays["storage_state"] == "diskless"
    storage = variant_config({"Hard Drive": "No HDD"})
    assert storage["storage_state"] == "diskless" and storage["ram_gb"] == 16


# 5. Index core totals require the index CPU to match the resolved CPU ---------


def test_title_count_different_from_countless_index_drops_cores():
    (config,), _ = build(rec(title="Dell R640 8SFF Dual Gold 6138 64GB", cpu="Gold 6138", cores=20))
    assert config["cpu_count"] == 2
    assert config["total_cores"] is None


def test_seller_count_with_countless_index_drops_cores():
    desc = verified_description("<p>1x Intel Xeon Gold 6138</p>")
    (config,), _ = build(rec(title="Dell R640 8SFF Gold 6138 64GB", cpu="Gold 6138", cores=40), desc=desc)
    assert config["cpu_count"] == 1
    assert config["total_cores"] is None


def test_matching_index_count_and_model_keeps_cores():
    (config,), _ = build(rec())
    assert config["cpu_count"] == 2 and config["cpu_model"] == "Gold 6150"
    assert config["total_cores"] == 36


def test_positive_included_statement_ends_plain_paragraph_exclusions():
    details = parse_description("<p>Not included:</p><p>Rails</p><p>64GB RAM included</p>")
    assert details["ram_gb"] == 64
    assert "Rack rails not included" in details["exclusions"]


@pytest.mark.parametrize(("labels", "field", "expected"), [
    ({"CPU": "No CPU errors"}, "cpu_count", None),
    ({"Storage": "No drive errors"}, "storage_state", "unknown"),
])
def test_variation_status_wording_is_not_absent_hardware(labels, field, expected):
    config = variant_config(labels)
    assert config[field] == expected
    assert config["hardware_state"] != "missing"


@pytest.mark.parametrize("prose", [
    "This server has 64GB DDR4 RAM and 2x 750W PSUs.",
    "We tested this server with 64GB DDR4 RAM and 2x 750W PSUs.",
])
def test_narrative_prose_ends_plain_paragraph_exclusions(prose):
    details = parse_description(f"<p>Not included:</p><p>Rails</p><p>{prose}</p>")
    assert details["ram_gb"] == 64
    assert details["psu"] == "present"
    assert "Rack rails not included" in details["exclusions"]
