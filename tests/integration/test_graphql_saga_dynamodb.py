import pytest

pytestmark = pytest.mark.integration

RESERVE = """
mutation($i: ReserveAndStartChargingInput!) {
  reserveAndStartCharging(input: $i) {
    sagaId status amount failedStep errorCode correlationId
    steps { name action status attempts }
  }
}"""

SET_FLAG = "mutation($n:String!,$e:Boolean!){ setFeatureFlag(name:$n, enabled:$e){ name enabled } }"


def reserve(gql, connector, fleet="FLEET-IT", key=None, kwh=20):
    data = gql(
        RESERVE,
        {
            "i": {
                "stationId": "IT-01",
                "connectorId": connector,
                "fleetId": fleet,
                "energyKwh": kwh,
                "idempotencyKey": key,
            }
        },
    )
    return data["reserveAndStartCharging"]


def connector(table, cid):
    return table("reservations").get_item(Key={"pk": "STATION#IT-01", "sk": f"CONNECTOR#{cid}"})["Item"]


def wallet_balance(table, fleet="FLEET-IT"):
    return float(table("billing").get_item(Key={"pk": f"WALLET#{fleet}", "sk": "META"})["Item"]["balance"])


def test_schema_exposes_queries_mutations_and_subscription(gateway):
    sdl = gateway.get("/schema.graphql").text
    for token in (
        "type Query",
        "type Mutation",
        "type Subscription",
        "reserveAndStartCharging",
        "featureFlags",
        "stationsUpdated",
        "correlationId",
    ):
        assert token in sdl


def test_stations_and_wallets_are_aggregated(gql):
    data = gql("{ stations { stationId availableConnectors } wallets { fleetId balance } environment { name } }")
    assert data["stations"][0]["stationId"] == "IT-01"
    assert {w["fleetId"] for w in data["wallets"]} == {"FLEET-IT", "FLEET-BROKE"}
    assert data["environment"]["name"].startswith("it")


def test_successful_saga_persists_state_in_three_tables(gql, table):
    before = wallet_balance(table)
    saga = reserve(gql, "C1")
    assert saga["status"] == "CHARGING" and saga["correlationId"] == saga["sagaId"]
    assert connector(table, "C1")["status"] == "CHARGING"
    assert wallet_balance(table) == pytest.approx(before - saga["amount"])
    session = table("dispatch").get_item(Key={"pk": f"SESSION#{saga['sagaId']}", "sk": "META"})["Item"]
    assert session["status"] == "ACTIVE"
    assert table("sagas").get_item(Key={"saga_id": saga["sagaId"]})["Item"]["status"] == "CHARGING"

    finished = gql("mutation($s:String!){ finishCharging(sagaId:$s){ status } }", {"s": saga["sagaId"]})
    assert finished["finishCharging"]["status"] == "FINISHED"
    assert connector(table, "C1")["status"] == "AVAILABLE"


def test_hardware_fault_flag_triggers_compensations_in_dynamodb(gql, table):
    gql(SET_FLAG, {"n": "SIMULATE_CHARGER_HARDWARE_FAULT", "e": True})
    try:
        before = wallet_balance(table)
        saga = reserve(gql, "C2")
    finally:
        gql(SET_FLAG, {"n": "SIMULATE_CHARGER_HARDWARE_FAULT", "e": False})

    assert saga["status"] == "COMPENSATED"
    assert saga["failedStep"] == "START_CHARGING" and saga["errorCode"] == "CHARGER_HARDWARE_FAULT"
    assert [s["name"] for s in saga["steps"] if s["action"] == "COMPENSATE"] == ["REFUND_WALLET", "RELEASE_CONNECTOR"]
    sid = saga["sagaId"]
    charge = table("billing").get_item(Key={"pk": "WALLET#FLEET-IT", "sk": f"TX#{sid}#CHARGE"})["Item"]
    refund = table("billing").get_item(Key={"pk": "WALLET#FLEET-IT", "sk": f"TX#{sid}#REFUND"})["Item"]
    assert charge["status"] == "REFUNDED" and refund["amount"] == charge["amount"]
    assert wallet_balance(table) == pytest.approx(before)
    assert connector(table, "C2")["status"] == "AVAILABLE"
    res = table("reservations").get_item(Key={"pk": f"RESERVATION#{sid}", "sk": "META"})["Item"]
    assert res["status"] == "RELEASED" and res["released_reason"] == "COMPENSATION"


def test_insufficient_funds_releases_connector(gql, table):
    saga = reserve(gql, "C2", fleet="FLEET-BROKE")
    assert saga["status"] == "COMPENSATED" and saga["errorCode"] == "INSUFFICIENT_FUNDS"
    assert [s["name"] for s in saga["steps"] if s["action"] == "COMPENSATE"] == ["RELEASE_CONNECTOR"]
    assert connector(table, "C2")["status"] == "AVAILABLE"
    assert wallet_balance(table, "FLEET-BROKE") == 1.0


def test_ultra_connector_depends_on_boost_flag(gql):
    saga = reserve(gql, "C3")
    assert saga["status"] == "FAILED" and saga["errorCode"] == "ULTRA_CHARGE_DISABLED"
    gql(SET_FLAG, {"n": "ENABLE_FAST_CHARGE_BOOST", "e": True})
    try:
        assert reserve(gql, "C3")["status"] == "CHARGING"
    finally:
        gql(SET_FLAG, {"n": "ENABLE_FAST_CHARGE_BOOST", "e": False})


def test_idempotency_key_prevents_double_charge(gql, table):
    before = wallet_balance(table)
    first = reserve(gql, "C2", key="it-idem-1", kwh=10)
    second = reserve(gql, "C2", key="it-idem-1", kwh=10)
    assert first["sagaId"] == second["sagaId"]
    assert wallet_balance(table) == pytest.approx(before - first["amount"])


def test_flag_change_is_visible_through_graphql(gql):
    gql(SET_FLAG, {"n": "ENABLE_DYNAMIC_SURGE_PRICING", "e": True})
    try:
        q = gql('{ priceQuote(connectorType:"FAST", energyKwh:10){ multiplier amount } }')["priceQuote"]
        assert q["multiplier"] == 1.5 and q["amount"] == 4.5
    finally:
        gql(SET_FLAG, {"n": "ENABLE_DYNAMIC_SURGE_PRICING", "e": False})
    q = gql('{ priceQuote(connectorType:"FAST", energyKwh:10){ multiplier amount } }')["priceQuote"]
    assert q["multiplier"] == 1.0 and q["amount"] == 3.0


def test_sagas_query_lists_compensated_by_status(gql):
    data = gql("{ sagas(status: COMPENSATED){ sagaId status } }")["sagas"]
    assert data and all(s["status"] == "COMPENSATED" for s in data)
