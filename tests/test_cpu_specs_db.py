"""Listing CPU reference totals: derived only for a known populated count, never replacing reported data."""
import pytest
from psycopg.types.json import Jsonb
from test_db import TRAP, conn, requires_db  # noqa: F401 - `conn` is a fixture
from test_manual_configurations import _create, _detail, _discover, _listed, _row, _source_id

pytestmark = requires_db


def _shape(conn, config_id, *, model="Gold 6150", model_state="confirmed", count=2, count_state="confirmed",
           cpu_essential="confirmed", total_cores=None):
    conn.execute(
        """UPDATE configurations SET cpu_model = %s, cpu_model_state = %s, cpu_count = %s, cpu_count_state = %s,
                  total_cores = %s, essential_states = essential_states || %s WHERE id = %s""",
        (model, model_state, count, count_state, total_cores, Jsonb({"cpu": cpu_essential}), config_id))


def _item(conn, config_id):
    return next(i for i in _listed(conn, min_ram=None, min_bays=None, diskless=False) if i["id"] == config_id)


@pytest.mark.parametrize("shape,inferred,has_spec", [
    ({}, 36, True),
    ({"count": 1, "count_state": "inferred"}, 18, True),
    ({"count": None, "count_state": "unknown"}, None, True),
    ({"count": 2, "count_state": "unknown"}, None, True),
    ({"count": 2, "count_state": "conflicting"}, None, True),
    ({"model_state": "conflicting"}, None, False),
    ({"cpu_essential": "missing"}, None, False),
    ({"model": "Gold 6150 NOT-A-REAL-SUFFIX"}, None, False),
])
def test_reference_total_needs_known_model_and_known_population(conn, shape, inferred, has_spec):
    _discover(conn, [TRAP])
    src = _source_id(conn, TRAP[0])
    _shape(conn, src, **shape)
    item = _item(conn, src)
    assert item["inferred_total_cores"] == inferred
    assert (item["cpu_spec"] or {}).get("cores_per_cpu") == (18 if has_spec else None)
    assert item["total_cores"] is None  # reference data is never materialized as a reported total


def test_reported_total_is_preserved_beside_a_different_reference_total(conn):
    _discover(conn, [TRAP])
    src = _source_id(conn, TRAP[0])
    _shape(conn, src, total_cores=42)
    item = _item(conn, src)
    assert item["total_cores"] == 42 and item["inferred_total_cores"] == 36
    assert _row(conn, src)["total_cores"] == 42


def test_manual_total_overrides_display_data_and_detail_matches_listing(conn):
    _discover(conn, [TRAP])
    src = _source_id(conn, TRAP[0])
    _shape(conn, src)
    manual_id = _create(conn, src, fields={"cpu_model": "Gold 6150", "cpu_count": 1, "total_cores": 42})
    listed = _item(conn, manual_id)
    detail = _detail(conn, manual_id)
    for item in (listed, detail):
        assert item["total_cores"] == 42 and item["inferred_total_cores"] == 18
        assert item["cpu_spec"]["cores_per_cpu"] == 18
    assert _row(conn, manual_id)["total_cores"] == 42


def test_manually_cleared_cpu_count_leaves_only_per_cpu_reference(conn):
    _discover(conn, [TRAP])
    src = _source_id(conn, TRAP[0])
    _shape(conn, src)
    manual_id = _create(conn, src, fields={"cpu_count": None})
    item = _item(conn, manual_id)
    assert item["inferred_total_cores"] is None and item["cpu_spec"]["cores_per_cpu"] == 18


def test_legacy_missing_hardware_without_structured_essentials_cannot_imply_installed_cores(conn):
    _discover(conn, [TRAP])
    src = _source_id(conn, TRAP[0])
    _shape(conn, src)
    conn.execute("UPDATE configurations SET essential_states = NULL, hardware_state = 'missing' WHERE id = %s", (src,))
    item = _detail(conn, src)
    assert item["inferred_total_cores"] is None
    assert item["total_cores"] is None
