"""SQLite storage: users, assignments, per-radio wait queues, bookings, audit trail.

Radios are not stored here — they come from the server config file.

Allocation is race-free by construction: a request claims a radio by inserting
a PENDING row, and partial unique indexes let at most one PENDING-or-ACTIVE row
exist per radio and per callsign. Two simultaneous requests cannot both win,
whatever the interleaving — the loser's INSERT fails and becomes ClaimConflict.
Every later state change is conditional on the state it expects, so a session
that someone else has already ended is never ended (or extended) again.

There is no migration framework: the schema is CREATE ... IF NOT EXISTS, and
the few columns added since 0.1.0 are back-filled by `_add_missing_columns`.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from ..models import (
    LIVE_STATES,
    Assignment,
    AssignmentState,
    AuditEntry,
    Booking,
    BookingException,
    BookingKind,
    BookingMode,
    QueueEntry,
    User,
    isoformat,
    parse_dt,
    utcnow,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    callsign     TEXT PRIMARY KEY,
    api_key_hash TEXT NOT NULL UNIQUE,
    is_admin     INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    radio_id    TEXT NOT NULL,
    callsign    TEXT NOT NULL,
    password    TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    state       TEXT NOT NULL,
    acked       INTEGER NOT NULL DEFAULT 0,
    ended_at    TEXT,
    end_reason  TEXT NOT NULL DEFAULT '',
    client_addr TEXT
);
CREATE INDEX IF NOT EXISTS idx_assignments_state ON assignments(state);
CREATE INDEX IF NOT EXISTS idx_assignments_radio ON assignments(radio_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS uq_live_assignment_radio
    ON assignments(radio_id) WHERE state IN ('pending', 'active');
CREATE UNIQUE INDEX IF NOT EXISTS uq_live_assignment_callsign
    ON assignments(callsign) WHERE state IN ('pending', 'active');

CREATE TABLE IF NOT EXISTS queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    radio_id     TEXT NOT NULL,
    callsign     TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    offered_at   TEXT,
    UNIQUE(radio_id, callsign)
);

CREATE TABLE IF NOT EXISTS audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit(timestamp);

CREATE TABLE IF NOT EXISTS bookings (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    radio_id           TEXT NOT NULL,
    owner              TEXT NOT NULL,
    kind               TEXT NOT NULL,
    mode               TEXT NOT NULL,
    tz                 TEXT NOT NULL,
    start              TEXT NOT NULL,
    duration_minutes   INTEGER NOT NULL,
    rrule              TEXT,
    created_at         TEXT NOT NULL,
    ending_notice_sent INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bookings_radio ON bookings(radio_id);

CREATE TABLE IF NOT EXISTS booking_exceptions (
    booking_id           INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    occ_date             TEXT NOT NULL,
    action               TEXT NOT NULL,
    reason               TEXT NOT NULL DEFAULT '',
    new_start            TEXT,
    new_duration_minutes INTEGER,
    blocked_by           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (booking_id, occ_date)
);

CREATE TABLE IF NOT EXISTS occurrence_state (
    booking_id      INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    occ_date        TEXT NOT NULL,
    auto_started_at TEXT,
    PRIMARY KEY (booking_id, occ_date)
);

CREATE TABLE IF NOT EXISTS notices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    callsign   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    message    TEXT NOT NULL,
    read_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_notices_callsign ON notices(callsign, read_at);
"""

# Columns added after the first release: (table, column, declaration). Existing
# databases get them via ALTER TABLE on open; new ones already have them.
ADDED_COLUMNS = [
    ("assignments", "client_addr", "TEXT"),
]


def _to_user(row: sqlite3.Row) -> User:
    return User(
        callsign=row["callsign"],
        api_key_hash=row["api_key_hash"],
        is_admin=bool(row["is_admin"]),
        enabled=bool(row["enabled"]),
    )


def _to_assignment(row: sqlite3.Row) -> Assignment:
    return Assignment(
        id=row["id"],
        radio_id=row["radio_id"],
        callsign=row["callsign"],
        password=row["password"],
        started_at=parse_dt(row["started_at"]),
        expires_at=parse_dt(row["expires_at"]),
        state=AssignmentState(row["state"]),
        acked=bool(row["acked"]),
        ended_at=parse_dt(row["ended_at"]) if row["ended_at"] else None,
        end_reason=row["end_reason"],
        client_addr=row["client_addr"],
    )


