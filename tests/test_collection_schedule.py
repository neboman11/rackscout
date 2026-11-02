"""Enrichment scheduling and page-verification validity.

Verified modern-era 1U target matches must be rechecked before their verification
window closes, even when many never-checked non-target or new listings are
queued, while new targets and the oldest other candidates keep bounded progress.
Database tests share the isolated UUID-schema fixture from ``test_db``.
"""
from datetime import datetime, timedelta, timezone

import pytest
from test_db import (  # noqa: F401 - `conn` is a fixture
    TRAP,
    TRAP_VARIATIONS,
    FakeFetcher,
    conn,
    recompute_listing,
    requires_db,
    settings,
    snapshot,
    verified_page,
)

TARGET_TITLE = TRAP[1]
CRON_INTERVAL = timedelta(hours=4)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _item_id(n):
    return f"9000{n:08d}"


def _discover(conn, rows, when=None):
    """rows: (item_id, model, price) tuples; all listed in one valid snapshot."""
    from rackscout import collector

    when = when or datetime.now(timezone.utc)
    snap = snapshot(when, [(item_id, TARGET_TITLE if model != "R620" else "Dell PowerEdge R620 1U 8SFF 2x E5-2650 v2 64GB",
                            price, "0.00", "2× Gold 6150", 64) for item_id, model, price in rows])
    for rec, (_, model, _) in zip(snap.records, rows):
        rec["model"] = model
    assert collector.run_discovery(conn, settings(), FakeFetcher(snap)) == "success"


def _index_price(conn, item_id):
    return conn.execute("SELECT source_record->>'item_price' AS p FROM listings WHERE item_id = %s",
                        (item_id,)).fetchone()["p"]


def _mark_verified(conn, item_id, checked_at, *, window_hours, due=True, variations=None):
    """State left by an earlier successful collector page check."""
    from rackscout import collector

    price = _index_price(conn, item_id)
    variations = variations or [{"variant_id": "2", "labels": {"Memory": "32GB"}, "price": price}]
    page = verified_page(checked_at, price_at_check=price, variations=variations, fresh_hours=window_hours)
    conn.execute(
        """UPDATE listings SET page_status = 'verified', page_claims = %s, page_checked_at = %s, last_checked_at = %s,
             next_check_after = now() + %s WHERE item_id = %s""",
        (collector._json(page), checked_at, checked_at, timedelta(minutes=-5) if due else timedelta(hours=1), item_id),
    )
    recompute_listing(conn, item_id)


def _mark_checked(conn, item_id, checked_at, *, first_seen=None):
    conn.execute(
        "UPDATE listings SET last_checked_at = %s, next_check_after = now() - interval '5 minutes',"
        " first_seen_at = coalesce(%s, first_seen_at) WHERE item_id = %s",
        (checked_at, first_seen, item_id),
    )


def _selected(conn, budget):
    from rackscout import collector

    rows = collector.select_candidates(conn, budget)
    ids = [r["item_id"] for r in rows]
    assert len(ids) == len(set(ids)) and len(ids) <= budget
    return ids


class DescriptionFetcher:
    def fetch_description(self, item_id, max_bytes):
        return {"ram_gb": None, "exclusions": []}


def _checker(result=None, error=None):
    class Checker:
        started = True
        checked: list[str] = []

        def __init__(self, *args, **kwargs):
            pass

        def check(self, item_id):
            Checker.checked.append(item_id)
            if error is not None:
                raise error
            return dict(result)

        def close(self):
            pass

    return Checker


def _match_state(conn, item_id, variant_id="2"):
    from rackscout import queries

    items = [i for i in queries.query_listings(conn, queries.Filters())["items"]
             if i["item_id"] == item_id and i["variant_id"] == variant_id]
    return items[0]["match_state"] if items else None


# ---------------------------------------------------------------------------
# Verification validity window
# ---------------------------------------------------------------------------


def test_page_verification_window_outlasts_recheck_cooldown_plus_cron(monkeypatch):
    from rackscout.config import load_settings

    s = load_settings()
    assert timedelta(hours=s.page_verification_hours) > timedelta(hours=s.recheck_cooldown_hours) + CRON_INTERVAL
    monkeypatch.setenv("RACKSCOUT_PAGE_VERIFICATION_HOURS", "20")
    assert load_settings().page_verification_hours == 20


def test_page_verification_window_shorter_than_cooldown_is_rejected(monkeypatch):
    from rackscout.config import ConfigError, load_settings

    monkeypatch.setenv("RACKSCOUT_RECHECK_COOLDOWN_HOURS", "8")
    monkeypatch.setenv("RACKSCOUT_PAGE_VERIFICATION_HOURS", "6")
    with pytest.raises(ConfigError):
        load_settings()


