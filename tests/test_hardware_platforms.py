"""CPU manufacturer / architecture / memory-platform references: exact SKUs only, cited, never guessed."""
import re
from datetime import datetime

import pytest

from rackscout import cpu_specs, hardware_platforms

MANUFACTURERS = {"Intel", "AMD"}
MEMORY_PLATFORMS = {"DDR3", "DDR4", "DDR5"}
RAPTOR_LAKE_XEON_E = ["E-2414", "E-2434", "E-2436", "E-2456", "E-2468", "E-2478", "E-2488"]


def _ref(model):
    found = hardware_platforms.lookup(model)
    assert found is not None, model
    return found["manufacturer"], found["architecture"], found["memory_platform"]


@pytest.mark.parametrize("model,expected", [
    ("Gold 6130", ("Intel", "Skylake", "DDR4")),
    ("Gold 6230", ("Intel", "Cascade Lake", "DDR4")),
    ("Silver 4314", ("Intel", "Ice Lake", "DDR4")),
    ("Gold 6430", ("Intel", "Sapphire Rapids", "DDR5")),
    ("Gold 6530", ("Intel", "Emerald Rapids", "DDR5")),
    ("E5-2680 v2", ("Intel", "Ivy Bridge", "DDR3")),
    ("E5-2680 v3", ("Intel", "Haswell", "DDR4")),
    ("E5-2680 v4", ("Intel", "Broadwell", "DDR4")),
    ("E5-2670 0", ("Intel", "Sandy Bridge", "DDR3")),
    ("X5650", ("Intel", "Westmere", "DDR3")),
    ("EPYC 7551", ("AMD", "Naples", "DDR4")),
    ("EPYC 7402", ("AMD", "Rome", "DDR4")),
    ("EPYC 7402P", ("AMD", "Rome", "DDR4")),
    ("EPYC 7443", ("AMD", "Milan", "DDR4")),
    ("EPYC 7443P", ("AMD", "Milan", "DDR4")),
    ("EPYC 9654", ("AMD", "Genoa", "DDR5")),
    ("EPYC 9655", ("AMD", "Turin", "DDR5")),
    ("EPYC 7251", ("AMD", "Naples", "DDR4")),
    ("Opteron 6376", ("AMD", "Abu Dhabi", None)),  # no primary memory-type source verified
])
def test_mainstream_server_families_resolve_to_exact_references(model, expected):
    assert _ref(model) == expected


@pytest.mark.parametrize("model", RAPTOR_LAKE_XEON_E)
def test_raptor_lake_xeon_e2400_models_are_ddr5_raptor_lake(model):
    assert _ref(f"Intel Xeon {model}") == ("Intel", "Raptor Lake", "DDR5")


@pytest.mark.parametrize("model,architecture", [
    ("Core i9-13900K", "Raptor Lake"),  # supports both DDR4 and DDR5
    ("E3-1270 v6", "Kaby Lake"),        # supports both DDR4 and DDR3L
])
def test_platforms_supporting_two_ddr_generations_have_no_single_memory_platform(model, architecture):
    assert _ref(model) == ("Intel", architecture, None)


@pytest.mark.parametrize("model,expected", [
    ("EPYC 4564P", ("AMD", "Raphael", "DDR5")),  # upstream mislabels it Genoa
    ("EPYC 7532", ("AMD", "Rome", "DDR4")),      # upstream mislabels it Milan
    ("X5560", ("Intel", "Nehalem", "DDR3")),     # upstream mislabels it Westmere-EP
])
def test_known_upstream_codename_errors_are_corrected(model, expected):
    assert _ref(model) == expected


@pytest.mark.parametrize("model,architecture", [
    ("W-2125", "Skylake"),               # Skylake-W
    ("E5-2407 v2", "Ivy Bridge"),        # Ivy Bridge-EN
    ("E5-2650 v2", "Ivy Bridge"),        # Ivy Bridge-EP
    ("E5-2450", "Sandy Bridge"),         # Sandy Bridge-EN
    ("E5520", "Nehalem"),                # Nehalem-EP
    ("i7-5960X", "Haswell"),             # Haswell-E
    ("EPYC 7773X", "Milan"),             # Milan-X
    ("Xeon Max 9480", "Sapphire Rapids"),  # Sapphire Rapids HBM
])
def test_architecture_labels_are_broad_families(model, architecture):
    assert _ref(model)[1] == architecture


def test_distinct_families_are_not_merged():
    assert _ref("EPYC 7402")[1] != _ref("EPYC 7443")[1]
    assert _ref("Gold 6130")[1] != _ref("Gold 6230")[1]
    assert _ref("EPYC 9654")[1] != _ref("EPYC 9655")[1]


@pytest.mark.parametrize("text", [
    "Intel(R) Xeon(R) Gold 6130 CPU @ 2.10GHz",
    "Intel Xeon Gold 6130",
    "Xeon® Gold 6130",
    "gold 6130",
])
def test_decorated_names_resolve_to_the_same_exact_row(text):
    found = hardware_platforms.lookup(text)
    assert found["cpu_model"] == "Gold 6130"
    assert hardware_platforms.reference_key(text) == "gold 6130"


@pytest.mark.parametrize("text", [
    None, "", "   ", "Unidentified CPU",
    "Gold 6130 or Gold 6230", "Gold 6130/6230", "2x Gold 6130",
    "Gold 6131", "Gold 613", "Gold 61", "Gold 6130X", "Gold 6130 NOT-A-REAL-SUFFIX",
    "EPYC 740", "EPYC 7402PX", "EPYC 74", "E-24", "E-2434X", "Core i9-13900",
    "AMD Gold 6130", "AMD X5650", "AMD E-2434", "Intel EPYC 7402",
])
def test_unknown_ambiguous_or_contradictory_names_never_borrow_a_nearby_sku(text):
    assert hardware_platforms.lookup(text) is None
    assert hardware_platforms.reference_key(text) is None


def test_suffix_variants_are_distinct_exact_rows():
    assert hardware_platforms.reference_key("EPYC 7402") == "epyc 7402"
    assert hardware_platforms.reference_key("EPYC 7402P") == "epyc 7402p"
    assert hardware_platforms.lookup("Gold 6230R")["cpu_model"] == "Gold 6230R"


def test_references_have_exact_canonical_keys_and_supported_labels():
    rows = hardware_platforms.references()
    assert rows
    keys = [r["cpu_key"] for r in rows]
    assert len(keys) == len(set(keys))
    for row in rows:
        assert set(row) == {"cpu_key", "cpu_model", "manufacturer", "architecture", "memory_platform",
                            "source_url", "provider", "retrieved_at"}
        assert row["cpu_key"] == cpu_specs.canonical_key(row["cpu_model"])
        assert row["manufacturer"] in MANUFACTURERS
        assert isinstance(row["architecture"], str) and row["architecture"].strip() == row["architecture"] != ""
        assert not re.search(r"-(?:EP|EN|EX|SP|AP|W|E|X)\b|\bHBM\b", row["architecture"]), row
        assert row["memory_platform"] in MEMORY_PLATFORMS | {None}


def test_every_reference_cites_its_source():
    for row in hardware_platforms.references():
        assert row["source_url"].startswith("https://"), row
        assert row["provider"], row
        datetime.fromisoformat(row["retrieved_at"].replace("Z", "+00:00"))


def test_every_core_count_catalog_cpu_has_a_platform_reference():
    catalog_keys = {cpu_specs.canonical_key(c["cpu_model"]) for c in cpu_specs._catalog().values()}
    reference_keys = {r["cpu_key"] for r in hardware_platforms.references()}
    assert catalog_keys <= reference_keys
