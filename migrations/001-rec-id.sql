-- Only for databases created from the first schema, before rec_id existed.
-- New installs: schema.sql already has rec_id; skip this file.
-- Run as postgres BEFORE starting the new logger:
--   psql -U postgres -d psma -v ON_ERROR_STOP=1 -f migrations/001-rec-id.sql
-- Existing rows keep rec_id NULL; NULLs never conflict, so nothing old is touched.
BEGIN;
ALTER TABLE ups.reading ADD COLUMN rec_id uuid UNIQUE;
ALTER TABLE ups.event   ADD COLUMN rec_id uuid UNIQUE;
ALTER TABLE ups.facts   ADD COLUMN rec_id uuid UNIQUE;
COMMENT ON COLUMN ups.reading.rec_id IS 'Set by the logger when the record is made; a resent record is ignored (ON CONFLICT DO NOTHING).';
COMMENT ON COLUMN ups.event.rec_id   IS 'Set by the logger when the record is made; a resent record is ignored (ON CONFLICT DO NOTHING).';
COMMENT ON COLUMN ups.facts.rec_id   IS 'Set by the logger when the record is made; a resent record is ignored (ON CONFLICT DO NOTHING).';
COMMIT;
