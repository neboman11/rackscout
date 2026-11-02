-- RackScout initial schema. Applied once by `python -m rackscout migrate`.

CREATE TABLE server_models (
    model           text PRIMARY KEY,
    generation      integer NOT NULL CHECK (generation > 0),
    rack_units      integer NOT NULL CHECK (rack_units > 0),
    socket_capacity integer NOT NULL CHECK (socket_capacity > 0),
    cpu_family      text,
    source_url      text NOT NULL,
    source_text     text NOT NULL,
    retrieved_at    timestamptz NOT NULL
);

CREATE TABLE cpu_benchmarks (
    cpu_key       text PRIMARY KEY,
    cpu_model     text NOT NULL,
    provider      text NOT NULL,
    cpu_mark      integer NOT NULL CHECK (cpu_mark > 0),
    single_thread integer NOT NULL CHECK (single_thread > 0),
    samples       integer NOT NULL CHECK (samples > 0),
    version       text NOT NULL,
    scope         text NOT NULL CHECK (scope = 'single_cpu'),
    source_url    text NOT NULL,
    retrieved_at  timestamptz NOT NULL
);

CREATE TABLE source_runs (
    id                  bigserial PRIMARY KEY,
    provider            text NOT NULL,
    kind                text NOT NULL CHECK (kind IN ('discovery', 'enrichment')),
    started_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz,
    outcome             text CHECK (outcome IN ('success', 'partial', 'blocked', 'failed')),
    upstream_updated_at timestamptz,
    discovered_count    integer NOT NULL DEFAULT 0,
    enriched_count      integer NOT NULL DEFAULT 0,
    message             text
);
CREATE INDEX source_runs_kind_finished ON source_runs (kind, finished_at DESC);

CREATE TABLE origin_backoff (
    origin        text PRIMARY KEY,
    blocked_until timestamptz NOT NULL,
    reason        text NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE listings (
    id                     bigserial PRIMARY KEY,
    provider               text NOT NULL,
    item_id                text NOT NULL UNIQUE CHECK (item_id ~ '^[0-9]{9,15}$'),
    canonical_url          text NOT NULL CHECK (canonical_url ~ '^https://www\.ebay\.com/itm/[0-9]{9,15}$'),
    original_url           text NOT NULL,
    title                  text NOT NULL,
    seller                 text,
    condition              text,
    model                  text,
    source_record          jsonb NOT NULL,
    first_seen_at          timestamptz NOT NULL DEFAULT now(),
    last_seen_at           timestamptz NOT NULL DEFAULT now(),
    last_seen_source_at    timestamptz NOT NULL,
    archived_at            timestamptz,
    ended_at               timestamptz,
    ended_evidence         text,
    description_status     text CHECK (description_status IN ('verified', 'blocked', 'failed', 'absent')),
    description_checked_at timestamptz,
    description_claims     jsonb,
    page_status            text CHECK (page_status IN ('verified', 'blocked', 'failed')),
    page_checked_at        timestamptz,
    page_claims            jsonb,
    detail_status          text NOT NULL DEFAULT 'never'
                           CHECK (detail_status IN ('never', 'verified', 'partial', 'blocked', 'failed')),
    last_checked_at        timestamptz,
    next_check_after       timestamptz,
    check_message          text
);
CREATE INDEX listings_active ON listings (archived_at, ended_at);
CREATE INDEX listings_check_queue ON listings (next_check_after, last_checked_at);

CREATE TABLE configurations (
    id               bigserial PRIMARY KEY,
    listing_id       bigint NOT NULL REFERENCES listings (id) ON DELETE CASCADE,
    variant_key      text NOT NULL DEFAULT '',
    variant_id       text,
    variant_label    text,
    active           boolean NOT NULL DEFAULT true,
    model            text,
    cpu_model        text,
    cpu_key          text,
    cpu_model_state  text NOT NULL CHECK (cpu_model_state IN ('confirmed', 'inferred', 'unknown', 'conflicting')),
    cpu_count        integer CHECK (cpu_count > 0),
    cpu_count_state  text NOT NULL CHECK (cpu_count_state IN ('confirmed', 'inferred', 'unknown', 'conflicting')),
    total_cores      integer CHECK (total_cores > 0),
    ram_gb           integer CHECK (ram_gb >= 0),
    ram_state        text NOT NULL CHECK (ram_state IN ('confirmed', 'inferred', 'unknown', 'conflicting')),
    drive_bays       integer CHECK (drive_bays > 0),
    bays_state       text NOT NULL CHECK (bays_state IN ('confirmed', 'inferred', 'unknown', 'conflicting')),
    drive_size       text CHECK (drive_size IN ('SFF', 'LFF')),
    size_state       text NOT NULL CHECK (size_state IN ('confirmed', 'inferred', 'unknown', 'conflicting')),
    storage_state    text NOT NULL CHECK (storage_state IN ('diskless', 'included', 'unknown')),
    storage_conf     text NOT NULL CHECK (storage_conf IN ('confirmed', 'inferred', 'unknown', 'conflicting')),
    hardware_state   text NOT NULL CHECK (hardware_state IN ('confirmed', 'review', 'missing', 'damaged')),
    damage           boolean,
    price_kind       text NOT NULL CHECK (price_kind IN ('exact', 'base', 'range', 'unknown')),
    item_price       numeric(12, 2) CHECK (item_price >= 0),
    shipping_price   numeric(12, 2) CHECK (shipping_price >= 0),
    quoted_total     numeric(12, 2) GENERATED ALWAYS AS (item_price + shipping_price) STORED,
    currency         text NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    shipping_context text NOT NULL,
    evidence         jsonb NOT NULL DEFAULT '[]'::jsonb,
    warnings         jsonb NOT NULL DEFAULT '[]'::jsonb,
    exclusions       jsonb NOT NULL DEFAULT '[]'::jsonb,
    observed_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (listing_id, variant_key)
);
CREATE INDEX configurations_listing ON configurations (listing_id);
CREATE INDEX configurations_filter ON configurations (active, model, ram_gb, drive_bays);
CREATE INDEX configurations_total ON configurations (quoted_total);

CREATE TABLE price_history (
    id               bigserial PRIMARY KEY,
    configuration_id bigint NOT NULL REFERENCES configurations (id) ON DELETE CASCADE,
    observed_at      timestamptz NOT NULL DEFAULT now(),
    price_kind       text NOT NULL,
    item_price       numeric(12, 2),
    shipping_price   numeric(12, 2),
    source           text NOT NULL
);
CREATE INDEX price_history_config_time ON price_history (configuration_id, observed_at DESC);
CREATE INDEX price_history_time ON price_history (observed_at);
