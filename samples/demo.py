"""Run an isolated demo with synthetic rooms and labels; no persistent database."""
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="room-reservations-demo-") as folder:
        base = [sys.executable, str(ROOT / "room_reservations.py"), "--db", str(Path(folder) / "demo.db")]
        actions = [
            ["init"],
            ["add-room", "Birch", "6"],
            ["add-room", "Maple", "12"],
            ["book", "Birch", "2026-11-02T10:00Z", "2026-11-02T11:00Z", "--people", "3", "--label", "Sample reading group"],
            ["book", "Birch", "2026-11-02T11:00Z", "2026-11-02T11:30Z", "--people", "2", "--label", "Sample pair review"],
            ["book", "Maple", "2026-11-02T10:30Z", "2026-11-02T11:00Z", "--people", "8", "--label", "Sample planning"],
            ["list"],
        ]
        print("Room Reservations — synthetic CLI demo")
        for action in actions:
            result = subprocess.run(base + action, text=True, capture_output=True, check=True)
            print(result.stdout, end="")


if __name__ == "__main__":
    main()
