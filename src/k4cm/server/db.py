"""SQLite storage: users, assignments, per-radio wait queues, audit trail.

Radios are not stored here — they come from the server config file.

Allocation is race-free by construction: a request claims a radio by inserting
a PENDING row, and partial unique indexes let at most one PENDING-or-ACTIVE row
exist per radio and per callsign. Two simultaneous requests cannot both win,
whatever the interleaving — the loser's INSERT fails and becomes ClaimConflict.
Every later state change is conditional on the state it expects, so a session
that someone else has already ended is never ended (or extended) again.
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
    end_reason  TEXT NOT NULL DEFAULT ''
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
"""


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
    )


def _to_queue_entry(row: sqlite3.Row) -> QueueEntry:
    return QueueEntry(
        id=row["id"],
        radio_id=row["radio_id"],
        callsign=row["callsign"],
        requested_at=parse_dt(row["requested_at"]),
        offered_at=parse_dt(row["offered_at"]) if row["offered_at"] else None,
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
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.initialize()

    def initialize(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

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
        self, radio_id: str, callsign: str, password: str, expires_at: datetime
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
                    " (radio_id, callsign, password, started_at, expires_at, state, acked)"
                    " VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (
                        radio_id,
                        callsign,
                        password,
                        isoformat(now),
                        isoformat(expires_at),
                        AssignmentState.PENDING.value,
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
