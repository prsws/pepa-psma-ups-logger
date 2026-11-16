#!/usr/bin/env python3
"""PSMA UPS logger: part of the Pepa Self Monitoring Arm (PSMA).

https://github.com/prsws/pepa-psma-ups-logger

Reads the UPS straight from NUT (not through Home Assistant), writes every record
to a local journal first, then to Postgres (database psma, schema ups).
Append-only: rows are only ever inserted.

- readings: every INTERVAL_MAINS s on mains, every INTERVAL_BATTERY s on battery
- events:   every ups.status change, loss/return of contact with NUT,
            logger start/stop, and recording gaps found at start-up
- facts:    model, serial, thresholds etc., recorded when they change

If Postgres is unreachable, records wait in pending.jsonl and are drained
(in order) on the next successful connection. The journal is never trimmed
by this program.

Every record carries a rec_id (a random UUID) given when it is made. The
database ignores a rec_id it already has, so a record sent twice is stored
once. A line in pending.jsonl that cannot be read (for example, half-written
when the power went out) is moved aside to pending-unreadable.jsonl instead
of blocking every later write.
"""
import datetime as dt
import json
import os
import signal
import socket
import sys
import time
import uuid

import psycopg2
from psycopg2.extras import Json

NUT_HOST = os.environ.get("NUT_HOST", "127.0.0.1")
NUT_PORT = int(os.environ.get("NUT_PORT", "3493"))
UPS = os.environ["UPS_NAME"]  # the UPS name as NUT knows it (upsc -l)
PG_DSN = os.environ["PG_DSN"]
STATE_DIR = os.environ.get("STATE_DIR", "/var/lib/psma/ups")
INTERVAL_MAINS = int(os.environ.get("INTERVAL_MAINS", "30"))
INTERVAL_BATTERY = int(os.environ.get("INTERVAL_BATTERY", "5"))
RETRY_NO_NUT = 10
GAP_FACTOR = 3  # a silence longer than 3 mains intervals is a gap

STATE_FILE = os.path.join(STATE_DIR, "state.json")
PENDING_FILE = os.path.join(STATE_DIR, "pending.jsonl")
UNREADABLE_FILE = os.path.join(STATE_DIR, "pending-unreadable.jsonl")

FACT_KEYS = [
    "device.mfr", "device.model", "device.serial", "device.type",
    "ups.mfr", "ups.model", "ups.serial", "ups.vendorid", "ups.productid",
    "battery.type", "battery.mfr.date", "battery.voltage.nominal",
    "battery.charge.low", "battery.charge.warning", "battery.runtime.low",
    "input.voltage.nominal", "ups.realpower.nominal",
    "ups.delay.shutdown", "ups.delay.start", "ups.beeper.status",
    "ups.test.result",
    "driver.name", "driver.version", "driver.version.data",
    "driver.flag.allow_killpower", "driver.parameter.pollfreq",
    "driver.parameter.pollinterval",
]

# Names for status flags appearing (+) or disappearing (-). Any other change is "status_change".
EVENT_NAMES = {
    "OB+": "power_lost", "OL+": "power_restored",
    "LB+": "low_battery", "LB-": "low_battery_cleared",
    "RB+": "replace_battery", "RB-": "replace_battery_cleared",
    "FSD+": "forced_shutdown", "FSD-": "forced_shutdown_cleared",
    "OVER+": "overload", "OVER-": "overload_cleared",
    "BYPASS+": "on_bypass", "BYPASS-": "off_bypass",
    "OFF+": "ups_output_off", "OFF-": "ups_output_on",
    "ALARM+": "alarm", "ALARM-": "alarm_cleared",
    "CAL+": "calibration_started", "CAL-": "calibration_ended",
}
# Order of preference when one status change carries several flags.
EVENT_PRIORITY = ["FSD+", "LB+", "OB+", "OL+", "RB+", "OVER+", "OFF+", "BYPASS+",
                  "ALARM+", "LB-", "RB-", "OVER-", "OFF-", "BYPASS-", "ALARM-",
                  "FSD-", "CAL+", "CAL-"]

RUNNING = True


class NutError(Exception):
    pass


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.isoformat(timespec="milliseconds")


def new_rec_id():
    return str(uuid.uuid4())


def log(msg):
    print(msg, flush=True)  # goes to the systemd journal


# ---------------------------------------------------------------- NUT

def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1]
    return v.replace('\\"', '"').replace("\\\\", "\\")


