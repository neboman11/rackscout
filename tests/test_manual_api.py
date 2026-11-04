"""Manual editing HTTP boundaries and persistent multi-configuration workflow."""
import json
from datetime import datetime, timezone

import pytest
from test_db import TRAP, FakeFetcher, conn, requires_db, settings, snapshot  # noqa: F401

ORIGIN = "http://rackscout.apartment"
HEADERS = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin", "X-RackScout-Edit": "1"}


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from rackscout import api

    monkeypatch.setenv("RACKSCOUT_EDIT_ORIGINS", ORIGIN)
    monkeypatch.setenv("RACKSCOUT_REFRESH_ORIGINS", ORIGIN)
    monkeypatch.setattr(api, "_settings", settings())
    return TestClient(api.app)


def body(revision=1, ram=32, price="220.00"):
    return {"revision": revision, "label": f"{ram}GB option", "notes": "Checked selected option",
            "price_confirmed": True,
            "fields": {"ram_gb": ram, "item_price": price, "shipping_price": "0.00",
                       "cpu_model": "Gold 6138", "cpu_count": 2, "drive_bays": 8,
                       "storage_state": "diskless", "psu": "present", "cooling": "present",
                       "nic": "present", "drive_path": "present"}}


def no_database(*args, **kwargs):
    raise AssertionError("Rejected request opened a database connection")


@pytest.mark.parametrize("headers", [
    {},
    {**HEADERS, "Origin": "https://untrusted.invalid"},
    {k: v for k, v in HEADERS.items() if k != "X-RackScout-Edit"},
    {**HEADERS, "Sec-Fetch-Site": "cross-site"},
])
def test_manual_write_rejects_untrusted_request_before_database(client, monkeypatch, headers):
    from rackscout import api

    monkeypatch.setattr(api.db, "connect", no_database)
    response = client.post("/api/listings/1/manual", headers=headers, json=body())
    assert response.status_code == 403


@pytest.mark.parametrize("change", [
    {"label": ""},
    {"revision": True},
    {"fields": {"ram_gb": True}},
    {"fields": {"item_price": "NaN"}},
    {"fields": {"item_price": "Infinity"}},
    {"fields": {"item_price": "1.234"}},
    {"fields": {"shipping_price": -1}},
    {"fields": {"origin": "source"}},
    {"notes": "bad\x00text"},
    {"label": "bad\ud800text"},
    {"source_configuration_id": 2},
    {"bad\ud800": "invalid top-level key"},
    {"fields": {"bad\ud800": 1}},
    {"bad\x00": "invalid top-level key"},
    {"fields": {"bad\x00": 1}},
])
def test_manual_invalid_payload_is_rejected_before_database(client, monkeypatch, change):
    from rackscout import api

    monkeypatch.setattr(api.db, "connect", no_database)
    response = client.post("/api/listings/1/manual", headers={**HEADERS, "Content-Type": "application/json"},
                           content=json.dumps({**body(), **change}).encode("ascii"))
    assert response.status_code == 422


def test_manual_body_limits_and_media_type_precede_database(client, monkeypatch):
    from rackscout import api

    monkeypatch.setattr(api.db, "connect", no_database)
    response = client.post("/api/listings/1/manual", headers={**HEADERS, "Content-Type": "text/plain"}, content="{}")
    assert response.status_code == 415
    response = client.post("/api/listings/1/manual", headers={**HEADERS, "Content-Type": "application/json"},
                           content=b" " * 32769)
    assert response.status_code == 413


def test_manual_disabled_origin_configuration_is_read_only(client, monkeypatch):
    from rackscout import api

    monkeypatch.delenv("RACKSCOUT_EDIT_ORIGINS", raising=False)
    monkeypatch.setattr(api, "_settings", settings())
    monkeypatch.setattr(api.db, "connect", no_database)
    assert client.post("/api/listings/1/manual", headers=HEADERS, json=body()).status_code == 403


@requires_db
def test_http_manual_variants_filter_independently_survive_refresh_and_reset(conn, client):
    from rackscout import collector

    now = datetime.now(timezone.utc)
    collector.run_discovery(conn, settings(), FakeFetcher(snapshot(now, [TRAP])))
    original_id = conn.execute("SELECT id FROM configurations WHERE variant_key = ''").fetchone()["id"]
    original = client.get(f"/api/listings/{original_id}").json()
    editor = client.get(f"/api/listings/{original_id}/manual")
    assert editor.status_code == 200
    assert editor.json()["enabled"] is True
    revision = editor.json()["revision"]
    created = []
    for ram, price in [(32, "220.00"), (64, "260.00")]:
        response = client.post(f"/api/listings/{original_id}/manual", headers=HEADERS,
                               json=body(revision, ram, price))
        assert response.status_code == 201, response.text
        item = response.json()["item"]
        assert item["ram_gb"] == ram and item["item_price"] == price
        assert item["manual"]["price_confirmed"] is True
        assert item["page_verification_current"] is False
        created.append(item)

    inventory = client.get("/api/listings", params={"max_total": "225"}).json()["items"]
    assert [i["id"] for i in inventory] == [created[0]["id"]]
    assert all(i["id"] != original_id for i in inventory)
    assert client.get(f"/api/listings/{original_id}").json()["ram_gb"] == original["ram_gb"]

    changed = list(TRAP)
    changed[2] = "19.00"
    collector.run_discovery(conn, settings(), FakeFetcher(snapshot(now, [tuple(changed)])))
    assert client.get(f"/api/listings/{created[0]['id']}").json()["item_price"] == "220.00"

    first_id = created[0]["id"]
    first_revision = created[0]["revision"]
    saved = client.patch(f"/api/listings/{first_id}/manual", headers=HEADERS,
                         json={**body(first_revision, 32, "225.00"), "notes": "Updated once"})
    assert saved.status_code == 200, saved.text
    stale = client.patch(f"/api/listings/{first_id}/manual", headers=HEADERS,
                         json=body(first_revision, 32, "1.00"))
    assert stale.status_code == 409
    assert client.get(f"/api/listings/{first_id}").json()["item_price"] == "225.00"
    source_delete = client.request("DELETE", f"/api/listings/{original_id}/manual", headers=HEADERS,
                                   json={"revision": revision})
    assert source_delete.status_code == 409
    for item in [saved.json()["item"], created[1]]:
        response = client.request("DELETE", f"/api/listings/{item['id']}/manual", headers=HEADERS,
                                  json={"revision": item["revision"]})
        assert response.status_code == 200, response.text
    inventory = client.get("/api/listings").json()["items"]
    assert [i["id"] for i in inventory] == [original_id]
    assert inventory[0]["item_price"] == "19.00"


