"""Price-first mixed inventory and server-side confidence filtering."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from test_db import conn, requires_db, FakeFetcher, settings, snapshot, TRAP, UNKNOWN_SHIP, verified_page, recompute_listing

pytestmark = requires_db


def inventory(conn):
    from rackscout import collector
    expensive_shipping = ('400000000003', 'Dell R640 8SFF 2x Gold 6138 64GB No HDD', '200.00', '500.00', '2× Gold 6138', 64)
    no_price = ('400000000004', 'Dell R640 8SFF 2x Gold 6138 128GB No HDD', '9999.00', None, '2× Gold 6138', 128)
    collector.run_discovery(conn, settings(), FakeFetcher(snapshot(datetime.now(timezone.utc), [TRAP, UNKNOWN_SHIP, expensive_shipping, no_price])))
    conn.execute('UPDATE listings SET page_status = %s, page_claims = %s WHERE item_id = %s',
                 ('verified', collector._json(verified_page(datetime.now(timezone.utc))), TRAP[0]))
    recompute_listing(conn, TRAP[0])
    conn.execute('UPDATE configurations SET item_price = NULL WHERE listing_id = (SELECT id FROM listings WHERE item_id = %s)', (no_price[0],))
    return [UNKNOWN_SHIP[0], TRAP[0], expensive_shipping[0], no_price[0]]


def test_price_order_mixes_confidence_and_uses_known_total_or_item_lower_bound(conn):
    from rackscout import queries
    expected = inventory(conn)
    result = queries.query_listings(conn, queries.Filters())
    assert [i['item_id'] for i in result['items']] == expected
    assert [i['match_state'] for i in result['items']] == ['lead', 'match', 'lead', 'lead']
    assert result['items'][0]['quoted_total'] is None  # sorting never invents free shipping
    assert result['total_matches'] == 1 and result['total_leads'] == 3
    pages = [queries.query_listings(conn, queries.Filters(page=p, page_size=1)) for p in range(1, 5)]
    assert [p['items'][0]['item_id'] for p in pages] == expected
    assert all(p['total'] == 4 and p['pages'] == 4 for p in pages)


def test_alternative_sort_is_global_not_match_first(conn):
    from rackscout import queries
    inventory(conn)
    items = queries.query_listings(conn, queries.Filters(sort='ram'))['items']
    assert [i['ram_gb'] for i in items] == [128, 64, 32, 16]


@pytest.mark.parametrize('status,states,count', [('all', {'match', 'lead'}, 4), ('match', {'match'}, 1), ('lead', {'lead'}, 3)])
def test_match_status_filters_counts_and_pages_before_pagination(conn, status, states, count):
    from rackscout import queries
    inventory(conn)
    result = queries.query_listings(conn, queries.Filters(match_status=status, page_size=1))
    assert result['total'] == count and result['pages'] == count
    assert {i['match_state'] for i in result['items']} <= states
    assert result['total_matches'] == (0 if status == 'lead' else 1)
    assert result['total_leads'] == (0 if status == 'match' else 3)
    if status == 'lead':
        assert queries.query_listings(conn, queries.Filters(match_status=status, confirmed_only=True))['total'] == 0


def test_match_status_does_not_relax_unknown_shipping_budget_rules(conn):
    from rackscout import queries
    inventory(conn)
    match = queries.query_listings(conn, queries.Filters(match_status='match', max_total=Decimal('500')))
    assert [i['item_id'] for i in match['items']] == [TRAP[0]]
    lead = queries.query_listings(conn, queries.Filters(match_status='lead', max_total=Decimal('500')))
    assert [i['item_id'] for i in lead['items']] == [UNKNOWN_SHIP[0]]
    assert lead['items'][0]['quoted_total'] is None


def test_match_status_api_validation_and_http_filtering(conn, monkeypatch):
    from fastapi import HTTPException
    from fastapi.testclient import TestClient
    from rackscout import api
    inventory(conn)
    monkeypatch.setattr(api, '_settings', settings())
    client = TestClient(api.app)
    for status in ('all', 'match', 'lead'):
        parsed = api.parse_filters(match_status=status)
        assert parsed.match_status == status
        response = client.get('/api/listings', params={'match_status': status})
        assert response.status_code == 200
        if status != 'all':
            assert {i['match_state'] for i in response.json()['items']} == {status}
    for invalid in ('verified', 'other', 'match; DROP'):
        with pytest.raises(HTTPException) as exc:
            api.parse_filters(match_status=invalid)
        assert exc.value.status_code == 422
