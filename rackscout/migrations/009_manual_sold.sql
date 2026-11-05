-- Persistent manual "sold" mark for a marketplace listing. Additive and nullable: NULL (the value for every
-- existing row) means not manually marked; the mark is independent of the source's own ended_at/archived_at,
-- and the collector never writes it. Rollback needs the pre-upgrade database backup; older releases reject
-- schema 009. The collector lock prevents old collection writes at cutover.
SELECT pg_advisory_xact_lock(x'53434F55'::bit(32)::bigint);

ALTER TABLE listings ADD COLUMN manual_sold_at timestamptz;
