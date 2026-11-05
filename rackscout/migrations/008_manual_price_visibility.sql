-- Recover explicit user-entered amounts hidden by the old price-confirmation gate.
-- Forward-only data correction: never revise user fields, confirmations, timestamps, hardware,
-- source rows or historical observations. Rollback requires the pre-upgrade database backup;
-- older releases reject schema 008. The collector lock prevents old collection writes at cutover.
SELECT pg_advisory_xact_lock(x'53434F55'::bit(32)::bigint);

UPDATE configurations
SET item_price = (manual_data->'fields'->>'item_price')::numeric,
    shipping_price = (manual_data->'fields'->>'shipping_price')::numeric,
    price_kind = 'unknown',
    shipping_context = CASE WHEN manual_data->'fields'->>'shipping_price' IS NOT NULL
        THEN 'Manually entered shipping amount; destination not confirmed; tax excluded'
        ELSE 'Shipping not entered; total unknown (not treated as free)' END,
    warnings = (warnings - 'Price not confirmed for this manual configuration; total unknown')
        || '["Price not confirmed for this manual configuration"]'::jsonb
WHERE origin = 'manual' AND manual_data->>'price_confirmed' = 'false'
    AND (manual_data->'fields'->>'item_price' IS NOT NULL
         OR manual_data->'fields'->>'shipping_price' IS NOT NULL)
    AND item_price IS NULL AND shipping_price IS NULL;
