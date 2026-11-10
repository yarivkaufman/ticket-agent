"""SQLite store: tracked events, price snapshots, what has been announced, alerts, runs."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    request TEXT,                 -- what was typed when adding it
    starts_local TEXT,            -- venue local time, e.g. 2026-11-21T19:30:00
    starts_utc TEXT,
    venue TEXT,
    quantity INTEGER NOT NULL DEFAULT 2,
    max_price REAL,               -- target: all-in per ticket
    seatgeek_id TEXT,
    seatgeek_url TEXT,
    tm_id TEXT,
    tm_url TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_scan_at TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (   -- one row per source per check (chart + history)
    event_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    source TEXT NOT NULL,
    lowest REAL,
    average REAL,
    highest REAL,
    listings INTEGER,
    status TEXT
);
CREATE INDEX IF NOT EXISTS snapshots_event ON snapshots(event_id, at);
CREATE TABLE IF NOT EXISTS state (       -- what has been announced per event
    event_id INTEGER PRIMARY KEY,
    low_announced REAL,
    listings_announced INTEGER,
    under_target INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    kinds TEXT NOT NULL,          -- JSON list: target, low, listings
    text TEXT NOT NULL,
    sent_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    events INTEGER,
    alerts INTEGER,
    errors TEXT                   -- JSON list
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def local_time(s: str | None, tz: str) -> str:
    if not s:
        return ""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ZoneInfo(tz)).strftime("%b %d %H:%M")


def event_time(starts_local: str | None) -> str:
    """'2026-11-21T19:30:00' -> 'Sat Nov 21, 7:30 PM' (already venue-local)."""
    if not starts_local:
        return "date TBA"
    dt = datetime.fromisoformat(starts_local)
    if dt.hour == 0 and dt.minute == 0:
        return dt.strftime("%a %b %d")
    return dt.strftime("%a %b %d, %-I:%M %p")


class DB:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # --- events -----------------------------------------------------------------------------

    def add_event(self, **fields: Any) -> int:
        fields.setdefault("created_at", now())
        cols = ", ".join(fields)
        with self.conn() as c:
            cur = c.execute(f"INSERT INTO events ({cols}) VALUES ({', '.join('?' * len(fields))})",
                            list(fields.values()))
            return cur.lastrowid

    def event(self, event_id: int) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()

    def events(self, active: bool | None = True) -> list[sqlite3.Row]:
        q = "SELECT * FROM events"
        args: tuple = ()
        if active is not None:
            q += " WHERE active = ?"
            args = (int(active),)
        with self.conn() as c:
            return c.execute(q + " ORDER BY starts_local IS NULL, starts_local, id", args).fetchall()

    def update_event(self, event_id: int, **fields: Any) -> bool:
        if not fields:
            return False
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.conn() as c:
            return c.execute(f"UPDATE events SET {sets} WHERE id = ?",
                             [*fields.values(), event_id]).rowcount > 0

    def delete_event(self, event_id: int) -> None:
        with self.conn() as c:
            for t in ("snapshots", "state", "alerts"):
                c.execute(f"DELETE FROM {t} WHERE event_id = ?", (event_id,))
            c.execute("DELETE FROM events WHERE id = ?", (event_id,))

    # --- snapshots ----------------------------------------------------------------------------

    def add_snapshot(self, event_id: int, at: str, source: str, lowest: float | None,
                     average: float | None, highest: float | None, listings: int | None,
                     status: str | None = None) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (event_id, at, source, lowest, average, highest, listings, status))

    def latest(self, event_id: int) -> dict[str, sqlite3.Row]:
        """The most recent snapshot per source."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT s.* FROM snapshots s JOIN (SELECT source, MAX(at) AS at FROM snapshots "
                "WHERE event_id = ? GROUP BY source) m ON s.source = m.source AND s.at = m.at "
                "WHERE s.event_id = ?", (event_id, event_id)).fetchall()
        return {r["source"]: r for r in rows}

    def history(self, event_id: int, source: str = "seatgeek", days: int = 120) -> list[sqlite3.Row]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        with self.conn() as c:
            return c.execute("SELECT at, lowest, listings FROM snapshots WHERE event_id = ? AND "
                             "source = ? AND at >= ? ORDER BY at", (event_id, source, since)).fetchall()

    def lowest_seen(self, event_id: int) -> float | None:
        with self.conn() as c:
            r = c.execute("SELECT MIN(lowest) FROM snapshots WHERE event_id = ? AND source = 'seatgeek'",
                          (event_id,)).fetchone()
        return r[0] if r else None

    # --- announced state ------------------------------------------------------------------------

    def state(self, event_id: int) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute("SELECT * FROM state WHERE event_id = ?", (event_id,)).fetchone()

    def set_state(self, event_id: int, low: float | None, listings: int | None, under_target: bool) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO state VALUES (?, ?, ?, ?, ?) ON CONFLICT(event_id) DO UPDATE SET "
                      "low_announced = excluded.low_announced, listings_announced = "
                      "excluded.listings_announced, under_target = excluded.under_target, "
                      "updated_at = excluded.updated_at",
                      (event_id, low, listings, int(under_target), now()))

    def reset_state(self, event_id: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM state WHERE event_id = ?", (event_id,))

    # --- alerts -------------------------------------------------------------------------------

    def add_alert(self, event_id: int, kinds: list[str], text: str) -> int:
        with self.conn() as c:
            return c.execute("INSERT INTO alerts (event_id, at, kinds, text) VALUES (?, ?, ?, ?)",
                             (event_id, now(), json.dumps(kinds), text)).lastrowid

    def unsent_alerts(self) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute("SELECT * FROM alerts WHERE sent_at IS NULL ORDER BY id").fetchall()

    def mark_sent(self, alert_id: int) -> None:
        with self.conn() as c:
            c.execute("UPDATE alerts SET sent_at = ? WHERE id = ?", (now(), alert_id))

    def alerts(self, event_id: int | None = None, limit: int = 50) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM alerts", ()
        if event_id is not None:
            q, args = q + " WHERE event_id = ?", (event_id,)
        with self.conn() as c:
            return c.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()

    # --- runs -----------------------------------------------------------------------------------

    def start_run(self) -> int:
        with self.conn() as c:
            return c.execute("INSERT INTO runs (started_at) VALUES (?)", (now(),)).lastrowid

    def finish_run(self, run_id: int, events: int, alerts: int, errors: list[str]) -> None:
        with self.conn() as c:
            c.execute("UPDATE runs SET finished_at = ?, events = ?, alerts = ?, errors = ? WHERE id = ?",
                      (now(), events, alerts, json.dumps(errors), run_id))

    def last_run(self) -> sqlite3.Row | None:
        runs = self.recent_runs(1)
        return runs[0] if runs else None

    def recent_runs(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