def nut_list_vars(ups):
    """Plain NUT network protocol, read-only, no login needed for LIST VAR."""
    with socket.create_connection((NUT_HOST, NUT_PORT), timeout=5) as s:
        f = s.makefile("rw", encoding="utf-8", newline="\n")
        f.write(f"LIST VAR {ups}\n")
        f.flush()
        first = f.readline().strip()
        if first.startswith("ERR"):
            raise NutError(first)
        if not first.startswith("BEGIN LIST VAR"):
            raise NutError(f"unexpected reply: {first!r}")
        out = {}
        for line in f:
            line = line.strip()
            if line.startswith("END LIST VAR"):
                break
            if line.startswith("ERR"):
                raise NutError(line)
            parts = line.split(" ", 3)  # VAR <ups> <name> "<value>"
            if len(parts) == 4 and parts[0] == "VAR":
                out[parts[2]] = _unquote(parts[3])
        else:
            raise NutError("connection closed mid-list")
        try:
            f.write("LOGOUT\n")
            f.flush()
        except OSError:
            pass
        return out


def num(v, cast=float):
    try:
        return cast(float(v))
    except (TypeError, ValueError):
        return None


def flags(status):
    return set((status or "").split())


def on_battery(status):
    fl = flags(status)
    return "OB" in fl or "LB" in fl or ("DISCHRG" in fl and "OL" not in fl)


# ---------------------------------------------------------------- local files

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)


def _ends_torn(path):
    """True if the file exists, is not empty and its last byte is not a newline,
    which is what a write cut short by a power loss leaves behind."""
    try:
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            return f.read(1) != b"\n"
    except (FileNotFoundError, OSError):  # missing or empty file
        return False


def append_raw(path, text):
    """Append text as whole lines. If the file ends in a torn line, close it off
    first, so the new line is never glued onto the broken one."""
    data = text.encode("utf-8")
    if _ends_torn(path):
        data = b"\n" + data
    with open(path, "ab") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def append_line(path, rec):
    append_raw(path, json.dumps(rec, separators=(",", ":")) + "\n")