def _to_queue_entry(row: sqlite3.Row) -> QueueEntry:
    return QueueEntry(
        id=row["id"],
        radio_id=row["radio_id"],
        callsign=row["callsign"],
        requested_at=parse_dt(row["requested_at"]),
        offered_at=parse_dt(row["offered_at"]) if row["offered_at"] else None,
    )


def _local(value: str | None) -> datetime | None:
    """Booking times are naive local wall-clock times, stored without offset."""
    return datetime.fromisoformat(value) if value else None


def _local_text(value: datetime | None) -> str | None:
    return value.replace(tzinfo=None).isoformat(timespec="minutes") if value else None


def _to_booking(row: sqlite3.Row) -> Booking:
    return Booking(
        id=row["id"],
        radio_id=row["radio_id"],
        owner=row["owner"],
        kind=BookingKind(row["kind"]),
        mode=BookingMode(row["mode"]),
        tz=row["tz"],
        start=_local(row["start"]),
        duration_minutes=row["duration_minutes"],
        rrule=row["rrule"],
        created_at=parse_dt(row["created_at"]),
        ending_notice_sent=bool(row["ending_notice_sent"]),
    )


def _to_exception(row: sqlite3.Row) -> BookingException:
    return BookingException(
        booking_id=row["booking_id"],
        occ_date=row["occ_date"],
        action=row["action"],
        reason=row["reason"],
        new_start=_local(row["new_start"]),
        new_duration_minutes=row["new_duration_minutes"],
        blocked_by=row["blocked_by"],
    )


_LIVE = tuple(s.value for s in LIVE_STATES)


class ClaimConflict(Exception):
    """The radio or the caller already has a live session; the claim was refused."""


