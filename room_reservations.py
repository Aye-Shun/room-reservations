"""Local SQLite room reservations. Times are fixed-format UTC minutes."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import quote

APPLICATION_ID = 0x52523031
VERSION = 1
SCHEMA = {
    "rooms": """CREATE TABLE rooms (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE CHECK(length(name) BETWEEN 1 AND 60),
        capacity INTEGER NOT NULL CHECK(typeof(capacity) = 'integer' AND capacity BETWEEN 1 AND 10000)
    )""",
    "reservations": """CREATE TABLE reservations (
        id INTEGER PRIMARY KEY,
        room_id INTEGER NOT NULL REFERENCES rooms(id),
        starts_at TEXT NOT NULL,
        ends_at TEXT NOT NULL,
        people INTEGER NOT NULL CHECK(typeof(people) = 'integer' AND people BETWEEN 1 AND 10000),
        label TEXT NOT NULL CHECK(length(label) BETWEEN 1 AND 100),
        cancelled INTEGER NOT NULL DEFAULT 0 CHECK(cancelled IN (0, 1)),
        CHECK(length(starts_at) = 17 AND length(ends_at) = 17 AND starts_at < ends_at)
    )""",
    "reservation_times": "CREATE INDEX reservation_times ON reservations(room_id, cancelled, starts_at, ends_at)",
}


class ReservationError(ValueError):
    """An invalid request that has not changed the database."""


def text_value(value, name, limit):
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ReservationError(f"{name} must be nonempty, without surrounding whitespace")
    if len(value) > limit or not value.isprintable():
        raise ReservationError(f"{name} must contain at most {limit} printable characters")
    return value


def positive(value, name):
    if type(value) is not int or not 1 <= value <= 10000:
        raise ReservationError(f"{name} must be an integer from 1 to 10000")
    return value


def utc_minute(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}Z", value):
        raise ReservationError("times must use YYYY-MM-DDTHH:MMZ (UTC)")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%MZ")
    except ValueError as exc:
        raise ReservationError("invalid calendar date or time") from exc
    return value


def _connect(path):
    uri = "file:" + quote(str(Path(path).resolve()), safe="/") + "?mode=rw"
    connection = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _validate_schema(connection):
    app_id = connection.execute("PRAGMA application_id").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    actual = {row["name"]: row["sql"] for row in connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    if app_id != APPLICATION_ID or version != VERSION or actual != SCHEMA:
        raise ReservationError("not a supported Room Reservations database; init will not replace it")


@contextmanager
def database(path):
    if not Path(path).is_file():
        raise ReservationError("database does not exist; run init first")
    connection = _connect(path)
    try:
        _validate_schema(connection)
        yield connection
    finally:
        connection.close()


@contextmanager
def transaction(connection):
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def initialize(path):
    """Create a new database, or validate our existing one without resetting it."""
    created = False
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
        created = True
    connection = _connect(path)
    try:
        with transaction(connection):
            if created:
                for statement in SCHEMA.values():
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {VERSION}")
            _validate_schema(connection)
    finally:
        connection.close()


def add_room(connection, name, capacity):
    name = text_value(name, "room name", 60)
    capacity = positive(capacity, "capacity")
    with transaction(connection):
        if connection.execute("SELECT 1 FROM rooms WHERE name = ?", (name,)).fetchone():
            raise ReservationError("room already exists")
        connection.execute("INSERT INTO rooms(name, capacity) VALUES (?, ?)", (name, capacity))


def find_room(connection, name):
    row = connection.execute("SELECT * FROM rooms WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise ReservationError("unknown room")
    return row


def book(connection, room, start, end, people, label):
    room = text_value(room, "room name", 60)
    label = text_value(label, "label", 100)
    people = positive(people, "people")
    start, end = utc_minute(start), utc_minute(end)
    if start >= end:
        raise ReservationError("end must be after start")
    # Acquire the writer lock before checking; two writers cannot both pass.
    with transaction(connection):
        selected = find_room(connection, room)
        if people > selected["capacity"]:
            raise ReservationError("people exceed room capacity")
        conflict = connection.execute("""SELECT id FROM reservations
            WHERE room_id = ? AND cancelled = 0 AND starts_at < ? AND ends_at > ?""",
            (selected["id"], end, start)).fetchone()
        if conflict:
            raise ReservationError(f"overlaps reservation {conflict['id']}")
        cursor = connection.execute("""INSERT INTO reservations
            (room_id, starts_at, ends_at, people, label) VALUES (?, ?, ?, ?, ?)""",
            (selected["id"], start, end, people, label))
        return cursor.lastrowid


def cancel(connection, reservation_id):
    if type(reservation_id) is not int or not 1 <= reservation_id <= 2**63 - 1:
        raise ReservationError("reservation ID must be a positive SQLite integer")
    with transaction(connection):
        changed = connection.execute("UPDATE reservations SET cancelled = 1 WHERE id = ? AND cancelled = 0",
                                     (reservation_id,)).rowcount
        if not changed:
            raise ReservationError("no active reservation with that ID")


def reservations(connection, room=None, include_cancelled=False):
    conditions, arguments = [], []
    if not include_cancelled:
        conditions.append("b.cancelled = 0")
    if room is not None:
        selected = find_room(connection, room)
        conditions.append("b.room_id = ?")
        arguments.append(selected["id"])
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    return connection.execute("""SELECT b.*, r.name AS room, r.capacity FROM reservations b
        JOIN rooms r ON b.room_id = r.id""" + where + " ORDER BY b.starts_at, r.name, b.id",
        arguments).fetchall()


def print_reservations(rows):
    print("ID | Room | Start UTC -> End UTC | People | Status | Label")
    for row in rows:
        state = "cancelled" if row["cancelled"] else "active"
        print(f"{row['id']} | {row['room']} | {row['starts_at']} -> {row['ends_at']} | "
              f"{row['people']}/{row['capacity']} | {state} | {row['label']}")
    if not rows:
        print("No reservations.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="room_reservations.db", help="SQLite database path")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create or validate the database")
    add = commands.add_parser("add-room", help="add a case-sensitive room name")
    add.add_argument("name")
    add.add_argument("capacity", type=int)
    booking = commands.add_parser("book", help="reserve a room during a UTC interval")
    booking.add_argument("room")
    booking.add_argument("start")
    booking.add_argument("end")
    booking.add_argument("--people", required=True, type=int)
    booking.add_argument("--label", required=True)
    listing = commands.add_parser("list", help="list active reservations")
    listing.add_argument("--room")
    listing.add_argument("--all", action="store_true", help="include cancelled reservations")
    cancellation = commands.add_parser("cancel", help="mark a reservation cancelled; keep its record")
    cancellation.add_argument("id", type=int)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            initialize(args.db)
            print("Database ready.")
        else:
            with database(args.db) as connection:
                if args.command == "add-room":
                    add_room(connection, args.name, args.capacity)
                    print(f"Added room {args.name} ({args.capacity} people).")
                elif args.command == "book":
                    booking_id = book(connection, args.room, args.start, args.end, args.people, args.label)
                    print(f"Booked reservation {booking_id}.")
                elif args.command == "list":
                    print_reservations(reservations(connection, args.room, args.all))
                else:
                    cancel(connection, args.id)
                    print(f"Cancelled reservation {args.id}; record retained.")
    except ReservationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError, OverflowError) as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