def read_pending():
    """Return (records, unreadable_lines) from the pending file."""
    good, bad = [], []
    if not os.path.exists(PENDING_FILE):
        return good, bad
    with open(PENDING_FILE, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                bad.append(line.rstrip("\n"))
                continue
            if isinstance(rec, dict) and rec.get("kind") in ("reading", "event", "facts"):
                good.append(rec)
            else:
                bad.append(line.rstrip("\n"))
    return good, bad


def journal(rec):
    day = rec["ts"][:10]
    append_line(os.path.join(STATE_DIR, f"journal-{day}.jsonl"), rec)


# ---------------------------------------------------------------- Postgres

class Store:
    def __init__(self):
        self.conn = None

    def _connect(self):
        if self.conn is None or self.conn.closed:
            self.conn = psycopg2.connect(PG_DSN, connect_timeout=5,
                                         application_name="psma-ups-logger")
            self.conn.autocommit = False

    @staticmethod
    def _insert(cur, rec):
        k = rec["kind"]
        if k == "reading":
            cur.execute(
                """INSERT INTO ups.reading (rec_id, ts, ups_name, ups_serial, status, load_pct,
                   charge_pct, runtime_s, battery_v, input_v, output_v, raw)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (rec_id) DO NOTHING""",
                (rec.get("rec_id"), rec["ts"], rec["ups_name"], rec["ups_serial"], rec["status"],
                 rec["load_pct"], rec["charge_pct"], rec["runtime_s"],
                 rec["battery_v"], rec["input_v"], rec["output_v"], Json(rec["raw"])))
        elif k == "event":
            cur.execute(
                """INSERT INTO ups.event (rec_id, ts, ups_name, ups_serial, event, from_status,
                   to_status, detail) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (rec_id) DO NOTHING""",
                (rec.get("rec_id"), rec["ts"], rec["ups_name"], rec["ups_serial"], rec["event"],
                 rec.get("from_status"), rec.get("to_status"), Json(rec.get("detail") or {})))
        elif k == "facts":
            cur.execute(
                """INSERT INTO ups.facts (rec_id, ts, ups_name, ups_serial, facts)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT (rec_id) DO NOTHING""",
                (rec.get("rec_id"), rec["ts"], rec["ups_name"], rec["ups_serial"], Json(rec["facts"])))

    def write(self, records):
        """Drain pending first (order matters), then the new records. Never raises."""
        try:
            self._connect()
            pending, unreadable = read_pending()
            with self.conn.cursor() as cur:
                for rec in pending + records:
                    self._insert(cur, rec)
            self.conn.commit()
            # Committed. A crash from here on can only cause a resend, and the
            # database ignores rec_ids it already has.
            if unreadable:
                append_raw(UNREADABLE_FILE, "".join(l + "\n" for l in unreadable))
                log(f"moved {len(unreadable)} unreadable pending line(s) to {UNREADABLE_FILE}")
            if pending or unreadable:
                os.remove(PENDING_FILE)
            if pending:
                log(f"drained {len(pending)} pending record(s) to Postgres")
            return True
        except Exception as e:  # noqa: BLE001 - any DB failure means: keep it local
            log(f"postgres unavailable ({type(e).__name__}: {e}); keeping records local")
            try:
                if self.conn is not None:
                    self.conn.close()
            except Exception:  # noqa: BLE001
                pass
            self.conn = None
            for rec in records:
                append_line(PENDING_FILE, rec)
            return False


# ---------------------------------------------------------------- records

def make_event(name, serial, from_status=None, to_status=None, detail=None, ts=None):
    return {"kind": "event", "rec_id": new_rec_id(), "ts": iso(ts or now_utc()), "ups_name": UPS,
            "ups_serial": serial, "event": name, "from_status": from_status,
            "to_status": to_status, "detail": detail or {}}


def status_events(old, new, serial, ts):
    if old is None or old == new:
        return []
    o, n = flags(old), flags(new)
    changes = [f"{x}+" for x in sorted(n - o)] + [f"{x}-" for x in sorted(o - n)]
    named = [c for c in EVENT_PRIORITY if c in changes]
    name = EVENT_NAMES[named[0]] if named else "status_change"
    return [make_event(name, serial, old, new,
                       {"added": sorted(n - o), "removed": sorted(o - n)}, ts)]


def boot_time():
    with open("/proc/uptime", encoding="ascii") as f:
        up = float(f.read().split()[0])
    return now_utc() - dt.timedelta(seconds=up)


def main():
    global RUNNING
    os.makedirs(STATE_DIR, exist_ok=True)
    store = Store()
    state = load_state()
    serial = state.get("serial")

    def stop(signum, _frame):
        global RUNNING
        RUNNING = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    # --- start-up: logger_started, plus a gap event if we were silent too long
    start = now_utc()
    boot = boot_time()
    startup = [make_event("logger_started", serial, detail={
        "boot_time": iso(boot), "interval_mains_s": INTERVAL_MAINS,
        "interval_battery_s": INTERVAL_BATTERY}, ts=start)]
    last_seen = state.get("last_ts")
    if last_seen:
        last_dt = dt.datetime.fromisoformat(last_seen)
        gap = (start - last_dt).total_seconds()
        if gap > GAP_FACTOR * INTERVAL_MAINS:
            startup.append(make_event("recording_gap", serial, detail={
                "last_record": last_seen, "gap_s": round(gap),
                "host_rebooted_during_gap": boot > last_dt,
                "clean_stop_before_gap": bool(state.get("clean_stop")),
                "last_status": state.get("status")}, ts=start))
    state["clean_stop"] = False
    for r in startup:
        journal(r)
    store.write(startup)
    save_state(state)
    log(f"started: ups={UPS} nut={NUT_HOST}:{NUT_PORT} state_dir={STATE_DIR}")

    nut_ok = True
    nut_down_since = None
    while RUNNING:
        t0 = time.monotonic()
        ts = now_utc()
        records = []
        try:
            v = nut_list_vars(UPS)
        except (OSError, NutError) as e:
            if nut_ok:
                nut_ok, nut_down_since = False, ts
                ev = make_event("nut_contact_lost", serial,
                                from_status=state.get("status"),
                                detail={"error": str(e)}, ts=ts)
                journal(ev)
                store.write([ev])
                log(f"NUT contact lost: {e}")
            _sleep(RETRY_NO_NUT, t0)
            continue

        serial = v.get("ups.serial") or v.get("device.serial") or serial
        status = v.get("ups.status")
        if not nut_ok:
            records.append(make_event("nut_contact_restored", serial, to_status=status, detail={
                "down_s": round((ts - nut_down_since).total_seconds())}, ts=ts))
            nut_ok, nut_down_since = True, None
            log("NUT contact restored")

        records += status_events(state.get("status"), status, serial, ts)

        facts = {k: v[k] for k in FACT_KEYS if k in v}
        if facts != state.get("facts"):
            records.append({"kind": "facts", "rec_id": new_rec_id(), "ts": iso(ts), "ups_name": UPS,
                            "ups_serial": serial, "facts": facts})

        records.append({
            "kind": "reading", "rec_id": new_rec_id(), "ts": iso(ts), "ups_name": UPS, "ups_serial": serial,
            "status": status,
            "load_pct": num(v.get("ups.load")),
            "charge_pct": num(v.get("battery.charge")),
            "runtime_s": num(v.get("battery.runtime"), int),
            "battery_v": num(v.get("battery.voltage")),
            "input_v": num(v.get("input.voltage")),
            "output_v": num(v.get("output.voltage")),
            "raw": v,
        })

        for r in records:
            journal(r)
        store.write(records)

        state.update({"last_ts": iso(ts), "status": status, "facts": facts, "serial": serial})
        save_state(state)

        _sleep(INTERVAL_BATTERY if on_battery(status) else INTERVAL_MAINS, t0)

    # --- clean stop
    ev = make_event("logger_stopped", serial, to_status=state.get("status"))
    journal(ev)
    store.write([ev])
    state["clean_stop"] = True
    state["last_ts"] = ev["ts"]
    save_state(state)
    log("stopped cleanly")
    return 0


def _sleep(seconds, t0):
    end = t0 + seconds
    while RUNNING:
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(left, 1.0))


if __name__ == "__main__":
    sys.exit(main())
