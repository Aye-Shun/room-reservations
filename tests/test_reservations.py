from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import room_reservations as app

ROOT = Path(__file__).resolve().parents[1]
START = "2026-11-02T10:00Z"
END = "2026-11-02T11:00Z"


class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "rooms.db"
        app.initialize(self.path)
        with app.database(self.path) as connection:
            app.add_room(connection, "Birch", 6)
            app.add_room(connection, "Maple", 12)

    def book(self, room="Birch", start=START, end=END, people=3, label="Sample group"):
        with app.database(self.path) as connection:
            return app.book(connection, room, start, end, people, label)

    def rows(self, **options):
        with app.database(self.path) as connection:
            return app.reservations(connection, **options)

    def cli(self, *arguments, path=None):
        return subprocess.run([sys.executable, str(ROOT / "room_reservations.py"),
                               "--db", str(path or self.path), *arguments],
                              text=True, capture_output=True, timeout=15)

    def test_booking_persists_with_capacity(self):
        booking_id = self.book()
        row = self.rows()[0]
        self.assertEqual((row["id"], row["room"], row["people"], row["capacity"], row["cancelled"]),
                         (booking_id, "Birch", 3, 6, 0))
        self.assertEqual(row["starts_at"], START)

    def test_each_overlap_shape_rejected(self):
        self.book()
        for start, end in [(START, END), ("2026-11-02T10:15Z", "2026-11-02T10:45Z"),
                           ("2026-11-02T09:00Z", "2026-11-02T12:00Z"),
                           ("2026-11-02T09:30Z", "2026-11-02T10:30Z"),
                           ("2026-11-02T10:30Z", "2026-11-02T11:30Z")]:
            with self.subTest(start=start, end=end), self.assertRaisesRegex(app.ReservationError, "overlaps"):
                self.book(start=start, end=end)
        self.assertEqual(len(self.rows()), 1)

    def test_adjacent_intervals_are_allowed(self):
        self.book()
        self.book(start="2026-11-02T09:00Z", end=START)
        self.book(start=END, end="2026-11-02T12:00Z")
        self.assertEqual(len(self.rows()), 3)

    def test_other_rooms_can_overlap(self):
        self.book()
        self.book(room="Maple", people=12)
        self.assertEqual(len(self.rows()), 2)

    def test_capacity_rejection_leaves_no_partial_booking(self):
        with self.assertRaisesRegex(app.ReservationError, "capacity"):
            self.book(people=7)
        self.assertEqual(self.rows(), [])
        self.assertIsInstance(self.book(people=6), int)

    def test_unknown_room_and_case_sensitive_names(self):
        for name in ["missing", "birch"]:
            with self.assertRaisesRegex(app.ReservationError, "unknown room"):
                self.book(room=name)
        self.assertEqual(self.rows(), [])

    def test_cancel_retains_row_and_releases_interval(self):
        booking_id = self.book()
        with app.database(self.path) as connection:
            app.cancel(connection, booking_id)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.rows(include_cancelled=True)[0]["cancelled"], 1)
        replacement = self.book()
        self.assertNotEqual(booking_id, replacement)
        self.assertEqual(len(self.rows(include_cancelled=True)), 2)

    def test_repeated_or_unknown_cancellation_is_rejected(self):
        booking_id = self.book()
        with app.database(self.path) as connection:
            app.cancel(connection, booking_id)
            for identifier in [booking_id, 999, 0, -1, True, 2**63]:
                with self.assertRaises(app.ReservationError):
                    app.cancel(connection, identifier)
        self.assertEqual(len(self.rows(include_cancelled=True)), 1)

    def test_invalid_calendar_or_noncanonical_times(self):
        for value in ["2026-02-30T10:00Z", "2026-11-02T24:00Z", "2026-11-02T10:60Z",
                      "2026-11-02T10:00:00Z", "2026-11-02T10:00+00:00", "2026-1-2T10:00Z",
                      "2026-11-02 10:00Z", "2026-11-02T10:00", "0000-01-01T00:00Z"]:
            with self.subTest(value=value), self.assertRaises(app.ReservationError):
                self.book(start=value)
        self.assertEqual(self.rows(), [])

    def test_zero_or_reverse_duration_rejected(self):
        for start, end in [(START, START), (END, START)]:
            with self.assertRaisesRegex(app.ReservationError, "end must be after start"):
                self.book(start=start, end=end)

    def test_positive_integer_limits(self):
        for value in [0, -1, True, 1.5, "3", 10001, 2**100]:
            with self.subTest(value=value), self.assertRaises(app.ReservationError):
                self.book(people=value)
            with app.database(self.path) as connection, self.assertRaises(app.ReservationError):
                app.add_room(connection, "Bad", value)

    def test_text_limits_and_terminal_controls(self):
        for value in ["", " padded", "padded ", "line\nbreak", "\x1b[31m", "a" * 101]:
            with self.subTest(value=value), self.assertRaises(app.ReservationError):
                self.book(label=value)
        with app.database(self.path) as connection:
            for value in ["", "\t", "a" * 61]:
                with self.assertRaises(app.ReservationError):
                    app.add_room(connection, value, 5)

    def test_duplicate_room_rollback_keeps_database_usable(self):
        with app.database(self.path) as connection:
            with self.assertRaisesRegex(app.ReservationError, "already exists"):
                app.add_room(connection, "Birch", 12)
            self.assertFalse(connection.in_transaction)
            app.add_room(connection, "Cedar", 4)
            self.assertEqual(connection.execute("SELECT capacity FROM rooms WHERE name='Birch'").fetchone()[0], 6)

    def test_failed_overlap_transaction_releases_lock(self):
        self.book()
        with app.database(self.path) as connection:
            with self.assertRaises(app.ReservationError):
                app.book(connection, "Birch", START, END, 2, "Conflict")
            self.assertFalse(connection.in_transaction)
            app.book(connection, "Maple", START, END, 2, "Other room")
        self.assertEqual(len(self.rows()), 2)

    def test_list_filter_order_and_empty_output(self):
        with app.database(self.path) as connection:
            text = StringIO()
            with redirect_stdout(text):
                app.print_reservations(app.reservations(connection))
            self.assertIn("No reservations.", text.getvalue())
            with self.assertRaises(app.ReservationError):
                app.reservations(connection, "Unknown")
        self.book(start=END, end="2026-11-02T12:00Z")
        self.book(start=START, end=END)
        self.book(room="Maple")
        self.assertEqual([row["starts_at"] for row in self.rows(room="Birch")], [START, END])

    def test_init_is_idempotent_and_preserves_rows(self):
        self.book()
        before = self.path.read_bytes()
        app.initialize(self.path)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_other_commands_do_not_create_missing_database(self):
        missing = Path(self.folder.name) / "missing.db"
        result = self.cli("list", path=missing)
        self.assertEqual(result.returncode, 1)
        self.assertIn("run init first", result.stderr)
        self.assertFalse(missing.exists())

    def test_unrelated_sqlite_and_plain_file_are_preserved(self):
        foreign = Path(self.folder.name) / "foreign.db"
        with sqlite3.connect(foreign) as connection:
            connection.execute("CREATE TABLE keep_me(value TEXT)")
            connection.execute("INSERT INTO keep_me VALUES ('untouched')")
        plain = Path(self.folder.name) / "notes.txt"
        plain.write_text("This file is not a database.\n")
        for path in [foreign, plain]:
            with self.subTest(path=path):
                before = path.read_bytes()
                result = self.cli("init", path=path)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(path.read_bytes(), before)

    def test_schema_version_or_added_table_is_not_silently_migrated(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE TABLE extra(value TEXT)")
        before = self.path.read_bytes()
        result = self.cli("init")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_sql_values_are_parameters(self):
        name = "O'Brien; --"
        label = "x'); DROP TABLE rooms; --"
        with app.database(self.path) as connection:
            app.add_room(connection, name, 6)
        self.book(room=name, label=label)
        self.assertEqual(self.rows(room=name)[0]["label"], label)
        self.assertEqual(self.book(), 2)

    def test_foreign_keys_and_constraints_enabled(self):
        with app.database(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO reservations(room_id,starts_at,ends_at,people,label) VALUES(999,?,?,1,'Sample')", (START, END))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO rooms(name,capacity) VALUES('Invalid',0)")

    def test_concurrent_cli_writers_only_one_booking_succeeds(self):
        command = [sys.executable, str(ROOT / "room_reservations.py"), "--db", str(self.path),
                   "book", "Birch", START, END, "--people", "2", "--label"]
        first = subprocess.Popen(command + ["First sample"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = subprocess.Popen(command + ["Second sample"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        results = []
        try:
            for process in [first, second]:
                stdout, stderr = process.communicate(timeout=15)
                results.append((process.returncode, stdout, stderr))
        finally:
            for process in [first, second]:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertEqual(sorted(code for code, _, _ in results), [0, 1], results)
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue(any("overlaps reservation" in stderr for _, _, stderr in results))

    def test_cli_rejected_request_usage_and_persistence(self):
        self.assertEqual(self.cli("book", "Birch", START, END, "--people", "7", "--label", "Too large").returncode, 1)
        self.assertEqual(self.cli("book", "Birch", START, END, "--people", "oops", "--label", "Invalid").returncode, 2)
        booked = self.cli("book", "Birch", START, END, "--people", "3", "--label", "Sample group")
        self.assertEqual(booked.returncode, 0, booked.stderr)
        self.assertEqual(booked.stdout, "Booked reservation 1.\n")
        self.assertIn("Sample group", self.cli("list").stdout)
        self.assertEqual(self.cli("cancel", "1").returncode, 0)
        self.assertIn("No reservations.", self.cli("list").stdout)
        self.assertIn("cancelled", self.cli("list", "--all").stdout)

    def test_uri_characters_in_database_filename(self):
        path = Path(self.folder.name) / "rooms?# ü.db"
        app.initialize(path)
        with app.database(path) as connection:
            app.add_room(connection, "Birch", 6)
        self.assertEqual(self.cli("list", path=path).returncode, 0)
        self.assertTrue(path.exists())

    def test_synthetic_demo_matches_expected(self):
        result = subprocess.run([sys.executable, str(ROOT / "samples/demo.py")], text=True,
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, (ROOT / "samples/expected.txt").read_text())
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
