"""HTTP API: authentication, member endpoints, admin endpoints."""

from __future__ import annotations

# -- authentication --------------------------------------------------------


async def test_health_needs_no_credentials(api):
    response = await api.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_endpoints_reject_missing_and_bad_keys(api, server):
    assert (await api.get("/status")).status_code == 401
    bad = {"Authorization": "Bearer not-a-real-key"}
    assert (await api.get("/status", headers=bad)).status_code == 401
    assert any(e.action == "auth.failed" for e in server.db.list_audit())


async def test_disabled_account_is_refused(api, server):
    server.db.set_user_enabled("W1AW", False)
    assert (await api.get("/status", headers=server.headers("W1AW"))).status_code == 401


async def test_member_cannot_use_admin_endpoints(api, server):
    member = server.headers("W1AW")
    assert (await api.get("/users", headers=member)).status_code == 403
    assert (await api.get("/audit", headers=member)).status_code == 403
    assert (await api.post("/radios/reload", headers=member)).status_code == 403


# -- assignment flow -------------------------------------------------------


async def test_request_and_release_round_trip(api, server):
    headers = server.headers("W1AW")

    created = await api.post("/assignments", json={"radio": "k4a"}, headers=headers)
    assert created.status_code == 201
    body = created.json()
    assert body["radio"]["id"] == "k4a"
    assert body["password"] == server.radio_a.password
    assert body["radio"]["connect_port"] == server.radio_a.remote_port

    current = (await api.get("/assignments/current", headers=headers)).json()
    assert current["assignment"]["radio"]["id"] == "k4a"
    assert current["assignment"]["can_extend"] is True

    released = await api.delete("/assignments/current", headers=headers)
    assert released.status_code == 200
    assert server.radio_a.rrc == 0

    after = (await api.get("/assignments/current", headers=headers)).json()
    assert after["assignment"] is None


async def test_request_requires_naming_a_radio(api, server):
    response = await api.post("/assignments", json={}, headers=server.headers("W1AW"))
    assert response.status_code == 422, "the radio field is required — there is no pool"


async def test_busy_radio_returns_conflict_with_queue_hint(api, server):
    server.add_user("K2ABC")
    await api.post("/assignments", json={"radio": "k4a"}, headers=server.headers("W1AW"))

    response = await api.post("/assignments", json={"radio": "k4a"}, headers=server.headers("K2ABC"))

    assert response.status_code == 409
    assert response.json()["in_use_by"] == "W1AW"
    assert response.json()["can_queue"] is True


async def test_unreachable_radio_returns_503(api, server):
    response = await api.post(
        "/assignments", json={"radio": "k4dead"}, headers=server.headers("W1AW")
    )
    assert response.status_code == 503


async def test_ack_and_extend(api, server):
    headers = server.headers("W1AW")
    await api.post("/assignments", json={"radio": "k4a"}, headers=headers)

    acked = await api.post("/assignments/current/ack", headers=headers)
    assert acked.json()["assignment"]["acked"] is True

    extended = await api.post("/assignments/current/extend", headers=headers)
    assert extended.status_code == 200
    assert extended.json()["assignment"]["acked"] is False, "extending reopens the negotiation"


async def test_extend_refused_when_a_user_is_queued(api, server):
    server.add_user("K2ABC")
    await api.post("/assignments", json={"radio": "k4a"}, headers=server.headers("W1AW"))
    await api.post("/queue", json={"radio": "k4a"}, headers=server.headers("K2ABC"))

    response = await api.post("/assignments/current/extend", headers=server.headers("W1AW"))

    assert response.status_code == 409
    assert response.json()["queue_depth"] == 1


# -- queue -----------------------------------------------------------------


async def test_queue_offer_and_accept_over_http(api, server):
    server.add_user("K2ABC")
    owner, waiter = server.headers("W1AW"), server.headers("K2ABC")
    await api.post("/assignments", json={"radio": "k4a"}, headers=owner)

    queued = await api.post("/queue", json={"radio": "k4a"}, headers=waiter)
    assert queued.status_code == 201
    assert queued.json()["position"] == 1

    await api.delete("/assignments/current", headers=owner)

    state = (await api.get("/queue/position", headers=waiter)).json()
    assert state["offer"]["radio"]["id"] == "k4a"

    accepted = await api.post("/queue/accept", headers=waiter)
    assert accepted.status_code == 201
    assert accepted.json()["password"] == server.radio_a.password


async def test_decline_and_leave_queue(api, server):
    server.add_user("K2ABC")
    await api.post("/assignments", json={"radio": "k4a"}, headers=server.headers("W1AW"))
    await api.post("/queue", json={"radio": "k4a"}, headers=server.headers("K2ABC"))

    left = await api.delete("/queue", headers=server.headers("K2ABC"))
    assert left.status_code == 200
    assert server.db.queue_depth("k4a") == 0


