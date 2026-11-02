-- Multi-OEM discovery. Non-destructive: every existing listing, configuration,
-- price history row and source run is kept. Everything collected before this
-- migration came from the PerUnitPrice Dell page, so it is backfilled as Dell.

-- Catalog: vendor-scoped generation label and modern-era flag.
ALTER TABLE server_models ADD COLUMN vendor text;
ALTER TABLE server_models ADD COLUMN generation_label text;
ALTER TABLE server_models ADD COLUMN target_era boolean;
UPDATE server_models SET vendor = 'Dell', generation_label = generation || 'G', target_era = generation >= 14;
ALTER TABLE server_models ALTER COLUMN vendor SET NOT NULL;
ALTER TABLE server_models ALTER COLUMN generation_label SET NOT NULL;
ALTER TABLE server_models ALTER COLUMN target_era SET NOT NULL;
ALTER TABLE server_models ADD CONSTRAINT server_models_vendor_check
    CHECK (vendor IN ('Dell', 'HPE', 'Supermicro', 'Lenovo', 'Cisco'));

-- Listings: canonical vendor (NULL = unknown OEM) and the vendor index page that last listed it.
ALTER TABLE listings ADD COLUMN vendor text;
ALTER TABLE listings ADD COLUMN index_url text;
UPDATE listings
   SET vendor = 'Dell',
       index_url = 'https://perunitprice.com/servers/dell/',
       source_record = source_record || jsonb_build_object('vendor', 'Dell', 'index_url', 'https://perunitprice.com/servers/dell/');
CREATE INDEX listings_vendor ON listings (vendor);
CREATE INDEX listings_index_page ON listings (index_url, last_seen_source_at);

-- Per vendor index page acquisition state, so one fresh page never hides another stale or failing page.
CREATE TABLE source_pages (
    url                 text PRIMARY KEY,
    vendor              text NOT NULL,
    last_attempt_at     timestamptz,
    last_outcome        text CHECK (last_outcome IN ('success', 'partial', 'blocked', 'failed')),
    last_message        text,
    upstream_updated_at timestamptz,
    last_success_at     timestamptz,
    record_count        integer
);
INSERT INTO source_pages (url, vendor, last_attempt_at, last_outcome, last_message, upstream_updated_at, last_success_at,
                          record_count)
SELECT 'https://perunitprice.com/servers/dell/', 'Dell',
       (SELECT max(started_at) FROM source_runs WHERE kind = 'discovery'),
       (SELECT outcome FROM source_runs WHERE kind = 'discovery' AND finished_at IS NOT NULL
         ORDER BY finished_at DESC, id DESC LIMIT 1),
       (SELECT message FROM source_runs WHERE kind = 'discovery' AND finished_at IS NOT NULL
         ORDER BY finished_at DESC, id DESC LIMIT 1),
       (SELECT max(upstream_updated_at) FROM source_runs WHERE kind = 'discovery' AND outcome IN ('success', 'partial')),
       (SELECT max(finished_at) FROM source_runs WHERE kind = 'discovery' AND outcome = 'success'),
       (SELECT discovered_count FROM source_runs WHERE kind = 'discovery' AND outcome IN ('success', 'partial')
         ORDER BY finished_at DESC, id DESC LIMIT 1)
WHERE EXISTS (SELECT 1 FROM source_runs WHERE kind = 'discovery');
