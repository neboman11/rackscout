"""Untrusted-content boundaries, not incidental implementation details."""
import pytest


def test_deep_seller_markup_is_controlled_rejection_not_recursion_failure():
    from rackscout.sources import SourceError, parse_description
    html = '<div>' * 2000 + '<p>Installed RAM: 16GB</p>' + '</div>' * 2000
    with pytest.raises(SourceError):
        parse_description(html)


@pytest.mark.parametrize('url', [
    'http://www.ebay.com/itm/126905936007',
    'https://www.ebay.com:8443/itm/126905936007',
    'https://user:password@www.ebay.com/itm/126905936007',
    'https://www.ebay.com.evil.invalid/itm/126905936007',
    'https://127.0.0.1/private',
])
def test_browser_policy_rejects_unsafe_destinations(url):
    from rackscout.browser import allowed_browser_url
    assert allowed_browser_url(url, '126905936007', navigation=True) is False


def test_browser_navigation_bound_to_requested_item():
    from rackscout.browser import allowed_browser_url
    assert allowed_browser_url('https://www.ebay.com/itm/126905936007', '126905936007', navigation=True)
    assert not allowed_browser_url('https://www.ebay.com/itm/187634883092', '126905936007', navigation=True)
    assert allowed_browser_url('https://ir.ebaystatic.com/assets/item.js', '126905936007', navigation=False)