class Database:
    """Thread-safe SQLite wrapper. Operations are short, so one connection
    guarded by a lock is simpler and fast enough for a club-sized site."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Bumped on every booking write, so cached schedules know they are stale.
        self.bookings_rev = 0
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.initialize()

    def initialize(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._add_missing_columns()
            self._conn.commit()

    def _add_missing_columns(self) -> None:
        for table, column, declaration in ADDED_COLUMNS:
            existing = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- users -------------------------------------------------------------

    def create_user(self, callsign: str, api_key_hash: str, is_admin: bool = False) -> User:
        callsign = callsign.strip().upper()
        with self._lock:
            self._conn.execute(
                "INSERT INTO users (callsign, api_key_hash, is_admin, enabled, created_at)"
                " VALUES (?, ?, ?, 1, ?)",
                (callsign, api_key_hash, int(is_admin), isoformat(utcnow())),
            )
            self._conn.commit()
        return User(callsign=callsign, api_key_hash=api_key_hash, is_admin=is_admin, enabled=True)

    def get_user(self, callsign: str) -> User | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE callsign = ?", (callsign.strip().upper(),)
            ).fetchone()
        return _to_user(row) if row else None

    def get_user_by_key_hash(self, api_key_hash: str) -> User | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE api_key_hash = ?", (api_key_hash,)
            ).fetchone()
        return _to_user(row) if row else None

    def list_users(self) -> list[User]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM users ORDER BY callsign").fetchall()
        return [_to_user(r) for r in rows]

    def delete_user(self, callsign: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM users WHERE callsign = ?", (callsign.strip().upper(),)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def set_user_key(self, callsign: str, api_key_hash: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET api_key_hash = ? WHERE callsign = ?",
                (api_key_hash, callsign.strip().upper()),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def set_user_enabled(self, callsign: str, enabled: bool) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET enabled = ? WHERE callsign = ?",
                (int(enabled), callsign.strip().upper()),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def count_admins(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1"
            ).fetchone()
        return int(row["n"])

    # -- assignments -------------------------------------------------------

    def claim_radio(
        self,
        radio_id: str,
        callsign: str,
        password: str,
        expires_at: datetime,
        client_addr: str | None = None,
    ) -> Assignment:
        """Atomically reserve a radio for a caller as a PENDING session.

        Raises ClaimConflict if the radio or the caller already has a pending
        or active session — the unique indexes decide, not a prior SELECT.
        """
        now = utcnow()
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO assignments"
                    " (radio_id, callsign, password, started_at, expires_at, state, acked, client_addr)"
                    " VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                    (
                        radio_id,
                        callsign,
                        password,
                        isoformat(now),
                        isoformat(expires_at),
                        AssignmentState.PENDING.value,
                        client_addr,
                    ),
                )
            except sqlite3.IntegrityError as e:
                self._conn.rollback()
                raise ClaimConflict(f"{radio_id} or {callsign} already has a live session") from e
            self._conn.commit()
            assignment_id = int(cur.lastrowid)
        return Assignment(
            id=assignment_id,
            radio_id=radio_id,
            callsign=callsign,
            password=password,
            started_at=now,
            expires_at=expires_at,
            state=AssignmentState.PENDING,
            client_addr=client_addr,
        )

    def activate(self, assignment_id: int) -> bool:
        """PENDING -> ACTIVE. False if the claim was abandoned in the meantime."""
        return self._transition(assignment_id, AssignmentState.PENDING, AssignmentState.ACTIVE)

    def get_assignment(self, assignment_id: int) -> Assignment | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM assignments WHERE id = ?", (assignment_id,)
            ).fetchone()
        return _to_assignment(row) if row else None

    def live_assignment_for_callsign(self, callsign: str) -> Assignment | None:
        """The caller's pending or active session, if any."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM assignments WHERE callsign = ? AND state IN (?, ?)",
                (callsign, *_LIVE),
            ).fetchone()
        return _to_assignment(row) if row else None

    def live_assignment_for_radio(self, radio_id: str) -> Assignment | None:
        """The session occupying a radio (pending or active), if any."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM assignments WHERE radio_id = ? AND state IN (?, ?)",
                (radio_id, *_LIVE),
            ).fetchone()
        return _to_assignment(row) if row else None

    def active_assignments(self) -> list[Assignment]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM assignments WHERE state = ? ORDER BY id",
                (AssignmentState.ACTIVE.value,),
            ).fetchall()
        return [_to_assignment(r) for r in rows]

    def pending_assignments(self, started_before: datetime) -> list[Assignment]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM assignments WHERE state = ? AND started_at < ? ORDER BY id",
                (AssignmentState.PENDING.value, isoformat(started_before)),
            ).fetchall()
        return [_to_assignment(r) for r in rows]

    def list_assignments(self, limit: int = 100) -> list[Assignment]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM assignments ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_to_assignment(r) for r in rows]

    def _transition(
        self, assignment_id: int, from_state: AssignmentState, to_state: AssignmentState, reason: str = ""
    ) -> bool:
        ended = to_state not in LIVE_STATES
        with self._lock:
            cur = self._conn.execute(
                "UPDATE assignments SET state = ?, ended_at = ?, end_reason = ?"
                " WHERE id = ? AND state = ?",
                (
                    to_state.value,
                    isoformat(utcnow()) if ended else None,
                    reason,
                    assignment_id,
                    from_state.value,
                ),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def finish_assignment(
        self,
        assignment_id: int,
        state: AssignmentState,
        reason: str = "",
        from_state: AssignmentState = AssignmentState.ACTIVE,
    ) -> bool:
        """End a session. False if it was no longer in `from_state` — someone
        else ended it first — in which case nothing is changed."""
        return self._transition(assignment_id, from_state, state, reason)

    def set_acked(self, assignment_id: int, acked: bool = True) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE assignments SET acked = ? WHERE id = ?", (int(acked), assignment_id)
            )
            self._conn.commit()

    def set_expiry(self, assignment_id: int, expires_at: datetime) -> bool:
        """Move an active session's deadline. False if it has already ended."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE assignments SET expires_at = ?, acked = 0 WHERE id = ? AND state = ?",
                (isoformat(expires_at), assignment_id, AssignmentState.ACTIVE.value),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def set_client_addr(self, assignment_id: int, client_addr: str) -> bool:
        """Record where a live session's member is calling from. False if the
        session has ended or the address is unchanged."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE assignments SET client_addr = ?"
                " WHERE id = ? AND state IN (?, ?) AND client_addr IS NOT ?",
                (client_addr, assignment_id, *_LIVE, client_addr),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def set_password(self, assignment_id: int, password: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE assignments SET password = ? WHERE id = ?", (password, assignment_id)
            )
            self._conn.commit()

    # -- queue -------------------------------------------------------------

    def enqueue(self, radio_id: str, callsign: str) -> QueueEntry:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO queue (radio_id, callsign, requested_at)"
                " VALUES (?, ?, ?)",
                (radio_id, callsign, isoformat(utcnow())),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM queue WHERE radio_id = ? AND callsign = ?", (radio_id, callsign)
            ).fetchone()
        return _to_queue_entry(row)

    def dequeue(self, radio_id: str, callsign: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM queue WHERE radio_id = ? AND callsign = ?", (radio_id, callsign)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def remove_queue_entry(self, entry_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM queue WHERE id = ?", (entry_id,))
            self._conn.commit()

    def queue_for_radio(self, radio_id: str) -> list[QueueEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM queue WHERE radio_id = ? ORDER BY id", (radio_id,)
            ).fetchall()
        return [_to_queue_entry(r) for r in rows]

    def queue_entry_for_callsign(self, callsign: str) -> QueueEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM queue WHERE callsign = ? ORDER BY id LIMIT 1", (callsign,)
            ).fetchone()
        return _to_queue_entry(row) if row else None

    def queue_position(self, radio_id: str, callsign: str) -> int | None:
        for index, entry in enumerate(self.queue_for_radio(radio_id), start=1):
            if entry.callsign == callsign:
                return index
        return None

    def queue_depth(self, radio_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM queue WHERE radio_id = ?", (radio_id,)
            ).fetchone()
        return int(row["n"])

    def set_offer(self, entry_id: int, offered_at: datetime | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE queue SET offered_at = ? WHERE id = ?",
                (isoformat(offered_at) if offered_at else None, entry_id),
            )
            self._conn.commit()

    def outstanding_offers(self) -> list[QueueEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM queue WHERE offered_at IS NOT NULL ORDER BY id"
            ).fetchall()
        return [_to_queue_entry(r) for r in rows]

    def offer_for_radio(self, radio_id: str) -> QueueEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM queue WHERE radio_id = ? AND offered_at IS NOT NULL LIMIT 1",
                (radio_id,),
            ).fetchone()
        return _to_queue_entry(row) if row else None

    # -- bookings ----------------------------------------------------------

    def create_booking(self, booking: Booking, exceptions: list[BookingException] = ()) -> Booking:
        """Insert a booking and its initial exceptions (dates skipped for
        conflicts) in one transaction. Returns it with its new id."""
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO bookings (radio_id, owner, kind, mode, tz, start, duration_minutes,"
                    " rrule, created_at, ending_notice_sent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        booking.radio_id,
                        booking.owner,
                        booking.kind.value,
                        booking.mode.value,
                        booking.tz,
                        _local_text(booking.start),
                        booking.duration_minutes,
                        booking.rrule,
                        isoformat(booking.created_at),
                        int(booking.ending_notice_sent),
                    ),
                )
                booking_id = int(cur.lastrowid)
                for exc in exceptions:
                    self._put_exception(booking_id, exc)
            except sqlite3.Error:
                self._conn.rollback()
                raise
            self.bookings_rev += 1
            self._conn.commit()
        booking.id = booking_id
        return booking

    def update_booking(self, booking: Booking) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE bookings SET radio_id = ?, mode = ?, tz = ?, start = ?, duration_minutes = ?,"
                " rrule = ?, ending_notice_sent = ? WHERE id = ?",
                (
                    booking.radio_id,
                    booking.mode.value,
                    booking.tz,
                    _local_text(booking.start),
                    booking.duration_minutes,
                    booking.rrule,
                    int(booking.ending_notice_sent),
                    booking.id,
                ),
            )
            self.bookings_rev += 1
            self._conn.commit()
        return cur.rowcount == 1

    def get_booking(self, booking_id: int) -> Booking | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        return _to_booking(row) if row else None

    def list_bookings(self, radio_id: str | None = None, owner: str | None = None) -> list[Booking]:
        clauses, params = [], []
        if radio_id:
            clauses.append("radio_id = ?")
            params.append(radio_id)
        if owner:
            clauses.append("owner = ?")
            params.append(owner.strip().upper())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(f"SELECT * FROM bookings{where} ORDER BY id", params).fetchall()
        return [_to_booking(r) for r in rows]

    def delete_booking(self, booking_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
            self.bookings_rev += 1
            self._conn.commit()
        return cur.rowcount == 1

    def _put_exception(self, booking_id: int, exc: BookingException) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO booking_exceptions (booking_id, occ_date, action, reason,"
            " new_start, new_duration_minutes, blocked_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                booking_id,
                exc.occ_date,
                exc.action,
                exc.reason,
                _local_text(exc.new_start),
                exc.new_duration_minutes,
                exc.blocked_by,
            ),
        )

    def put_exception(self, exc: BookingException) -> None:
        with self._lock:
            self._put_exception(exc.booking_id, exc)
            self.bookings_rev += 1
            self._conn.commit()

    def delete_exceptions(
        self, booking_id: int, reason: str | None = None, from_date: str | None = None
    ) -> None:
        clauses, params = ["booking_id = ?"], [booking_id]
        if reason is not None:
            clauses.append("reason = ?")
            params.append(reason)
        if from_date is not None:
            clauses.append("occ_date >= ?")
            params.append(from_date)
        with self._lock:
            self._conn.execute(f"DELETE FROM booking_exceptions WHERE {' AND '.join(clauses)}", params)
            self.bookings_rev += 1
            self._conn.commit()

    def exceptions_for(self, booking_id: int) -> list[BookingException]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM booking_exceptions WHERE booking_id = ? ORDER BY occ_date", (booking_id,)
            ).fetchall()
        return [_to_exception(r) for r in rows]

    def all_exceptions(self) -> dict[int, list[BookingException]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM booking_exceptions ORDER BY occ_date").fetchall()
        result: dict[int, list[BookingException]] = {}
        for row in rows:
            result.setdefault(row["booking_id"], []).append(_to_exception(row))
        return result

    def mark_auto_started(self, booking_id: int, occ_date: str) -> bool:
        """Record that an auto-session was opened for this occurrence. False if
        one already was, so the monitor never opens it twice."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO occurrence_state (booking_id, occ_date, auto_started_at)"
                " VALUES (?, ?, ?)",
                (booking_id, occ_date, isoformat(utcnow())),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def auto_started(self, booking_id: int, occ_date: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM occurrence_state WHERE booking_id = ? AND occ_date = ?",
                (booking_id, occ_date),
            ).fetchone()
        return row is not None

    # -- notices -----------------------------------------------------------

    def add_notice(self, callsign: str, message: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO notices (callsign, created_at, message) VALUES (?, ?, ?)",
                (callsign.strip().upper(), isoformat(utcnow()), message),
            )
            self._conn.commit()

    def take_notices(self, callsign: str) -> list[dict]:
        """Unread notices for a member, marked read as they are returned."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM notices WHERE callsign = ? AND read_at IS NULL ORDER BY id",
                (callsign.strip().upper(),),
            ).fetchall()
            if rows:
                self._conn.execute(
                    "UPDATE notices SET read_at = ? WHERE callsign = ? AND read_at IS NULL",
                    (isoformat(utcnow()), callsign.strip().upper()),
                )
                self._conn.commit()
        return [{"created_at": r["created_at"], "message": r["message"]} for r in rows]

    # -- audit -------------------------------------------------------------

    def audit(self, actor: str, action: str, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit (timestamp, actor, action, detail) VALUES (?, ?, ?, ?)",
                (isoformat(utcnow()), actor, action, detail),
            )
            self._conn.commit()

    def list_audit(
        self, since: datetime | None = None, actor: str | None = None, limit: int = 200
    ) -> list[AuditEntry]:
        clauses, params = [], []
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(isoformat(since))
        if actor:
            clauses.append("actor = ?")
            params.append(actor.strip().upper())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM audit{where} ORDER BY id DESC LIMIT ?", params
            ).fetchall()
        return [
            AuditEntry(
                id=r["id"],
                timestamp=parse_dt(r["timestamp"]),
                actor=r["actor"],
                action=r["action"],
                detail=r["detail"],
            )
            for r in rows
        ]

    def purge_audit_older_than(self, age: timedelta) -> int:
        cutoff = utcnow() - age
        with self._lock:
            cur = self._conn.execute("DELETE FROM audit WHERE timestamp < ?", (isoformat(cutoff),))
            self._conn.commit()
        return cur.rowcount

    # -- backup ------------------------------------------------------------

    def backup_to(self, destination: Path) -> Path:
        """Online snapshot via SQLite's backup API — safe while the server runs."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            target = sqlite3.connect(destination)
            try:
                self._conn.backup(target)
            finally:
                target.close()
        return destination