@requires_db
def test_successful_check_writes_configured_window_and_failures_never_extend_it(conn, monkeypatch):
    from rackscout import browser, collector
    from rackscout.sources import SourceError

    monkeypatch.setenv("RACKSCOUT_PAGE_VERIFICATION_HOURS", "20")
    s = settings()
    _discover(conn, [(TRAP[0], "R640", TRAP[2])])
    result = {"status": "verified", "has_variations": True, "price_kind": "exact", "variations": TRAP_VARIATIONS}
    monkeypatch.setattr(browser, "BrowserChecker", _checker(result=result))
    collector.run_enrichment(conn, s, DescriptionFetcher(), 5, browser_enabled=True)
    page = conn.execute("SELECT page_claims FROM listings WHERE item_id = %s", (TRAP[0],)).fetchone()["page_claims"]
    checked, fresh_until = datetime.fromisoformat(page["checked_at"]), datetime.fromisoformat(page["fresh_until"])
    assert fresh_until - checked == timedelta(hours=20)

    # Browser-disabled and failed attempts neither move the success time nor reset expiry.
    for enabled, checker in ((False, None), (True, _checker(error=SourceError("layout changed")))):
        conn.execute("UPDATE listings SET next_check_after = now() - interval '1 minute' WHERE item_id = %s", (TRAP[0],))
        if checker is not None:
            monkeypatch.setattr(browser, "BrowserChecker", checker)
        collector.run_enrichment(conn, s, DescriptionFetcher(), 5, browser_enabled=enabled)
        after = conn.execute("SELECT page_claims FROM listings WHERE item_id = %s", (TRAP[0],)).fetchone()["page_claims"]
        assert after["checked_at"] == page["checked_at"]
        assert after["fresh_until"] == page["fresh_until"]


@requires_db
def test_eight_hour_old_verification_stays_valid_until_timely_recheck(conn, monkeypatch):
    from rackscout import browser, collector

    _discover(conn, [(TRAP[0], "R640", TRAP[2])])
    s = settings()
    old = datetime.now(timezone.utc) - timedelta(hours=8)  # cooldown passed, next cron run is the recheck
    _mark_verified(conn, TRAP[0], old, window_hours=s.page_verification_hours, variations=TRAP_VARIATIONS)
    assert _match_state(conn, TRAP[0]) == "match"
    assert TRAP[0] in _selected(conn, 1)

    result = {"status": "verified", "has_variations": True, "price_kind": "exact", "variations": TRAP_VARIATIONS}
    monkeypatch.setattr(browser, "BrowserChecker", _checker(result=result))
    collector.run_enrichment(conn, s, DescriptionFetcher(), 1, browser_enabled=True)
    page = conn.execute("SELECT page_claims FROM listings WHERE item_id = %s", (TRAP[0],)).fetchone()["page_claims"]
    assert datetime.fromisoformat(page["checked_at"]) > old + timedelta(hours=7)
    assert _match_state(conn, TRAP[0]) == "match"


@requires_db
def test_query_respects_stored_window_and_legacy_fallback_is_conservative(conn):
    from rackscout import collector

    _discover(conn, [(TRAP[0], "R640", TRAP[2])])
    old = datetime.now(timezone.utc) - timedelta(hours=8)
    _mark_verified(conn, TRAP[0], old, window_hours=7, variations=TRAP_VARIATIONS)
    assert _match_state(conn, TRAP[0]) == "lead"  # stored window closed one hour ago

    legacy = verified_page(old, variations=TRAP_VARIATIONS)
    del legacy["fresh_until"]
    conn.execute("UPDATE listings SET page_claims = %s WHERE item_id = %s", (collector._json(legacy), TRAP[0]))
    recompute_listing(conn, TRAP[0])
    assert _match_state(conn, TRAP[0]) == "lead"  # no recorded window: not trusted beyond the 6h cooldown


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------


