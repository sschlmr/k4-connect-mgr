"""Assignment logic, queue behaviour and the session expiry state machine."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from k4cm import cat
from k4cm.models import AssignmentState, utcnow
from k4cm.server.db import ClaimConflict
from k4cm.server.manager import ManagerError


async def connect_local_panel(server, radio_fake, password: str) -> None:
    """Drive the fake K4/0 to attach to a central radio, as the client CLI would."""
    async with cat.CatClient("127.0.0.1", server.local_radio.cat_port) as local:
        await local.send(f"RRT127.0.0.1:{radio_fake.remote_port}?{password};")
        assert await local.ping()


# -- assignment ------------------------------------------------------------


async def test_assignment_sets_fresh_password_on_the_radio(server):
    result = await server.manager.request_assignment("W1AW", "k4a")

    assert result["radio"]["id"] == "k4a"
    assert result["password"], "the caller must receive the password"
    assert server.radio_a.password == result["password"], "radio must hold the same password"
    assert server.radio_a.rrc == 1, "radio must admit one remote client"
    assert server.db.live_assignment_for_radio("k4a").callsign == "W1AW"


async def test_each_assignment_gets_a_different_random_password(server):
    first = await server.manager.request_assignment("W1AW", "k4a")
    await server.manager.release("W1AW")
    second = await server.manager.request_assignment("W1AW", "k4a")
    assert first["password"] != second["password"]


async def test_radio_can_be_requested_by_name_as_well_as_id(server):
    result = await server.manager.request_assignment("W1AW", "K4 Alpha")
    assert result["radio"]["id"] == "k4a"


async def test_unknown_radio_is_rejected(server):
    with pytest.raises(ManagerError) as excinfo:
        await server.manager.request_assignment("W1AW", "k4zz")
    assert excinfo.value.status_code == 404


async def test_busy_radio_is_refused_with_queue_offer(server):
    await server.manager.request_assignment("W1AW", "k4a")
    with pytest.raises(ManagerError) as excinfo:
        await server.manager.request_assignment("K2ABC", "k4a")
    error = excinfo.value
    assert error.status_code == 409
    assert error.extra["in_use_by"] == "W1AW"
    assert error.extra["can_queue"] is True


async def test_one_assignment_per_user(server):
    await server.manager.request_assignment("W1AW", "k4a")
    with pytest.raises(ManagerError) as excinfo:
        await server.manager.request_assignment("W1AW", "k4b")
    assert excinfo.value.status_code == 409
    assert server.db.live_assignment_for_radio("k4b") is None


async def test_unreachable_radio_is_not_assigned(server):
    with pytest.raises(ManagerError) as excinfo:
        await server.manager.request_assignment("W1AW", "k4dead")
    assert excinfo.value.status_code == 503
    assert server.db.live_assignment_for_radio("k4dead") is None
    assert server.manager.reachability["k4dead"] is False


async def test_disabled_radio_is_not_assigned(server):
    server.config.radios["k4b"].enabled = False
    with pytest.raises(ManagerError) as excinfo:
        await server.manager.request_assignment("W1AW", "k4b")
    assert excinfo.value.status_code == 409


# -- race-free allocation --------------------------------------------------


async def outcome(coro):
    try:
        return await coro
    except ManagerError as e:
        return e


async def test_simultaneous_requests_for_one_radio_have_exactly_one_winner(server):
    callsigns = ["W1AW", "K2ABC", "N0CALL", "W9XYZ"]
    results = await asyncio.gather(
        *(outcome(server.manager.request_assignment(c, "k4a")) for c in callsigns)
    )

    winners = [r for r in results if not isinstance(r, ManagerError)]
    losers = [r for r in results if isinstance(r, ManagerError)]
    assert len(winners) == 1
    assert all(e.status_code == 409 for e in losers)
    assert server.radio_a.password == winners[0]["password"], "the radio holds the winner's password"
    assert server.db.live_assignment_for_radio("k4a").callsign == winners[0]["callsign"]


async def test_simultaneous_requests_by_one_user_get_one_radio(server):
    results = await asyncio.gather(
        outcome(server.manager.request_assignment("W1AW", "k4a")),
        outcome(server.manager.request_assignment("W1AW", "k4b")),
    )
    assert sum(not isinstance(r, ManagerError) for r in results) == 1


async def test_database_refuses_a_second_live_claim(server):
    expires = utcnow() + timedelta(hours=1)
    first = server.db.claim_radio("k4a", "W1AW", "pw1", expires)
    with pytest.raises(ClaimConflict):
        server.db.claim_radio("k4a", "K2ABC", "pw2", expires)
    with pytest.raises(ClaimConflict):
        server.db.claim_radio("k4b", "W1AW", "pw3", expires)

    server.db.finish_assignment(first.id, AssignmentState.FAILED, "test", AssignmentState.PENDING)
    server.db.claim_radio("k4a", "K2ABC", "pw2", expires)  # free again once the claim has ended


async def test_new_session_is_not_scrambled_by_the_previous_lockout(server):
    await server.manager.request_assignment("W1AW", "k4a")
    releasing = asyncio.create_task(server.manager.release("W1AW"))
    # Wait until the release has been recorded but its lockout CAT is still running.
    while server.db.live_assignment_for_radio("k4a") is not None:
        await asyncio.sleep(0)

    result = await server.manager.request_assignment("K2ABC", "k4a")
    await releasing

    assert server.radio_a.password == result["password"]
    assert server.radio_a.rrc == 1
    assert server.db.live_assignment_for_radio("k4a").state is AssignmentState.ACTIVE


async def test_a_session_ends_only_once_when_endings_collide(server):
    await server.manager.request_assignment("W1AW", "k4a")
    assignment = server.db.live_assignment_for_callsign("W1AW")

    results = await asyncio.gather(
        outcome(server.manager.release("W1AW")),
        outcome(server.manager.force_release(assignment.id, "ADMIN")),
    )

    assert sum(isinstance(r, ManagerError) for r in results) == 1
    endings = [e for e in server.db.list_audit() if e.action.startswith("session.")]
    assert len(endings) == 1


async def test_stale_monitor_decision_does_not_end_an_extended_session(server):
    await server.manager.request_assignment("W1AW", "k4a")
    snapshot = server.db.live_assignment_for_callsign("W1AW")
    server.db.set_expiry(snapshot.id, utcnow() - timedelta(minutes=1))
    server.config.session.ack_grace_minutes = 0
    stale = server.db.get_assignment(snapshot.id)  # what the monitor read: overdue

    await server.manager.extend("W1AW")  # the user answers before the monitor acts

    now = utcnow()
    ended = await server.manager.end_assignment(
        stale, AssignmentState.EXPIRED, "overdue", "system",
        still_due=lambda a: now >= a.expires_at,
    )
    assert ended is False
    assert server.db.get_assignment(snapshot.id).state is AssignmentState.ACTIVE


async def test_orphaned_claim_is_abandoned_and_the_radio_freed(server):
    claim = server.db.claim_radio("k4a", "W1AW", "orphan", utcnow() + timedelta(hours=1))
    server.radio_a.password, server.radio_a.rrc = "orphan", 1

    await server.manager.abandon_stale_claims(now=utcnow() + server.manager.claim_timeout * 2)

    assert server.db.get_assignment(claim.id).state is AssignmentState.FAILED
    assert server.radio_a.password != "orphan" and server.radio_a.rrc == 0
    await server.manager.request_assignment("K2ABC", "k4a")


async def test_fresh_claim_is_not_abandoned(server):
    claim = server.db.claim_radio("k4a", "W1AW", "pw", utcnow() + timedelta(hours=1))
    await server.monitor.tick()
    assert server.db.get_assignment(claim.id).state is AssignmentState.PENDING


async def test_pending_session_cannot_be_released_yet(server):
    server.db.claim_radio("k4a", "W1AW", "pw", utcnow() + timedelta(hours=1))
    with pytest.raises(ManagerError) as excinfo:
        await server.manager.release("W1AW")
    assert excinfo.value.status_code == 409
    assert "being set up" in excinfo.value.message


async def test_failed_setup_leaves_a_failed_record_and_a_free_radio(server):
    with pytest.raises(ManagerError):
        await server.manager.request_assignment("W1AW", "k4dead")
    assert server.db.list_assignments()[0].state is AssignmentState.FAILED
    assert server.db.live_assignment_for_callsign("W1AW") is None


# -- release ---------------------------------------------------------------


async def test_release_scrambles_password_and_locks_out(server):
    result = await server.manager.request_assignment("W1AW", "k4a")
    issued = result["password"]

    await server.manager.release("W1AW")

    assert server.radio_a.password != issued, "old password must no longer work"
    assert server.radio_a.rrc == 0, "radio must stop admitting clients"
    assert server.db.live_assignment_for_radio("k4a") is None
    assert server.db.list_assignments()[0].state is AssignmentState.RELEASED


async def test_lockout_alone_leaves_an_attached_panel_connected(server):
    # The reason relay_port exists (see test_relay.py): on an unrelayed radio
    # the lockout stops new connections, but a real K4 keeps the client it has.
    result = await server.manager.request_assignment("W1AW", "k4a")
    await connect_local_panel(server, server.radio_a, result["password"])
    assert server.radio_a.client_count == 1

    await server.manager.release("W1AW")
    assert server.radio_a.rrc == 0
    assert server.radio_a.client_count == 1


async def test_release_without_assignment_is_an_error(server):
    with pytest.raises(ManagerError) as excinfo:
        await server.manager.release("W1AW")
    assert excinfo.value.status_code == 404


# -- extension and acknowledgement ----------------------------------------


async def test_extension_granted_when_nobody_is_waiting(server):
    before = await server.manager.request_assignment("W1AW", "k4a")
    after = await server.manager.extend("W1AW")
    assert after["assignment"]["expires_at"] > before["expires_at"]


async def test_extension_refused_when_someone_is_queued(server):
    await server.manager.request_assignment("W1AW", "k4a")
    server.manager.enqueue("K2ABC", "k4a")

    with pytest.raises(ManagerError) as excinfo:
        await server.manager.extend("W1AW")
    assert excinfo.value.status_code == 409
    assert excinfo.value.extra["queue_depth"] == 1


async def test_ack_is_recorded_and_visible(server):
    await server.manager.request_assignment("W1AW", "k4a")
    state = server.manager.ack("W1AW")
    assert state["assignment"]["acked"] is True


async def test_can_extend_flag_tracks_the_queue(server):
    await server.manager.request_assignment("W1AW", "k4a")
    assert server.manager.session_state("W1AW")["assignment"]["can_extend"] is True
    server.manager.enqueue("K2ABC", "k4a")
    assert server.manager.session_state("W1AW")["assignment"]["can_extend"] is False


# -- monitor: disconnect detection ----------------------------------------


async def test_attached_panel_keeps_the_session(server):
    result = await server.manager.request_assignment("W1AW", "k4a")
    await connect_local_panel(server, server.radio_a, result["password"])
    server.config.monitor.disconnect_grace_minutes = 0

    await server.monitor.tick()

    assert server.db.live_assignment_for_callsign("W1AW") is not None


async def test_session_reclaimed_after_disconnect_grace(server):
    await server.manager.request_assignment("W1AW", "k4a")  # no panel ever attaches
    server.config.monitor.disconnect_grace_minutes = 0

    await server.monitor.tick()

    assert server.db.live_assignment_for_callsign("W1AW") is None
    ended = server.db.list_assignments()[0]
    assert ended.state is AssignmentState.RECLAIMED
    assert server.radio_a.rrc == 0


async def test_session_survives_when_grace_has_not_elapsed(server):
    await server.manager.request_assignment("W1AW", "k4a")
    server.config.monitor.disconnect_grace_minutes = 30

    await server.monitor.tick()

    assert server.db.live_assignment_for_callsign("W1AW") is not None


# -- monitor: deadline enforcement ----------------------------------------


async def test_acknowledged_session_ends_at_its_deadline(server):
    result = await server.manager.request_assignment("W1AW", "k4a")
    await connect_local_panel(server, server.radio_a, result["password"])
    assignment = server.db.live_assignment_for_callsign("W1AW")
    server.db.set_expiry(assignment.id, utcnow())
    server.db.set_acked(assignment.id, True)

    await server.monitor.tick()

    ended = server.db.get_assignment(assignment.id)
    assert ended.state is AssignmentState.EXPIRED
    assert "acknowledged" in ended.end_reason


async def test_unacknowledged_session_gets_the_ack_grace_then_ends(server):
    result = await server.manager.request_assignment("W1AW", "k4a")
    await connect_local_panel(server, server.radio_a, result["password"])
    assignment = server.db.live_assignment_for_callsign("W1AW")
    server.db.set_expiry(assignment.id, utcnow())  # deadline reached, not acked

    # Within the ack grace the user keeps the radio: the negotiation is still open.
    await server.monitor.tick()
    assert server.db.get_assignment(assignment.id).state is AssignmentState.ACTIVE

    server.config.session.ack_grace_minutes = 0
    await server.monitor.tick()

    ended = server.db.get_assignment(assignment.id)
    assert ended.state is AssignmentState.EXPIRED
    assert "no acknowledgement" in ended.end_reason


async def test_extension_pushes_the_deadline_back(server):
    result = await server.manager.request_assignment("W1AW", "k4a")
    await connect_local_panel(server, server.radio_a, result["password"])
    assignment = server.db.live_assignment_for_callsign("W1AW")
    server.db.set_expiry(assignment.id, utcnow())
    server.config.session.ack_grace_minutes = 0

    await server.manager.extend("W1AW")
    await server.monitor.tick()

    assert server.db.get_assignment(assignment.id).state is AssignmentState.ACTIVE


# -- queue and offers ------------------------------------------------------


async def test_queue_offer_passes_to_head_when_radio_frees(server):
    await server.manager.request_assignment("W1AW", "k4a")
    server.manager.enqueue("K2ABC", "k4a")
    server.manager.enqueue("N0CALL", "k4a")

    await server.manager.release("W1AW")

    offer = server.db.offer_for_radio("k4a")
    assert offer.callsign == "K2ABC", "FIFO: first in line is offered the radio"
    assert server.manager.session_state("K2ABC")["offer"]["radio"]["id"] == "k4a"
    assert server.manager.session_state("N0CALL")["queued"]["position"] == 2


async def test_offer_holder_can_accept_and_others_cannot_jump_in(server):
    await server.manager.request_assignment("W1AW", "k4a")
    server.manager.enqueue("K2ABC", "k4a")
    await server.manager.release("W1AW")

    with pytest.raises(ManagerError) as excinfo:
        await server.manager.request_assignment("N0CALL", "k4a")
    assert "held for K2ABC" in excinfo.value.message

    result = await server.manager.accept_offer("K2ABC")
    assert result["radio"]["id"] == "k4a"
    assert server.db.queue_depth("k4a") == 0


async def test_declined_offer_moves_to_the_next_in_line(server):
    await server.manager.request_assignment("W1AW", "k4a")
    server.manager.enqueue("K2ABC", "k4a")
    server.manager.enqueue("N0CALL", "k4a")
    await server.manager.release("W1AW")

    server.manager.decline_offer("K2ABC")

    assert server.db.offer_for_radio("k4a").callsign == "N0CALL"


async def test_lapsed_offer_moves_to_the_next_in_line(server):
    await server.manager.request_assignment("W1AW", "k4a")
    server.manager.enqueue("K2ABC", "k4a")
    server.manager.enqueue("N0CALL", "k4a")
    await server.manager.release("W1AW")

    server.config.session.queue_hold_minutes = 0
    server.manager.expire_offers()

    assert server.db.offer_for_radio("k4a").callsign == "N0CALL"
    assert server.db.queue_position("k4a", "K2ABC") is None, "no-show is dropped from the queue"


async def test_queueing_for_a_free_radio_is_refused(server):
    with pytest.raises(ManagerError) as excinfo:
        server.manager.enqueue("W1AW", "k4a")
    assert excinfo.value.status_code == 409
    assert "free right now" in excinfo.value.message


async def test_leaving_the_queue(server):
    await server.manager.request_assignment("W1AW", "k4a")
    server.manager.enqueue("K2ABC", "k4a")
    server.manager.leave_queue("K2ABC")
    assert server.db.queue_depth("k4a") == 0


# -- admin -----------------------------------------------------------------


async def test_force_release_ends_another_users_session(server):
    await server.manager.request_assignment("W1AW", "k4a")
    assignment = server.db.live_assignment_for_callsign("W1AW")

    await server.manager.force_release(assignment.id, "ADMIN")

    ended = server.db.get_assignment(assignment.id)
    assert ended.state is AssignmentState.FORCED
    assert server.radio_a.rrc == 0
    assert any(e.action == "session.forced" for e in server.db.list_audit())


async def test_status_reports_use_and_queues(server):
    await server.manager.request_assignment("W1AW", "k4a")
    server.manager.enqueue("K2ABC", "k4a")

    rows = {row["id"]: row for row in server.manager.status()}

    assert rows["k4a"]["in_use"] is True
    assert rows["k4a"]["user"] == "W1AW"
    assert rows["k4a"]["queue_depth"] == 1
    assert rows["k4b"]["in_use"] is False


async def test_reload_radios_picks_up_config_edits(server):
    original = server.config.path.read_text(encoding="utf-8")
    server.config.path.write_text(
        original + '\n[[radios]]\nid = "k4c"\nname = "K4 Charlie"\nhost = "127.0.0.1"\npublic_host = "127.0.0.1"\n',
        encoding="utf-8",
    )

    result = await server.manager.reload_radios("ADMIN")

    assert result["radios"] == 4
    assert server.config.find_radio("k4c") is not None


async def test_audit_trail_records_the_session_lifecycle(server):
    await server.manager.request_assignment("W1AW", "k4a")
    await server.manager.release("W1AW")

    actions = [e.action for e in server.db.list_audit()]
    assert "assign" in actions
    assert "session.released" in actions
