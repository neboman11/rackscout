-- CPU platform references and memory platforms. Non-destructive: every existing listing, configuration,
-- manual row, price history row, source run, source page, backoff and refresh request keeps its id and
-- values; no revision changes. Waits for the global collector lock (rackscout.db.COLLECTOR_LOCK_KEY) so
-- no old collection writes during the DDL; the lock is released with the commit recording this version.
SELECT pg_advisory_xact_lock(x'53434F55'::bit(32)::bigint);

-- Exact-model CPU platform references (rackscout/data/cpu_platforms.json), synchronized by
-- db.sync_reference_data. Model/platform reference facts, never installed-hardware verification.
CREATE TABLE cpu_platforms (
    cpu_key         text PRIMARY KEY,
    cpu_model       text NOT NULL,
    manufacturer    text NOT NULL CHECK (manufacturer IN ('Intel', 'AMD')),
    architecture    text NOT NULL CHECK (architecture <> ''),
    memory_platform text CHECK (memory_platform IN ('DDR3', 'DDR4', 'DDR5')),
    source_url      text NOT NULL CHECK (source_url ~ '^https://'),
    provider        text NOT NULL,
    retrieved_at    timestamptz NOT NULL
);

-- Canonical reference key of the configuration's own CPU model (hardware_platforms.reference_key),
-- derived at persistence boundaries and re-derived by db.sync_reference_data (which also backfills
-- existing rows right after this migration). Not a foreign key: references may be withdrawn.
ALTER TABLE configurations ADD COLUMN cpu_platform_key text;
CREATE INDEX configurations_cpu_platform ON configurations (cpu_platform_key) WHERE cpu_platform_key IS NOT NULL;

-- Unambiguous explicit memory type stated for a source configuration (eBay index memory type; retailer
-- installed Memory field or native variant Memory label). Manual rows keep the value frozen in their own
-- baseline only while their CPU is the base option's CPU.
ALTER TABLE configurations ADD COLUMN memory_type text CHECK (memory_type IN ('DDR3', 'DDR4', 'DDR5'));
-- The configuration's own explicit memory statement names several generations (e.g. "DDR4 or DDR5"):
-- its memory platform is unknown, never filled from the CPU reference.
ALTER TABLE configurations ADD COLUMN memory_type_conflict boolean NOT NULL DEFAULT false;
ALTER TABLE configurations ADD CONSTRAINT configurations_memory_type_conflict
    CHECK (NOT memory_type_conflict OR memory_type IS NULL);

-- Source rows are backfilled right after this file, in the same transaction, by db.migrate with the
-- collector's own parser (collector.listing_memory), so the next collection changes no revision.
-- Existing manual rows stay NULL: their baselines predate frozen memory types, and the source's current
-- type may have changed since the copy.
