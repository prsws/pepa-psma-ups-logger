-- Record, by hand, what a UPS powers. Run as postgres against the psma database:
--   psql -U postgres -d psma -f examples/feed.example.sql
-- Rows are never edited: when something changes, add a new row with today's date.
INSERT INTO ups.feed (ups_name, ups_serial, feeds, note, asserted_by) VALUES
 ('myups', 'SERIAL-FROM-upsc', 'the machine running this logger',
  'Example: why this load is on this UPS, and until when.',
  'Your name, 1 Jan 2027');