@requires_db
def test_due_verified_target_outranks_cheap_never_checked_non_targets(conn, monkeypatch):
    from rackscout import browser, collector

    target, not_due = _item_id(1), _item_id(2)
    cheap = [_item_id(100 + n) for n in range(30)]
    _discover(conn, [(target, "R640", "400.00"), (not_due, "R440", "300.00")]
              + [(i, "R620", f"{50 + n}.00") for n, i in enumerate(cheap)])
    old = datetime.now(timezone.utc) - timedelta(hours=8)
    _mark_verified(conn, target, old, window_hours=settings().page_verification_hours)
    _mark_verified(conn, not_due, old, window_hours=settings().page_verification_hours, due=False)

    selected = _selected(conn, 5)
    assert target in selected
    assert not_due not in selected  # cooldown preserved

    result = {"status": "verified", "has_variations": True, "price_kind": "exact",
              "variations": [{"variant_id": "2", "labels": {"Memory": "32GB"}, "price": "400.00"}]}
    checker = _checker(result=result)
    monkeypatch.setattr(browser, "BrowserChecker", checker)
    collector.run_enrichment(conn, settings(), DescriptionFetcher(), 5, browser_enabled=True)
    assert target in checker.checked
    assert len(checker.checked) == 5  # budget preserved


@requires_db
def test_full_budget_splits_verified_new_target_and_oldest_lanes(conn):
    verified = [_item_id(n) for n in range(1, 16)]
    new_targets = [_item_id(100 + n) for n in range(15)]
    oldest = _item_id(500)
    _discover(conn, [(i, "R640", f"{300 + n}.00") for n, i in enumerate(verified)]
              + [(i, "R340", f"{100 + n}.00") for n, i in enumerate(new_targets)]
              + [(oldest, "R620", "999.00")])
    now = datetime.now(timezone.utc)
    for i in verified:
        _mark_verified(conn, i, now - timedelta(hours=8), window_hours=settings().page_verification_hours)
    _mark_checked(conn, oldest, now - timedelta(days=30), first_seen=now - timedelta(days=40))

    selected = _selected(conn, 25)
    assert len(selected) == 25
    assert len([i for i in selected if i in verified]) == 12
    assert len([i for i in selected if i in new_targets]) == 12
    assert oldest in selected
    # Cheapest new targets first within their lane.
    assert set(new_targets[:12]) <= set(selected)


@requires_db
def test_unused_lane_quota_is_filled_and_budget_never_exceeded(conn):
    verified = [_item_id(n) for n in range(1, 3)]
    others = [_item_id(100 + n) for n in range(40)]
    _discover(conn, [(i, "R640", "400.00") for i in verified] + [(i, "R620", f"{50 + n}.00") for n, i in enumerate(others)])
    for i in verified:
        _mark_verified(conn, i, datetime.now(timezone.utc) - timedelta(hours=8),
                       window_hours=settings().page_verification_hours)
    selected = _selected(conn, 25)
    assert len(selected) == 25 and set(verified) <= set(selected)


@requires_db
def test_new_cheap_listings_cannot_starve_oldest_checked_candidate(conn):
    old_item = _item_id(1)
    fresh = [_item_id(100 + n) for n in range(30)]
    now = datetime.now(timezone.utc)
    _discover(conn, [(old_item, "R620", "900.00")], when=now - timedelta(days=31))
    _mark_checked(conn, old_item, now - timedelta(days=30), first_seen=now - timedelta(days=31))
    _discover(conn, [(old_item, "R620", "900.00")] + [(i, "R620", f"{20 + n}.00") for n, i in enumerate(fresh)])

    assert old_item in _selected(conn, 3)


@requires_db
def test_small_budgets_are_deterministic_and_fair(conn):
    verified, new_target, oldest = _item_id(1), _item_id(2), _item_id(3)
    _discover(conn, [(verified, "R640", "400.00"), (new_target, "R240", "150.00"), (oldest, "R620", "60.00")])
    now = datetime.now(timezone.utc)
    _mark_verified(conn, verified, now - timedelta(hours=8), window_hours=settings().page_verification_hours)
    _mark_checked(conn, oldest, now - timedelta(days=20), first_seen=now - timedelta(days=21))

    assert _selected(conn, 1) == [verified] == _selected(conn, 1)
    assert sorted(_selected(conn, 2)) == sorted([verified, new_target])
    assert sorted(_selected(conn, 3)) == sorted([verified, new_target, oldest])
    assert _selected(conn, 0) == []


@requires_db
def test_cheapest_due_target_rechecks_before_stale_costly_target(conn):
    cheap, costly = _item_id(1), _item_id(2)
    now = datetime.now(timezone.utc)
    rows = [(cheap, "R640", "300.00"), (costly, "R640", "1500.00")]
    _discover(conn, rows, when=now - timedelta(days=2))
    _discover(conn, rows, when=now)
    _mark_verified(conn, cheap, now - timedelta(hours=8), window_hours=settings().page_verification_hours)
    _mark_verified(conn, costly, now - timedelta(hours=16), window_hours=settings().page_verification_hours)

    assert _selected(conn, 1) == [cheap]
    assert costly in _selected(conn, 3)  # reserved oldest lane still makes progress
