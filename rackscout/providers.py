"""Inventory providers and the pure per-provider configuration build.

``PerUnitPrice`` is the existing eBay inventory (indexed by PerUnitPrice, enriched from eBay
pages and seller descriptions). Every other provider is a fixed US retailer from
``rackscout.retail_sources.RETAILERS``. The collector and manual baseline reconstruction both
build configurations through ``build_listing`` so the same stored observation always yields the
same configurations, and a retailer quote is never presented as an eBay page verification.
"""
from __future__ import annotations

from typing import Any

from . import domain
from .retail_sources import RETAILERS

EBAY = "PerUnitPrice"
EBAY_NAME = "eBay (PerUnitPrice)"


def names() -> dict[str, str]:
    """Stable provider key -> display name, eBay first, then retailers in collection order."""
    return {EBAY: EBAY_NAME, **{key: retailer.name for key, retailer in RETAILERS.items()}}


def effective_page(row: dict[str, Any]) -> dict[str, Any] | None:
    """Last successful eBay page verification usable for the current index observation.

    A page check is ignored when the index price changed since the check
    (configuration may have changed). Otherwise the previous verification is
    kept as evidence even when it has aged past its ``fresh_until`` window or
    the latest attempt failed/was blocked; queries treat such evidence as
    stale (never a match) and expose its actual verification time.
    """
    page = row.get("page_claims")
    if not page or page.get("status") != "verified":
        return None
    if page.get("index_price_at_check") != row["source_record"].get("item_price"):
        return None
    return page


def build_listing(row: dict[str, Any], catalog: dict[str, Any] | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(configurations, listing updates) for one stored listing row.

    ``row`` needs ``provider``, ``source_record``, ``description_claims``, ``page_claims``,
    ``page_status`` and ``quote_status``; ``catalog`` is the listing model's catalog row or None.
    Every configuration carries ``source_quote`` (None for eBay). Retailer quote times come only
    from the stored observation (a native variant retained from an earlier observation keeps its
    own method and times, see ``merge_incomplete``), so rebuilding never renews a quote.
    """
    provider = row["provider"]
    record = row["source_record"]
    if provider == EBAY:
        page = effective_page(row)
        configs, listing = domain.build_configurations(record, catalog, row.get("description_claims"), page)
        if page and row.get("page_status") in ("blocked", "failed"):
            # Latest page attempt did not succeed: report it rather than the older verification.
            listing["detail_status"] = row["page_status"]
        for c in configs:
            c["source_quote"] = None
        return configs, listing
    retailer = RETAILERS.get(provider)
    if retailer is None:
        raise ValueError(f"unknown provider {provider!r}")
    status = row.get("quote_status") or "success"
    configs, listing = domain.build_configurations(record, catalog, None, None, retail_status=status)
    retail = record["retail"]
    variants = {str(v["variant_id"]): v for v in retail.get("variants") or []}
    for c in configs:
        v = variants.get(c["variant_id"] or "", {})
        c["source_quote"] = {
            "method": v.get("method") or retail["method"], "provider": provider, "native_variant_id": c["variant_id"],
            "source_url": record["source_url"], "observed_at": v.get("observed_at") or retail["observed_at"],
            "fresh_until": v.get("fresh_until") or retail["fresh_until"], "status": variant_status(status, v),
        }
    listing["seller"] = retailer.name
    return configs, listing


def variant_status(listing_status: str, variant: dict[str, Any]) -> str:
    """Effective latest-attempt status of one native variant's quote.

    A failed/blocked latest acquisition of the listing covers every variant. After a successful one,
    a variant retained unseen from an earlier observation keeps the status it had then (``status``),
    so a failure is never cleared until that variant is actually observed again.
    """
    if listing_status != "success":
        return listing_status
    return variant.get("status") or "success"


# Product-level facts every native variant of a retailer observation is built from. A variant retained
# from an earlier observation is only combined with a new observation that states the same facts.
_COMMON_RECORD_FACTS = ("title", "condition", "vendor", "model", "cpu_raw", "source_ram_gb", "total_cores",
                        "no_ram_flag", "shipping_price", "currency", "source_url")
_COMMON_RETAIL_FACTS = ("handle", "product_type", "kind", "installed_text")


def _common_facts(record: dict[str, Any]) -> tuple[Any, ...]:
    retail = record["retail"]
    return (tuple(record.get(k) for k in _COMMON_RECORD_FACTS)
            + tuple(retail.get(k) for k in _COMMON_RETAIL_FACTS))


def _with_variants(record: dict[str, Any], variants: list[dict[str, Any]]) -> dict[str, Any]:
    return {**record, "retail": {**record["retail"], "variants": variants,
                                 "available": any(v.get("available") is True for v in variants)}}


def merge_incomplete(record: dict[str, Any], previous: dict[str, Any] | None,
                     previous_status: str | None) -> tuple[dict[str, Any], bool]:
    """(observation to store, kept_last_good) for a product seen in an incomplete retailer acquisition.

    An incomplete catalog may omit native variants that still exist. Previously stored variants the new
    observation did not include are retained with their own last method, observation/freshness times and
    effective quote status (never renewed or cleared). That is only sound when the new observation states
    the same product-level facts the retained variants were built from. Otherwise the last-good
    observation (``previous``) is kept (``kept_last_good`` True: its listing status must not change),
    applying explicit unavailability and invalidating observed quotes whose changed facts cannot safely
    replace the old configuration. Retained variants are flat entries, at most one per
    native variant id, so repeated incomplete observations never nest or grow. A complete observation is
    stored as-is and drops every retained variant.
    """
    if previous is None or not isinstance(previous.get("retail"), dict):
        return record, False
    old_retail = previous["retail"]
    seen = {str(v["variant_id"]) for v in record["retail"]["variants"]}
    unseen = [v for v in old_retail.get("variants") or [] if str(v["variant_id"]) not in seen]
    if not unseen:
        return record, False
    if _common_facts(record) != _common_facts(previous):
        quote_only = record["retail"].get("kind") == "quote_only"
        refused = {str(v["variant_id"]) for v in record["retail"]["variants"] if v.get("available") is not True}
        variants = [
            {**v, "available": False} if quote_only or str(v["variant_id"]) in refused
            else {**v, "status": "failed"} if str(v["variant_id"]) in seen
            else v
            for v in old_retail.get("variants") or []
        ]
        return _with_variants(previous, variants), True
    status = previous_status or "success"
    retained = [{**v, "method": v.get("method") or old_retail["method"],
                 "observed_at": v.get("observed_at") or old_retail["observed_at"],
                 "fresh_until": v.get("fresh_until") or old_retail["fresh_until"],
                 "status": variant_status(status, v)} for v in unseen]
    return _with_variants(record, record["retail"]["variants"] + retained), False
