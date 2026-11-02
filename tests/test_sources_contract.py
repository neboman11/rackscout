"""Consumer-visible source boundaries; no network required."""
import json
from decimal import Decimal


def index_html(records):
    cfg = {"updated": "2026-11-02T12:23:10+00:00", "dec": 2, "nsel": 3,
           "cols": [{"f": "int", "hide": False}, {"f": "text", "hide": False},
                    {"f": "gb", "hide": False}, {"f": "text", "hide": True},
                    {"f": "text", "hide": True}], "tog": [True], "flags": ["No RAM", "EPYC"]}
    return f'<script id="d" type="application/json">{json.dumps(records)}</script><script id="cfg" type="application/json">{json.dumps(cfg)}</script>'


def record(item, shipping=None):
    return [7.47, 268.88, shipping, "Used", "Dell R640 64GB NO HDD", f"https://www.ebay.com/itm/{item}", 1,
            36, "2× Gold 6150", 64, "Dell R640", "DDR4", "Dell", "DDR4", "Rack", 0, ""]


def test_complete_embedded_inventory_and_unknown_shipping():
    from rackscout.sources import parse_index
    records = [record(str(100000000000 + i)) for i in range(249)]
    snapshot = parse_index(index_html(records))
    assert len(snapshot.records) == 249
    assert snapshot.records[-1]["item_id"] == "100000000248"
    assert snapshot.records[0]["item_price"] == Decimal("268.88")
    assert snapshot.records[0]["shipping_price"] is None
    assert snapshot.updated_at.utcoffset().total_seconds() == 0


def test_explicit_no_ram_not_overridden_by_title_style_options():
    from rackscout.sources import parse_description
    details = parse_description('<p>Installed memory: NO RAM</p><p>Upgrade options: 16GB, 32GB, 64GB</p><p>No drives or trays included.</p>')
    assert details["ram_gb"] == 0
    assert details["storage_state"] == "diskless"
    assert any("tray" in text.lower() for text in details["exclusions"])


def test_challenge_is_not_empty_success():
    import pytest
    from rackscout.sources import SourceError, parse_index
    with pytest.raises(SourceError):
        parse_index('<html><title>Pardon Our Interruption</title><body>Verify you are human</body></html>')


def test_seller_markup_does_not_turn_into_evidence_script():
    from rackscout.sources import parse_description
    details = parse_description('<script>Installed RAM 512GB</script><style>.x{content:"128GB"}</style><p>Installed RAM: 16GB DDR4</p><p>No HDD.</p>')
    assert details["ram_gb"] == 16
    assert details["storage_state"] == "diskless"
