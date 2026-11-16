-- Database for the PSMA UPS logger. Run once, as the postgres superuser:
--   psql -U postgres -v pw='<password for psma_ups>' -v ON_ERROR_STOP=1 -f schema.sql
-- Creates the role psma_ups, the database psma and the schema ups.
-- The logger may insert and read rows, never update or delete them.

CREATE ROLE psma_ups LOGIN PASSWORD :'pw';
CREATE DATABASE psma OWNER postgres ENCODING 'UTF8';
\connect psma
REVOKE ALL ON DATABASE psma FROM PUBLIC;
GRANT CONNECT ON DATABASE psma TO psma_ups;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE SCHEMA ups;
COMMENT ON SCHEMA ups IS 'UPS records written by psma-ups-logger. Append-only: the writer may insert and read, never update or delete. Timestamps are UTC.';
GRANT USAGE ON SCHEMA ups TO psma_ups;

CREATE TABLE ups.reading (
  rec_id      uuid UNIQUE,  -- set by the logger; a resent record is ignored
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts          timestamptz NOT NULL,
  ups_name    text NOT NULL,
  ups_serial  text,
  status      text,
  load_pct    numeric(5,1),
  charge_pct  numeric(5,1),
  runtime_s   integer,
  battery_v   numeric(6,2),
  input_v     numeric(6,1),
  output_v    numeric(6,1),
  raw         jsonb NOT NULL
);
CREATE INDEX reading_serial_ts ON ups.reading (ups_serial, ts);
COMMENT ON TABLE ups.reading IS 'Periodic readings: every 30 s on mains, every 5 s on battery by default. raw = full NUT variable dump.';

CREATE TABLE ups.event (
  rec_id       uuid UNIQUE,  -- set by the logger; a resent record is ignored
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts           timestamptz NOT NULL,
  ups_name     text NOT NULL,
  ups_serial   text,
  event        text NOT NULL,
  from_status  text,
  to_status    text,
  detail       jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX event_ts ON ups.event (ts);
CREATE INDEX event_event_ts ON ups.event (event, ts);
COMMENT ON TABLE ups.event IS 'power_lost, power_restored, low_battery, replace_battery, forced_shutdown, status_change, nut_contact_lost/restored, logger_started/stopped, recording_gap ...';

CREATE TABLE ups.facts (
  rec_id      uuid UNIQUE,  -- set by the logger; a resent record is ignored
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts          timestamptz NOT NULL,
  ups_name    text NOT NULL,
  ups_serial  text,
  facts       jsonb NOT NULL
);
COMMENT ON TABLE ups.facts IS 'Fixed UPS facts (model, serial, thresholds, driver). A new row only when something changed.';

CREATE TABLE ups.feed (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts           timestamptz NOT NULL DEFAULT now(),
  ups_name     text NOT NULL,
  ups_serial   text,
  feeds        text NOT NULL,
  note         text,
  asserted_by  text NOT NULL
);
COMMENT ON TABLE ups.feed IS 'What each UPS powers, written by a person as dated assertions. A newer row supersedes an older one for the same load; rows are never edited. See examples/feed.example.sql.';

GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA ups TO psma_ups;
