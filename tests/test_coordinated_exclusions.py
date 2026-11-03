"""Shared negation in seller component lists must not become unknown hardware."""
import json
import subprocess
import sys

import pytest

from rackscout import domain
from rackscout.sources import parse_description


@pytest.mark.parametrize("text", [
    "Dell EMC PowerEdge R440 Xeon Silver 4208 2.10GHz 2x 1100W 1xCC No HDD, RAM, NC",
    "No HDD / memory",
    "No CPU, RAM and HDD",
    "Without drives, power supplies or RAM",
])
def test_shared_negation_excludes_ram_and_storage(text):
    claims = domain.parse_title(text)
    assert claims["ram_gb"] == 0
    assert claims["storage_state"] == "diskless"
    assert claims["ram_text"] == text


def test_drive_trays_in_exclusion_list_do_not_mean_no_storage():
    claims = domain.parse_title("No drive trays, RAM")
    assert claims["ram_gb"] == 0
    assert claims["storage_state"] == "unknown"


@pytest.mark.parametrize("text,ram", [
    ("No HDD, 64GB RAM", 64),
    ("No HDD, RAM included", None),
    ("No HDD, RAM upgrades available", None),
    ("Memory options: No HDD, RAM", None),
])
def test_shared_negation_stops_before_positive_or_optional_memory(text, ram):
    assert parse_description(f"<p>{text}</p>")["ram_gb"] == ram


@pytest.mark.parametrize("text", ["No HDD or RAM errors", "No CPU, HDD and RAM faults"])
def test_coordinated_diagnostic_status_is_not_missing_hardware(text):
    claims = domain.parse_title(text)
    assert claims["ram_gb"] is None
    assert claims["storage_state"] == "unknown"
    assert not claims["cpu_missing"]


@pytest.mark.parametrize("text", [
    "No HDD, RAM, or CPU errors",
    "No HDD, RAM or CPU-related errors",
])
def test_coordinated_status_does_not_backtrack_into_absence_claim(text):
    claims = domain.parse_title(text)
    assert claims["ram_gb"] is None
    assert claims["storage_state"] == "unknown"
    assert not claims["cpu_missing"]


def test_oxford_comma_preserves_last_missing_component():
    claims = domain.parse_title("No HDD, rails, and NIC")
    assert claims["storage_state"] == "diskless"
    assert claims["nic"] == "missing"
    assert "Rack rails not included" in claims["exclusions"]


@pytest.mark.parametrize("text,ram,storage", [
    ("No " + "HDD/SSD/" * 60 + "HDD X", None, "diskless"),
    ("No " + "RAM, " * 60 + "RAM X", 0, "unknown"),
], ids=["slash-aliases", "comma-spacing"])
def test_long_component_lists_finish_with_correct_absence_claim(text, ram, storage):
    result = subprocess.run(
        [sys.executable, "-c",
         "import json,sys; from rackscout.domain import parse_title; print(json.dumps(parse_title(sys.argv[1])))",
         text],
        capture_output=True, text=True, check=True, timeout=3,
    )
    claims = json.loads(result.stdout)
    assert claims["ram_gb"] == ram
    assert claims["storage_state"] == storage


@pytest.mark.parametrize("component", ["storage controller", "rail kits"])
def test_multiword_component_does_not_hide_later_missing_nic(component):
    claims = domain.parse_title(f"No HDD, {component}, NIC")
    assert claims["storage_state"] == "diskless"
    assert claims["nic"] == "missing"


@pytest.mark.parametrize("text,ram,storage", [
    ("No RAM, NIC tested with no errors", 0, "unknown"),
    ("No RAM, HDD included without errors", 0, "included"),
    ("No HDD, RAM installed with no problems", None, "diskless"),
])
def test_positive_component_status_does_not_erase_explicit_absence(text, ram, storage):
    claims = domain.parse_title(text)
    assert claims["ram_gb"] == ram
    assert claims["storage_state"] == storage


def test_selected_combined_memory_storage_exclusions_keep_diskless_state():
    facts = domain.interpret_variant({"Memory / Storage": "No RAM/HDD"})
    assert facts.ram == 0 and facts.storage == "diskless"
    assert facts.storage_unresolved is None


def test_selected_diagnostic_wording_is_not_diskless_evidence():
    facts = domain.interpret_variant({"Storage": "No HDD errors"})
    assert facts.storage is None
