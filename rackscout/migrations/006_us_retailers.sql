-- US retailer platforms. Non-destructive: every existing listing, configuration, manual row, price
-- history row, source run, source page and refresh request keeps its id and values. Everything
-- collected before this migration is eBay inventory indexed by PerUnitPrice (provider 'PerUnitPrice').
--
-- Cutover: this release's db.schema_ready requires exactly this version, and the previous release
-- requires exactly 005, so old collectors stop writing once this commits. The migration first waits
-- for the global collector lock (rackscout.db.COLLECTOR_LOCK_KEY, held for a whole collection by the
-- scheduled collector and the refresh worker), so no old collection writes during or after the DDL;
-- the lock is released with the commit that also records this version.
SELECT pg_advisory_xact_lock(x'53434F55'::bit(32)::bigint);

-- Native identities are unique per provider, never globally: the same digits may name an eBay item
-- and a retailer product. Each provider keeps its own fixed native-ID and canonical-URL grammar.
ALTER TABLE listings DROP CONSTRAINT listings_item_id_key;
ALTER TABLE listings DROP CONSTRAINT listings_item_id_check;
ALTER TABLE listings DROP CONSTRAINT listings_canonical_url_check;
ALTER TABLE listings ADD CONSTRAINT listings_provider_item_id UNIQUE (provider, item_id);
ALTER TABLE listings ADD CONSTRAINT listings_provider_identity CHECK (
    (provider = 'PerUnitPrice' AND item_id ~ '^[0-9]{9,15}$'
        AND canonical_url ~ '^https://www\.ebay\.com/itm/[0-9]{9,15}$')
    OR (provider = 'TechMikeNY' AND item_id ~ '^[0-9]{1,20}$'
        AND canonical_url ~ '^https://techmikeny\.com/products/[a-z0-9][a-z0-9_-]{0,254}$')
    OR (provider = 'SaveMyServer' AND item_id ~ '^[0-9]{1,20}$'
        AND canonical_url ~ '^https://savemyserver\.com/products/[a-z0-9][a-z0-9_-]{0,254}$')
    OR (provider = 'CloudNinjas' AND item_id ~ '^[0-9]{1,20}$'
        AND canonical_url ~ '^https://cloudninjas\.com/products/[a-z0-9][a-z0-9_-]{0,254}$')
);

-- Outcome of the latest retailer acquisition (catalog or product) applied to this listing. NULL for
-- eBay listings, whose page/description attempts keep their own columns.
ALTER TABLE listings ADD COLUMN quote_status text CHECK (quote_status IN ('success', 'blocked', 'failed'));
ALTER TABLE listings ADD CONSTRAINT listings_quote_status_provider
    CHECK (provider <> 'PerUnitPrice' OR quote_status IS NULL);
CREATE INDEX listings_provider_page ON listings (provider, index_url, last_seen_source_at);

-- Source pages: eBay vendor index pages keep their vendor; retailer catalogs mix OEMs (vendor NULL).
ALTER TABLE source_pages ADD COLUMN provider text NOT NULL DEFAULT 'PerUnitPrice'
    CHECK (provider IN ('PerUnitPrice', 'TechMikeNY', 'SaveMyServer', 'CloudNinjas'));
ALTER TABLE source_pages ALTER COLUMN vendor DROP NOT NULL;
ALTER TABLE source_pages ADD CONSTRAINT source_pages_vendor_provider
    CHECK (provider <> 'PerUnitPrice' OR vendor IS NOT NULL);

-- Retailer quote provenance of a source configuration (method, provider, native variant, source URL,
-- observation and freshness times, latest attempt status). Never set on manual rows; existing rows
-- (eBay) stay NULL.
ALTER TABLE configurations ADD COLUMN source_quote jsonb;
ALTER TABLE configurations ADD CONSTRAINT configurations_source_quote_shape CHECK (
    source_quote IS NULL OR (origin = 'source' AND jsonb_typeof(source_quote) = 'object'
        AND source_quote->>'method' IN ('retailer_catalog', 'retailer_product')
        AND source_quote->>'status' IN ('success', 'blocked', 'failed'))
);
