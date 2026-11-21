from conftest import set_flag

RESERVE = {"saga_id": "saga-1", "station_id": "BOG-01", "connector_id": "C1", "fleet_id": "FLEET-A"}


def connector_status(api, cid="C1"):
    st = api.get("/stations").json()[0]
    return next(c for c in st["connectors"] if c["connector_id"] == cid)


def test_reserve_locks_connector(reservation_api):
    r = reservation_api.post("/reservations", json=RESERVE)
    assert r.status_code == 201 and r.json()["status"] == "RESERVED"
    c = connector_status(reservation_api)
    assert c["status"] == "RESERVED" and c["reserved_by"] == "saga-1"


def test_second_saga_cannot_take_reserved_connector(reservation_api):
    reservation_api.post("/reservations", json=RESERVE)
    r = reservation_api.post("/reservations", json={**RESERVE, "saga_id": "saga-2"})
    assert r.status_code == 409 and r.json()["code"] == "CONNECTOR_NOT_AVAILABLE"


def test_reserve_is_idempotent_for_same_saga(reservation_api):
    reservation_api.post("/reservations", json=RESERVE)
    r = reservation_api.post("/reservations", json=RESERVE)
    assert r.status_code in (200, 201) and r.json()["status"] == "RESERVED"


def test_release_twice_is_idempotent(reservation_api):
    reservation_api.post("/reservations", json=RESERVE)
    first = reservation_api.post("/reservations/saga-1/release", json={"reason": "COMPENSATION"})
    second = reservation_api.post("/reservations/saga-1/release", json={"reason": "COMPENSATION"})
    assert first.json()["status"] == "RELEASED"
    assert second.json()["status"] == "RELEASED" and second.json()["idempotent"] is True
    assert connector_status(reservation_api)["status"] == "AVAILABLE"


def test_release_without_reservation_is_noop(reservation_api):
    r = reservation_api.post("/reservations/never-reserved/release", json={})
    assert r.status_code == 200 and r.json()["status"] == "NOOP"


def test_release_never_frees_connector_of_another_saga(reservation_api):
    reservation_api.post("/reservations", json=RESERVE)
    reservation_api.post("/reservations/saga-1/release", json={})
    reservation_api.post("/reservations", json={**RESERVE, "saga_id": "saga-2"})
    reservation_api.post("/reservations/saga-1/release", json={})
    assert connector_status(reservation_api)["reserved_by"] == "saga-2"


def test_ultra_connector_requires_boost_flag(reservation_api):
    body = {**RESERVE, "connector_id": "C3"}
    r = reservation_api.post("/reservations", json=body)
    assert r.status_code == 409 and r.json()["code"] == "ULTRA_CHARGE_DISABLED"
    set_flag("ENABLE_FAST_CHARGE_BOOST", True)
    import station_reservation.app as m

    m.flags._loaded_at = 0
    assert reservation_api.post("/reservations", json=body).status_code == 201


CHARGE = {"saga_id": "saga-1", "fleet_id": "FLEET-A", "energy_kwh": 40, "connector_type": "FAST"}


def balance(api, fleet="FLEET-A"):
    return api.get(f"/wallets/{fleet}").json()["balance"]


def test_charge_deducts_balance(billing_api):
    r = billing_api.post("/charges", json=CHARGE)
    assert r.status_code == 201 and r.json()["amount"] == 12.0
    assert balance(billing_api) == 88.0


def test_charge_twice_only_charges_once(billing_api):
    billing_api.post("/charges", json=CHARGE)
    billing_api.post("/charges", json=CHARGE)
    assert balance(billing_api) == 88.0


def test_insufficient_funds_rejected_and_recorded(billing_api):
    r = billing_api.post("/charges", json={**CHARGE, "fleet_id": "FLEET-POOR"})
    assert r.status_code == 402 and r.json()["code"] == "INSUFFICIENT_FUNDS"
    assert balance(billing_api, "FLEET-POOR") == 1.0
    txs = billing_api.get("/transactions", params={"fleet_id": "FLEET-POOR"}).json()
    assert txs[0]["type"] == "CHARGE_REJECTED"


def test_refund_twice_only_refunds_once(billing_api):
    billing_api.post("/charges", json=CHARGE)
    first = billing_api.post("/charges/saga-1/refund", json={"fleet_id": "FLEET-A"})
    second = billing_api.post("/charges/saga-1/refund", json={"fleet_id": "FLEET-A"})
    assert first.json()["type"] == "REFUND" and second.json()["idempotent"] is True
    assert balance(billing_api) == 100.0
    types = sorted(
        (t["type"], t["status"]) for t in billing_api.get("/transactions", params={"fleet_id": "FLEET-A"}).json()
    )
    assert types == [("CHARGE", "REFUNDED"), ("REFUND", "COMPLETED")]


def test_refund_without_charge_is_noop(billing_api):
    r = billing_api.post("/charges/ghost/refund", json={"fleet_id": "FLEET-A"})
    assert r.json()["status"] == "NOOP" and balance(billing_api) == 100.0


def test_surge_flag_changes_price_without_redeploy(billing_api):
    q1 = billing_api.get("/pricing/quote", params={"connector_type": "FAST", "energy_kwh": 40}).json()
    set_flag("ENABLE_DYNAMIC_SURGE_PRICING", True)
    import billing_wallet.app as m

    m.flags._loaded_at = 0
    q2 = billing_api.get("/pricing/quote", params={"connector_type": "FAST", "energy_kwh": 40}).json()
    assert q1["amount"] == 12.0 and q2["amount"] == 18.0 and q2["multiplier"] == 1.5


SESSION = {
    "saga_id": "saga-1",
    "station_id": "BOG-01",
    "connector_id": "C1",
    "fleet_id": "FLEET-A",
    "energy_kwh": 40,
    "power_kw": 50,
}


def test_start_session_active(dispatch_api):
    r = dispatch_api.post("/sessions", json=SESSION)
    assert r.status_code == 201 and r.json()["status"] == "ACTIVE"


def test_hardware_fault_flag_fails_step3(dispatch_api):
    set_flag("SIMULATE_CHARGER_HARDWARE_FAULT", True)
    r = dispatch_api.post("/sessions", json=SESSION)
    assert r.status_code == 422 and r.json()["code"] == "CHARGER_HARDWARE_FAULT"
    again = dispatch_api.post("/sessions", json=SESSION)
    assert again.status_code == 422 and again.json()["idempotent"] is True
    assert dispatch_api.get("/sessions/saga-1").json()["status"] == "FAILED"


def test_telemetry_is_recorded(dispatch_api):
    import energy_dispatch.app as m

    dispatch_api.post("/sessions", json=SESSION)
    assert m.record_telemetry_once() == 1
    t = dispatch_api.get("/sessions/saga-1/telemetry").json()
    assert t["current"]["energy_kwh"] >= 0 and len(t["samples"]) == 1
