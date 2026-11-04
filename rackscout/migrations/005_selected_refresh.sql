-- Selected-listing refresh. Additive: the single control row optionally targets one marketplace
-- listing (stable listings.id, independent of any configuration). NULL keeps the existing global
-- meaning, so every existing request and the singleton queue behave as before. RESTRICT (never
-- SET NULL) so a deleted listing can never silently widen a queued request into a global one.
ALTER TABLE refresh_control
    ADD COLUMN target_listing_id bigint REFERENCES listings (id);
