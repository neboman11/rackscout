"""Consumer-visible review regressions for OEM page identity and mixed server lots."""
from datetime import datetime, timedelta, timezone

import pytest
from test_db import conn, requires_db, settings  # noqa: F401 - imported fixture
from test_multi_oem_db import (
    DELL_R640, HPE_DL360_G10, PAGES, PageFetcher, by_vendor, discover, page_snapshot,
)


@pytest.mark.parametrize("title", [
    "Supermicro SYS-1029U-TR4 / 2028U-E1CNRT+ lot 64GB No HDD",
    "Supermicro 2028U-E1CNRT+ and SYS-1029U-TR4 servers",
])
def test_mixed_prefixed_and_bare_supermicro_systems_stay_unknown(title):
    from rackscout.domain import normalize_model

    assert normalize_model("Supermicro", "Supermicro", title) is None


def test_repeated_prefixed_and_bare_alias_is_still_one_supermicro_system():
    from rackscout.domain import normalize_model

    assert normalize_model("Supermicro", "Supermicro", "SYS-1029U-TR4 / 1029U-TR4") == "Supermicro SYS-1029U-TR4"


@requires_db
def test_wrong_vendor_payload_never_archives_or_refreshes_last_good_vendor(conn):
    from rackscout import collector, queries

    old = datetime.now(timezone.utc) - timedelta(days=10)
    assert discover(conn, [DELL_R640, HPE_DL360_G10], when=old) == "success"
    before = next(s for s in queries.status(conn)["sources"] if s["vendor"] == "HPE")
    results = by_vendor(datetime.now(timezone.utc), [DELL_R640])
    results[PAGES["HPE"]] = page_snapshot(datetime.now(timezone.utc), [DELL_R640])

    assert collector.run_discovery(conn, settings(), PageFetcher(results)) == "partial"
    after = next(s for s in queries.status(conn)["sources"] if s["vendor"] == "HPE")
    assert after["state"] == "failed"
    assert after["source_updated_at"] == before["source_updated_at"] and after["stale"] is True
    assert conn.execute("SELECT archived_at FROM listings WHERE item_id = %s", (HPE_DL360_G10[0],)).fetchone()["archived_at"] is None


@requires_db
def test_mixed_vendor_payload_updates_valid_rows_without_archiving_absence(conn):
    from rackscout import collector, queries

    old = datetime.now(timezone.utc) - timedelta(days=10)
    assert discover(conn, [HPE_DL360_G10], when=old) == "success"
    new_hpe = ("200000000020", *HPE_DL360_G10[1:])
    results = by_vendor(datetime.now(timezone.utc), [])
    results[PAGES["HPE"]] = page_snapshot(datetime.now(timezone.utc), [new_hpe, DELL_R640])

    assert collector.run_discovery(conn, settings(), PageFetcher(results)) == "partial"
    hpe = next(s for s in queries.status(conn)["sources"] if s["vendor"] == "HPE")
    assert hpe["state"] == "partial"
    rows = {r["item_id"]: r["archived_at"] for r in conn.execute("SELECT item_id, archived_at FROM listings")}
    assert rows == {HPE_DL360_G10[0]: None, new_hpe[0]: None}
