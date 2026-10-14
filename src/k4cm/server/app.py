"""FastAPI application: the REST API members and admins talk to.

All endpoints except /health require a bearer API key. Admin endpoints
additionally require the caller's is_admin flag.

Member endpoints pass the caller's address to the manager: the relay admits a
panel only from the address its member last reached the API from.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..models import AssignmentState, User, isoformat, parse_dt, utcnow
from .auth import (
    client_address,
    generate_api_key,
    hash_api_key,
    require_admin,
    require_user,
)
from .backup import BackupScheduler
from .bookings import BookingChanges, BookingError, BookingInput
from .config import ServerConfig
from .db import Database
from .manager import AssignmentManager, ManagerError
from .monitor import Monitor
from .schedule import RepeatSpec


class RadioRequest(BaseModel):
    radio: str = Field(..., description="id or name of the specific K4 being requested")
    duration_minutes: int | None = Field(None, description="ask for a session shorter than the default")
    tz: str | None = Field(None, description="the member's IANA time zone, for times in messages")


class RepeatBody(BaseModel):
    freq: str = Field(..., description="daily, weekly, monthly or yearly")
    every: int = 1
    on: list[str] = Field(default_factory=list, description="tue,thu | 1,15,last | 3rd-tue,last-fri")
    count: int | None = None
    until: str | None = Field(None, description="ISO date, inclusive")

    def spec(self) -> RepeatSpec:
        return RepeatSpec(self.freq, self.every, list(self.on), self.count, self.until)


class BookingBody(BaseModel):
    radio: str
    start: str = Field(..., description="local wall-clock time, e.g. 2026-10-20 19:00")
    duration: str = Field(..., description="2h, 90m, 1h30m")
    tz: str = Field(..., description="IANA time zone the start is in")
    mode: str = "reserve"
    kind: str = "booking"
    repeat: RepeatBody | None = None
    override: bool = False


class BookingEditBody(BaseModel):
    scope: str = "all"
    date: str | None = Field(None, description="the date (YYYY-MM-DD) a 'this' or 'following' edit applies to")
    start: str | None = None
    duration: str | None = None
    mode: str | None = None
    radio: str | None = None
    repeat: RepeatBody | None = None
    count: int | None = None
    until: str | None = None


class NewUserRequest(BaseModel):
    callsign: str
    is_admin: bool = False


class PowerRequest(BaseModel):
    action: str = Field(..., description="on, off or cycle")


def create_app(
    config: ServerConfig,
    db: Database | None = None,
    logger: logging.Logger | None = None,
    background: bool = True,
) -> FastAPI:
    logger = logger or logging.getLogger("k4cm.server")
    db = db or Database(config.database_path)
    manager = AssignmentManager(config, db, logger)
    monitor = Monitor(manager)
    backups = BackupScheduler(config, db, logger)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info(
            "k4cm server %s starting: %d radio(s), %d active session(s)",
            __version__, len(config.radios), len(db.active_assignments()),
        )
        # The relay is not a background loop: without it, relayed radios are
        # unreachable, so it runs whether or not `background` is set.
        await manager.relay.sync()
        if background:
            monitor.start()
            backups.start()
        try:
            yield
        finally:
            if background:
                await monitor.stop()
                await backups.stop()
            await manager.relay.stop()
            logger.info("k4cm server stopped")

    app = FastAPI(title="K4 Connect Manager", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.db = db
    app.state.manager = manager
    app.state.monitor = monitor

    @app.exception_handler(ManagerError)
    async def _manager_error(request: Request, exc: ManagerError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.payload())

    @app.exception_handler(BookingError)
    async def _booking_error(request: Request, exc: BookingError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, **exc.extra})

    # -- unauthenticated ---------------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "time": isoformat(utcnow())}

    # -- member endpoints --------------------------------------------------

    @app.post("/assignments", status_code=status.HTTP_201_CREATED)
    async def create_assignment(body: RadioRequest, request: Request, user: User = Depends(require_user)):
        return await manager.request_assignment(
            user.callsign, body.radio, client_address(request), body.duration_minutes, body.tz
        )

    @app.get("/assignments/current")
    async def current_assignment(request: Request, user: User = Depends(require_user)):
        manager.note_client_addr(user.callsign, client_address(request))
        return manager.session_state(user.callsign)

    @app.get("/assignments/current/link")
    async def assignment_link(request: Request, user: User = Depends(require_user)):
        manager.note_client_addr(user.callsign, client_address(request))
        return await manager.link_state(user.callsign)

    @app.post("/assignments/current/ack")
    async def ack_assignment(request: Request, user: User = Depends(require_user)):
        manager.note_client_addr(user.callsign, client_address(request))
        return manager.ack(user.callsign)

    @app.post("/assignments/current/extend")
    async def extend_assignment(request: Request, user: User = Depends(require_user)):
        manager.note_client_addr(user.callsign, client_address(request))
        return await manager.extend(user.callsign)

    @app.delete("/assignments/current")
    async def release_assignment(user: User = Depends(require_user)):
        return await manager.release(user.callsign)

    @app.get("/status")
    async def site_status(user: User = Depends(require_user)):
        return {"radios": manager.status(), "time": isoformat(utcnow())}

    # -- queue -------------------------------------------------------------

    @app.post("/queue", status_code=status.HTTP_201_CREATED)
    async def join_queue(body: RadioRequest, user: User = Depends(require_user)):
        return manager.enqueue(user.callsign, body.radio, body.tz)

    @app.get("/queue/position")
    async def queue_position(user: User = Depends(require_user)):
        return manager.session_state(user.callsign)

    @app.delete("/queue")
    async def leave_queue(user: User = Depends(require_user)):
        return manager.leave_queue(user.callsign)

    @app.post("/queue/accept", status_code=status.HTTP_201_CREATED)
    async def accept_offer(request: Request, user: User = Depends(require_user)):
        return await manager.accept_offer(user.callsign, client_address(request))

    @app.post("/queue/decline")
    async def decline_offer(user: User = Depends(require_user)):
        return manager.decline_offer(user.callsign)

    # -- bookings ----------------------------------------------------------

    @app.get("/bookings")
    async def list_bookings(
        start: str | None = None,
        end: str | None = None,
        radio: str | None = None,
        owner: str | None = None,
        user: User = Depends(require_user),
    ):
        """Occurrences overlapping [start, end) (UTC ISO; default the next 14 days)."""
        begin = parse_dt(start) if start else utcnow()
        finish = parse_dt(end) if end else begin + timedelta(days=14)
        return {
            "start": isoformat(begin),
            "end": isoformat(finish),
            "occurrences": manager.bookings.list_occurrences(begin, finish, radio, owner),
        }

    @app.get("/bookings/series")
    async def list_series(radio: str | None = None, owner: str | None = None, user: User = Depends(require_user)):
        return manager.bookings.list_series(owner, radio)

    @app.post("/bookings", status_code=status.HTTP_201_CREATED)
    async def create_booking(body: BookingBody, dry_run: bool = False, user: User = Depends(require_user)):
        return manager.bookings.create(
            user,
            BookingInput(
                radio=body.radio,
                start=body.start,
                duration=body.duration,
                tz=body.tz,
                mode=body.mode,
                kind=body.kind,
                repeat=body.repeat.spec() if body.repeat else None,
                override=body.override,
            ),
            dry_run=dry_run,
        )

    @app.get("/bookings/{booking_id}")
    async def get_booking(booking_id: int, user: User = Depends(require_user)):
        return manager.bookings.get(booking_id)

    @app.patch("/bookings/{booking_id}")
    async def edit_booking(
        booking_id: int, body: BookingEditBody, dry_run: bool = False, user: User = Depends(require_user)
    ):
        changes = BookingChanges(
            start=body.start,
            duration=body.duration,
            mode=body.mode,
            radio=body.radio,
            repeat=body.repeat.spec() if body.repeat else None,
            count=body.count,
            until=body.until,
        )
        return manager.bookings.edit(user, booking_id, changes, body.scope, body.date, dry_run)

    @app.delete("/bookings/{booking_id}")
    async def delete_booking(
        booking_id: int, scope: str = "all", date: str | None = None, user: User = Depends(require_user)
    ):
        return manager.bookings.remove(user, booking_id, scope, date)

    @app.get("/notices")
    async def notices(user: User = Depends(require_user)):
        return {"notices": db.take_notices(user.callsign)}

    # -- admin: users ------------------------------------------------------

    @app.get("/users")
    async def list_users(admin: User = Depends(require_admin)):
        return [
            {"callsign": u.callsign, "is_admin": u.is_admin, "enabled": u.enabled}
            for u in db.list_users()
        ]

    @app.post("/users", status_code=status.HTTP_201_CREATED)
    async def create_user(body: NewUserRequest, admin: User = Depends(require_admin)):
        callsign = body.callsign.strip().upper()
        if db.get_user(callsign) is not None:
            raise ManagerError(409, f"user {callsign} already exists")
        api_key = generate_api_key()
        db.create_user(callsign, hash_api_key(api_key), body.is_admin)
        db.audit(admin.callsign, "user.create", f"{callsign} (admin={body.is_admin})")
        logger.info("user %s created by %s", callsign, admin.callsign)
        return {
            "callsign": callsign,
            "is_admin": body.is_admin,
            "api_key": api_key,
            "note": "This key is shown once. Give it to the user over a trusted channel.",
        }

    @app.delete("/users/{callsign}")
    async def delete_user(callsign: str, admin: User = Depends(require_admin)):
        callsign = callsign.strip().upper()
        target = db.get_user(callsign)
        if target is None:
            raise ManagerError(404, f"no such user: {callsign}")
        if target.is_admin and db.count_admins() <= 1:
            raise ManagerError(409, "refusing to delete the last admin account")
        active = db.live_assignment_for_callsign(callsign)
        if active is not None and active.state is AssignmentState.PENDING:
            raise ManagerError(409, f"{callsign} has a session being set up right now; try again shortly")
        if active is not None:
            await manager.end_assignment(
                active, AssignmentState.FORCED, f"user deleted by {admin.callsign}", admin.callsign
            )
        db.delete_user(callsign)
        db.audit(admin.callsign, "user.delete", callsign)
        return {"deleted": callsign}

    @app.post("/users/{callsign}/reset-key")
    async def reset_key(callsign: str, admin: User = Depends(require_admin)):
        callsign = callsign.strip().upper()
        if db.get_user(callsign) is None:
            raise ManagerError(404, f"no such user: {callsign}")
        api_key = generate_api_key()
        db.set_user_key(callsign, hash_api_key(api_key))
        db.audit(admin.callsign, "user.reset_key", callsign)
        return {"callsign": callsign, "api_key": api_key, "note": "This key is shown once."}

    # -- admin: radios -----------------------------------------------------

    @app.get("/radios")
    async def list_radios(admin: User = Depends(require_admin)):
        return [
            {
                **radio.public(),
                "cat_host": radio.host,
                "cat_port": radio.cat_port,
                "reachable": manager.reachability.get(radio.id),
                "relay_port": radio.relay_port,
                "relay_links": manager.relay.link_count(radio.id),
                "power_method": radio.power.method.value,
                "power_address": radio.power.address,
                "power_outlet": radio.power.outlet,
            }
            for radio in config.radios.values()
        ]

    @app.post("/radios/reload")
    async def reload_radios(admin: User = Depends(require_admin)):
        return await manager.reload_radios(admin.callsign)

    @app.post("/radios/{radio_id}/power")
    async def radio_power(radio_id: str, body: PowerRequest, admin: User = Depends(require_admin)):
        return await manager.power_action(radio_id, body.action, admin.callsign)

    # -- admin: sessions and audit ----------------------------------------

    @app.get("/assignments")
    async def list_assignments(limit: int = 100, admin: User = Depends(require_admin)):
        return [
            manager.assignment_payload(a, include_password=False)
            for a in db.list_assignments(limit=limit)
        ]

    @app.delete("/assignments/{assignment_id}")
    async def force_release(assignment_id: int, admin: User = Depends(require_admin)):
        return await manager.force_release(assignment_id, admin.callsign)

    @app.get("/audit")
    async def read_audit(
        hours: float | None = None,
        actor: str | None = None,
        limit: int = 200,
        admin: User = Depends(require_admin),
    ):
        since = utcnow() - timedelta(hours=hours) if hours else None
        return [
            {
                "timestamp": isoformat(e.timestamp),
                "actor": e.actor,
                "action": e.action,
                "detail": e.detail,
            }
            for e in db.list_audit(since=since, actor=actor, limit=limit)
        ]

    return app
