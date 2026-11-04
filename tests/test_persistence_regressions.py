"""Persistence/query regressions: hard exclusions, missing essentials, verification
freshness, partial snapshots and test-database isolation.

Database tests share the isolated UUID-schema fixture from ``test_db``.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from test_db import (  # noqa: F401 - `conn` is a fixture
    TRAP,
    TRAP_VARIATIONS,
    UNKNOWN_SHIP,
    FakeFetcher,
    conn,
    dedicated_test_dsn,
    recompute_listing,
    requires_db,
    settings,
    snapshot,
    verified_page,
)

PERMISSIVE = dict(generation=None, height=None, min_ram=None, min_bays=None, diskless=False)


# ---------------------------------------------------------------------------
# Test database DSN validation (no database needed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "   ", " \t\n "])
def test_blank_test_dsn_means_unset_never_ambient_fallback(raw):
    assert dedicated_test_dsn(raw) is None


@pytest.mark.parametrize("raw", [
    "not a dsn at all",
    "dbname=rackscout_test",  # host would come from ambient PG* settings
    "postgresql://postgres:secret@127.0.0.1:55439",  # database would come from ambient settings
    "postgresql://postgres:secret@127.0.0.1:55439/rackscout",  # production-like name
    "postgresql://postgres:secret@127.0.0.1:55439/contest",
    "host=127.0.0.1 port=55439 dbname=postgres",
])
def test_unsafe_test_dsn_is_rejected_before_connecting(raw):
    with pytest.raises(ValueError):
        dedicated_test_dsn(raw)


def test_explicit_dedicated_test_dsn_is_stripped():
    dsn = "postgres://postgres:rackscout-local-dev@127.0.0.1:55439/rackscout_test"
    assert dedicated_test_dsn(f"  {dsn}\n") == dsn
    assert dedicated_test_dsn("host=127.0.0.1 port=55439 dbname=rackscout_test user=postgres") is not None


@requires_db
def test_fixture_runs_in_isolated_schema_and_leaves_public_alone(conn):
    schema = conn.execute("SELECT current_schema() AS s").fetchone()["s"]
    assert schema.startswith("rackscout_test_") and schema != "public"
    assert conn.execute("SELECT to_regnamespace('public') IS NOT NULL AS ok").fetchone()["ok"]
    tables = conn.execute(
        "SELECT table_schema FROM information_schema.tables WHERE table_name = 'listings' AND table_schema = %s",
        (schema,),
    ).fetchall()
    assert len(tables) == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _discover(conn, items, when=None):
    from rackscout import collector

    when = when or datetime.now(timezone.utc)
    assert collector.run_discovery(conn, settings(), FakeFetcher(snapshot(when, items))) == "success"


def _set_page(conn, item_id, page, *, status="verified", checked_at=None):
    from rackscout import collector

    conn.execute(
        "UPDATE listings SET page_status = %s, page_claims = %s, page_checked_at = %s WHERE item_id = %s",
        (status, collector._json(page), checked_at, item_id),
    )


def _set_description(conn, item_id, claims):
    from rackscout import collector

    conn.execute(
        "UPDATE listings SET description_status = 'verified', description_checked_at = now(), description_claims = %s"
        " WHERE item_id = %s",
        (collector._json({"status": "verified", "checked_at": datetime.now(timezone.utc).isoformat(), **claims}), item_id),
    )


def _items(conn, item_id, **filters):
    from rackscout import queries

    result = queries.query_listings(conn, queries.Filters(**filters))
    return [i for i in result["items"] if i["item_id"] == item_id]


def _variant(conn, item_id, variant_id, **filters):
    found = [i for i in _items(conn, item_id, **filters) if i["variant_id"] == variant_id]
    assert len(found) <= 1
    return found[0] if found else None


class DescriptionFetcher:
    def __init__(self):
        self.calls = []

    def fetch_description(self, item_id, max_bytes):
        self.calls.append(item_id)
        return {"ram_gb": None, "exclusions": []}


def _fake_checker(result=None, error=None):
    class FakeChecker:
        started = True

        def __init__(self, *args, **kwargs):
            self.checked = []

        def check(self, item_id):
            self.checked.append(item_id)
            if error is not None:
                raise error
            return dict(result)

        def close(self):
            pass

    return FakeChecker


def _fresh_trap_match(conn):
    _discover(conn, [TRAP])
    now = datetime.now(timezone.utc)
    _set_page(conn, TRAP[0], verified_page(now), checked_at=now)
    recompute_listing(conn, TRAP[0])
    match = _variant(conn, TRAP[0], "2")
    assert match is not None and match["match_state"] == "match"  # precondition
    return match


# ---------------------------------------------------------------------------
# Finding 8: hard exclusions apply to matches and leads alike
# ---------------------------------------------------------------------------


@requires_db
def test_hide_damaged_removes_damaged_exact_match_not_only_leads(conn):
    _fresh_trap_match(conn)
    _set_description(conn, TRAP[0], {"damage_text": "Front bezel cracked and rack ear bent"})
    recompute_listing(conn, TRAP[0])
    stored = conn.execute(
        "SELECT bool_or(c.damage) AS damaged FROM configurations c JOIN listings l ON l.id = c.listing_id"
        " WHERE l.item_id = %s AND c.active", (TRAP[0],),
    ).fetchone()
    assert stored["damaged"] is True  # precondition: damage recorded

    assert _items(conn, TRAP[0]) == []  # default hide_damaged: neither match nor lead
    assert _items(conn, TRAP[0], **PERMISSIVE) == []
    shown = _variant(conn, TRAP[0], "2", hide_damaged=False)
    assert shown is not None and shown["damage"] is True


@requires_db
@pytest.mark.parametrize("claims", [
    {"cpu_missing": True, "cpu_text": "Processors not included"},
    {"psu": "missing", "psu_text": "Power supplies not included"},
    {"psu": "missing", "psu_text": "Power supplies not included", "damage_text": "Cracked bezel"},
], ids=["cpu", "psu", "psu-and-damaged"])
def test_explicitly_missing_essentials_are_neither_matches_nor_leads(conn, claims):
    from rackscout import queries

    match = _fresh_trap_match(conn)
    _discover(conn, [TRAP, UNKNOWN_SHIP])
    assert _items(conn, UNKNOWN_SHIP[0], min_bays=None, diskless=False)  # precondition: index-only lead
    for item_id in (TRAP[0], UNKNOWN_SHIP[0]):
        _set_description(conn, item_id, claims)
        recompute_listing(conn, item_id)

    for item_id in (TRAP[0], UNKNOWN_SHIP[0]):
        assert _items(conn, item_id) == []
        assert _items(conn, item_id, min_bays=None, diskless=False) == []
        assert _items(conn, item_id, hide_damaged=False, **PERMISSIVE) == []
    # Detail stays reachable and explains why it is excluded.
    detail = queries.get_listing(conn, match["id"])
    assert detail is not None
    assert detail["match_state"] == "lead" and detail["hardware_state"] == "missing"


# ---------------------------------------------------------------------------
# Finding 9: page verification freshness is separate from latest attempts
# ---------------------------------------------------------------------------


@requires_db
def test_page_verification_older_than_cooldown_is_stale_lead_not_match(conn):
    _discover(conn, [TRAP])
    old = datetime.now(timezone.utc) - timedelta(hours=7)
    _set_page(conn, TRAP[0], verified_page(old), checked_at=old)
    recompute_listing(conn, TRAP[0])

    item = _variant(conn, TRAP[0], "2")
    assert item is not None, "old verified variant must stay visible"
    assert item["match_state"] == "lead"
    assert item["item_price"] == "429.88"
    assert item["page_verified_at"] is not None and item["page_verified_at"].startswith(old.isoformat()[:19])
    assert any("not reverified" in w for w in item["warnings"])


@requires_db
def test_page_staleness_is_evaluated_at_query_time_without_recompute(conn):
    _discover(conn, [TRAP])
    checked = datetime.now(timezone.utc) - timedelta(hours=1)
    # Verification window already closed although no collector run has recomputed since.
    _set_page(conn, TRAP[0], verified_page(checked, fresh_hours=0.5), checked_at=checked)
    recompute_listing(conn, TRAP[0])
    item = _variant(conn, TRAP[0], "2")
    assert item is not None and item["match_state"] == "lead"


@requires_db
def test_browser_disabled_enrichment_cannot_refresh_old_page_verification(conn):
    from rackscout import collector

    _discover(conn, [TRAP])
    old = datetime.now(timezone.utc) - timedelta(hours=7)
    _set_page(conn, TRAP[0], verified_page(old), checked_at=old)
    recompute_listing(conn, TRAP[0])

    fetcher = DescriptionFetcher()
    collector.run_enrichment(conn, settings(), fetcher, 5, browser_enabled=False)
    assert fetcher.calls == [TRAP[0]]

    row = conn.execute("SELECT page_claims, page_status, page_checked_at, last_checked_at FROM listings WHERE item_id = %s",
                       (TRAP[0],)).fetchone()
    assert row["page_claims"]["checked_at"] == old.isoformat()
    assert row["page_checked_at"] == old
    assert row["last_checked_at"] is not None
    item = _variant(conn, TRAP[0], "2")
    assert item is not None and item["match_state"] == "lead"
    assert item["page_verified_at"].startswith(old.isoformat()[:19])
    assert item["last_checked_at"] is not None


@requires_db
def test_failed_page_check_keeps_old_variants_visible_but_stale(conn, monkeypatch):
    from rackscout import browser, collector
    from rackscout.sources import SourceError

    match = _fresh_trap_match(conn)
    verified_at = conn.execute("SELECT page_claims->>'checked_at' AS t FROM listings WHERE item_id = %s",
                               (TRAP[0],)).fetchone()["t"]
    monkeypatch.setattr(browser, "BrowserChecker", _fake_checker(error=SourceError("page layout changed")))
    collector.run_enrichment(conn, settings(), DescriptionFetcher(), 5, browser_enabled=True)

    row = conn.execute("SELECT page_claims, page_status, page_checked_at, detail_status FROM listings WHERE item_id = %s",
                       (TRAP[0],)).fetchone()
    assert row["page_status"] == "failed"
    assert row["page_claims"]["checked_at"] == verified_at  # success date not refreshed by failure
    assert row["page_checked_at"] > datetime.fromisoformat(verified_at)  # latest attempt recorded separately
    assert row["detail_status"] == "failed"

    item = _variant(conn, TRAP[0], "2")
    assert item is not None and item["id"] == match["id"]
    assert item["match_state"] == "lead"
    assert item["page_verified_at"].startswith(verified_at[:19])
    assert any("not reverified" in w for w in item["warnings"])
    history = conn.execute("SELECT count(*) AS n FROM price_history WHERE configuration_id = %s", (match["id"],)).fetchone()
    assert history["n"] >= 1


@requires_db
def test_successful_page_check_records_verification_window(conn, monkeypatch):
    from rackscout import browser, collector

    _discover(conn, [TRAP])
    result = {"status": "verified", "has_variations": True, "price_kind": "exact", "variations": TRAP_VARIATIONS}
    monkeypatch.setattr(browser, "BrowserChecker", _fake_checker(result=result))
    before = datetime.now(timezone.utc)
    collector.run_enrichment(conn, settings(), DescriptionFetcher(), 5, browser_enabled=True)

    page = conn.execute("SELECT page_claims FROM listings WHERE item_id = %s", (TRAP[0],)).fetchone()["page_claims"]
    checked = datetime.fromisoformat(page["checked_at"])
    assert checked >= before
    assert datetime.fromisoformat(page["fresh_until"]) == checked + timedelta(hours=settings().page_verification_hours)
    item = _variant(conn, TRAP[0], "2")
    assert item is not None and item["match_state"] == "match"
    assert item["page_verified_at"] is not None
    assert not any("not reverified" in w for w in item["warnings"])


# ---------------------------------------------------------------------------
# Finding 11: snapshots with rejected records never archive by absence
# ---------------------------------------------------------------------------


@requires_db
def test_partial_snapshot_updates_present_rows_but_never_archives_absence(conn):
    from rackscout import collector, queries

    t0 = datetime.now(timezone.utc) - timedelta(days=10)
    _discover(conn, [TRAP, UNKNOWN_SHIP], when=t0)
    archived = lambda: {r["item_id"] for r in conn.execute("SELECT item_id FROM listings WHERE archived_at IS NOT NULL")}

    partial = replace(snapshot(t0 + timedelta(days=8), [replace_price(TRAP, "250.00")]), rejected=1)
    assert collector.run_discovery(conn, settings(), FakeFetcher(partial)) == "partial"
    assert archived() == set()
    trap = conn.execute("SELECT source_record, last_seen_source_at FROM listings WHERE item_id = %s", (TRAP[0],)).fetchone()
    assert trap["source_record"]["item_price"] == "250.00"  # positive updates applied
    assert trap["last_seen_source_at"] == t0 + timedelta(days=8)
    status = queries.status(conn)
    assert status["discovery"]["state"] == "partial"
    assert "rejected" in status["discovery"]["message"]
    assert status["source_updated_at"].startswith((t0 + timedelta(days=8)).isoformat()[:19])

    older = snapshot(t0 + timedelta(days=7), [TRAP, UNKNOWN_SHIP])
    assert collector.run_discovery(conn, settings(), FakeFetcher(older)) == "failed"  # older than applied partial

    complete = snapshot(t0 + timedelta(days=9), [TRAP])
    assert collector.run_discovery(conn, settings(), FakeFetcher(complete)) == "success"
    assert archived() == {UNKNOWN_SHIP[0]}


def replace_price(item, price):
    return (item[0], item[1], price, *item[3:])


# ---------------------------------------------------------------------------
# Offer contract: price/configuration pairing is derived at read time
# ---------------------------------------------------------------------------

def _offer(conn, item_id, variant_id=None):
    found = [i for i in _items(conn, item_id, hide_damaged=False, **PERMISSIVE) if i["variant_id"] == variant_id]
    assert len(found) == 1
    return found[0]


def _titled(item, title):
    return (item[0], title, *item[2:])


def _options_page(now, **over):
    """Verified page that has variations the parser could not read: only a base/range quote exists."""
    return {**verified_page(now, variations=[]), **over}


@requires_db
def test_index_only_quote_is_never_paired_with_advertised_ram(conn):
    _discover(conn, [TRAP])  # title advertises 64GB, price is the cheapest/base quote
    item = _offer(conn, TRAP[0])
    assert item["offer"]["kind"] == "unverified"
    assert item["offer"]["specification_price_paired"] is False
    assert item["offer"]["price_is_starting"] is True
    assert item["offer"]["evidence"] == []
    # raw server facts and provenance are not rewritten
    assert item["ram_gb"] == 64
    assert item["price_kind"] == "base" and item["item_price"] == "268.88"


@requires_db
def test_current_exact_variants_are_paired_and_keep_their_own_price(conn):
    _discover(conn, [TRAP])
    now = datetime.now(timezone.utc)
    variations = [{"variant_id": "2", "labels": {"Memory": "32GB"}, "price": "429.88"},
                  {"variant_id": "3", "labels": {"Memory": "64GB"}, "price": "519.88"}]
    _set_page(conn, TRAP[0], verified_page(now, variations=variations), checked_at=now)
    recompute_listing(conn, TRAP[0])
    for variant_id, price in (("2", "429.88"), ("3", "519.88")):
        item = _offer(conn, TRAP[0], variant_id)
        assert item["offer"]["kind"] == "configuration"
        assert item["offer"]["specification_price_paired"] is True
        assert item["offer"]["price_is_starting"] is False
        assert item["item_price"] == price and item["variant_label"]


@requires_db
def test_stale_exact_variant_is_historical_not_a_starting_price(conn):
    _discover(conn, [TRAP])
    old = datetime.now(timezone.utc) - timedelta(hours=7)
    _set_page(conn, TRAP[0], verified_page(old), checked_at=old)
    recompute_listing(conn, TRAP[0])
    item = _offer(conn, TRAP[0], "2")
    assert item["offer"]["kind"] == "historical_configuration"
    assert item["offer"]["specification_price_paired"] is False
    assert item["offer"]["price_is_starting"] is False
    assert item["item_price"] == "429.88" and item["variant_label"]


@requires_db
def test_current_exact_single_page_quote_is_configuration(conn):
    _discover(conn, [TRAP])
    now = datetime.now(timezone.utc)
    _set_page(conn, TRAP[0], {**verified_page(now, variations=[]), "has_variations": False, "price": "268.88"},
              checked_at=now)
    recompute_listing(conn, TRAP[0])
    item = _offer(conn, TRAP[0])
    assert item["price_kind"] == "exact"
    assert item["offer"]["kind"] == "configuration"
    assert item["offer"]["specification_price_paired"] is True
    assert item["offer"]["price_is_starting"] is False


@requires_db
def test_page_with_unreadable_variations_is_multiple_options_with_starting_price(conn):
    _discover(conn, [TRAP])
    now = datetime.now(timezone.utc)
    _set_page(conn, TRAP[0], _options_page(now), checked_at=now)
    recompute_listing(conn, TRAP[0])
    item = _offer(conn, TRAP[0])
    assert item["price_kind"] in ("base", "range")
    assert item["offer"]["kind"] == "multiple_options"
    assert item["offer"]["price_is_starting"] is True
    assert item["offer"]["specification_price_paired"] is False
    assert item["offer"]["evidence"] and all(isinstance(e, str) and len(e) <= 200 for e in item["offer"]["evidence"])


@requires_db
@pytest.mark.parametrize("title", [
    "Dell R640 8SFF 2x Gold 6138 selectRAM",
    "Dell R640 8SFF 2x Gold 6138 choosememory 32GB 64GB",
    "Dell R640 8SFF 2x Gold 6138 Select Configuration",
    "Dell R640 8SFF 2x Gold 6138 No HDD selectRAM",
    "Dell R640 8SFF 2x Gold 6138 Without storage choose memory",
], ids=["selectRAM", "choosememory", "configuration", "diskless-with-memory-choice", "without-storage-with-memory-choice"])
def test_title_hardware_choice_marks_multiple_options(conn, title):
    _discover(conn, [_titled(UNKNOWN_SHIP, title)])
    item = _offer(conn, UNKNOWN_SHIP[0])
    assert item["offer"]["kind"] == "multiple_options"
    assert item["offer"]["specification_price_paired"] is False
    assert item["offer"]["price_is_starting"] is True
    assert item["offer"]["evidence"]


@requires_db
def test_description_hardware_choice_marks_multiple_options_without_leaking_text(conn):
    _discover(conn, [UNKNOWN_SHIP])
    text = "Please select RAM from the drop down menu. Ships fast."
    _set_description(conn, UNKNOWN_SHIP[0], {"text_excerpt": text})
    item = _offer(conn, UNKNOWN_SHIP[0])
    assert item["offer"]["kind"] == "multiple_options"
    assert all(len(e) <= 200 for e in item["offer"]["evidence"])
    assert text not in repr(item) and "text_excerpt" not in item and "source_record" not in item


@requires_db
@pytest.mark.parametrize("title,text", [
    ("Dell R640 8SFF 2x Gold 6138 16GB No HDD", "We accept many payment options. Multiple shipping options available."),
    ("Dell R640 8SFF 2x Gold 6138 16GB No HDD", "Upgrades available, message us. Ask about a RAM upgrade or custom quote."),
    ("Dell R640 8SFF 2x Gold 6138 16GB No HDD - Best Offer / Free Shipping Options", "Great deals, choose your payment method at checkout."),
], ids=["payment-shipping", "upgrade-marketing", "title-boilerplate"])
def test_generic_seller_boilerplate_is_not_multiple_options(conn, title, text):
    _discover(conn, [_titled(UNKNOWN_SHIP, title)])
    _set_description(conn, UNKNOWN_SHIP[0], {"text_excerpt": text})
    item = _offer(conn, UNKNOWN_SHIP[0])
    assert item["offer"]["kind"] == "unverified"
    assert item["offer"]["evidence"] == []


@requires_db
def test_exact_rows_ignore_option_text_and_list_matches_detail(conn):
    from rackscout import queries

    match = _fresh_trap_match(conn)
    _set_description(conn, TRAP[0], {"text_excerpt": "Select RAM from the drop down"})
    item = _offer(conn, TRAP[0], "2")
    assert item["offer"]["kind"] == "configuration"
    detail = queries.get_listing(conn, match["id"])
    assert detail["offer"] == item["offer"]


def _stored_page_options(conn, *, hours_ago=0.0, status="verified", fresh_hours=6):
    _discover(conn, [TRAP])
    checked = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    _set_page(conn, TRAP[0], _options_page(checked, fresh_until=(checked + timedelta(hours=fresh_hours)).isoformat()),
              status=status, checked_at=checked)
    recompute_listing(conn, TRAP[0])


@requires_db
def test_current_page_variations_remain_multiple_options_baseline(conn):
    _stored_page_options(conn)
    assert _offer(conn, TRAP[0])["offer"]["kind"] == "multiple_options"


@requires_db
@pytest.mark.parametrize("how", ["changed-index-price", "expired-window", "latest-check-failed"])
def test_superseded_expired_or_failed_page_does_not_prove_options(conn, how):
    if how == "expired-window":
        _stored_page_options(conn, hours_ago=7)
    elif how == "latest-check-failed":
        _stored_page_options(conn, status="failed")
    else:
        _stored_page_options(conn)
        conn.execute("UPDATE listings SET source_record = jsonb_set(source_record, '{item_price}', '\"300.00\"') WHERE item_id = %s",
                     (TRAP[0],))
    item = _offer(conn, TRAP[0])
    assert item["offer"]["kind"] == "unverified"
    assert item["offer"]["evidence"] == []
    assert item["offer"]["specification_price_paired"] is False


@requires_db
def test_independent_title_evidence_survives_stale_page(conn):
    _discover(conn, [_titled(TRAP, TRAP[1] + " selectRAM")])
    title_only = _offer(conn, TRAP[0])["offer"]
    assert title_only["kind"] == "multiple_options"
    old = datetime.now(timezone.utc) - timedelta(hours=7)
    _set_page(conn, TRAP[0], _options_page(old), checked_at=old)
    recompute_listing(conn, TRAP[0])
    assert _offer(conn, TRAP[0])["offer"] == title_only


@requires_db
@pytest.mark.parametrize("text", [
    "No memory options are available for this server.",
    "Contact us for RAM options or upgrades and a custom quote.",
    "RAM options are not available. Memory choices unavailable.",
    "Message us to ask about CPU options.",
    "RAM options aren't available for this server.",
    "Memory choices aren’t available for this server.",
    "Don't select RAM; no alternatives are offered.",
    "No other RAM options are available for this server.",
    "No additional memory choices are offered.",
], ids=["negated", "contact-for-quote", "unavailable", "ask-only", "straight-contraction", "curly-contraction", "negative-instruction", "no-other-options", "no-additional-choices"])
def test_negated_or_request_only_hardware_marketing_is_not_multiple_options(conn, text):
    _discover(conn, [UNKNOWN_SHIP])
    _set_description(conn, UNKNOWN_SHIP[0], {"text_excerpt": text})
    item = _offer(conn, UNKNOWN_SHIP[0])
    assert item["offer"]["kind"] == "unverified"
    assert item["offer"]["evidence"] == []
