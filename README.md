# Room Reservations

Book rooms locally with Python and SQLite. Reservations cannot overlap or exceed
capacity; cancellation keeps the original record. The demo uses synthetic data.

With Python 3.11+ (no packages to install):

```sh
python3 room_reservations.py init
python3 room_reservations.py add-room Birch 6
python3 room_reservations.py book Birch 2026-11-02T10:00Z 2026-11-02T11:00Z --people 3 --label "Sample reading group"
python3 room_reservations.py list --room Birch
python3 room_reservations.py cancel 1
python3 room_reservations.py list --all
```

Pass `--db path/to/rooms.db` **before** the command to choose a database. `init`
creates a new file or validates this project's existing schema; it refuses to
replace an unrelated database. Other commands require an initialized file.

Times use `YYYY-MM-DDTHH:MMZ`, in UTC with whole minutes. Intervals include their
start and exclude their end, so adjacent bookings are allowed. A write transaction starts before checking for a conflict, so
concurrent CLI writers cannot both reserve the same interval.

Run an isolated example and the tests:

```sh
python3 samples/demo.py
python3 -m unittest discover -s tests -v
```

The expected demo is in `samples/expected.txt`; tests cover conflicts, capacity,
cancellation, persistence, invalid input, and concurrent writers.

This is a local exercise, not a campus booking service: no accounts, timezone
conversion, recurring events, or room editing. External SQL tools can bypass
booking rules. Exit codes are 0 for success, 1 for rejected requests, and 2 for
usage or database failures.
