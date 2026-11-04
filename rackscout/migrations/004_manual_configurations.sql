-- Manual configurations. Additive: every existing configuration, listing, price history row and
-- source run is kept unchanged; existing rows become source-origin revision 1 with no structured
-- essentials (NULL until the collector next recomputes them, reconstructed on demand meanwhile).
ALTER TABLE configurations
    ADD COLUMN origin                  text NOT NULL DEFAULT 'source' CHECK (origin IN ('source', 'manual')),
    ADD COLUMN source_configuration_id bigint,
    ADD COLUMN essential_states        jsonb,
    ADD COLUMN manual_data             jsonb,
    ADD COLUMN revision                integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    ADD COLUMN manual_created_at       timestamptz,
    ADD COLUMN manual_updated_at       timestamptz;

-- A manual row references a configuration of the same listing (composite key). NO ACTION (checked at
-- statement end) lets a listing deletion cascade to parent and children together.
ALTER TABLE configurations ADD CONSTRAINT configurations_listing_id_id UNIQUE (listing_id, id);
ALTER TABLE configurations ADD CONSTRAINT configurations_source_parent
    FOREIGN KEY (listing_id, source_configuration_id) REFERENCES configurations (listing_id, id);

-- Source rows carry no manual parent or metadata; manual rows always do and use server-generated
-- keys without a marketplace variation ID.
ALTER TABLE configurations ADD CONSTRAINT configurations_origin_shape CHECK (
    (origin = 'source' AND source_configuration_id IS NULL AND manual_data IS NULL
        AND manual_created_at IS NULL AND manual_updated_at IS NULL AND variant_key !~ '^manual:')
    OR (origin = 'manual' AND source_configuration_id IS NOT NULL AND manual_data IS NOT NULL
        AND manual_created_at IS NOT NULL AND manual_updated_at IS NOT NULL AND variant_id IS NULL
        AND variant_key ~ '^manual:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
);

CREATE INDEX configurations_manual_children ON configurations (source_configuration_id)
    WHERE origin = 'manual' AND active;