@requires_db
def test_selected_refresh_endpoint_enqueues_only_selected_listing(conn, client):
    from rackscout import collector

    collector.run_discovery(conn, settings(), FakeFetcher(snapshot(datetime.now(timezone.utc), [TRAP])))
    cid = conn.execute("SELECT id FROM configurations WHERE variant_key = ''").fetchone()["id"]
    response = client.post(f"/api/listings/{cid}/refresh",
                           headers={"Origin": ORIGIN, "X-RackScout-Refresh": "1"}, json={})
    assert response.status_code == 202, response.text
    assert response.json()["scope"] == "listing"
    assert response.json()["target_item_id"] == TRAP[0]
    status = client.get("/api/refresh").json()
    assert status["request_id"] == response.json()["request_id"] and status["state"] == "queued"


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
def test_every_manual_mutation_checks_origin_before_database(client, monkeypatch, method):
    from rackscout import api

    monkeypatch.setattr(api.db, "connect", no_database)
    response = client.request(method, "/api/listings/1/manual",
                              headers={**HEADERS, "Origin": "http://untrusted.invalid"}, json=body())
    assert response.status_code == 403


@pytest.mark.parametrize("payload", [
    {}, {"revision": True}, {"revision": 1.0}, {"revision": "1"}, {"revision": None},
    {"revision": 0}, {"revision": -1}, {"revision": 2**63}, {"revision": 1, "extra": True},
])
def test_delete_requires_only_a_strict_positive_revision_before_database(client, monkeypatch, payload):
    from rackscout import api

    monkeypatch.setattr(api.db, "connect", no_database)
    assert client.request("DELETE", "/api/listings/1/manual", headers=HEADERS, json=payload).status_code == 422


@pytest.mark.parametrize("content", [
    b'{"label":"\xff"}', b"[" * 1100 + b"0" + b"]" * 1100,
    b'{"revision":' + b"9" * 5000 + b"}", b"null", b"[]", b'"text"',
])
def test_malformed_json_decoder_inputs_are_rejected_before_database(client, monkeypatch, content):
    from rackscout import api

    monkeypatch.setattr(api.db, "connect", no_database)
    response = client.post("/api/listings/1/manual",
                           headers={**HEADERS, "Content-Type": "application/json"}, content=content)
    assert response.status_code == 422


@pytest.mark.parametrize("path,limit,header", [
    ("/api/listings/1/manual", 32768, "X-RackScout-Edit"),
    ("/api/listings/1/refresh", 1024, "X-RackScout-Refresh"),
])
def test_streamed_body_without_declared_length_is_bounded(client, monkeypatch, path, limit, header):
    import asyncio
    import httpx
    from rackscout import api

    monkeypatch.setattr(api.db, "connect", no_database)

    async def run():
        async def chunks():
            yield b" " * (limit - 1)
            yield b"  "
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url=ORIGIN) as c:
            return await c.post(path, headers={"Origin": ORIGIN, header: "1", "Content-Type": "application/json"},
                                content=chunks())
    assert asyncio.run(run()).status_code == 413


@pytest.mark.parametrize("method,path", [
    ("PUT", "/api/listings/1/manual"), ("OPTIONS", "/api/listings/1/manual"),
    ("POST", "/api/listings/1/manual/"), ("POST", "/api/listings/1/manual/extra"),
    ("POST", "/api/listings/-1/manual"), ("POST", "/api/listings/١/manual"),
    ("PATCH", "/api/listings/1/refresh"), ("DELETE", "/api/listings/1/refresh"),
    ("POST", "/api/status"),
])
def test_mutation_method_and_path_allowlist_cannot_be_widened(client, monkeypatch, method, path):
    from rackscout import api

    monkeypatch.setattr(api.db, "connect", no_database)
    assert client.request(method, path, headers=HEADERS, json={}).status_code == 405


def test_edit_and_refresh_origin_permissions_are_independent(client, monkeypatch):
    from rackscout import api

    monkeypatch.setattr(api.db, "connect", no_database)
    monkeypatch.delenv("RACKSCOUT_REFRESH_ORIGINS", raising=False)
    monkeypatch.setattr(api, "_settings", settings())
    response = client.post("/api/listings/1/refresh",
                           headers={"Origin": ORIGIN, "X-RackScout-Refresh": "1"}, json={})
    assert response.status_code == 403
    monkeypatch.setenv("RACKSCOUT_REFRESH_ORIGINS", ORIGIN)
    monkeypatch.delenv("RACKSCOUT_EDIT_ORIGINS", raising=False)
    monkeypatch.setattr(api, "_settings", settings())
    assert client.post("/api/listings/1/manual", headers=HEADERS, json=body()).status_code == 403