# -- status ----------------------------------------------------------------


async def test_status_lists_every_radio(api, server):
    await api.post("/assignments", json={"radio": "k4a"}, headers=server.headers("W1AW"))

    rows = {r["id"]: r for r in (await api.get("/status", headers=server.headers("W1AW"))).json()["radios"]}

    assert set(rows) == {"k4a", "k4b", "k4dead"}
    assert rows["k4a"]["user"] == "W1AW"
    assert rows["k4b"]["in_use"] is False
    assert rows["k4a"]["description"] == "Test radio A"


# -- admin: users ----------------------------------------------------------


async def test_admin_creates_user_whose_key_works(api, server):
    admin = server.headers("ADMIN")

    created = await api.post("/users", json={"callsign": "n0new"}, headers=admin)
    assert created.status_code == 201
    api_key = created.json()["api_key"]
    assert created.json()["callsign"] == "N0NEW"

    new_headers = {"Authorization": f"Bearer {api_key}"}
    assert (await api.get("/status", headers=new_headers)).status_code == 200
    assert (await api.get("/users", headers=new_headers)).status_code == 403


async def test_duplicate_user_is_refused(api, server):
    admin = server.headers("ADMIN")
    await api.post("/users", json={"callsign": "N0NEW"}, headers=admin)
    again = await api.post("/users", json={"callsign": "N0NEW"}, headers=admin)
    assert again.status_code == 409


async def test_reset_key_invalidates_the_old_one(api, server):
    old_headers = server.headers("W1AW")
    reset = await api.post("/users/W1AW/reset-key", headers=server.headers("ADMIN"))
    new_headers = {"Authorization": f"Bearer {reset.json()['api_key']}"}

    assert (await api.get("/status", headers=old_headers)).status_code == 401
    assert (await api.get("/status", headers=new_headers)).status_code == 200


async def test_last_admin_cannot_be_deleted(api, server):
    response = await api.delete("/users/ADMIN", headers=server.headers("ADMIN"))
    assert response.status_code == 409


async def test_deleting_a_user_frees_their_radio(api, server):
    await api.post("/assignments", json={"radio": "k4a"}, headers=server.headers("W1AW"))

    deleted = await api.delete("/users/W1AW", headers=server.headers("ADMIN"))

    assert deleted.status_code == 200
    assert server.db.live_assignment_for_radio("k4a") is None
    assert server.radio_a.rrc == 0


# -- admin: radios, sessions, audit ---------------------------------------


async def test_admin_radio_listing_and_reload(api, server):
    admin = server.headers("ADMIN")
    radios = (await api.get("/radios", headers=admin)).json()
    assert {r["id"] for r in radios} == {"k4a", "k4b", "k4dead"}
    assert radios[0]["cat_port"] == server.radio_a.cat_port

    reloaded = await api.post("/radios/reload", headers=admin)
    assert reloaded.json()["radios"] == 3


async def test_power_on_without_actuator_is_reported_clearly(api, server):
    response = await api.post("/radios/k4b/power", json={"action": "on"}, headers=server.headers("ADMIN"))
    assert response.status_code == 502
    assert "no remote power-on method" in response.json()["detail"]


async def test_power_off_refused_while_radio_is_in_use(api, server):
    await api.post("/assignments", json={"radio": "k4a"}, headers=server.headers("W1AW"))
    response = await api.post(
        "/radios/k4a/power", json={"action": "off"}, headers=server.headers("ADMIN")
    )
    assert response.status_code == 409


async def test_admin_force_release(api, server):
    await api.post("/assignments", json={"radio": "k4a"}, headers=server.headers("W1AW"))
    assignment_id = server.db.live_assignment_for_radio("k4a").id

    forced = await api.delete(f"/assignments/{assignment_id}", headers=server.headers("ADMIN"))

    assert forced.status_code == 200
    assert server.db.live_assignment_for_radio("k4a") is None

    sessions = (await api.get("/assignments", headers=server.headers("ADMIN"))).json()
    assert sessions[0]["state"] == "forced"
    assert "password" not in sessions[0], "admin session listing must not expose passwords"


async def test_audit_endpoint_reports_actions(api, server):
    headers = server.headers("W1AW")
    await api.post("/assignments", json={"radio": "k4a"}, headers=headers)
    await api.delete("/assignments/current", headers=headers)

    entries = (await api.get("/audit", headers=server.headers("ADMIN"))).json()
    actions = [e["action"] for e in entries]

    assert "assign" in actions
    assert "session.released" in actions
    assert all({"timestamp", "actor", "action", "detail"} <= set(e) for e in entries)
