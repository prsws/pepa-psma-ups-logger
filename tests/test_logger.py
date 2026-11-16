"""Tests for psma_ups_logger. Run from the repo root:

    python3 -m unittest discover -s tests -v

Most tests need only Python and psycopg2. The database test runs only when
PSMA_TEST_DSN points at a throwaway psma database built from schema.sql,
for example: PSMA_TEST_DSN="host=127.0.0.1 dbname=psma user=psma_ups password=..."
"""
import importlib.util
import json
import os
import pathlib
import shutil
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = tempfile.mkdtemp(prefix="psma-test-")
os.environ.update(
    STATE_DIR=STATE,
    UPS_NAME="testups",
    PG_DSN=os.environ.get("PSMA_TEST_DSN", "dbname=not_used"),
)
_spec = importlib.util.spec_from_file_location("lg", ROOT / "psma_ups_logger.py")
lg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lg)


def tearDownModule():
    shutil.rmtree(STATE, ignore_errors=True)


class Clean(unittest.TestCase):
    def setUp(self):
        for f in pathlib.Path(STATE).iterdir():
            f.unlink()


class OnBattery(unittest.TestCase):
    def test_cases(self):
        cases = {
            "OL": False, "OL CHRG": False, "OL DISCHRG": False, "": False, None: False,
            "OB": True, "OB DISCHRG": True, "OB LB": True, "LB": True, "DISCHRG": True,
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                self.assertEqual(lg.on_battery(status), expected)


class StatusEvents(unittest.TestCase):
    def test_no_event_without_change(self):
        self.assertEqual(lg.status_events(None, "OL", "S", lg.now_utc()), [])
        self.assertEqual(lg.status_events("OL", "OL", "S", lg.now_utc()), [])

    def test_names(self):
        cases = [("OL", "OB DISCHRG", "power_lost"), ("OB DISCHRG", "OL CHRG", "power_restored"),
                 ("OB DISCHRG", "OB DISCHRG LB", "low_battery"), ("OL", "OL RB", "replace_battery"),
                 ("OL", "OL CHRG", "status_change")]
        for old, new, name in cases:
            with self.subTest(old=old, new=new):
                (ev,) = lg.status_events(old, new, "S", lg.now_utc())
                self.assertEqual(ev["event"], name)


class RecordIds(unittest.TestCase):
    def test_every_event_has_its_own_id(self):
        ids = {lg.make_event("x", "S")["rec_id"] for _ in range(100)}
        self.assertEqual(len(ids), 100)


class TornLines(Clean):
    def test_new_line_is_not_glued_to_a_torn_one(self):
        lg.append_line(lg.PENDING_FILE, lg.make_event("a", "S"))
        with open(lg.PENDING_FILE, "a") as f:
            f.write('{"kind":"event","rec_id":"half-writ')   # power cut mid-write
        lg.append_line(lg.PENDING_FILE, lg.make_event("c", "S"))
        lines = pathlib.Path(lg.PENDING_FILE).read_text().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[2])["event"], "c")

    def test_read_pending_sets_unreadable_lines_aside(self):
        lg.append_line(lg.PENDING_FILE, lg.make_event("a", "S"))
        with open(lg.PENDING_FILE, "a") as f:
            f.write('not json\n["a list"]\n{"kind":"unknown"}\n')
        lg.append_line(lg.PENDING_FILE, lg.make_event("b", "S"))
        good, bad = lg.read_pending()
        self.assertEqual([r["event"] for r in good], ["a", "b"])
        self.assertEqual(len(bad), 3)


@unittest.skipUnless(os.environ.get("PSMA_TEST_DSN"), "set PSMA_TEST_DSN to run the database test")
class Database(Clean):
    def test_drain_quarantine_and_no_duplicates(self):
        import psycopg2
        e1, e2 = lg.make_event("test_a", "S"), lg.make_event("test_b", "S")
        lg.append_line(lg.PENDING_FILE, e1)
        with open(lg.PENDING_FILE, "a") as f:
            f.write('{"torn')
        self.assertTrue(lg.Store().write([e2]))
        self.assertFalse(os.path.exists(lg.PENDING_FILE))
        self.assertIn("torn", pathlib.Path(lg.UNREADABLE_FILE).read_text())
        # crash after commit: the same records are sent again
        lg.append_line(lg.PENDING_FILE, e1)
        self.assertTrue(lg.Store().write([e2]))
        with psycopg2.connect(lg.PG_DSN) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM ups.event WHERE rec_id IN (%s, %s)",
                        (e1["rec_id"], e2["rec_id"]))
            self.assertEqual(cur.fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
