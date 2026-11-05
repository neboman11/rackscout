"""CPU lookup must not confuse SKUs, physical cores, or populated sockets."""
import pytest
from fastapi.testclient import TestClient

from rackscout.api import app


@pytest.mark.parametrize("model,cores", [
    ("Gold 6150", 18),
    ("Intel(R) Xeon(R) Gold 6150 CPU @ 2.70GHz", 18),
    ("Silver 4110", 8),
    ("Bronze 3104", 6),
    ("E5-2680 v3", 12),
    ("E5-2680v4", 14),
])
def test_lookup_returns_physical_cores_and_total_for_populated_cpus(model, cores):
    with TestClient(app) as client:
        response = client.get("/api/cpu-specs", params={"cpu_model": model, "cpu_count": 2})
    assert response.status_code == 200
    result = response.json()
    assert result["match"]["cores_per_cpu"] == cores
    assert result["total_cores"] == cores * 2


def test_lookup_does_not_assume_chassis_socket_capacity_is_population():
    with TestClient(app) as client:
        result = client.get("/api/cpu-specs", params={"cpu_model": "Gold 6150"}).json()
    assert result["match"]["cores_per_cpu"] == 18
    assert result["total_cores"] is None


@pytest.mark.parametrize("model", ["i7-5960X", "Core i7-5960X", "Intel(R) Core(TM) i7-5960X CPU @ 3.00GHz"])
def test_core_brand_word_is_decoration_not_part_of_the_sku(model):
    with TestClient(app) as client:
        result = client.get("/api/cpu-specs", params={"cpu_model": model, "cpu_count": 1}).json()
    assert result["match"]["cpu_model"] == "i7-5960X"
    assert result["total_cores"] == 8


@pytest.mark.parametrize("model", ["Core i7-5960", "Core i7", "Core"])
def test_core_brand_word_alone_never_borrows_a_neighbor_sku(model):
    with TestClient(app) as client:
        result = client.get("/api/cpu-specs", params={"cpu_model": model, "cpu_count": 1}).json()
    assert result == {"match": None, "total_cores": None}



@pytest.mark.parametrize("model", [
    "Gold 6150 or Gold 6148", "Gold 6150 NOT-A-REAL-SUFFIX",
    "E5-2680 v99", "Unidentified CPU", "AMD Xeon Gold 6150",
    "2x Gold 6150", "AMD X5650", "AMD L5630", "AMD W-2125", "AMD E5520", "AMD i7-5960X",
    "Gold 6150 R", "Gold 6150 TM",
])
def test_lookup_leaves_ambiguous_unknown_or_incompatible_names_unmatched(model):
    with TestClient(app) as client:
        response = client.get("/api/cpu-specs", params={"cpu_model": model, "cpu_count": 2})
    assert response.status_code == 200
    assert response.json() == {"match": None, "total_cores": None}


@pytest.mark.parametrize("count", ["0", "9", "-1", "2.5", "unknown"])
def test_lookup_rejects_invalid_populated_cpu_counts(count):
    with TestClient(app) as client:
        response = client.get("/api/cpu-specs", params={"cpu_model": "Gold 6150", "cpu_count": count})
    assert response.status_code == 422


def test_lookup_rejects_unbounded_cpu_names():
    with TestClient(app) as client:
        response = client.get("/api/cpu-specs", params={"cpu_model": "X" * 101})
    assert response.status_code == 422
