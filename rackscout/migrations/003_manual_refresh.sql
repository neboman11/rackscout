-- Manual refresh queue. Additive: no existing inventory, benchmark, price history
-- or source run data is touched. A single control row serializes requests: at
-- most one request is queued or running, and requested_at (time of the last
-- accepted request) drives the durable cooldown; completion never resets it.
CREATE TABLE refresh_control (
    id           smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    state        text NOT NULL DEFAULT 'idle'
                 CHECK (state IN ('idle', 'queued', 'running', 'succeeded', 'partial', 'blocked', 'failed')),
    request_id   uuid,
    requested_at timestamptz,
    started_at   timestamptz,
    finished_at  timestamptz,
    message      text,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CHECK (state = 'idle' OR (request_id IS NOT NULL AND requested_at IS NOT NULL)),
    CHECK (state NOT IN ('running', 'succeeded', 'partial', 'blocked') OR started_at IS NOT NULL)
);
INSERT INTO refresh_control (id) VALUES (1);
