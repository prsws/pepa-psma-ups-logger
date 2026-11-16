# pepa-psma-ups-logger

Records what a UPS (battery backup) reports, straight from
[Network UPS Tools](https://networkupstools.org/) into PostgreSQL. It is the first
working helper of the **Pepa Self Monitoring Arm (PSMA)**, the part of
[Pepa](https://ai4aging.org) that watches Pepa itself. The arm is described in
[Pepa Looks After Itself](https://ai4aging.org/arms/self-monitoring/the-self-monitoring-arm/).

> **Experimental. Records only.** This program reads and writes down. It sends no
> alerts and never shuts anything down. It is not a safety system and not a medical
> device.

## What it records

| Table | What | When |
| --- | --- | --- |
| `ups.reading` | Status, load, charge, runtime, voltages, plus every NUT variable as JSON | Every 30 s on mains, every 5 s on battery |
| `ups.event` | `power_lost`, `power_restored`, `low_battery`, `replace_battery`, `forced_shutdown`, other status changes, `nut_contact_lost` / `nut_contact_restored`, `logger_started` / `logger_stopped`, and `recording_gap` (with whether the host rebooted or the logger was stopped cleanly) | When it happens |
| `ups.facts` | Model, serial, thresholds, driver | Only when something changed |
| `ups.feed` | What the UPS powers, written by a person (see `examples/feed.example.sql`) | By hand |

The database account it uses can **insert and read, never update or delete**, so what
was recorded stays recorded. All timestamps are UTC.

## How it stays reliable

- **Local file first.** Every record goes to a daily file
  (`journal-YYYY-MM-DD.jsonl` in the state directory) before the database.
- **Database down? Nothing is lost.** Records wait in `pending.jsonl` and go in, in
  order, on the next successful connection.
- **No duplicates.** Every record gets a random `rec_id` when it is made; the
  database ignores an id it already has.
- **Power cut mid-write.** A half-written line in `pending.jsonl` is moved to
  `pending-unreadable.jsonl` and kept, instead of blocking later writes. New lines
  are never glued onto a broken one.
- **Its own gaps.** At start-up it records how long it was silent and why.

## Requirements

- Linux with systemd (tested on Debian 13)
- Python 3.8+ and psycopg2 (`sudo apt install python3-psycopg2`)
- A NUT server that can see the UPS (`upsc -l` lists its name). The logger only
  uses `LIST VAR`, which needs no NUT login.
- PostgreSQL 12 or newer (tested on 18)

## Install

```sh
# 1. Database (as the postgres superuser)
psql -U postgres -v pw='choose-a-password' -v ON_ERROR_STOP=1 -f schema.sql
#    Let the logger's host connect: add a pg_hba.conf line for user psma_ups,
#    database psma, from that host only (scram-sha-256), then reload Postgres.

# 2. A system account and the program
sudo useradd --system --no-create-home --shell /usr/sbin/nologin psma
sudo install -d /opt/psma/ups-logger
sudo install -m 644 psma_ups_logger.py /opt/psma/ups-logger/

# 3. Settings (holds the database password)
sudo install -d /etc/psma
sudo install -m 640 -o root -g psma examples/ups-logger.env.example /etc/psma/ups-logger.env
sudo nano /etc/psma/ups-logger.env        # set PG_DSN and UPS_NAME

# 4. Service
sudo install -m 644 systemd/psma-ups-logger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now psma-ups-logger
journalctl -u psma-ups-logger -f          # expect "started: ups=..."
```

## Upgrading a database made before `rec_id`

If your `psma` database was created from the first version of `schema.sql`, add the
`rec_id` columns **before** starting this version of the logger:

```sh
psql -U postgres -d psma -v ON_ERROR_STOP=1 -f migrations/001-rec-id.sql
```

Existing rows are untouched. Records still waiting in `pending.jsonl` from the old
version are accepted.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The database test runs only when `PSMA_TEST_DSN` points at a throwaway `psma`
database built from `schema.sql`.

## Known limits

- One UPS per running copy.
- The daily journal files are never deleted (a few MB a day). Retention is not
  decided yet.
- A record that reads fine but that the database rejects for another reason would
  still hold up the pending queue. No such case is known.

## License

MIT. See [LICENSE](LICENSE).
